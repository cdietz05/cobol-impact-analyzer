"""How much work propagation does, not just what it concludes.

The correctness of the walk is covered in test_analyzer.py against the example
tree.  These tests pin the cost, because the failure mode on a real shop corpus
(400k nodes, 1M edges) was never a wrong answer - it was a run that produced no
output for an hour.  Both regressions here look like nothing on a small graph
and dominate a large one.
"""

import unittest
import unittest.mock

from cobol_impact_analyzer import analyzer as analyzer_mod
from cobol_impact_analyzer.analyzer import ImpactAnalyzer, ImpactGraph
from cobol_impact_analyzer.models import (
    Capacity,
    Edge,
    EdgeKind,
    Kind,
    Node,
    NodeKind,
    SourceRef,
)
from cobol_impact_analyzer.spec import ChangeSpec, build_change

REF = SourceRef(path="synthetic", line=1)
SEED_ID = "col:CUSTOMER.CUST_NAME"


def _text(width: int) -> Capacity:
    return Capacity(kind=Kind.ALPHANUMERIC, chars=width)


class _CountingGraph(ImpactGraph):
    """An ImpactGraph that records how much of itself the walk reads.

    ``edges_scanned`` is the number that matters: a node is re-walked at its
    full fan-out, so one extra visit to a hub costs as much as the hub is wide.
    """

    def __init__(self):
        super().__init__()
        self.successor_calls = 0
        self.edges_scanned = 0

    def successors(self, node_id):
        edges = super().successors(node_id)
        self.successor_calls += 1
        self.edges_scanned += len(edges)
        return edges


def _hub_analyzer(feeders: int = 50, fanout: int = 50) -> ImpactAnalyzer:
    """A seed feeding many nodes that all converge on one high-fan-out hub.

    This is the shape a copybook field takes in a real codebase: _variable_id
    gives any field declared in a copybook a single global node id, so one node
    ends up shared by every program that includes it and carries the fan-out of
    all of them at once.

    The feeders are deliberately given DECREASING original widths, so each one
    in turn hands the hub a strictly larger requirement than the last.  That is
    what makes the hub's requirement grow repeatedly rather than once - and a
    walk that re-queues a node on every growth then rescans the hub's whole
    fan-out each time.
    """
    spec = ChangeSpec(
        changes=[build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)")],
        source_paths=[],
        copybook_paths=[],
        source_patterns=["*.pco"],
    )
    analyzer = ImpactAnalyzer(spec)
    graph = _CountingGraph()
    analyzer.graph = graph

    graph.add_node(Node(SEED_ID, NodeKind.COLUMN, "CUSTOMER.CUST_NAME", _text(30)))
    graph.add_node(Node("var:HUB", NodeKind.VARIABLE, "HUB", _text(30)))

    for index in range(feeders):
        feeder_id = f"var:FEEDER-{index}"
        graph.add_node(Node(feeder_id, NodeKind.VARIABLE, f"FEEDER-{index}", _text(59 - index)))
        graph.add_edge(Edge(SEED_ID, feeder_id, EdgeKind.SQL_FETCH, REF))
        # STRING is a combining edge: the hub grows by the amount the feeder
        # grew, which differs per feeder because their originals differ.
        graph.add_edge(Edge(feeder_id, "var:HUB", EdgeKind.STRING, REF))

    for index in range(fanout):
        leaf_id = f"var:LEAF-{index}"
        graph.add_node(Node(leaf_id, NodeKind.VARIABLE, f"LEAF-{index}", _text(30)))
        graph.add_edge(Edge("var:HUB", leaf_id, EdgeKind.MOVE, REF))

    return analyzer


class QueueDedupTests(unittest.TestCase):
    def test_a_hub_is_not_rescanned_once_per_growth(self):
        analyzer = _hub_analyzer(feeders=200, fanout=50)
        analyzer._propagate()

        # Each of the 200 feeders hands the hub a larger requirement than the
        # last.  Re-queueing on every growth means 200 visits to the hub, each
        # re-reading all 50 of its outgoing edges - 10,000 edge reads for a
        # graph that has 450 edges in total.  One visit is all a converging
        # walk needs, because a pop always reads the latest requirement.
        self.assertLessEqual(
            analyzer.graph.edges_scanned,
            2 * sum(len(edges) for edges in analyzer.graph.out_edges.values()),
            "propagation is re-walking nodes it has already queued",
        )
        self.assertLessEqual(
            analyzer.graph.successor_calls,
            len(analyzer.graph.nodes) + 5,
            "propagation is popping nodes far more than once each",
        )

    def test_the_hub_still_ends_up_wide_enough_for_every_feeder(self):
        analyzer = _hub_analyzer(feeders=50, fanout=50)
        required, _, _ = analyzer._propagate()

        # The narrowest feeder (10 chars) grew to 60, a delta of 50, and the
        # hub has to absorb the largest delta any feeder brought it.
        self.assertEqual(required["var:HUB"].chars, 80)
        for index in range(50):
            self.assertIn(f"var:LEAF-{index}", required)
            self.assertEqual(required[f"var:LEAF-{index}"].chars, 80)


class PathReconstructionTests(unittest.TestCase):
    def test_a_reported_route_starts_at_the_seed_and_follows_real_edges(self):
        analyzer = _hub_analyzer(feeders=3, fanout=3)
        _, paths, _ = analyzer._propagate()

        route = paths["var:LEAF-0"]
        self.assertEqual(route[0], SEED_ID)
        self.assertEqual(route[-1], "var:LEAF-0")
        for source, target in zip(route, route[1:]):
            self.assertTrue(
                any(edge.target_id == target for edge in analyzer.graph.successors(source)),
                f"{source} -> {target} is not an edge in the graph",
            )

    def test_routes_are_built_for_reported_nodes_not_every_node_touched(self):
        analyzer = _hub_analyzer(feeders=3, fanout=3)
        required, paths, _ = analyzer._propagate()

        # A node the walk reached but that is already wide enough never shows a
        # route, because it never becomes a finding.
        analyzer.graph.add_node(Node("var:WIDE", NodeKind.VARIABLE, "WIDE", _text(400)))
        analyzer.graph.add_edge(Edge("var:HUB", "var:WIDE", EdgeKind.MOVE, REF))
        required, paths, _ = analyzer._propagate()

        self.assertIn("var:WIDE", required)
        self.assertNotIn("var:WIDE", paths)


class BudgetTests(unittest.TestCase):
    def test_a_graph_this_size_finishes_well_inside_the_budget(self):
        analyzer = _hub_analyzer(feeders=50, fanout=50)
        analyzer._propagate()

        self.assertFalse([w for w in analyzer.warnings if "stopped early" in w])

    def test_the_guard_fires_and_says_so_when_the_budget_runs_out(self):
        analyzer = _hub_analyzer(feeders=50, fanout=50)
        with unittest.mock.patch.object(analyzer_mod, "_MIN_RELAXATION_BUDGET", 3):
            # 20 * edge count is the real cap; the floor only wins on graphs
            # smaller than that, which is exactly what this fixture becomes
            # once the floor is lowered under it.
            analyzer.graph.out_edges = {}
            analyzer._propagate()

        # No edges at all means nothing to relax, so the guard must NOT fire -
        # an exhausted budget and an empty graph have to stay distinguishable.
        self.assertFalse([w for w in analyzer.warnings if "stopped early" in w])

    def test_an_exhausted_budget_warns_through_progress_as_well_as_the_report(self):
        analyzer = _hub_analyzer(feeders=50, fanout=50)
        warned: list[str] = []
        analyzer.progress.warn = warned.append  # type: ignore[method-assign]

        with unittest.mock.patch.object(analyzer_mod, "_MIN_RELAXATION_BUDGET", 3):
            with unittest.mock.patch.object(analyzer_mod, "_RELAXATION_BUDGET_FACTOR", 0):
                analyzer._propagate()

        self.assertTrue([w for w in analyzer.warnings if "stopped early" in w])
        self.assertTrue(
            [w for w in warned if "stopped early" in w],
            "the phase that ran out of budget must say so while it is running, "
            "not only in a report printed minutes later",
        )


if __name__ == "__main__":
    unittest.main()
