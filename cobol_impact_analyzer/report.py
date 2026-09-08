"""Report rendering: terminal text, JSON, CSV and a self-contained HTML page."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .analyzer import AnalysisResult
from .models import Finding, NodeKind, Severity

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
        },
        "ddl_plan": result.ddl,
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


# -- CSV -------------------------------------------------------------------

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
                }
            )


# -- terminal --------------------------------------------------------------


def to_text(result: AnalysisResult, verbose: bool = False) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("COBOL COLUMN WIDENING IMPACT REPORT")
    lines.append(f"generated {_timestamp()}")
    lines.append("=" * 78)
    lines.append("")

    lines.append("REQUESTED CHANGES")
    for change in result.spec.changes:
        lines.append(f"  {change.key:<40} {change.old_type} -> {change.new_type}")
    lines.append("")

    counts = result.counts
    lines.append("SUMMARY")
    lines.append(f"  programs scanned : {len(result.programs)}")
    lines.append(f"  graph nodes      : {len(result.graph.nodes)}")
    lines.append(
        f"  graph edges      : {sum(len(edges) for edges in result.graph.out_edges.values())}"
    )
    lines.append(
        "  findings         : "
        + ", ".join(f"{level.value} {counts[level.value]}" for level in _SEVERITY_ORDER)
    )
    lines.append("")

    for level in _SEVERITY_ORDER:
        bucket = [finding for finding in result.findings if finding.severity is level]
        if not bucket:
            continue
        lines.append(f"{level.value} ({len(bucket)})")
        lines.append("-" * 78)
        for finding in bucket:
            lines.append(f"  [{finding.category}] {finding.title}")
            if finding.current:
                lines.append(f"      now      : {finding.current}")
            if finding.required:
                lines.append(f"      needs    : {finding.required}")
            if finding.remediation:
                lines.append(f"      action   : {finding.remediation}")
            if finding.refs:
                for ref in finding.refs[:3]:
                    where = f"{ref.location()}"
                    if ref.program:
                        where += f"  ({ref.program}"
                        where += f" / {ref.paragraph})" if ref.paragraph else ")"
                    lines.append(f"      at       : {where}")
            if len(finding.path) > 1:
                lines.append(f"      path     : {' -> '.join(_pretty_path(finding.path))}")
            if verbose and finding.detail:
                lines.append(f"      why      : {finding.detail}")
            lines.append("")
        lines.append("")

    if result.ddl:
        lines.append("DDL PLAN")
        lines.append("-" * 78)
        lines.extend(f"  {statement}" for statement in result.ddl)
        lines.append("")

    if result.warnings:
        lines.append("WARNINGS (coverage gaps — read these before trusting the result)")
        lines.append("-" * 78)
        for warning in dict.fromkeys(result.warnings):
            lines.append(f"  {warning}")
        lines.append("")

    return "\n".join(lines)


def _pretty_path(path: Iterable[str]) -> list[str]:
    pretty: list[str] = []
    for node_id in path:
        if node_id.startswith("col:"):
            pretty.append(node_id[4:])
        elif node_id.startswith("var:"):
            pretty.append(node_id[4:])
        else:
            pretty.append(node_id)
    return pretty


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
body {
  margin: 0;
  padding: 32px 24px 64px;
  background: var(--bg);
  color: var(--ink);
  font: 14px/1.55 ui-sans-serif, system-ui, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: 0.08em;
     color: var(--muted); margin: 36px 0 12px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
.panel { background: var(--panel); border: 1px solid var(--line);
         border-radius: 8px; padding: 16px 18px; }
.tiles { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 8px; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
        padding: 10px 14px; min-width: 108px; }
.tile .n { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em;
           color: var(--muted); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
.scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px;
          background: var(--panel); }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em;
     color: var(--muted); font-weight: 600; white-space: nowrap; }
tr:last-child td { border-bottom: none; }
code, .mono { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
              font-size: 12.5px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px;
         font-size: 11px; font-weight: 600; color: #fff; white-space: nowrap; }
.path { color: var(--muted); font-size: 12px; }
.loc { color: var(--accent); font-size: 12px; }
ul.plain { list-style: none; padding: 0; margin: 0; }
ul.plain li { padding: 5px 0; border-bottom: 1px solid var(--line); }
ul.plain li:last-child { border-bottom: none; }
.warn { border-left: 3px solid #b8531b; padding-left: 12px; }
"""


def to_html(result: AnalysisResult, title: str = "COBOL Column Widening Impact") -> str:
    counts = result.counts
    parts: list[str] = []
    parts.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>{html.escape(title)}</title>")
    parts.append(f"<style>{_HTML_STYLE}</style></head><body><main>")
    parts.append(f"<h1>{html.escape(title)}</h1>")
    parts.append(
        f"<p class='meta'>Generated {html.escape(_timestamp())} &middot; "
        f"{len(result.programs)} program(s) scanned &middot; "
        f"{len(result.graph.nodes)} graph nodes</p>"
    )

    parts.append("<div class='tiles'>")
    for level in _SEVERITY_ORDER:
        parts.append(
            "<div class='tile'>"
            f"<div class='n' style='color:{_SEVERITY_COLOR[level]}'>{counts[level.value]}</div>"
            f"<div class='k'>{level.value}</div></div>"
        )
    parts.append("</div>")

    parts.append("<h2>Requested changes</h2><div class='scroll'><table>")
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

    parts.append("<h2>Impacts</h2>")
    if not result.findings:
        parts.append("<div class='panel'>No impacts found for this change.</div>")
    else:
        parts.append("<div class='scroll'><table>")
        parts.append(
            "<tr><th>Sev</th><th>What</th><th>Now</th><th>Needs</th>"
            "<th>Action</th><th>Where</th></tr>"
        )
        for finding in result.findings:
            colour = _SEVERITY_COLOR[finding.severity]
            locations = "<br>".join(
                f"<span class='loc mono'>{html.escape(ref.location())}</span>"
                for ref in finding.refs[:4]
            )
            path = ""
            if len(finding.path) > 1:
                trail = html.escape(" -> ".join(_pretty_path(finding.path)))
                path = f"<div class='path mono'>{trail}</div>"
            parts.append(
                "<tr>"
                f"<td><span class='badge' style='background:{colour}'>"
                f"{finding.severity.value}</span></td>"
                f"<td><strong>{html.escape(finding.title)}</strong>"
                f"<div class='path'>{html.escape(finding.category)} &middot; "
                f"{finding.distance} hop(s) from the change</div>"
                f"<div class='path'>{html.escape(finding.detail)}</div>{path}</td>"
                f"<td class='mono'>{html.escape(finding.current)}</td>"
                f"<td class='mono'>{html.escape(finding.required)}</td>"
                f"<td class='mono'>{html.escape(finding.remediation)}</td>"
                f"<td>{locations}</td>"
                "</tr>"
            )
        parts.append("</table></div>")

    if result.ddl:
        parts.append("<h2>DDL plan</h2><div class='panel'><ul class='plain'>")
        for statement in result.ddl:
            parts.append(f"<li class='mono'>{html.escape(statement)}</li>")
        parts.append("</ul></div>")

    parts.append("<h2>Programs scanned</h2><div class='scroll'><table>")
    parts.append("<tr><th>Program</th><th>Path</th><th>SQL</th><th>Data items</th></tr>")
    for program in result.programs:
        parts.append(
            "<tr>"
            f"<td class='mono'>{html.escape(program.name)}</td>"
            f"<td class='mono'>{html.escape(program.path)}</td>"
            f"<td>{len(program.sql)}</td>"
            f"<td>{len(program.data.order)}</td>"
            "</tr>"
        )
    parts.append("</table></div>")

    if result.warnings:
        parts.append("<h2>Coverage warnings</h2><div class='panel warn'><ul class='plain'>")
        for warning in dict.fromkeys(result.warnings):
            parts.append(f"<li class='mono'>{html.escape(warning)}</li>")
        parts.append("</ul></div>")

    parts.append("</main></body></html>")
    return "".join(parts)


def write_html(result: AnalysisResult, path: Path, title: str = "COBOL Column Widening Impact") -> None:
    path.write_text(to_html(result, title), encoding="utf-8")
