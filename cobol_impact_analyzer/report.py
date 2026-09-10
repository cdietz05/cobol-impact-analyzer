"""Report rendering: terminal text, JSON, CSV, a self-contained HTML page, and a
per-file change summary in Markdown.

The reader wants three things and nothing else: the base value that is changing,
the trace of where it flows, and — file by file — the edits each module needs.
The text and HTML renderers are both built around those three sections so the
same information is never laid out twice.
"""

from __future__ import annotations

import csv
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .analyzer import AnalysisResult
from .models import Capacity, Finding, Kind, Node, Severity
from .spec import ChangeSpec

_SEVERITY_ORDER = [
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
]

_SEVERITY_COLOR = {
    Severity.CRITICAL: "#b3261e",
    Severity.HIGH: "#b8531b",
    Severity.MEDIUM: "#8a6d10",
    Severity.LOW: "#3f6b8a",
    Severity.INFO: "#5b6470",
}

# Findings that describe a database change, not a source edit in a file.
_NON_FILE_CATEGORIES = {"requested-change", "coverage-gap", "database-column"}

# One line per severity for the section headers, and the full account for the
# key. These mirror how the analyzer actually assigns severity: a truncating
# edge (SQL fetch, MOVE, STRING/UNSTRING, COMPUTE, arithmetic, WRITE FROM,
# READ INTO, reference modification) into an undersized field is CRITICAL;
# the same shortfall reached by a non-truncating path, or another column that
# needs its own ALTER, is HIGH; a grown record length or a width-sensitive
# comparison is MEDIUM; and so on. A field that trips several of these keeps
# one finding and takes the worst.
_SEVERITY_BLURB = {
    Severity.CRITICAL: (
        "Silent data loss. A wider value lands in a field too small to hold it "
        "through an edge that truncates without complaint."
    ),
    Severity.HIGH: (
        "Wrong, but it shows. Another database column now needs its own ALTER, "
        "or a field is undersized on a non-truncating path, or a hard-coded "
        "reference modification no longer spans the field."
    ),
    Severity.MEDIUM: (
        "Needs a human. A record length grew, a padded comparison or INSPECT "
        "shifts, a VALUE clause may be stale, or a column could not be traced "
        "at all."
    ),
    Severity.LOW: (
        "Cosmetic or informational. DISPLAY alignment, INITIALIZE, SET — "
        "check it, nothing breaks on its own."
    ),
    Severity.INFO: "The change you asked for, echoed back with its DDL. The root of every trace.",
}

_SEVERITY_DETAIL = {
    Severity.CRITICAL: [
        "The value reaches the field through a SELECT/FETCH INTO, MOVE, "
        "STRING/UNSTRING, COMPUTE, ADD/SUBTRACT/MULTIPLY/DIVIDE, WRITE FROM, "
        "READ INTO or reference modification.",
        "COBOL drops the overflow with no error at compile time or run time — "
        "trailing characters for text, high-order digits for numbers.",
    ],
    Severity.HIGH: [
        "Another table's column receives the widened value and must be ALTERed "
        "too; writing the wider value first raises ORA-01401 / ORA-12899.",
        "A field is too small but is reached by a CALL argument, a host variable "
        "in a WHERE predicate, or a REDEFINES over shared storage — it fails "
        "as a bind error or a mismatch rather than losing data quietly.",
        "A reference modification with a hard-coded offset or length that no "
        "longer covers the field.",
    ],
    Severity.MEDIUM: [
        "A group or record length grew, so every file, queue, CALL interface or "
        "REDEFINES built on the old length must be reviewed and rebuilt.",
        "A literal comparison or INSPECT whose result changes because the field "
        "is now padded wider.",
        "A VALUE clause that may no longer be right.",
        "A column that could not be traced (dynamic SQL, SELECT *, a cursor in "
        "an unscanned copybook) — reported so “no impact” is not read as “safe”.",
    ],
    Severity.LOW: [
        "DISPLAY output or log alignment shifts.",
        "INITIALIZE or SET on the field — confirm the value still fits.",
    ],
    Severity.INFO: [
        "The requested column change and its ALTER statement.",
    ],
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# -- JSON ------------------------------------------------------------------


def to_json(result: AnalysisResult, include_graph: bool = False) -> str:
    payload: dict[str, object] = {
        "generated_at": _timestamp(),
        "spec": result.spec.to_dict(),
        "summary": {
            "programs_scanned": len(result.programs),
            "nodes": len(result.graph.nodes),
            "edges": sum(len(edges) for edges in result.graph.out_edges.values()),
            "findings": result.counts,
            "programs_affected": len(result.program_impacts),
            "programs_recompile_only": sum(
                1 for impact in result.program_impacts if impact.recompile_only
            ),
        },
        "ddl_plan": result.ddl,
        "program_impacts": [impact.to_dict() for impact in result.program_impacts],
        "findings": [finding.to_dict() for finding in result.findings],
        "programs": [
            {
                "name": program.name,
                "path": program.path,
                "sql_statements": len(program.sql),
                "data_items": len(program.data.order),
                "copybooks": sorted(set(program.copybooks)),
            }
            for program in result.programs
        ],
        "warnings": result.warnings,
    }
    if include_graph:
        payload["graph"] = result.graph.to_dict()
    return json.dumps(payload, indent=2)


def write_json(result: AnalysisResult, path: Path, include_graph: bool = False) -> None:
    path.write_text(to_json(result, include_graph), encoding="utf-8")


# -- CSV -----------------------------------------------------------------------

_CSV_COLUMNS = [
    "severity",
    "category",
    "node",
    "title",
    "current",
    "required",
    "remediation",
    "hops_from_change",
    "propagation_path",
    "locations",
    "detail",
    "other_usages",
]


def write_csv(result: AnalysisResult, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for finding in result.findings:
            writer.writerow(
                {
                    "severity": finding.severity.value,
                    "category": finding.category,
                    "node": finding.node_id,
                    "title": finding.title,
                    "current": finding.current,
                    "required": finding.required,
                    "remediation": finding.remediation,
                    "hops_from_change": finding.distance,
                    "propagation_path": " -> ".join(_pretty_path(finding.path)),
                    "locations": "; ".join(ref.location() for ref in finding.refs),
                    "detail": finding.detail,
                    "other_usages": "; ".join(finding.notes),
                }
            )


# -- shared shaping ----------------------------------------------------------


def _plain_name(node_id: str) -> str:
    """``var:PROG::WS-FOO`` -> ``WS-FOO``; ``col:CUSTOMER.CUST_NAME`` -> that."""
    body = node_id.split(":", 1)[1] if ":" in node_id else node_id
    return body.split("::", 1)[1] if "::" in body else body


def _module_of(node_id: str, node: Optional[Node], finding: Optional[Finding]) -> str:
    body = node_id.split(":", 1)[1] if ":" in node_id else node_id
    if "::" in body:
        return body.split("::", 1)[0]
    if node is not None and node.programs:
        return sorted(node.programs)[0]
    if finding is not None and finding.refs and finding.refs[0].program:
        return finding.refs[0].program
    return ""


def _cap_short(cap: Optional[Capacity]) -> str:
    if cap is None or cap.kind is Kind.UNKNOWN:
        return "?"
    if cap.kind.is_numeric():
        body = f"9({cap.int_digits})"
        if cap.dec_digits:
            body += f"V9({cap.dec_digits})"
        return ("S" + body) if cap.signed else body
    return f"X({cap.chars})"


def _short_path(path: str) -> str:
    """The file's own name, which is how a maintainer refers to it."""
    try:
        return Path(path).name or path
    except (ValueError, OSError):
        return path


def _rel_path(path: str) -> str:
    """A path relative to where the report is read, when that is shorter."""
    import os

    try:
        rel = os.path.relpath(path)
        if not rel.startswith(".."):
            return rel.replace("\\", "/")
    except (ValueError, OSError):
        pass
    return path.replace("\\", "/")


def _pretty_path(path: Iterable[str]) -> list[str]:
    return [_strip_prefix(node_id) for node_id in path]


def _strip_prefix(node_id: str) -> str:
    if node_id.startswith(("col:", "var:")):
        return node_id[4:]
    return node_id


def _seed_change(result: AnalysisResult, plain: str):
    for change in result.spec.changes:
        if change.key == plain:
            return change
    return None


# -- Flow: one trace tree per base value -----------------------------------


def _flow_forest(result: AnalysisResult) -> dict[str, dict]:
    """Nest every reported route into a forest keyed by its seed column.

    ``result.paths`` already holds one parent-chain per finding (and per seed),
    so a simple trie over those chains is the whole tree - each field appears
    once, under the route that reached it.
    """
    roots: dict[str, dict] = {}
    for target, route in result.paths.items():
        chain = route or [target]
        children = roots
        for node_id in chain:
            node = children.setdefault(node_id, {"id": node_id, "children": {}})
            children = node["children"]
    return roots


def _flow_line(result: AnalysisResult, by_node: dict[str, Finding], node_id: str) -> tuple[str, str, str, str, str]:
    """(name, module, width-change, severity or '', location or '')."""
    node = result.graph.nodes.get(node_id)
    finding = by_node.get(node_id)
    name = _plain_name(node_id)
    module = _module_of(node_id, node, finding)
    need = result.required.get(node_id)
    change = ""
    if node is not None and need is not None and not node.capacity.covers(need):
        change = f"{_cap_short(node.capacity)} -> {_cap_short(need)}"
    sev = finding.severity.value if finding is not None else ""
    loc = ""
    if finding is not None and finding.refs:
        loc = finding.refs[0].location()
    elif node is not None and node.declared_in is not None:
        loc = node.declared_in.location()
    return name, module, change, sev, loc


# -- Changes by module ----------------------------------------------------


def _declaration_ref(result: AnalysisResult, finding: Finding):
    node = result.graph.nodes.get(finding.node_id)
    if node is not None and node.declared_in is not None:
        return node.declared_in
    if node is not None and node.field_ref is not None and node.field_ref.source is not None:
        return node.field_ref.source
    return finding.refs[-1] if finding.refs else None


def _changes_by_file(result: AnalysisResult) -> tuple[list[dict], list[tuple[str, list[str]]]]:
    """Group the source edits by the file they land in, worst file first.

    Returns ``(edit_blocks, recompile_only)``. An edit block is
    ``{"file", "worst", "edits":[{name, now, to, review, line, severity}]}``.
    """
    by_file: dict[str, list[dict]] = {}
    for finding in result.findings:
        if finding.category in _NON_FILE_CATEGORIES:
            continue
        ref = _declaration_ref(result, finding)
        if ref is None:
            continue
        entry = {
            "name": _plain_name(finding.node_id),
            "severity": finding.severity,
            "line": ref.line,
        }
        if finding.remediation.startswith("Change to:"):
            entry["now"] = finding.current
            entry["to"] = finding.remediation[len("Change to:") :].strip()
            entry["review"] = ""
        else:
            entry["now"] = ""
            entry["to"] = ""
            entry["review"] = f"{finding.title}: {finding.remediation}"
        by_file.setdefault(ref.path, []).append(entry)

    blocks: list[dict] = []
    for path, edits in by_file.items():
        edits.sort(key=lambda item: (item["severity"].rank, item["name"]))
        # One physical edit per line, however many programs' flows reach it. A
        # field declared in a shared copybook produces a finding per includer;
        # they are all the same one-line change to the same file.
        seen: set[tuple] = set()
        deduped: list[dict] = []
        for item in edits:
            key = (item["name"],) if item["review"] else (
                item["name"],
                item["now"],
                item["to"],
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        edits = deduped
        blocks.append(
            {
                "file": _rel_path(path),
                "worst": min(item["severity"].rank for item in edits),
                "edits": edits,
            }
        )
    blocks.sort(key=lambda block: (block["worst"], block["file"]))

    recompile = [
        (impact.program, [_short_path(book) for book in impact.changed_copybooks])
        for impact in result.program_impacts
        if impact.recompile_only
    ]
    return blocks, recompile


def _untraced(result: AnalysisResult) -> list[Finding]:
    return [f for f in result.findings if f.category == "coverage-gap"]


# -- terminal --------------------------------------------------------------


def to_text(result: AnalysisResult, verbose: bool = False) -> str:
    counts = result.counts
    lines: list[str] = []
    lines.append("COBOL COLUMN WIDENING IMPACT")
    lines.append(f"generated {_timestamp()}")
    tally = ", ".join(
        f"{level.value} {counts[level.value]}" for level in _SEVERITY_ORDER
    )
    lines.append(f"{len(result.programs)} program(s) scanned  |  findings: {tally}")
    lines.append("")

    lines.append("SEVERITY  (worst wins when a field trips several)")
    for level in _SEVERITY_ORDER:
        lines.append(f"  {level.value:<9} {_SEVERITY_BLURB[level]}")
    lines.append("")

    lines.append("REQUESTED CHANGES")
    for change in result.spec.changes:
        lines.append(f"  {change.key:<40} {change.old_type} -> {change.new_type}")
    lines.append("")

    lines.append("FLOW  (where the value moves; [SEVERITY] marks a field that will not hold it)")
    forest = _flow_forest(result)
    by_node = {finding.node_id: finding for finding in result.findings}

    def walk(children: dict[str, dict], depth: int) -> None:
        for node_id, child in children.items():
            name, module, change, sev, loc = _flow_line(result, by_node, node_id)
            bits = [name]
            if change:
                bits.append(change)
            if sev:
                bits.append(f"[{sev}]")
            if module:
                bits.append(module)
            if loc:
                bits.append(loc)
            lines.append("  " + "  " * depth + "  ".join(bits))
            finding = by_node.get(node_id)
            if finding is not None:
                for note in finding.notes:
                    lines.append("  " + "  " * (depth + 1) + "note: " + note)
                if verbose and finding.detail:
                    lines.append("  " + "  " * (depth + 1) + "why: " + finding.detail)
            walk(child["children"], depth + 1)

    if not forest:
        lines.append("  (no COBOL field is reached by this change)")
    for root_id, root in sorted(forest.items()):
        plain = _plain_name(root_id)
        change = _seed_change(result, plain)
        head = plain
        if change is not None:
            head += f"   {change.old_type} -> {change.new_type}"
        lines.append(f"  {head}")
        walk(root["children"], 1)
    lines.append("")

    edit_blocks, recompile = _changes_by_file(result)
    lines.append(f"CHANGES BY MODULE  ({len(edit_blocks)} file(s) to edit)")
    if not edit_blocks:
        lines.append("  none - every affected program only needs rebuilding.")
    for block in edit_blocks:
        lines.append(f"  {block['file']}")
        for edit in block["edits"]:
            if edit["review"]:
                lines.append(f"      {edit['name']:<24} review: {edit['review']}")
            else:
                lines.append(f"      {edit['name']:<24} {edit['now']}")
                lines.append(f"      {'':<24}   -> {edit['to']}   (line {edit['line']})")
    if recompile:
        lines.append("")
        lines.append(f"  RECOMPILE ONLY  ({len(recompile)} — rebuild, no source edit)")
        for program, books in recompile:
            lines.append(f"      {program:<20} {', '.join(books)}")
    lines.append("")

    untraced = _untraced(result)
    if untraced:
        lines.append(f"UNTRACED COLUMNS  ({len(untraced)} — could not follow past here)")
        for finding in untraced:
            lines.append(f"  {finding.title}")
        lines.append("")

    if result.ddl:
        lines.append("DDL PLAN")
        lines.extend(f"  {statement}" for statement in result.ddl)
        lines.append("")

    warnings = list(dict.fromkeys(result.warnings))
    if warnings:
        lines.append(f"COVERAGE WARNINGS  ({len(warnings)} — gaps in the trace, not errors)")
        for warning in warnings:
            lines.append(f"  {warning}")
        lines.append("")

    return "\n".join(lines)


# -- Markdown summary ----------------------------------------------------------


def to_summary(result: AnalysisResult) -> str:
    counts = result.counts
    tables = ", ".join(sorted({change.table.upper() for change in result.spec.changes}))
    lines: list[str] = []
    lines.append(f"# {tables or 'Column'} widening — changes by file")
    lines.append("")
    lines.append(f"_Generated {_timestamp()}._")
    lines.append("")

    lines.append("## Requested change")
    lines.append("")
    for change in result.spec.changes:
        lines.append(f"- `{change.key}` &nbsp; `{change.old_type}` → `{change.new_type}`")
    lines.append("")
    tally = ", ".join(
        f"{level.value} {counts[level.value]}"
        for level in _SEVERITY_ORDER
        if counts[level.value]
    )
    lines.append(f"Findings: {tally or 'none'}.")
    lines.append("")

    edit_blocks, recompile = _changes_by_file(result)
    lines.append("## Files to edit")
    lines.append("")
    if not edit_blocks:
        lines.append("_None — every affected program only needs rebuilding._")
        lines.append("")
    for block in edit_blocks:
        lines.append(f"### `{block['file']}`")
        lines.append("")
        for edit in block["edits"]:
            if edit["review"]:
                lines.append(
                    f"- **{edit['name']}** — {edit['severity'].value} — {edit['review']}"
                )
            else:
                lines.append(
                    f"- **{edit['name']}** (line {edit['line']}, {edit['severity'].value}): "
                    f"`{edit['now']}` → `{edit['to']}`"
                )
        lines.append("")

    lines.append("## Recompile only (rebuild, no source edit)")
    lines.append("")
    if not recompile:
        lines.append("_None._")
    for program, books in recompile:
        lines.append(f"- **{program}** — includes {', '.join(books)}")
    lines.append("")

    if result.ddl:
        lines.append("## DDL plan")
        lines.append("")
        lines.append("```sql")
        lines.extend(result.ddl)
        lines.append("```")
        lines.append("")

    untraced = _untraced(result)
    warnings = list(dict.fromkeys(result.warnings))
    if untraced or warnings:
        lines.append("## Coverage gaps (not errors — read before trusting a clean run)")
        lines.append("")
        for finding in untraced:
            lines.append(f"- {finding.title}")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    return "\n".join(lines)


def write_summary(result: AnalysisResult, path: Path) -> None:
    path.write_text(to_summary(result), encoding="utf-8")


def output_basename(spec: ChangeSpec) -> str:
    """Filename stem for --out, from the tables being changed.

    A directory of impact.json files from six different runs is six files
    nobody can tell apart a week later. Naming them for the table means the
    output says what it is without being opened.
    """
    names = [_slug(table) for table in spec.tables]
    names = [name for name in names if name]
    if not names:
        return "impact"
    if len(names) <= _MAX_TABLES_IN_NAME:
        return "_".join(names)
    return f"{names[0]}_and_{len(names) - 1}_more"


_MAX_TABLES_IN_NAME = 3


def _slug(table: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", table).strip("_")
    return cleaned


# -- HTML ------------------------------------------------------------------

_HTML_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f7f7f5;
  --panel: #ffffff;
  --ink: #1c1d1f;
  --muted: #5b6470;
  --line: #dcdcd6;
  --accent: #244f6b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181b;
    --panel: #1e2125;
    --ink: #e8e8e6;
    --muted: #9aa2ad;
    --line: #33373d;
    --accent: #7fb3d5;
  }
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  padding: 32px 24px 64px;
  background: var(--bg);
  color: var(--ink);
  font: 14px/1.55 ui-sans-serif, system-ui, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.08em;
     color: var(--muted); margin: 40px 0 12px; }
h3 { font-size: 14px; margin: 22px 0 8px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.muted { color: var(--muted); }
.panel { background: var(--panel); border: 1px solid var(--line);
         border-radius: 8px; padding: 14px 16px; }
section { scroll-margin-top: 14px; }

.tiles { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 8px; }
a.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
         padding: 10px 14px; min-width: 104px; text-decoration: none; color: inherit;
         display: block; }
a.tile:hover { border-color: var(--accent); }
a.tile .n { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
a.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em;
            color: var(--muted); }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
.scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px;
          background: var(--panel); }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em;
     color: var(--muted); font-weight: 600; white-space: nowrap; }
tr:last-child td { border-bottom: none; }
code, .mono { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
              font-size: 12.5px; }
.mono, .loc { overflow-wrap: anywhere; }
.loc { color: var(--accent); font-size: 12px; }
.badge { display: inline-block; padding: 0 7px; border-radius: 999px;
         font-size: 10.5px; font-weight: 700; color: #fff; white-space: nowrap;
         vertical-align: 1px; }
.legend { color: var(--muted); font-size: 12.5px; margin: 0 0 10px; }

/* Flow tree */
.flowpanel { margin-bottom: 12px; }
.flowroot { font-weight: 600; margin-bottom: 6px; }
ul.flow { list-style: none; margin: 0; padding-left: 16px;
          border-left: 1px solid var(--line); }
ul.flow li { padding: 3px 0; }
ul.flow .fname { font-weight: 600; }
ul.flow .fmod { color: var(--muted); }
ul.flow .fwid { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }

ul.notes { margin: 4px 0 0; padding-left: 16px; color: var(--muted); font-size: 12px; }
ul.notes li { margin: 2px 0; }
ul.plain { list-style: none; padding: 0; margin: 0; }
ul.plain li { padding: 5px 0; border-bottom: 1px solid var(--line); }
ul.plain li:last-child { border-bottom: none; }
.filehead { font-weight: 600; margin: 16px 0 4px; }
.editrow { padding: 4px 0; border-bottom: 1px solid var(--line); }
.editrow:last-child { border-bottom: none; }
details.warn { background: var(--panel); border: 1px solid var(--line);
               border-left: 3px solid #b8531b; border-radius: 8px; padding: 10px 14px; }
details.warn summary { cursor: pointer; font-weight: 600; }
details.warn ul { margin: 10px 0 0; }

details.sevkey { background: var(--panel); border: 1px solid var(--line);
                 border-radius: 8px; padding: 10px 14px; margin-bottom: 8px; }
details.sevkey summary { cursor: pointer; font-weight: 600; color: var(--muted); }
details.sevkey dl { margin: 12px 0 0; display: grid;
                    grid-template-columns: max-content 1fr; gap: 6px 14px; }
details.sevkey dt { font-weight: 700; font-size: 11px; letter-spacing: 0.04em; }
details.sevkey dd { margin: 0; }
details.sevkey dd ul { margin: 4px 0 0; padding-left: 16px; color: var(--muted);
                       font-size: 12.5px; }
h3 .blurb { display: block; font-size: 12px; font-weight: 400; color: var(--muted);
            margin-top: 2px; }
"""


def _badge(sev: str) -> str:
    if not sev:
        return ""
    try:
        colour = _SEVERITY_COLOR[Severity(sev)]
    except ValueError:
        colour = "#5b6470"
    return f"<span class='badge' style='background:{colour}'>{html.escape(sev)}</span>"


def _flow_html(result: AnalysisResult) -> str:
    forest = _flow_forest(result)
    if not forest:
        return "<div class='panel'>No COBOL field is reached by this change.</div>"
    by_node = {finding.node_id: finding for finding in result.findings}
    parts: list[str] = []

    def render(children: dict[str, dict]) -> str:
        if not children:
            return ""
        items: list[str] = []
        for node_id, child in children.items():
            name, module, change, sev, loc = _flow_line(result, by_node, node_id)
            row = [f"<span class='fname'>{html.escape(name)}</span>"]
            if change:
                row.append(f"<span class='fwid'>{html.escape(change)}</span>")
            if sev:
                row.append(_badge(sev))
            if module:
                row.append(f"<span class='fmod'>{html.escape(module)}</span>")
            if loc:
                row.append(f"<span class='loc'>{html.escape(loc)}</span>")
            finding = by_node.get(node_id)
            notes = ""
            if finding is not None and finding.notes:
                notes = "<ul class='notes'>" + "".join(
                    f"<li>{html.escape(note)}</li>" for note in finding.notes
                ) + "</ul>"
            items.append(
                "<li>" + " &nbsp; ".join(row) + notes + render(child["children"]) + "</li>"
            )
        return "<ul class='flow'>" + "".join(items) + "</ul>"

    for root_id, root in sorted(forest.items()):
        plain = _plain_name(root_id)
        change = _seed_change(result, plain)
        head = html.escape(plain)
        if change is not None:
            head += (
                f" &nbsp; <span class='fwid'>{html.escape(change.old_type)} "
                f"&rarr; {html.escape(change.new_type)}</span>"
            )
        parts.append(
            f"<div class='panel flowpanel'><div class='flowroot mono'>{head}</div>"
            + render(root["children"])
            + "</div>"
        )
    return "".join(parts)


def _changes_by_file_html(result: AnalysisResult) -> str:
    edit_blocks, recompile = _changes_by_file(result)
    parts: list[str] = []
    if not edit_blocks:
        parts.append("<div class='panel'>No file needs a source edit — every affected program only needs rebuilding.</div>")
    for block in edit_blocks:
        parts.append(f"<div class='filehead mono'>{html.escape(block['file'])}</div>")
        parts.append("<div class='panel'>")
        for edit in block["edits"]:
            if edit["review"]:
                parts.append(
                    "<div class='editrow'>"
                    f"{_badge(edit['severity'].value)} &nbsp; "
                    f"<strong>{html.escape(edit['name'])}</strong> &nbsp; "
                    f"<span class='muted'>{html.escape(edit['review'])}</span></div>"
                )
            else:
                parts.append(
                    "<div class='editrow'>"
                    f"{_badge(edit['severity'].value)} &nbsp; "
                    f"<strong>{html.escape(edit['name'])}</strong> "
                    f"<span class='muted'>line {edit['line']}</span><br>"
                    f"<span class='mono'>{html.escape(edit['now'])}</span> "
                    f"&rarr; <span class='mono'>{html.escape(edit['to'])}</span></div>"
                )
        parts.append("</div>")

    if recompile:
        parts.append(
            f"<div class='filehead'>Recompile only ({len(recompile)}) — rebuild, no source edit</div>"
        )
        parts.append("<div class='scroll'><table><tr><th>Program</th><th>Includes</th></tr>")
        for program, books in recompile:
            parts.append(
                f"<tr><td class='mono'>{html.escape(program)}</td>"
                f"<td class='mono'>{html.escape(', '.join(books))}</td></tr>"
            )
        parts.append("</table></div>")
    return "".join(parts)


def to_html(result: AnalysisResult, title: str = "COBOL Column Widening Impact") -> str:
    counts = result.counts
    parts: list[str] = []
    parts.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>{html.escape(title)}</title>")
    parts.append(f"<style>{_HTML_STYLE}</style></head><body><main>")
    parts.append(f"<h1>{html.escape(title)}</h1>")

    edit_blocks, _ = _changes_by_file(result)
    parts.append(
        f"<p class='meta'>Generated {html.escape(_timestamp())} &middot; "
        f"{len(result.programs)} program(s) scanned &middot; "
        f"{len(edit_blocks)} file(s) to edit</p>"
    )

    parts.append("<div class='tiles'>")
    for level in _SEVERITY_ORDER:
        parts.append(
            f"<a class='tile' href='#sev-{level.value.lower()}'>"
            f"<div class='n' style='color:{_SEVERITY_COLOR[level]}'>{counts[level.value]}</div>"
            f"<div class='k'>{level.value}</div></a>"
        )
    parts.append("</div>")

    parts.append(
        "<details class='sevkey'><summary>What the severities mean</summary><dl>"
    )
    for level in _SEVERITY_ORDER:
        bullets = "".join(
            f"<li>{html.escape(item)}</li>" for item in _SEVERITY_DETAIL[level]
        )
        parts.append(
            f"<dt style='color:{_SEVERITY_COLOR[level]}'>{level.value}</dt>"
            f"<dd>{html.escape(_SEVERITY_BLURB[level])}<ul>{bullets}</ul></dd>"
        )
    parts.append(
        "</dl><p class='legend' style='margin:10px 0 0'>A field that trips "
        "several of these stays one finding and takes the worst severity.</p>"
        "</details>"
    )

    # 1. the base value
    parts.append("<h2>Requested change</h2><div class='scroll'><table>")
    parts.append("<tr><th>Column</th><th>From</th><th>To</th><th>DDL</th></tr>")
    for change in result.spec.changes:
        parts.append(
            "<tr>"
            f"<td class='mono'>{html.escape(change.key)}</td>"
            f"<td class='mono'>{html.escape(change.old_type)}</td>"
            f"<td class='mono'>{html.escape(change.new_type)}</td>"
            f"<td class='mono'>{html.escape(change.alter_statement())}</td>"
            "</tr>"
        )
    parts.append("</table></div>")

    # 2. the trace
    parts.append("<h2>Flow</h2>")
    parts.append(
        "<p class='legend'>Where the value moves. A badge marks a field that "
        "cannot hold the widened value; COBOL truncates it silently — characters "
        "on the right, digits on the left, no runtime error.</p>"
    )
    parts.append(_flow_html(result))

    # 3. the per-module edits
    parts.append("<h2>Changes by module</h2>")
    parts.append(_changes_by_file_html(result))

    if result.ddl:
        parts.append("<h2>DDL plan</h2><div class='panel'><ul class='plain'>")
        for statement in result.ddl:
            parts.append(f"<li class='mono'>{html.escape(statement)}</li>")
        parts.append("</ul></div>")

    # 4. severity index — the tiles jump here
    parts.append("<h2>Findings by severity</h2>")
    for level in _SEVERITY_ORDER:
        bucket = [f for f in result.findings if f.severity is level]
        parts.append(f"<section id='sev-{level.value.lower()}'>")
        parts.append(
            f"<h3>{level.value} <span class='muted'>({len(bucket)})</span>"
            f"<span class='blurb'>{html.escape(_SEVERITY_BLURB[level])}</span></h3>"
        )
        if not bucket:
            parts.append("<p class='muted'>none</p></section>")
            continue
        parts.append("<div class='scroll'><table>")
        parts.append("<tr><th>Field</th><th>Now &rarr; needs</th><th>Where</th></tr>")
        for finding in bucket:
            where = "<br>".join(
                f"<span class='loc mono'>{html.escape(ref.location())}</span>"
                for ref in finding.refs[:2]
            )
            notes = ""
            if finding.notes:
                notes = "<ul class='notes'>" + "".join(
                    f"<li>{html.escape(note)}</li>" for note in finding.notes
                ) + "</ul>"
            parts.append(
                "<tr>"
                f"<td><strong>{html.escape(_plain_name(finding.node_id))}</strong> "
                f"<span class='muted'>{html.escape(finding.category)}</span>{notes}</td>"
                f"<td class='mono'>{html.escape(finding.current)} &rarr; "
                f"{html.escape(finding.required)}</td>"
                f"<td>{where}</td>"
                "</tr>"
            )
        parts.append("</table></div></section>")

    # 5. coverage
    warnings = list(dict.fromkeys(result.warnings))
    untraced = _untraced(result)
    if warnings or untraced:
        total = len(warnings) + len(untraced)
        parts.append(
            f"<h2>Coverage gaps</h2><details class='warn'><summary>{total} "
            "gap(s) in the trace — not errors, but read them before trusting a clean run"
            "</summary><ul class='plain'>"
        )
        for finding in untraced:
            parts.append(f"<li>{html.escape(finding.title)}</li>")
        for warning in warnings:
            parts.append(f"<li class='mono'>{html.escape(warning)}</li>")
        parts.append("</ul></details>")

    parts.append("</main></body></html>")
    return "".join(parts)


def write_html(result: AnalysisResult, path: Path, title: str = "COBOL Column Widening Impact") -> None:
    path.write_text(to_html(result, title), encoding="utf-8")
