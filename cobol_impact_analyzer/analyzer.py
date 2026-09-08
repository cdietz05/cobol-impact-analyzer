"""The impact engine: build the data-flow graph, then propagate width changes.

The model is deliberately simple and explainable:

1. Every column and every data item becomes a node carrying its *current*
   capacity.
2. Every assignment, bind, fetch, concatenation and call argument becomes a
   directed edge.
3. Seeding the changed columns with their *new* capacity and pushing that
   requirement along the edges until nothing grows any more yields, for each
   node, the capacity it must have after the change.
4. Any node whose current capacity cannot cover its required capacity is a
   finding, reported with the path that got there.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import picture
from .copybook import CopybookResolver
from .models import (
    Capacity,
    ColumnChange,
    Edge,
    EdgeKind,
    Field,
    Finding,
    Kind,
    Node,
    NodeKind,
    Severity,
    SourceRef,
)
from .pco import CallSite, Flow, Program, ProgramParser, Usage, discover_sources
from .spec import ChangeSpec
from .sqlparse import Direction, SqlStatement

# Edges where the destination accumulates rather than simply receiving a value.
_COMBINING = frozenset(
    {EdgeKind.STRING, EdgeKind.COMPUTE, EdgeKind.ARITHMETIC}
)
# Edges where growing the source forces the destination to grow in lockstep
# because they share physical storage.
_LAYOUT = frozenset({EdgeKind.GROUP_PARENT, EdgeKind.REDEFINES})


@dataclass
class ImpactGraph:
    """Nodes plus adjacency, with a couple of convenience indexes."""

    nodes: dict[str, Node] = field(default_factory=dict)
    out_edges: dict[str, list[Edge]] = field(default_factory=dict)
    in_edges: dict[str, list[Edge]] = field(default_factory=dict)

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.node_id)
        if existing is not None:
            existing.programs |= node.programs
            if existing.capacity.kind is Kind.UNKNOWN:
                existing.capacity = node.capacity
            return existing
        self.nodes[node.node_id] = node
        return node

    def add_edge(self, edge: Edge) -> None:
        if edge.source_id not in self.nodes or edge.target_id not in self.nodes:
            return
        if edge.source_id == edge.target_id:
            return
        bucket = self.out_edges.setdefault(edge.source_id, [])
        for existing in bucket:
            if (
                existing.target_id == edge.target_id
                and existing.kind is edge.kind
                and existing.ref.line == edge.ref.line
                and existing.ref.path == edge.ref.path
            ):
                return
        bucket.append(edge)
        self.in_edges.setdefault(edge.target_id, []).append(edge)

    def successors(self, node_id: str) -> list[Edge]:
        return self.out_edges.get(node_id, [])

    def to_dict(self) -> dict[str, object]:
        return {
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [
                edge.to_dict()
                for edges in self.out_edges.values()
                for edge in edges
            ],
        }


@dataclass
class AnalysisResult:
    """Everything a report needs."""

    spec: ChangeSpec
    programs: list[Program]
    graph: ImpactGraph
    findings: list[Finding]
    required: dict[str, Capacity]
    paths: dict[str, list[str]]
    warnings: list[str]
    ddl: list[str]

    @property
    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {level.value: 0 for level in Severity}
        for finding in self.findings:
            totals[finding.severity.value] += 1
        return totals


class ImpactAnalyzer:
    """Runs one analysis end to end."""

    def __init__(self, spec: ChangeSpec) -> None:
        self.spec = spec
        self.graph = ImpactGraph()
        self.programs: list[Program] = []
        self.warnings: list[str] = []
        # (program name, field name) -> node id, for resolving usages later.
        self._field_nodes: dict[tuple[str, str], str] = {}
        self._node_fields: dict[str, list[tuple[Program, Field]]] = {}
        self._seed_changes: dict[str, ColumnChange] = {}
        self._depth: dict[str, int] = {}
        # Columns whose current width was guessed from a bound host variable.
        self._inferred_columns: set[str] = set()

    # -- entry point ------------------------------------------------------

    def run(self) -> AnalysisResult:
        self._parse_sources()
        self._build_graph()
        required, paths, edges_used = self._propagate()
        findings = self._collect_findings(required, paths, edges_used)
        ddl = self._ddl_plan(required)
        return AnalysisResult(
            spec=self.spec,
            programs=self.programs,
            graph=self.graph,
            findings=findings,
            required=required,
            paths=paths,
            warnings=self.warnings,
            ddl=ddl,
        )

    # -- stage 1: parse ---------------------------------------------------

    def _parse_sources(self) -> None:
        resolver = CopybookResolver(self.spec.copybook_paths)
        parser = ProgramParser(resolver)
        sources = discover_sources(self.spec.source_paths, self.spec.source_patterns)
        if not sources:
            self.warnings.append(
                "no source files matched "
                f"{', '.join(self.spec.source_patterns)} under "
                f"{', '.join(str(path) for path in self.spec.source_paths) or '(no paths given)'}"
            )
        for path in sources:
            try:
                program = parser.parse(path, self.spec.source_format)
            except OSError as error:
                self.warnings.append(f"{path}: could not be read ({error})")
                continue
            self.programs.append(program)
            self.warnings.extend(program.warnings)

    # -- stage 2: graph ---------------------------------------------------

    def _build_graph(self) -> None:
        for change in self.spec.changes:
            node = Node(
                node_id=_column_id(change.table, change.column),
                kind=NodeKind.COLUMN,
                name=change.key,
                capacity=change.old_capacity,
            )
            self.graph.add_node(node)
            self._seed_changes[node.node_id] = change

        for program in self.programs:
            self._add_program_nodes(program)
        for program in self.programs:
            self._add_sql_edges(program)
            self._add_flow_edges(program)
            self._add_layout_edges(program)
        self._add_call_edges()

    def _add_program_nodes(self, program: Program) -> None:
        for key in program.data.order:
            item = program.data.fields[key]
            if item.level == 88:
                continue
            node_id = self._variable_id(program, item)
            node = Node(
                node_id=node_id,
                kind=NodeKind.VARIABLE,
                name=item.name,
                capacity=_effective_capacity(item),
                declared_in=item.source,
                field_ref=item,
                programs={program.name},
            )
            self.graph.add_node(node)
            self._field_nodes[(program.name, item.name)] = node_id
            self._node_fields.setdefault(node_id, []).append((program, item))

    def _add_sql_edges(self, program: Program) -> None:
        for statement in program.sql:
            for note in statement.unresolved:
                self.warnings.append(f"{statement.ref.location()}: {note}")
            for binding in statement.bindings:
                if not binding.column:
                    continue
                table = binding.table or (statement.tables[0] if statement.tables else "")
                if not table:
                    continue
                variable_id = self._field_nodes.get((program.name, binding.host_var))
                if variable_id is None:
                    self.warnings.append(
                        f"{statement.ref.location()}: host variable :{binding.host_var} "
                        f"is not declared in {program.name}"
                    )
                    continue
                column_id = _column_id(table, binding.column)
                if column_id not in self.graph.nodes:
                    # Columns other than the ones being changed have no declared
                    # type here — this tool never reads DDL. The host variable
                    # bound to them is the best available proxy: a shop sizes
                    # PIC X(30) against VARCHAR2(30). Findings raised against an
                    # inferred column say so.
                    inferred = self.graph.nodes[variable_id].capacity
                    self._inferred_columns.add(column_id)
                    self.graph.add_node(
                        Node(
                            node_id=column_id,
                            kind=NodeKind.COLUMN,
                            name=f"{table.upper()}.{binding.column.upper()}",
                            capacity=inferred,
                        )
                    )
                ref = SourceRef(
                    path=statement.ref.path,
                    line=statement.ref.line,
                    program=program.name,
                    text=statement.text[:200],
                )
                if binding.direction is Direction.OUT:
                    self.graph.add_edge(
                        Edge(column_id, variable_id, EdgeKind.SQL_FETCH, ref, binding.note)
                    )
                elif binding.direction is Direction.IN:
                    self.graph.add_edge(
                        Edge(variable_id, column_id, EdgeKind.SQL_BIND, ref, binding.note)
                    )
                else:
                    self.graph.add_edge(
                        Edge(column_id, variable_id, EdgeKind.SQL_PREDICATE, ref, binding.note)
                    )

    def _add_flow_edges(self, program: Program) -> None:
        for flow in program.flows:
            target_id = self._field_nodes.get((program.name, flow.target))
            if target_id is None:
                continue
            ref = flow.ref
            for source in flow.sources:
                source_id = self._field_nodes.get((program.name, source))
                if source_id is None:
                    continue
                self.graph.add_edge(Edge(source_id, target_id, flow.kind, ref, flow.note))

    def _add_layout_edges(self, program: Program) -> None:
        """Group membership and REDEFINES: storage that moves together."""
        for key in program.data.order:
            item = program.data.fields[key]
            if item.level == 88:
                continue
            child_id = self._field_nodes.get((program.name, item.name))
            if child_id is None:
                continue
            if item.parent:
                parent = program.data.fields.get(item.parent)
                if parent is not None:
                    parent_id = self._field_nodes.get((program.name, parent.name))
                    if parent_id:
                        ref = item.source or SourceRef(path=program.path, line=0)
                        self.graph.add_edge(
                            Edge(child_id, parent_id, EdgeKind.GROUP_PARENT, ref, "group member")
                        )
            if item.redefines:
                other_id = self._field_nodes.get((program.name, item.redefines))
                if other_id:
                    ref = item.source or SourceRef(path=program.path, line=0)
                    self.graph.add_edge(
                        Edge(other_id, child_id, EdgeKind.REDEFINES, ref, "shares storage")
                    )
                    self.graph.add_edge(
                        Edge(child_id, other_id, EdgeKind.REDEFINES, ref, "shares storage")
                    )

    def _add_call_edges(self) -> None:
        by_name = {program.name: program for program in self.programs}
        for program in self.programs:
            for call in program.calls:
                callee = by_name.get(call.target)
                if callee is None:
                    if call.args:
                        self.warnings.append(
                            f"{call.ref.location()}: called program {call.target} is outside the "
                            "scan scope; its parameter widths cannot be checked"
                        )
                    continue
                for position, argument in enumerate(call.args):
                    if position >= len(callee.linkage_using):
                        continue
                    caller_id = self._field_nodes.get((program.name, argument))
                    callee_id = self._field_nodes.get(
                        (callee.name, callee.linkage_using[position])
                    )
                    if not caller_id or not callee_id:
                        continue
                    note = f"argument {position + 1} of CALL {call.target}"
                    self.graph.add_edge(
                        Edge(caller_id, callee_id, EdgeKind.CALL_ARG, call.ref, note)
                    )
                    self.graph.add_edge(
                        Edge(callee_id, caller_id, EdgeKind.CALL_ARG, call.ref, note)
                    )

    # -- stage 3: propagate -----------------------------------------------

    def _propagate(self) -> tuple[dict[str, Capacity], dict[str, list[str]], dict[str, Edge]]:
        required: dict[str, Capacity] = {}
        paths: dict[str, list[str]] = {}
        depth: dict[str, int] = {}
        arriving: dict[str, Edge] = {}
        queue: deque[str] = deque()

        for change in self.spec.changes:
            node_id = _column_id(change.table, change.column)
            required[node_id] = change.new_capacity
            paths[node_id] = [node_id]
            depth[node_id] = 0
            queue.append(node_id)

        limit = self.spec.max_depth or 0
        # Propagation converges because requirements only ever grow toward a
        # finite bound, but a malformed source should not be able to hang a
        # batch job, so the walk is also capped.
        budget = max(10_000, 50 * (len(self.graph.nodes) + 1))
        while queue:
            budget -= 1
            if budget <= 0:
                self.warnings.append(
                    "propagation stopped early after "
                    f"{max(10_000, 50 * (len(self.graph.nodes) + 1))} steps; "
                    "the result may be incomplete"
                )
                break
            node_id = queue.popleft()
            node = self.graph.nodes.get(node_id)
            if node is None:
                continue
            current_depth = depth.get(node_id, 0)
            if limit and current_depth >= limit:
                continue
            source_current = node.capacity
            source_required = required[node_id]
            for edge in self.graph.successors(node_id):
                target = self.graph.nodes.get(edge.target_id)
                if target is None:
                    continue
                # Always measure against the target's ORIGINAL capacity. Feeding
                # back an already-grown requirement would add the delta again on
                # every pass around a cycle (REDEFINES and CALL argument edges
                # are bidirectional) and never converge.
                candidate = _required_at_target(
                    edge, source_current, source_required, target.capacity
                )
                if candidate is None:
                    continue
                previous = required.get(edge.target_id)
                if previous is not None and previous.covers(candidate):
                    continue
                merged = previous.grown_to_hold(candidate) if previous else candidate
                required[edge.target_id] = merged
                paths[edge.target_id] = paths.get(node_id, [node_id]) + [edge.target_id]
                depth[edge.target_id] = current_depth + 1
                arriving[edge.target_id] = edge
                queue.append(edge.target_id)

        self._depth = depth
        return required, paths, arriving

    # -- stage 4: findings -------------------------------------------------

    def _collect_findings(
        self,
        required: dict[str, Capacity],
        paths: dict[str, list[str]],
        arriving: dict[str, Edge],
    ) -> list[Finding]:
        findings: list[Finding] = []
        seeds = {_column_id(change.table, change.column) for change in self.spec.changes}
        impacted: set[str] = set()

        for node_id, need in required.items():
            node = self.graph.nodes.get(node_id)
            if node is None:
                continue
            distance = self._depth.get(node_id, 0)
            path = paths.get(node_id, [node_id])
            edge = arriving.get(node_id)

            if node_id in seeds:
                findings.append(self._seed_finding(node_id, node, distance, path))
                continue
            if node.capacity.covers(need):
                continue
            impacted.add(node_id)
            if node.kind is NodeKind.COLUMN:
                findings.append(self._column_finding(node, need, distance, path, edge))
            else:
                findings.append(self._variable_finding(node, need, distance, path, edge))

        findings.extend(self._usage_findings(impacted, required))
        findings.extend(self._unreferenced_findings(seeds))
        findings.sort(key=Finding.sort_key)
        return findings

    def _seed_finding(
        self, node_id: str, node: Node, distance: int, path: list[str]
    ) -> Finding:
        change = self._seed_changes[node_id]
        return Finding(
            severity=Severity.INFO,
            category="requested-change",
            node_id=node_id,
            title=f"{change.key}: {change.old_type} -> {change.new_type}",
            detail="The change you asked about. Everything below follows from it.",
            current=change.old_capacity.describe(),
            required=change.new_capacity.describe(),
            remediation=change.alter_statement(),
            distance=distance,
            path=path,
        )

    def _column_finding(
        self,
        node: Node,
        need: Capacity,
        distance: int,
        path: list[str],
        edge: Optional[Edge],
    ) -> Finding:
        refs = [edge.ref] if edge else []
        suggestion = _suggest_sql_type(node.capacity, need)
        table, _, column = node.name.partition(".")
        inferred = node.node_id in self._inferred_columns
        caveat = (
            " Its current width was inferred from the host variable bound to it, "
            "not read from the catalog — confirm against the real DDL."
            if inferred
            else ""
        )
        return Finding(
            severity=Severity.HIGH,
            category="database-column",
            node_id=node.node_id,
            title=f"{node.name} receives widened data and must grow too",
            detail=(
                "A host variable carrying the widened value is bound to this column"
                f"{_via(edge)}. Writing the wider value into the current column "
                f"raises ORA-01401/ORA-12899 or silently truncates.{caveat}"
            ),
            current=node.capacity.describe() + (" (inferred)" if inferred else ""),
            required=need.describe(),
            remediation=f"ALTER TABLE {table} MODIFY {column} {suggestion};",
            distance=distance,
            path=path,
            refs=refs,
        )

    def _variable_finding(
        self,
        node: Node,
        need: Capacity,
        distance: int,
        path: list[str],
        edge: Optional[Edge],
    ) -> Finding:
        entries = self._node_fields.get(node.node_id, [])
        item = entries[0][1] if entries else node.field_ref
        refs = [entry[1].source for entry in entries if entry[1].source]
        if edge is not None:
            refs = [edge.ref] + refs

        if item is not None and item.is_group:
            new_bytes = max(need.chars, item.storage_bytes)
            return Finding(
                severity=Severity.MEDIUM,
                category="record-layout",
                node_id=node.node_id,
                title=f"{node.name}: record length grows {item.storage_bytes} -> {new_bytes} bytes",
                detail=(
                    "This group contains a field that must widen, so every file, "
                    "queue, CALL interface or REDEFINES that assumes the old record "
                    "length has to be reviewed and rebuilt."
                ),
                current=f"{item.storage_bytes} bytes",
                required=f"{new_bytes} bytes",
                remediation=(
                    "Rebuild the record layout, then recompile every program that "
                    "copies this group and reload any file written with the old length."
                ),
                distance=distance,
                path=path,
                refs=refs,
            )

        severity = Severity.CRITICAL if edge is not None and edge.kind.truncates else Severity.HIGH
        usage = item.usage if item is not None else "DISPLAY"
        current_pic = item.picture if item is not None else ""
        suggested = (
            picture.render_picture(need, usage, template=current_pic)
            if current_pic
            else picture.render_picture(need, usage)
        )
        declaration = item.declaration() if item is not None else node.name
        level = item.level if item is not None else 5
        new_declaration = _redeclare(declaration, current_pic, suggested)
        return Finding(
            severity=severity,
            category="host-variable" if distance <= 1 else "derived-variable",
            node_id=node.node_id,
            title=f"{node.name} is too small for the widened value",
            detail=(
                f"Reached{_via(edge)}. Current PIC holds {node.capacity.describe()}, "
                f"but {need.describe()} arrives here. COBOL truncates on the left for "
                "numerics and on the right for alphanumerics, without any runtime error."
            ),
            current=f"{level:02d} {node.name} PIC {current_pic or '(none)'} {usage}".strip(),
            required=need.describe(),
            remediation=f"Change to: {new_declaration}",
            distance=distance,
            path=path,
            refs=refs,
        )

    def _usage_findings(
        self, impacted: set[str], required: dict[str, Capacity]
    ) -> list[Finding]:
        """Non-flow mentions of an impacted field that a widening will disturb."""
        findings: list[Finding] = []
        category_severity = {
            "reference-modification": Severity.HIGH,
            "literal-comparison": Severity.MEDIUM,
            "inspect": Severity.MEDIUM,
            "display": Severity.LOW,
            "file-write": Severity.MEDIUM,
            "initialize": Severity.LOW,
            "set": Severity.LOW,
        }
        category_advice = {
            "reference-modification": (
                "The offset/length is hard-coded and will not follow the new width. "
                "Recalculate it or replace it with a symbolic length."
            ),
            "literal-comparison": (
                "The literal is padded to the field width. A wider field changes the "
                "comparison result unless the literal is re-checked."
            ),
            "inspect": (
                "INSPECT scans the entire field including trailing spaces, so counts "
                "and replacements change when the field grows."
            ),
            "display": "Report or log column alignment shifts when the field grows.",
            "file-write": "The output record grows; downstream readers need the new layout.",
            "initialize": "Confirm the initialised value still fits the intended semantics.",
            "set": "Confirm the SET target still matches the new width.",
        }

        for program in self.programs:
            for usage in program.usages:
                node_id = self._field_nodes.get((program.name, usage.name))
                if node_id is None or node_id not in impacted:
                    continue
                severity = category_severity.get(usage.category, Severity.LOW)
                findings.append(
                    Finding(
                        severity=severity,
                        category=usage.category,
                        node_id=node_id,
                        title=f"{usage.name} used via {usage.category} in {program.name}",
                        detail=usage.detail or category_advice.get(usage.category, ""),
                        current=self.graph.nodes[node_id].capacity.describe(),
                        required=required[node_id].describe(),
                        remediation=category_advice.get(usage.category, "Review this usage."),
                        distance=self._depth.get(node_id, 0),
                        path=[node_id],
                        refs=[usage.ref],
                    )
                )

        for program in self.programs:
            for key in program.data.order:
                item = program.data.fields[key]
                node_id = self._field_nodes.get((program.name, item.name))
                if node_id is None or node_id not in impacted or not item.value:
                    continue
                findings.append(
                    Finding(
                        severity=Severity.MEDIUM,
                        category="value-clause",
                        node_id=node_id,
                        title=f"{item.name} has a VALUE clause that assumes the old width",
                        detail=f"Declared with VALUE {item.value}.",
                        current=item.declaration(),
                        required=required[node_id].describe(),
                        remediation="Re-check the initial value against the new field width.",
                        distance=self._depth.get(node_id, 0),
                        path=[node_id],
                        refs=[item.source] if item.source else [],
                    )
                )
        return findings

    def _unreferenced_findings(self, seeds: set[str]) -> list[Finding]:
        """Changed columns that no scanned program ever touches."""
        findings: list[Finding] = []
        for node_id in seeds:
            if self.graph.successors(node_id):
                continue
            change = self._seed_changes[node_id]
            findings.append(
                Finding(
                    severity=Severity.MEDIUM,
                    category="coverage-gap",
                    node_id=node_id,
                    title=f"{change.key} is not referenced by any scanned program",
                    detail=(
                        "Either nothing reads or writes this column, or the statement "
                        "that does was not parsed (dynamic SQL, a cursor declared in an "
                        "unscanned copybook, or a source directory missing from the scan). "
                        "Widen the scan before trusting a clean result here."
                    ),
                    current=change.old_type,
                    required=change.new_type,
                    remediation=(
                        "Re-run with the full source tree, then grep for the column name "
                        "to rule out dynamic SQL."
                    ),
                    distance=0,
                    path=[node_id],
                )
            )
        return findings

    # -- stage 5: DDL plan --------------------------------------------------

    def _ddl_plan(self, required: dict[str, Capacity]) -> list[str]:
        statements = [change.alter_statement() for change in self.spec.changes]
        seeds = {_column_id(change.table, change.column) for change in self.spec.changes}
        for node_id, need in sorted(required.items()):
            if node_id in seeds:
                continue
            node = self.graph.nodes.get(node_id)
            if node is None or node.kind is not NodeKind.COLUMN:
                continue
            if node.capacity.covers(need):
                continue
            table, _, column = node.name.partition(".")
            statements.append(
                f"ALTER TABLE {table} MODIFY {column} {_suggest_sql_type(node.capacity, need)};"
            )
        return statements

    # -- helpers ------------------------------------------------------------

    def _variable_id(self, program: Program, item: Field) -> str:
        origin = item.source.path if item.source else ""
        shared = bool(origin) and Path(origin) != Path(program.path)
        if self.spec.global_variable_scope or shared:
            return f"var:{item.name}"
        return f"var:{program.name}::{item.name}"


def _column_id(table: str, column: str) -> str:
    return f"col:{table.upper()}.{column.upper()}"


def _effective_capacity(item: Field) -> Capacity:
    if item.capacity.kind is Kind.UNKNOWN and item.storage_bytes:
        return Capacity(kind=Kind.GROUP, chars=item.storage_bytes)
    return item.capacity


def _via(edge: Optional[Edge]) -> str:
    if edge is None:
        return ""
    where = f" at {edge.ref.location()}" if edge.ref.path else ""
    return f" through a {edge.kind.value} edge{where}"


def _required_at_target(
    edge: Edge,
    source_current: Capacity,
    source_required: Capacity,
    target_current: Capacity,
) -> Optional[Capacity]:
    """Capacity the destination of ``edge`` needs, given a widened source."""
    if edge.kind in _COMBINING or edge.kind in _LAYOUT:
        return _grow_by_delta(target_current, source_current, source_required)
    if edge.kind is EdgeKind.UNSTRING:
        # One delimited piece of the source lands here; it can be as long as the
        # whole widened source in the worst case.
        return target_current.grown_to_hold(source_required)
    return target_current.grown_to_hold(source_required)


def _grow_by_delta(
    target_current: Capacity,
    source_current: Capacity,
    source_required: Capacity,
) -> Optional[Capacity]:
    """Grow the destination by exactly the amount the source grew."""
    if target_current.kind.is_numeric() and source_required.kind.is_numeric():
        int_delta = max(source_required.int_digits - source_current.int_digits, 0)
        dec_delta = max(source_required.dec_digits - source_current.dec_digits, 0)
        if not int_delta and not dec_delta:
            return None
        return Capacity(
            kind=target_current.kind,
            chars=target_current.chars,
            int_digits=target_current.int_digits + int_delta,
            dec_digits=target_current.dec_digits + dec_delta,
            signed=target_current.signed,
        )
    delta = max(source_required.text_width - source_current.text_width, 0)
    if not delta:
        return None
    if target_current.kind is Kind.UNKNOWN:
        return Capacity(kind=Kind.GROUP, chars=delta)
    return Capacity(
        kind=target_current.kind,
        chars=target_current.chars + delta,
        int_digits=target_current.int_digits,
        dec_digits=target_current.dec_digits,
        signed=target_current.signed,
    )


def _suggest_sql_type(current: Capacity, need: Capacity) -> str:
    if need.kind.is_numeric():
        digits = max(need.total_digits, current.total_digits)
        scale = max(need.dec_digits, current.dec_digits)
        return f"NUMBER({digits},{scale})" if scale else f"NUMBER({digits})"
    return f"VARCHAR2({max(need.text_width, current.chars)})"


def _redeclare(declaration: str, old_picture: str, new_picture: str) -> str:
    if old_picture and old_picture in declaration:
        return declaration.replace(f"PIC {old_picture}", f"PIC {new_picture}")
    return f"{declaration.rstrip('.')} -> PIC {new_picture}."


def analyze(spec: ChangeSpec) -> AnalysisResult:
    """Convenience wrapper around :class:`ImpactAnalyzer`."""
    return ImpactAnalyzer(spec).run()

