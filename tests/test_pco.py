import unittest
from pathlib import Path

from cobol_impact_analyzer.copybook import CopybookResolver
from cobol_impact_analyzer.models import EdgeKind, SourceRef
from cobol_impact_analyzer.pco import ProgramParser, discover_sources
from cobol_impact_analyzer.sqlparse import Direction, SqlAnalyzer

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _program(name: str):
    resolver = CopybookResolver([EXAMPLES / "copybooks"])
    return ProgramParser(resolver).parse(EXAMPLES / "src" / name)


class SqlParsingTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = SqlAnalyzer()
        self.ref = SourceRef(path="x.pco", line=1)

    def test_select_into_maps_columns_positionally(self):
        statement = self.analyzer.parse(
            "SELECT CUST_ID, CUST_NAME FROM CUSTOMER WHERE CUST_ID = :WS-ID", self.ref
        )
        # No INTO clause here, so only the predicate binds.
        self.assertEqual(statement.tables, ["CUSTOMER"])
        predicate = [b for b in statement.bindings if b.direction is Direction.PREDICATE]
        self.assertEqual(predicate[0].column, "CUST_ID")

        statement = self.analyzer.parse(
            "SELECT CUST_ID, CUST_NAME INTO :A, :B FROM CUSTOMER", self.ref
        )
        pairs = {(b.column, b.host_var) for b in statement.bindings}
        self.assertIn(("CUST_ID", "A"), pairs)
        self.assertIn(("CUST_NAME", "B"), pairs)

    def test_insert_maps_column_list_to_values(self):
        statement = self.analyzer.parse(
            "INSERT INTO ORDER_AUDIT (A_ID, A_NAME) VALUES (:WS-ID, :WS-NAME)", self.ref
        )
        self.assertEqual(statement.tables, ["ORDER_AUDIT"])
        pairs = {(b.column, b.host_var, b.direction) for b in statement.bindings}
        self.assertIn(("A_NAME", "WS-NAME", Direction.IN), pairs)

    def test_update_set_binds_inward(self):
        statement = self.analyzer.parse(
            "UPDATE ORDER_HISTORY SET CUST_NAME_SNAP = :CUST-NAME WHERE CUST_ID = :CUST-ID",
            self.ref,
        )
        inward = [b for b in statement.bindings if b.direction is Direction.IN]
        self.assertEqual(inward[0].column, "CUST_NAME_SNAP")
        self.assertEqual(inward[0].table, "ORDER_HISTORY")

    def test_cursor_declaration_feeds_the_fetch(self):
        self.analyzer.parse(
            "DECLARE CUST_CUR CURSOR FOR SELECT C.CUST_ID, C.CUST_NAME "
            "FROM CUSTOMER C WHERE C.CUST_ID > :WS-LOW",
            self.ref,
        )
        fetch = self.analyzer.parse("FETCH CUST_CUR INTO :A, :B", self.ref)
        pairs = {(b.table, b.column, b.host_var) for b in fetch.bindings}
        self.assertIn(("CUSTOMER", "CUST_NAME", "B"), pairs)

    def test_cursor_names_are_scoped_to_one_program(self):
        # Two analyzers sharing a registry stand in for two programs.
        shared: dict = {}
        first = SqlAnalyzer(shared)
        first.parse(
            "DECLARE C1 CURSOR FOR SELECT CUST_NAME FROM CUSTOMER", self.ref
        )
        second = SqlAnalyzer(shared)
        second.parse("DECLARE C1 CURSOR FOR SELECT VEND_NAME FROM VENDOR", self.ref)
        fetch = second.parse("FETCH C1 INTO :WS-A", self.ref)
        self.assertEqual(fetch.bindings[0].table, "VENDOR")
        self.assertEqual(fetch.bindings[0].column, "VEND_NAME")
        self.assertFalse(fetch.unresolved)

    def test_borrowing_a_cursor_from_another_program_is_flagged(self):
        shared: dict = {}
        declarer = SqlAnalyzer(shared)
        declarer.parse("DECLARE C1 CURSOR FOR SELECT CUST_NAME FROM CUSTOMER", self.ref)

        other = SqlAnalyzer(shared)
        fetch = other.parse("FETCH C1 INTO :WS-Z", self.ref)
        # The mapping is still offered, because a copybook-declared cursor is a
        # real pattern -- but never silently.
        self.assertEqual(fetch.bindings[0].column, "CUST_NAME")
        self.assertTrue(any("borrowed" in note for note in fetch.unresolved))

    def test_an_entirely_unknown_cursor_is_reported(self):
        analyzer = SqlAnalyzer()
        fetch = analyzer.parse("FETCH NOWHERE INTO :WS-A", self.ref)
        self.assertTrue(any("not declared in any scanned source" in n for n in fetch.unresolved))
        # The host variable is still recorded, just without a column.
        self.assertEqual(fetch.bindings[0].host_var, "WS-A")
        self.assertEqual(fetch.bindings[0].column, "")

    def test_open_using_a_borrowed_cursor_is_flagged_too(self):
        shared: dict = {}
        SqlAnalyzer(shared).parse(
            "DECLARE C1 CURSOR FOR SELECT CUST_NAME FROM CUSTOMER WHERE CUST_ID = :WS-ID",
            self.ref,
        )
        opened = SqlAnalyzer(shared).parse("OPEN C1", self.ref)
        self.assertTrue(any("borrowed" in note for note in opened.unresolved))

    def test_cursor_with_for_update_still_parses_its_select_list(self):
        analyzer = SqlAnalyzer()
        analyzer.parse(
            "DECLARE C1 CURSOR FOR SELECT CUST_NAME, CUST_CITY FROM CUSTOMER "
            "WHERE CUST_ID > :WS-LOW ORDER BY CUST_NAME FOR UPDATE",
            self.ref,
        )
        fetch = analyzer.parse("FETCH C1 INTO :A, :B", self.ref)
        pairs = {(b.column, b.host_var) for b in fetch.bindings}
        self.assertIn(("CUST_NAME", "A"), pairs)
        self.assertIn(("CUST_CITY", "B"), pairs)

    def test_indicator_variables_are_kept_separate(self):
        statement = self.analyzer.parse(
            "SELECT CUST_NAME INTO :CUST-NAME:IND-NAME FROM CUSTOMER", self.ref
        )
        self.assertEqual(statement.bindings[0].host_var, "CUST-NAME")
        self.assertEqual(statement.bindings[0].indicator, "IND-NAME")

    def test_select_star_is_reported_as_unresolved(self):
        statement = self.analyzer.parse("SELECT * INTO :A FROM CUSTOMER", self.ref)
        self.assertTrue(statement.unresolved)


class ProgramParsingTests(unittest.TestCase):
    def test_program_id_and_includes(self):
        program = _program("cust_update.pco")
        self.assertEqual(program.name, "CUSTUPD")
        self.assertIsNotNone(program.data.get("CUST-NAME"))
        self.assertIsNotNone(program.data.get("RL-CUST-NAME"))

    def test_exec_sql_blocks_are_extracted(self):
        program = _program("cust_update.pco")
        kinds = [statement.kind for statement in program.sql]
        self.assertIn("SELECT", kinds)
        self.assertIn("INSERT", kinds)
        self.assertIn("UPDATE", kinds)

    def test_move_chain(self):
        program = _program("cust_update.pco")
        moves = {
            (flow.sources[0], flow.target)
            for flow in program.flows
            if flow.kind is EdgeKind.MOVE and flow.sources
        }
        self.assertIn(("CUST-NAME", "RL-CUST-NAME"), moves)
        self.assertIn(("CUST-NAME", "WS-NAME-KEY"), moves)

    def test_string_collects_every_source(self):
        program = _program("cust_update.pco")
        strings = [flow for flow in program.flows if flow.kind is EdgeKind.STRING]
        self.assertTrue(strings)
        self.assertEqual(strings[0].target, "RL-CITY-STATE")
        self.assertIn("CUST-CITY", strings[0].sources)
        self.assertIn("CUST-STATE", strings[0].sources)

    def test_reference_modification_is_flagged(self):
        program = _program("cust_update.pco")
        refmods = [u for u in program.usages if u.category == "reference-modification"]
        self.assertTrue(any(u.name == "CUST-NAME" for u in refmods))

    def test_call_arguments_are_captured(self):
        program = _program("cust_update.pco")
        self.assertEqual(len(program.calls), 1)
        self.assertEqual(program.calls[0].target, "FMTNAME")
        self.assertEqual(program.calls[0].args, ["CUST-NAME", "WS-FULL-ADDRESS"])

    def test_linkage_using_order(self):
        program = _program("fmtname.pco")
        self.assertEqual(program.linkage_using, ["LK-CUST-NAME", "LK-FULL-ADDRESS"])

    def test_add_grows_its_accumulator(self):
        program = _program("cust_update.pco")
        adds = [flow for flow in program.flows if flow.kind is EdgeKind.ARITHMETIC]
        self.assertTrue(any(flow.target == "WS-ROWS-READ" for flow in adds))

    def test_paragraph_context_is_recorded(self):
        program = _program("cust_update.pco")
        paragraphs = {flow.ref.paragraph for flow in program.flows}
        self.assertIn("2000-FORMAT-LINE", paragraphs)

    def test_discover_sources_finds_the_examples(self):
        found = discover_sources([EXAMPLES / "src"], ["*.pco"])
        # Compared against the directory rather than a hard-coded count, which
        # broke every time an example was added and told nobody anything when
        # it did.
        self.assertEqual(
            sorted(path.name for path in found),
            sorted(path.name for path in (EXAMPLES / "src").glob("*.pco")),
        )


if __name__ == "__main__":
    unittest.main()
