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
from . import progress as progress_mod
from .copybook import DEFAULT_COPYBOOK_SUFFIXES, CopybookResolver
from .progress import Progress
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
    Slice,
    SourceRef,
)
from .pco import CallSite, Flow, Program, ProgramParser, Usage, discover_sources
from .spec import ChangeSpec
from .sqlparse import Direction, SqlStatement

# Edges where the destination accumulates rather than simply receiving a value.
# STRING and GROUP_PARENT also combine, but they are summed over every source
# in _propagate rather than grown one edge at a time.
_COMBINING = frozenset({EdgeKind.COMPUTE, EdgeKind.ARITHMETIC})
# Edges where growing the source forces the destination to grow in lockstep
# because they share physical storage.
_LAYOUT = frozenset({EdgeKind.GROUP_PARENT, EdgeKind.REDEFINES})
# Edges a field reached only through its copybook still follows: its storage
# changes, but no widened data arrives in it.
_STORAGE_ONLY = _LAYOUT | {EdgeKind.SHARED_DECLARATION}

# The propagation guard: at most this many edge relaxations per run, or this
# multiple of the edge count, whichever is larger. Both are named constants so a
# test can lower them rather than having to build a fixture big enough to trip
# the real cap.
_MIN_RELAXATION_BUDGET = 200_000
_RELAXATION_BUDGET_FACTOR = 20

# Mentions of a field that carry no width but break when it grows. Folded into
# that field's own finding rather than raised separately - see
# _attach_usage_notes.
_USAGE_SEVERITY = {
    "reference-modification": Severity.HIGH,
    "literal-comparison": Severity.MEDIUM,
    "inspect": Severity.MEDIUM,
    "display": Severity.LOW,
    "file-write": Severity.MEDIUM,
    "initialize": Severity.LOW,
    "set": Severity.LOW,
}
_USAGE_ADVICE = {
    "reference-modification": (
        "the offset/length is hard-coded and will not follow the new width."
    ),
    "literal-comparison": (
        "the literal is padded to the field width, so a wider field changes the "
        "comparison."
    ),
    "inspect": (
        "INSPECT scans the whole field including trailing spaces, so counts change."
    ),
    "display": "report or log column alignment shifts.",
    "file-write": "the output record grows; downstream readers need the new layout.",
    "initialize": "confirm the initialised value still fits.",
    "set": "confirm the SET target still matches the new width.",
}


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
class ProgramImpact:
    """What one program's maintainer actually has to do.

    The distinction that matters on a change like this is not how many findings
    a program collected, it is whether anyone has to open it. A program that
    merely COPYs the table's copybook has to be rebuilt against the new layout,
    but its own source is untouched - that is a build-list entry, not a work
    item, and mixing the two is what makes an impact report unusable.
    """

    program: str
    path: str
    # Copybooks this program includes that a widened field is declared in. The
    # reason it needs rebuilding at all.
    changed_copybooks: list[str]
    # Fields declared in this program's OWN source that must widen, plus any
    # mention (REFMOD, VALUE, literal comparison) that breaks. Non-empty means
    # somebody edits this program.
    own_work: list[str]

    @property
    def recompile_only(self) -> bool:
        return bool(self.changed_copybooks) and not self.own_work

    @property
    def verdict(self) -> str:
        if self.own_work:
            return "source change"
        if self.changed_copybooks:
            return "recompile only"
        return "not affected"

    def to_dict(self) -> dict[str, object]:
        return {
            "program": self.program,
            "path": self.path,
            "verdict": self.verdict,
            "recompile_only": self.recompile_only,
            "changed_copybooks": self.changed_copybooks,
            "own_work": self.own_work,
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
    # Per-program verdicts, affected programs only, worst first.
    program_impacts: list[ProgramImpact] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {level.value: 0 for level in Severity}
        for finding in self.findings:
            totals[finding.severity.value] += 1
        return totals


class ImpactAnalyzer:
    """Runs one analysis end to end."""

    def __init__(self, spec: ChangeSpec, progress: Optional[Progress] = None) -> None:
        self.spec = spec
        self.progress = progress_mod.resolve(progress)
        self.graph = ImpactGraph()
        self.programs: list[Program] = []
        self.warnings: list[str] = []
        # (program name, field name) -> node id, for resolving usages later.
        self._field_nodes: dict[tuple[str, str], str] = {}
        # Same index keyed by a dash/underscore-insensitive name, consulted only
        # after an exact lookup fails.
        self._field_nodes_loose: dict[tuple[str, str], str] = {}
        self._node_fields: dict[str, list[tuple[Program, Field]]] = {}
        self._seed_changes: dict[str, ColumnChange] = {}
        self._depth: dict[str, int] = {}
        # Columns whose current width was guessed from a bound host variable.
        self._inferred_columns: set[str] = set()
        # Nodes that widened data actually reaches. A copybook field widened only
        # because another program needs it wider is not in here - see
        # _propagate.
        self._flow_reached: set[str] = set()
        # Nodes some truncating edge (MOVE, fetch, STRING...) delivered more to
        # than they can hold. Severity comes from this, not from whichever edge
        # happened to grow the node last - a copybook or CALL link arriving
        # after a truncating MOVE must not downgrade it.
        self._truncated: set[str] = set()

    # -- entry point ------------------------------------------------------

    def run(self) -> AnalysisResult:
        self._parse_sources()
        self.progress.stage("building the data-flow graph")
        self._build_graph()
        self.progress.stage(
            f"graph built: {len(self.graph.nodes)} nodes, "
            f"{sum(len(edges) for edges in self.graph.out_edges.values())} edges"
        )
        self.progress.stage("propagating widths")
        required, paths, edges_used = self._propagate()
        self.progress.stage(f"collecting findings from {len(required)} affected node(s)")
        findings = self._collect_findings(required, paths, edges_used)
        ddl = self._ddl_plan(required)
        impacts = self._program_impacts(required)
        recompile_only = sum(1 for impact in impacts if impact.recompile_only)
        self.progress.done(
            f"analysis complete: {len(findings)} finding(s); {len(impacts)} program(s) "
            f"affected, {recompile_only} recompile-only"
        )
        return AnalysisResult(
            spec=self.spec,
            programs=self.programs,
            graph=self.graph,
            findings=findings,
            required=required,
            paths=paths,
            warnings=self.warnings,
            ddl=ddl,
            program_impacts=impacts,
        )

    # -- stage 1: parse ---------------------------------------------------

    def _parse_sources(self) -> None:
        # --copybook-ext adds to the defaults rather than replacing them; a shop
        # with one unusual extension still has extensionless members elsewhere,
        # and silently dropping those would cost real coverage.
        suffixes = None
        if self.spec.copybook_suffixes:
            suffixes = sorted(
                set(DEFAULT_COPYBOOK_SUFFIXES) | set(self.spec.copybook_suffixes)
            )
        resolver = CopybookResolver(
            self.spec.copybook_paths,
            suffixes=suffixes,
            progress=self.progress,
        )
        parser = ProgramParser(resolver)
        self.progress.stage(
            "discovering sources matching "
            f"{', '.join(self.spec.source_patterns)} under "
            + ", ".join(str(path) for path in self.spec.source_paths)
        )
        sources = discover_sources(
            self.spec.source_paths, self.spec.source_patterns, self.progress
        )
        if not sources:
            self.warnings.append(
                "no source files matched "
                f"{', '.join(self.spec.source_patterns)} under "
                f"{', '.join(str(path) for path in self.spec.source_paths) or '(no paths given)'}"
            )
        self.progress.stage(f"parsing {len(sources)} source file(s)")
        total = len(sources)
        for index, path in enumerate(sources, start=1):
            self.progress.step(index, total, str(path))
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
        self._add_shared_declarations()

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
            self._field_nodes_loose.setdefault((program.name, _loose(item.name)), node_id)
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
                    # SQL columns are spelled with underscores and COBOL fields
                    # with hyphens, and the two do get mixed up. Only fall back
                    # once the exact name has already missed, so this can soften
                    # a spelling slip without inventing a match.
                    variable_id = self._field_nodes_loose.get(
                        (program.name, _loose(binding.host_var))
                    )
                    if variable_id is not None:
                        self.warnings.append(
                            f"{statement.ref.location()}: host variable "
                            f":{binding.host_var} matched a declared field only after "
                            "normalising hyphens and underscores; confirm it is the "
                            "field you meant"
                        )
                if variable_id is None:
                    self.warnings.append(
                        f"{statement.ref.location()}: host variable :{binding.host_var} "
                        f"is not declared in {program.name} — its column binding is "
                        "not traced"
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
                self.graph.add_edge(
                    Edge(
                        source_id,
                        target_id,
                        flow.kind,
                        ref,
                        flow.note,
                        source_slice=flow.source_slices.get(source),
                        target_slice=flow.target_slice,
                        extra_chars=flow.literal_chars,
                    )
                )

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
                    # Directional. The overlaid item must be big enough to hold
                    # the grown redefining layout (redefines) - but the base
                    # growing does not force the redefining layout up to the
                    # base's size, it just changes the shared record length
                    # (redefined-by).
                    self.graph.add_edge(
                        Edge(child_id, other_id, EdgeKind.REDEFINES, ref, "redefines")
                    )
                    self.graph.add_edge(
                        Edge(other_id, child_id, EdgeKind.REDEFINES, ref, "redefined-by")
                    )

    def _add_call_edges(self) -> None:
        by_name = {program.name: program for program in self.programs}
        for program in self.programs:
            for call in program.calls:
                callees = self._call_targets(program, call, by_name)
                if not callees:
                    if call.args:
                        self.warnings.append(self._unresolved_call_warning(program, call))
                    continue
                for callee in callees:
                    label = call.target if not call.dynamic else f"{call.target} -> {callee.name}"
                    for position, argument in enumerate(call.args):
                        if position >= len(callee.linkage_using):
                            continue
                        caller_id = self._field_nodes.get((program.name, argument))
                        callee_id = self._field_nodes.get(
                            (callee.name, callee.linkage_using[position])
                        )
                        if not caller_id or not callee_id:
                            continue
                        note = f"argument {position + 1} of CALL {label}"
                        self.graph.add_edge(
                            Edge(caller_id, callee_id, EdgeKind.CALL_ARG, call.ref, note)
                        )
                        self.graph.add_edge(
                            Edge(callee_id, caller_id, EdgeKind.CALL_ARG, call.ref, note)
                        )

    def _add_shared_declarations(self) -> None:
        """Join every program's copy of one copybook declaration.

        Each program gets its own node for a copybook field, because the
        storage is not shared at run time. The source is: the moment one
        program needs CUST-NAME wider, CUSTOMER.cpy is edited, and every
        program that includes it gets the wider field and the longer record.
        Without this, one copybook was reported with a different record length
        in every program, each counting only the columns that program touched.

        The programs are joined through one declaration node rather than
        pairwise, so a copybook included by hundreds of programs adds edges in
        proportion to its includers, not to their square.
        """
        copies: dict[tuple[str, int, int, str, str, int], list[tuple[str, Field]]] = {}
        for program in self.programs:
            program_path = Path(program.path)
            for key in program.data.order:
                item = program.data.fields[key]
                if item.level == 88 or item.source is None:
                    continue
                if Path(item.source.path) == program_path:
                    continue  # declared inline, not in a copybook
                node_id = self._field_nodes.get((program.name, item.name))
                if node_id is None:
                    continue
                # Name is left out on purpose: COPY REPLACING gives the same
                # declaration a different name in each program.
                declaration = (
                    item.source.path,
                    item.source.line,
                    item.level,
                    item.picture,
                    item.usage,
                    item.occurs,
                )
                copies.setdefault(declaration, []).append((node_id, item))

        for declaration, members in copies.items():
            node_ids = list(dict.fromkeys(node_id for node_id, _ in members))
            if len(node_ids) < 2:
                continue
            path, line = declaration[0], declaration[1]
            item = members[0][1]
            hub_id = f"decl:{path}:{line}:{item.name}"
            self.graph.add_node(
                Node(
                    node_id=hub_id,
                    kind=NodeKind.DECLARATION,
                    name=f"{Path(path).name}:{line}",
                    capacity=_effective_capacity(item),
                    declared_in=item.source,
                    field_ref=item,
                )
            )
            ref = item.source or SourceRef(path=path, line=line)
            for node_id in node_ids:
                note = "same copybook declaration"
                self.graph.add_edge(Edge(node_id, hub_id, EdgeKind.SHARED_DECLARATION, ref, note))
                self.graph.add_edge(Edge(hub_id, node_id, EdgeKind.SHARED_DECLARATION, ref, note))

    def _call_targets(
        self, program: Program, call: CallSite, by_name: dict[str, Program]
    ) -> list[Program]:
        """Which scanned programs this CALL can reach.

        A literal target resolves to one program or none. An identifier target
        (``CALL WS-PGM-NAME``) names a variable, so it resolves through every
        literal ever assigned to that variable - by MOVE or by VALUE clause.
        That covers the way a great many shops reach their IO modules, and
        without it every such call is a dead end: the widened value stops at
        the caller and never reaches the called module's LINKAGE at all.

        Where a variable carries several program names, edges are built to ALL
        of them. Which one runs depends on data this tool cannot see, so the
        honest answer is every one it could be - over-reporting a call that
        might not happen is recoverable, missing a truncation is not.
        """
        direct = by_name.get(call.target)
        if direct is not None:
            return [direct]
        if not call.dynamic:
            return []
        resolved: list[Program] = []
        for literal in program.literal_moves.get(call.target, []):
            candidate = by_name.get(literal)
            if candidate is not None and candidate not in resolved:
                resolved.append(candidate)
        return resolved

    def _unresolved_call_warning(self, program: Program, call: CallSite) -> str:
        where = call.ref.location()
        if not call.dynamic:
            return (
                f"{where}: called program {call.target} is outside the scan scope; "
                "its parameter widths cannot be checked"
            )
        literals = program.literal_moves.get(call.target, [])
        if not literals:
            return (
                f"{where}: CALL {call.target} names a variable and no literal program "
                "name was ever moved into it, so the called program cannot be "
                "identified; its parameter widths cannot be checked"
            )
        return (
            f"{where}: CALL {call.target} resolves to {', '.join(literals)}, none of "
            "which were scanned; their parameter widths cannot be checked"
        )

    # -- stage 3: propagate -----------------------------------------------

    def _propagate(self) -> tuple[dict[str, Capacity], dict[str, list[str]], dict[str, Edge]]:
        required: dict[str, Capacity] = {}
        # How each node was first reached, as a single parent pointer rather
        # than a copy of the whole route. Building the full list at every
        # relaxation is quadratic in path depth and allocates a fresh list per
        # step - on a real shop corpus (400k nodes, 1M edges) that alone is
        # most of the run time, and it retains a list per affected node on top.
        # Routes are reconstructed once at the end, only for the nodes that
        # actually become findings.
        parents: dict[str, str] = {}
        depth: dict[str, int] = {}
        arriving: dict[str, Edge] = {}
        queue: deque[str] = deque()
        # Nodes already waiting to be walked. A node's requirement can grow
        # several times before it is popped, and without this every growth
        # queued another full rescan of its successors. That is harmless on a
        # toy graph and catastrophic on a real one, where copybook fields
        # become hub nodes shared by hundreds of programs: each rescan walks
        # the hub's entire fan-out again. Deduping loses nothing, because a
        # pop always reads the latest merged requirement.
        queued: set[str] = set()
        # A copybook field can widen for two reasons: widened data reaches it,
        # or another program's need for it edits the copybook. Only the first
        # carries the wider value on through this program's MOVEs and STRINGs.
        # A field reached only through its copybook follows storage edges
        # alone - its record grows, but nothing in this program fills it with
        # more data than before.
        flow_reached: set[str] = set()
        truncated: set[str] = set()
        # STRING targets and group items are sized by adding up what every
        # source brought, not by the largest single one. Kept as a running
        # total so a hub with thousands of members costs O(1) per update.
        self._growth: dict[tuple[str, ...], int] = {}
        self._contribution: dict[tuple[tuple[str, ...], str], int] = {}
        self._string_base: dict[tuple[str, ...], int] = {}
        # Every rule that ADDS to a width - a sum over STRING sources or group
        # members, an arithmetic result grown by its operand's delta - feeds on
        # itself around a cycle and never converges: MOVE CUST-REC TO a member
        # of CUST-REC grows the member to the record, which grows the record by
        # the member, and so on until the relaxation budget runs out with a
        # width thousands of digits long. Two guards keep every rule bounded.
        # Sums inside a strongly connected component take the widest member
        # instead of adding them up, and a delta never exceeds what the
        # requested changes themselves added.
        self._component = _strongly_connected_components(self.graph)
        self._delta_cap = _seed_growth(self.spec.changes)

        for change in self.spec.changes:
            node_id = _column_id(change.table, change.column)
            required[node_id] = change.new_capacity
            depth[node_id] = 0
            queue.append(node_id)
            queued.add(node_id)
            flow_reached.add(node_id)

        limit = self.spec.max_depth or 0
        total_edges = sum(len(edges) for edges in self.graph.out_edges.values())
        # Propagation converges because requirements only ever grow toward a
        # finite bound, but a malformed source should not be able to hang a
        # batch job, so the walk is also capped.
        #
        # Counted in edge relaxations, not queue pops: a pop costs as much as
        # the node has successors, so a pop budget bounds nothing at all when
        # one hub node carries tens of thousands of edges.
        budget = max(_MIN_RELAXATION_BUDGET, _RELAXATION_BUDGET_FACTOR * total_edges)
        relaxations = 0
        processed = 0
        exhausted = False

        while queue and not exhausted:
            node_id = queue.popleft()
            queued.discard(node_id)
            processed += 1
            # The report only prints at the very end, so without a heartbeat
            # this phase is indistinguishable from a hang. The total is an
            # estimate that moves as the frontier grows - honest, and enough
            # to show the walk is still making progress.
            if processed % 256 == 0:
                self.progress.step(
                    processed,
                    processed + len(queue),
                    f"{len(required)} node(s) affected",
                )
            node = self.graph.nodes.get(node_id)
            if node is None:
                continue
            current_depth = depth.get(node_id, 0)
            if limit and current_depth >= limit:
                continue
            source_required = required[node_id]
            flowing = node_id in flow_reached
            for edge in self.graph.successors(node_id):
                relaxations += 1
                if relaxations > budget:
                    message = (
                        f"propagation stopped early after {budget} edge relaxations; "
                        "the result may be incomplete"
                    )
                    self.warnings.append(message)
                    self.progress.warn(message)
                    exhausted = True
                    break
                if not flowing and edge.kind not in _STORAGE_ONLY:
                    continue
                target = self.graph.nodes.get(edge.target_id)
                if target is None:
                    continue
                # Always measure against the target's ORIGINAL capacity. Feeding
                # back an already-grown requirement would add the delta again on
                # every pass around a cycle (REDEFINES and CALL argument edges
                # are bidirectional) and never converge.
                candidate = self._candidate(edge, node, source_required, target)
                if candidate is None:
                    continue
                # Layout edges always carry: a longer record really is longer,
                # and moving it moves every byte.
                carries = edge.kind in _LAYOUT or (
                    flowing and edge.kind is not EdgeKind.SHARED_DECLARATION
                )
                newly_reached = carries and edge.target_id not in flow_reached
                if edge.kind.truncates and not target.capacity.covers(candidate):
                    truncated.add(edge.target_id)
                if newly_reached:
                    flow_reached.add(edge.target_id)
                previous = required.get(edge.target_id)
                if previous is not None and previous.covers(candidate):
                    if not newly_reached:
                        continue
                    # No wider, but now carrying data: walk its flows too.
                else:
                    merged = previous.grown_to_hold(candidate) if previous else candidate
                    required[edge.target_id] = merged
                    parents[edge.target_id] = node_id
                    depth[edge.target_id] = current_depth + 1
                    arriving[edge.target_id] = edge
                if edge.target_id not in queued:
                    queue.append(edge.target_id)
                    queued.add(edge.target_id)

        self.progress.step(processed, processed, f"{len(required)} node(s) affected")
        self._depth = depth
        self._flow_reached = flow_reached
        self._truncated = truncated
        return required, self._reconstruct_paths(required, parents), arriving

    def _candidate(
        self, edge: Edge, source: Node, source_required: Capacity, target: Node
    ) -> Optional[Capacity]:
        """What ``target`` must hold once ``source`` needs ``source_required``."""
        if edge.kind is EdgeKind.STRING:
            return self._string_requirement(edge, source, source_required, target)
        if edge.kind is EdgeKind.GROUP_PARENT:
            return self._group_requirement(edge, source, source_required, target)
        return _required_at_target(
            edge, source.capacity, source_required, target.capacity, self._delta_cap
        )

    def _in_cycle(self, edge: Edge) -> bool:
        component = self._component.get(edge.source_id)
        return component is not None and component == self._component.get(edge.target_id)

    def _accumulate(self, key: tuple[str, ...], member: str, amount: int) -> int:
        """Record what ``member`` adds to ``key`` and return the new total."""
        previous = self._contribution.get((key, member), 0)
        if amount != previous:
            self._contribution[(key, member)] = amount
            self._growth[key] = self._growth.get(key, 0) + amount - previous
        return self._growth.get(key, 0)

    def _string_requirement(
        self, edge: Edge, source: Node, source_required: Capacity, target: Node
    ) -> Optional[Capacity]:
        """A STRING target holds the sum of its sources, plus its literals.

        Growing it by each source's increase over-reported a target that had
        room to spare (60 characters STRINGed into 70 needs nothing) and, when
        two sources both widened, kept only the larger increase.
        """
        key = ("string", edge.target_id, edge.ref.path, str(edge.ref.line))
        before = _sliced_width(source.capacity, edge.source_slice)
        after = _sliced_width(source_required, edge.source_slice)
        if self._in_cycle(edge):
            # The target feeds its own source: hold the widest piece, but do
            # not add a total that grows every time round the loop.
            if after <= target.capacity.chars:
                return None
            return target.capacity.grown_to_hold(Capacity(kind=Kind.ALPHANUMERIC, chars=after))
        growth = self._accumulate(key, edge.source_id, max(after - before, 0))
        if growth <= 0:
            return None
        base = self._string_base.get(key)
        if base is None:
            base = self._statement_width(edge)
            self._string_base[key] = base
        total = base + growth
        current = target.capacity
        if total <= current.chars:
            return None
        return Capacity(
            kind=current.kind if current.kind is not Kind.UNKNOWN else Kind.GROUP,
            chars=total,
            int_digits=current.int_digits,
            dec_digits=current.dec_digits,
            signed=current.signed,
        )

    def _statement_width(self, edge: Edge) -> int:
        """Width of everything one STRING statement concatenates, before the change."""
        total = edge.extra_chars
        for other in self.graph.in_edges.get(edge.target_id, []):
            if (
                other.kind is EdgeKind.STRING
                and other.ref.path == edge.ref.path
                and other.ref.line == edge.ref.line
            ):
                member = self.graph.nodes.get(other.source_id)
                if member is not None:
                    total += _sliced_width(member.capacity, other.source_slice)
        return total

    def _group_requirement(
        self, edge: Edge, source: Node, source_required: Capacity, target: Node
    ) -> Optional[Capacity]:
        """A group grows by the bytes every member adds, times its OCCURS.

        Growing it by one member's increase at a time kept only the largest,
        ignored OCCURS, and measured packed and binary fields in display
        characters rather than bytes.
        """
        item = source.field_ref
        if item is not None and item.redefines:
            return None  # shares storage already counted; see copybook.finalize
        occurs = max(item.occurs, 1) if item is not None else 1
        added = _storage_growth(source, source_required) * occurs
        if self._in_cycle(edge):
            # The group flows back into this member (MOVE CUST-REC TO a field
            # of CUST-REC). No size satisfies that: the member must hold the
            # record, and the record contains the member. Any rule that adds
            # to the group or multiplies by OCCURS here grows it every time
            # round the loop, so inside a cycle the group only has to hold the
            # widest member, and the loop settles.
            needed = source_required.text_width
            if needed <= target.capacity.chars:
                return None
            return Capacity(kind=Kind.GROUP, chars=needed)
        growth = self._accumulate(("group", edge.target_id), edge.source_id, added)
        if growth <= 0:
            return None
        return Capacity(kind=Kind.GROUP, chars=target.capacity.chars + growth)

    def _reconstruct_paths(
        self, required: dict[str, Capacity], parents: dict[str, str]
    ) -> dict[str, list[str]]:
        """Walk parent pointers back to the seed, for reported nodes only.

        Every node the walk touched lands in ``required``, but only the ones
        whose own capacity cannot hold what arrived become findings - which is
        the only place a route is ever shown. Anything skipped here degrades to
        a single-element path at the call site, which is never read.
        """
        seeds = {_column_id(change.table, change.column) for change in self.spec.changes}
        paths: dict[str, list[str]] = {}
        for node_id, need in required.items():
            node = self.graph.nodes.get(node_id)
            if node is None:
                continue
            if node_id not in seeds and node.capacity.covers(need):
                continue
            route = [node_id]
            # A cycle in the parent pointers is not expected, but REDEFINES and
            # CALL argument edges are bidirectional and this must not be the
            # thing that hangs after the expensive part already finished.
            seen = {node_id}
            current = node_id
            while current in parents:
                current = parents[current]
                if current in seen:
                    break
                seen.add(current)
                route.append(current)
            route.reverse()
            paths[node_id] = route
        return paths

    # -- stage 4: findings -------------------------------------------------

    def _collect_findings(
        self,
        required: dict[str, Capacity],
        paths: dict[str, list[str]],
        arriving: dict[str, Edge],
    ) -> list[Finding]:
        """At most one finding per node - which is one per field per program.

        A field mentioned twenty times in one program is one thing to fix, not
        twenty findings. Every extra mention that matters (a REFMOD with a
        hard-coded length, a VALUE clause, a literal comparison) is folded into
        that single finding as an extra note and an extra source reference, and
        raises its severity if it is worse than the move itself. Nothing is
        lost; it just stops arriving as separate rows the reader has to
        reassemble by hand.
        """
        findings: list[Finding] = []
        seeds = {_column_id(change.table, change.column) for change in self.spec.changes}
        impacted: set[str] = set()

        for node_id, need in required.items():
            node = self.graph.nodes.get(node_id)
            if node is None or node.kind is NodeKind.DECLARATION:
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

        self._attach_usage_notes(findings, impacted)
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
        declared = [entry[1].source for entry in entries if entry[1].source]
        # A REDEFINES or copybook edge points at the declaration itself, which
        # is already listed; one location is one line of the report. Where they
        # collide the declaration wins, because it names this program.
        by_location = {(ref.path, ref.line): ref for ref in ([edge.ref] if edge else [])}
        for ref in declared:
            by_location[(ref.path, ref.line)] = ref
        refs = list(by_location.values())

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
                notes=self._redefines_notes(node.node_id, new_bytes),
            )

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
        current = f"{level:02d} {node.name} PIC {current_pic or '(none)'} {usage}".strip()

        if node.node_id not in self._flow_reached:
            # Widened because its copybook is edited for another program; no
            # wider value reaches it here. The edit is the other program's, so
            # this is a rebuild, not a truncation.
            book = Path(item.source.path).name if item is not None and item.source else "its copybook"
            return Finding(
                severity=Severity.LOW,
                category="copybook-field",
                node_id=node.node_id,
                title=f"{node.name} widens with {book}",
                detail=(
                    f"{book} is edited because another program needs this field to "
                    f"hold {need.describe()}. No widened value reaches it in this "
                    "program, so rebuilding against the new copybook is enough, "
                    "unless a statement here assumes the old width."
                ),
                current=current,
                required=need.describe(),
                # The same edit the other program's finding asks for, worded the
                # same, so a per-file view folds the two into one line.
                remediation=f"Change to: {new_declaration}",
                distance=distance,
                path=path,
                refs=refs,
            )

        severity = Severity.CRITICAL if node.node_id in self._truncated else Severity.HIGH
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
            current=current,
            required=need.describe(),
            remediation=f"Change to: {new_declaration}",
            distance=distance,
            path=path,
            refs=refs,
            notes=self._redefines_notes(node.node_id, max(need.chars, need.text_width)),
        )

    def _redefines_notes(self, node_id: str, new_bytes: int) -> list[str]:
        """Name the storage this node shares through a REDEFINES, sized or not.

        When a record grows, the layouts that overlay the same bytes are the
        thing a human most needs pointed at - and a big flat buffer that still
        happens to fit is exactly the one that gets missed, because it never
        becomes a finding of its own. Name it here either way.
        """
        notes: list[str] = []
        seen: set[str] = set()
        edges = list(self.graph.out_edges.get(node_id, []))
        edges += list(self.graph.in_edges.get(node_id, []))
        for edge in edges:
            if edge.kind is not EdgeKind.REDEFINES:
                continue
            other_id = edge.target_id if edge.source_id == node_id else edge.source_id
            if other_id == node_id or other_id in seen:
                continue
            seen.add(other_id)
            other = self.graph.nodes.get(other_id)
            if other is None:
                continue
            entries = self._node_fields.get(other_id, [])
            other_item = entries[0][1] if entries else other.field_ref
            size = 0
            if other_item is not None:
                size = other_item.storage_bytes or other_item.capacity.chars
            elif other.capacity.chars:
                size = other.capacity.chars
            decl = (other_item.declaration() if other_item is not None else other.name).rstrip(".")
            if size and size >= new_bytes:
                verdict = f"{size} bytes, still fits the new {new_bytes}"
            elif size:
                verdict = f"{size} bytes, smaller than the new {new_bytes} - check it"
            else:
                verdict = f"size unknown - check it covers {new_bytes} bytes"
            notes.append(f"overlays the same storage (REDEFINES): {decl} ({verdict})")
        return notes

    def _attach_usage_notes(self, findings: list[Finding], impacted: set[str]) -> None:
        """Fold every other mention of an impacted field into its own finding.

        These are the places a name appears without a width-carrying flow: a
        REFMOD with a hard-coded length, a literal comparison, an INSPECT, a
        VALUE clause. Each used to be its own finding, so one field mentioned
        twenty times produced twenty rows for what is a single edit. They are
        notes on the field's finding now - the severity rises to the worst of
        them, and every location is kept as a reference.
        """
        by_node: dict[str, Finding] = {}
        for finding in findings:
            # A node has exactly one finding by construction, but seeds are
            # skipped: a column has no COBOL usages to attach.
            if finding.node_id in impacted:
                by_node.setdefault(finding.node_id, finding)

        if not by_node:
            return

        for program in self.programs:
            for usage in program.usages:
                node_id = self._field_nodes.get((program.name, usage.name))
                finding = by_node.get(node_id) if node_id else None
                if finding is None:
                    continue
                severity = _USAGE_SEVERITY.get(usage.category, Severity.LOW)
                if severity.rank < finding.severity.rank:
                    finding.severity = severity
                advice = _USAGE_ADVICE.get(usage.category, "Review this usage.")
                # One note per category, however many times it occurs - the
                # reader needs to know REFMOD is in play, not that it is in
                # play eleven times. Every occurrence still gets a reference.
                label = f"{usage.category}: {advice}"
                if label not in finding.notes:
                    finding.notes.append(label)
                if usage.ref not in finding.refs:
                    finding.refs.append(usage.ref)

            for key in program.data.order:
                item = program.data.fields[key]
                node_id = self._field_nodes.get((program.name, item.name))
                finding = by_node.get(node_id) if node_id else None
                if finding is None or not item.value:
                    continue
                if Severity.MEDIUM.rank < finding.severity.rank:
                    finding.severity = Severity.MEDIUM
                label = (
                    f"value-clause: declared with VALUE {item.value}; re-check the "
                    "initial value against the new width."
                )
                if label not in finding.notes:
                    finding.notes.append(label)

    def _program_impacts(self, required: dict[str, Capacity]) -> list[ProgramImpact]:
        """Classify every affected program as recompile-only or a source change.

        A program has to be rebuilt when a copybook it includes changes - that
        is what a copybook is. Whether anyone has to EDIT it is a different
        question, and the answer is no unless a field declared in its own source
        has to widen, or one of its own statements assumes the old width.

        Fields are attributed by where they are DECLARED (Field.source.path),
        not by where they are used: a widened field belonging to the table's
        copybook is the copybook's change, however many programs move it about.
        """
        # Which copybook FILES change, across the whole scan. A copybook is a
        # shared declaration: the moment one program's flow widens a field
        # declared in it, that file changes shape for every program that
        # includes it - including programs where nothing flows through the
        # field at all. Those still have to be rebuilt, and they are the ones
        # most likely to be forgotten, because nothing in them looks different.
        changed_files: set[Path] = set()
        for program in self.programs:
            program_path = Path(program.path)
            for key in program.data.order:
                item = program.data.fields[key]
                if item.level == 88 or not item.source:
                    continue
                node_id = self._field_nodes.get((program.name, item.name))
                node = self.graph.nodes.get(node_id) if node_id else None
                need = required.get(node_id) if node_id else None
                if node is None or need is None or node.capacity.covers(need):
                    continue
                origin = Path(item.source.path)
                if origin != program_path:
                    changed_files.add(origin)

        impacts: list[ProgramImpact] = []
        for program in self.programs:
            program_path = Path(program.path)
            own_work: dict[str, None] = {}

            for key in program.data.order:
                item = program.data.fields[key]
                if item.level == 88:
                    continue
                node_id = self._field_nodes.get((program.name, item.name))
                if node_id is None:
                    continue
                node = self.graph.nodes.get(node_id)
                need = required.get(node_id)
                if node is None or need is None or node.capacity.covers(need):
                    continue

                origin = item.source.path if item.source else ""
                declared_here = not origin or Path(origin) == program_path
                if declared_here:
                    own_work[f"{item.name} must widen ({node.capacity.describe()} -> {need.describe()})"] = None

            # A statement in this program that assumes the old width is an edit
            # here regardless of where the field itself is declared.
            for usage in program.usages:
                node_id = self._field_nodes.get((program.name, usage.name))
                if node_id is None:
                    continue
                node = self.graph.nodes.get(node_id)
                need = required.get(node_id)
                if node is None or need is None or node.capacity.covers(need):
                    continue
                own_work[f"{usage.name} used via {usage.category}"] = None

            included_and_changed = sorted(
                {book for book in program.copybooks if Path(book) in changed_files}
            )

            if included_and_changed or own_work:
                impacts.append(
                    ProgramImpact(
                        program=program.name,
                        path=program.path,
                        changed_copybooks=included_and_changed,
                        own_work=sorted(own_work),
                    )
                )

        # Programs needing real work first; recompile-only is a build list.
        impacts.sort(key=lambda impact: (impact.recompile_only, impact.program))
        return impacts

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
        """One node per (program, field name).

        A field declared in a copybook used to get a single global node id
        shared by every program that included it. That is wrong twice over.
        The storage is not shared - each program compiles its own copy into its
        own WORKING-STORAGE - so it invented data flows between programs that
        never call each other: program A moving an amount into a copybook work
        field, and program B moving that same-named field into a date, became
        one continuous path from the amount to the date. It also collapsed
        hundreds of programs onto one node, which is what made propagation
        crawl.

        --global-variable-scope restores the old behaviour for a shop that
        really does want every mention of a name treated as one thing.
        """
        if self.spec.global_variable_scope:
            return f"var:{item.name}"
        return f"var:{program.name}::{item.name}"


def _strongly_connected_components(graph: ImpactGraph) -> dict[str, int]:
    """Component id per node (iterative Tarjan), so cycles can be recognised.

    Iterative because a real corpus has paths far deeper than Python's
    recursion limit.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    component: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    next_component = 0
    for root in graph.nodes:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node_id, position = work.pop()
            if position == 0:
                index[node_id] = low[node_id] = counter
                counter += 1
                stack.append(node_id)
                on_stack.add(node_id)
            edges = graph.out_edges.get(node_id, [])
            descended = False
            while position < len(edges):
                target = edges[position].target_id
                position += 1
                if target not in index:
                    work.append((node_id, position))
                    work.append((target, 0))
                    descended = True
                    break
                if target in on_stack:
                    low[node_id] = min(low[node_id], index[target])
            if descended:
                continue
            if low[node_id] == index[node_id]:
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component[member] = next_component
                    if member == node_id:
                        break
                next_component += 1
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node_id])
    return component


def _column_id(table: str, column: str) -> str:
    return f"col:{table.upper()}.{column.upper()}"


def _loose(name: str) -> str:
    """Fold the hyphen/underscore distinction, which the two languages disagree on."""
    return name.upper().replace("_", "-")


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
    cap: Optional["_Growth"] = None,
) -> Optional[Capacity]:
    """Capacity the destination of ``edge`` needs, given a widened source."""
    if edge.kind is EdgeKind.REDEFINES:
        # redefines: the overlaid item only has to be *big enough* for the grown
        # layout - a flat buffer that already reserves more than the record uses
        # needs nothing. redefined-by: the base grew, so the record length grew
        # by that much; do not inflate this layout to the base's size.
        if edge.note == "redefined-by":
            return _grow_by_delta(
                target_current, source_current, source_required, cap, overlay=True
            )
        return target_current.grown_to_hold(source_required)
    if edge.kind in _COMBINING or edge.kind in _LAYOUT:
        return _grow_by_delta(target_current, source_current, source_required, cap)
    carried = source_required
    if edge.source_slice is not None:
        # MOVE CUST-NAME (1:20) moves 20 characters however wide CUST-NAME
        # grows. Passing the whole field on reported the target, and every
        # column it was later bound to, as needing to grow.
        if edge.source_slice.length is not None:
            return None
        width = edge.source_slice.width(source_required.text_width)
        if width is not None:
            carried = Capacity(kind=Kind.ALPHANUMERIC, chars=width)
    if edge.target_slice is not None:
        # A literal length on the target caps what lands there.
        if edge.target_slice.length is not None:
            return None
        if edge.target_slice.offset is not None:
            carried = Capacity(
                kind=Kind.ALPHANUMERIC,
                chars=edge.target_slice.offset - 1 + carried.text_width,
            )
    # UNSTRING lands here too: one delimited piece of the source can be as long
    # as the whole widened source in the worst case.
    return target_current.grown_to_hold(carried)


def _sliced_width(capacity: Capacity, piece: Optional[Slice]) -> int:
    """Characters a (possibly reference-modified) item contributes as text."""
    width = capacity.text_width
    if piece is None:
        return width
    sliced = piece.width(width)
    return width if sliced is None else sliced


def _storage_growth(node: Node, need: Capacity) -> int:
    """Bytes ``node`` gains when it grows to hold ``need``.

    Bytes, not characters: S9(9)V99 COMP-3 to S9(11)V99 COMP-3 is two more
    digits but one more byte, and an edited picture adds its punctuation.
    """
    current = node.capacity
    if current.covers(need):
        return 0
    item = node.field_ref
    if item is None or not item.picture:
        if current.kind is Kind.GROUP or (item is not None and item.is_group):
            return max(need.chars - current.chars, 0)
        return max(need.text_width - current.text_width, 0)
    rendered = picture.render_picture(need, item.usage, template=item.picture)
    info = picture.parse_picture(rendered, item.usage, item.sign_separate)
    grown = info.storage_bytes + (2 if item.varying else 0)
    return max(grown - item.storage_bytes, 0)


@dataclass(frozen=True)
class _Growth:
    """The most any requested change widens a value by."""

    int_digits: int
    dec_digits: int
    chars: int


def _seed_growth(changes: Iterable[ColumnChange]) -> _Growth:
    int_digits = dec_digits = chars = 0
    for change in changes:
        old, new = change.old_capacity, change.new_capacity
        int_digits = max(int_digits, new.int_digits - old.int_digits)
        dec_digits = max(dec_digits, new.dec_digits - old.dec_digits)
        chars = max(chars, new.text_width - old.text_width)
    return _Growth(int_digits, dec_digits, chars)


def _grow_by_delta(
    target_current: Capacity,
    source_current: Capacity,
    source_required: Capacity,
    cap: Optional[_Growth] = None,
    overlay: bool = False,
) -> Optional[Capacity]:
    """Grow the destination by the amount the source grew.

    ``cap`` bounds the delta by what the requested changes added. A source's
    requirement can be far above its original for reasons that have nothing to
    do with the change - a MOVE from a much wider field upstream - and passing
    that on as a delta is what made cycles diverge.

    ``overlay`` is a REDEFINES: the base only pushes the record past the
    redefining item once it outgrows the larger of the two.
    """
    if target_current.kind.is_numeric() and source_required.kind.is_numeric():
        int_delta = max(source_required.int_digits - source_current.int_digits, 0)
        dec_delta = max(source_required.dec_digits - source_current.dec_digits, 0)
        if cap is not None:
            int_delta = min(int_delta, cap.int_digits)
            dec_delta = min(dec_delta, cap.dec_digits)
        if not int_delta and not dec_delta:
            return None
        return Capacity(
            kind=target_current.kind,
            chars=target_current.chars,
            int_digits=target_current.int_digits + int_delta,
            dec_digits=target_current.dec_digits + dec_delta,
            signed=target_current.signed,
        )
    baseline = source_current.text_width
    if overlay:
        baseline = max(baseline, target_current.text_width)
    delta = max(source_required.text_width - baseline, 0)
    if cap is not None and not overlay:
        # Group and REDEFINES sizes are bytes and can legitimately add several
        # changes together, so only value deltas are capped.
        delta = min(delta, max(cap.chars, cap.int_digits + cap.dec_digits))
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


def analyze(spec: ChangeSpec, progress: Optional[Progress] = None) -> AnalysisResult:
    """Convenience wrapper around :class:`ImpactAnalyzer`."""
    return ImpactAnalyzer(spec, progress).run()

