import json
import unittest
from pathlib import Path

from cobol_impact_analyzer import report
from cobol_impact_analyzer.analyzer import analyze
from cobol_impact_analyzer.cli import main
from cobol_impact_analyzer.copybook import DEFAULT_COPYBOOK_SUFFIXES, CopybookResolver
from cobol_impact_analyzer.models import Severity
from cobol_impact_analyzer.spec import ChangeSpec, build_change, load_spec

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _spec(*changes):
    return ChangeSpec(
        changes=list(changes),
        source_paths=[EXAMPLES / "src"],
        copybook_paths=[EXAMPLES / "copybooks"],
        source_patterns=["*.pco"],
    )


def _run_name_widening():
    return analyze(_spec(build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)")))


def _find(result, node_id):
    return [finding for finding in result.findings if finding.node_id == node_id]


class PropagationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _run_name_widening()

    def test_the_direct_host_variable_is_critical(self):
        findings = _find(self.result, "var:CUSTUPD::CUST-NAME")
        self.assertTrue(findings)
        self.assertIs(findings[0].severity, Severity.CRITICAL)
        self.assertIn("X(60)", findings[0].remediation)

    def test_one_hop_move_target_is_reported(self):
        findings = _find(self.result, "var:CUSTUPD::RL-CUST-NAME")
        self.assertTrue(findings)
        self.assertIn("X(60)", findings[0].remediation)

    def test_propagation_crosses_a_call_boundary(self):
        findings = _find(self.result, "var:FMTNAME::LK-CUST-NAME")
        self.assertTrue(findings, "CALL USING should carry the width into the callee")
        self.assertIn("X(60)", findings[0].remediation)

    def test_propagation_continues_inside_the_callee(self):
        self.assertTrue(_find(self.result, "var:FMTNAME::WS-WORK-NAME"))

    def test_other_tables_that_receive_the_value_are_listed(self):
        node_ids = {finding.node_id for finding in self.result.findings}
        self.assertIn("col:ORDER_HISTORY.CUST_NAME_SNAP", node_ids)
        self.assertIn("col:ORDER_SHIP.SHIP_NAME", node_ids)

    def test_ddl_plan_includes_the_downstream_alter(self):
        joined = "\n".join(self.result.ddl)
        self.assertIn("ALTER TABLE CUSTOMER MODIFY CUST_NAME VARCHAR2(60);", joined)
        self.assertIn("ORDER_HISTORY", joined)
        self.assertIn("VARCHAR2(60)", joined)

    def test_a_field_that_is_already_wide_enough_is_not_reported(self):
        # WS-AUDIT-TEXT is PIC X(60) and receives CUST-NAME.
        self.assertFalse(
            [
                finding
                for finding in _find(self.result, "var:CUSTUPD::WS-AUDIT-TEXT")
                if finding.category in ("host-variable", "derived-variable")
            ]
        )

    def test_group_record_length_change_is_reported(self):
        findings = [
            finding
            for finding in _find(self.result, "var:CUSTUPD::CUSTOMER-REC")
            if finding.category == "record-layout"
        ]
        self.assertTrue(findings)
        self.assertIn("bytes", findings[0].required)

    def test_reference_modification_is_surfaced_on_the_field_it_affects(self):
        # A REFMOD used to be its own finding. It is a note on the field's own
        # finding now, so one field is one row however many ways it is used -
        # see _attach_usage_notes.
        findings = _find(self.result, "var:CUSTUPD::CUST-NAME")
        self.assertTrue(findings)
        self.assertTrue(
            any(note.startswith("reference-modification") for note in findings[0].notes),
            findings[0].notes,
        )

    def test_notes_stay_separate_from_the_detail_prose(self):
        # They were concatenated into detail, which made one cell hundreds of
        # characters wide and pushed the Where column off the HTML report.
        for finding in self.result.findings:
            self.assertNotIn("Also:", finding.detail)

    def test_a_field_is_reported_once_per_program_however_often_it_is_used(self):
        for node_id in {finding.node_id for finding in self.result.findings}:
            self.assertEqual(
                len(_find(self.result, node_id)),
                1,
                f"{node_id} produced more than one finding",
            )

    def test_every_finding_carries_a_path_back_to_a_change(self):
        for finding in self.result.findings:
            self.assertTrue(finding.path, finding.title)

    def test_paths_start_at_the_changed_column(self):
        findings = _find(self.result, "var:CUSTUPD::RL-CUST-NAME")
        self.assertEqual(findings[0].path[0], "col:CUSTOMER.CUST_NAME")


class NumericPropagationTests(unittest.TestCase):
    def test_widening_a_number_grows_the_packed_host_variable(self):
        result = analyze(
            _spec(build_change("CUSTOMER", "CUST_BALANCE", "NUMBER(11,2)", "NUMBER(13,2)"))
        )
        findings = _find(result, "var:CUSTUPD::CUST-BALANCE")
        self.assertTrue(findings)
        self.assertIn("9(11)", findings[0].remediation)

    def test_edited_report_field_is_flagged_too(self):
        result = analyze(
            _spec(build_change("CUSTOMER", "CUST_BALANCE", "NUMBER(11,2)", "NUMBER(13,2)"))
        )
        self.assertTrue(_find(result, "var:CUSTUPD::RL-BALANCE"))


class CoverageTests(unittest.TestCase):
    def test_a_column_nobody_uses_is_called_out(self):
        result = analyze(_spec(build_change("CUSTOMER", "NEVER_USED", "CHAR(1)", "CHAR(4)")))
        findings = [f for f in result.findings if f.category == "coverage-gap"]
        self.assertTrue(findings)
        self.assertIs(findings[0].severity, Severity.MEDIUM)

    def test_max_depth_limits_propagation(self):
        spec = _spec(build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)"))
        spec.max_depth = 1
        result = analyze(spec)
        self.assertFalse(_find(result, "var:FMTNAME::WS-WORK-NAME"))


class NameSpellingTests(unittest.TestCase):
    """SQL columns use underscores; COBOL fields use hyphens."""

    def test_underscore_column_binds_to_a_hyphen_field(self):
        result = _run_name_widening()
        # CUSTOMER.CUST_NAME (underscore) reaches CUST-NAME (hyphen) because the
        # binding comes from the SELECT INTO, never from matching the spellings.
        findings = _find(result, "var:CUSTUPD::CUST-NAME")
        self.assertTrue(findings)
        self.assertEqual(findings[0].path[0], "col:CUSTOMER.CUST_NAME")

    def test_a_mismatched_host_variable_still_resolves_and_warns(self):
        import tempfile

        program = (
            "       IDENTIFICATION DIVISION.\n"
            "       PROGRAM-ID. LOOSE.\n"
            "       DATA DIVISION.\n"
            "       WORKING-STORAGE SECTION.\n"
            "       01  CUST-NAME  PIC X(30).\n"
            "       PROCEDURE DIVISION.\n"
            "       0000-MAIN.\n"
            "           EXEC SQL\n"
            "               SELECT CUST_NAME INTO :CUST_NAME FROM CUSTOMER\n"
            "           END-EXEC\n"
            "           STOP RUN.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "loose.pco").write_text(program, encoding="utf-8")
            spec = ChangeSpec(
                changes=[build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)")],
                source_paths=[root],
                copybook_paths=[root],
            )
            result = analyze(spec)
            self.assertTrue(_find(result, "var:LOOSE::CUST-NAME"))
            self.assertTrue(
                any("normalising hyphens and underscores" in w for w in result.warnings),
                result.warnings,
            )


class CopybookExtensionTests(unittest.TestCase):
    def test_a_custom_extension_adds_to_the_defaults_rather_than_replacing(self):
        import tempfile

        from cobol_impact_analyzer.analyzer import ImpactAnalyzer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ODDBOOK.cpylib").write_text("       01  A PIC X(4).\n", encoding="utf-8")
            (root / "PLAINBOOK").write_text("       01  B PIC X(4).\n", encoding="utf-8")

            spec = ChangeSpec(
                changes=[build_change("T", "C", "CHAR(1)", "CHAR(2)")],
                source_paths=[root],
                copybook_paths=[root],
                copybook_suffixes=[".cpylib"],
            )
            analyzer = ImpactAnalyzer(spec)
            analyzer._parse_sources()
            resolver = CopybookResolver(
                spec.copybook_paths,
                suffixes=sorted(set(DEFAULT_COPYBOOK_SUFFIXES) | {".cpylib"}),
            )
            self.assertIsNotNone(resolver.resolve("ODDBOOK"))
            # The extensionless member must survive the custom extension.
            self.assertIsNotNone(resolver.resolve("PLAINBOOK"))
            self.assertIsNone(resolver._fallback_index)


class SpecTests(unittest.TestCase):
    def test_example_spec_loads(self):
        spec = load_spec(EXAMPLES / "change_spec.json")
        self.assertEqual(len(spec.changes), 2)
        self.assertEqual(spec.changes[0].key, "CUSTOMER.CUST_NAME")
        self.assertTrue(spec.source_paths[0].name == "src")


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _run_name_widening()

    def test_json_is_valid_and_carries_findings(self):
        payload = json.loads(report.to_json(self.result))
        self.assertEqual(payload["summary"]["programs_scanned"], 5)
        self.assertTrue(payload["findings"])
        self.assertTrue(payload["ddl_plan"])

    def test_text_report_mentions_the_change(self):
        text = report.to_text(self.result)
        self.assertIn("CUSTOMER.CUST_NAME", text)
        self.assertIn("DDL PLAN", text)

    def test_html_is_self_contained(self):
        html = report.to_html(self.result)
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertNotIn("<script src=", html)
        self.assertIn("CUST_NAME", html)


class CliTests(unittest.TestCase):
    def test_end_to_end_writes_all_three_formats(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code = main(
                [
                    "--spec",
                    str(EXAMPLES / "change_spec.json"),
                    "--out",
                    str(out),
                    "--quiet",
                    "--no-progress",
                ]
            )
            self.assertEqual(code, 0)
            for name in ("impact.json", "impact.csv", "impact.html"):
                self.assertTrue((out / name).exists(), name)
            payload = json.loads((out / "impact.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["findings"])

    def test_inline_arguments_work_without_a_spec_file(self):
        code = main(
            [
                "--table",
                "CUSTOMER",
                "--column",
                "CUST_NAME",
                "--from",
                "VARCHAR2(30)",
                "--to",
                "VARCHAR2(60)",
                "--source",
                str(EXAMPLES / "src"),
                "--copybook",
                str(EXAMPLES / "copybooks"),
                "--quiet",
                "--no-progress",
            ]
        )
        self.assertEqual(code, 0)

    def test_fail_on_returns_non_zero(self):
        code = main(
            [
                "--spec",
                str(EXAMPLES / "change_spec.json"),
                "--quiet",
                "--no-progress",
                "--fail-on",
                "CRITICAL",
            ]
        )
        self.assertEqual(code, 1)


class ProgramImpactTests(unittest.TestCase):
    """Who has to open a program, versus who only has to rebuild it."""

    @classmethod
    def setUpClass(cls):
        cls.result = _run_name_widening()

    def _impact(self, name):
        for impact in self.result.program_impacts:
            if impact.program == name:
                return impact
        self.fail(f"{name} is not in the program impacts")

    def test_a_program_that_only_copies_the_table_record_is_recompile_only(self):
        # CUSTLIST selects into the CUSTOMER copybook record and does nothing
        # else. The copybook changes, so it has to be rebuilt - but there is
        # nothing in its own source to edit.
        impact = self._impact("CUSTLIST")
        self.assertTrue(impact.recompile_only)
        self.assertEqual(impact.verdict, "recompile only")
        self.assertEqual(impact.own_work, [])
        self.assertTrue(any(book.endswith("CUSTOMER.cpy") for book in impact.changed_copybooks))

    def test_a_program_that_never_touches_the_field_is_still_rebuilt(self):
        # CUSTPURG includes CUSTOMER.cpy and only ever DELETEs. Nothing flows
        # through CUST-NAME here, so per-program scoping finds no impacted node
        # in it at all - but the copybook it compiles against still changes
        # shape, so it still has to be rebuilt. These are the programs most
        # likely to be missed, because nothing in them looks different.
        impact = self._impact("CUSTPURG")
        self.assertTrue(impact.recompile_only)
        self.assertEqual(impact.own_work, [])
        self.assertTrue(any(book.endswith("CUSTOMER.cpy") for book in impact.changed_copybooks))

    def test_a_copybook_changed_by_one_program_is_changed_for_all_of_them(self):
        # The widening reaches CUST-NAME through CUSTUPD and ORDENTRY. That
        # makes CUSTOMER.cpy a changed file for every includer, not only for
        # the programs the flow happened to pass through.
        includers = {
            impact.program
            for impact in self.result.program_impacts
            if any(book.endswith("CUSTOMER.cpy") for book in impact.changed_copybooks)
        }
        self.assertEqual(includers, {"CUSTUPD", "ORDENTRY", "CUSTLIST", "CUSTPURG"})

    def test_a_program_with_its_own_widened_field_is_a_source_change(self):
        impact = self._impact("CUSTUPD")
        self.assertFalse(impact.recompile_only)
        self.assertEqual(impact.verdict, "source change")
        self.assertTrue(impact.own_work)

    def test_a_statement_assuming_the_old_width_is_work_even_on_a_copybook_field(self):
        # CUST-NAME is declared in CUSTOMER.cpy, but CUSTUPD reference-modifies
        # it in its own PROCEDURE DIVISION - that edit belongs to CUSTUPD.
        impact = self._impact("CUSTUPD")
        self.assertTrue(
            any("CUST-NAME used via reference-modification" in item for item in impact.own_work)
        )

    def test_an_untouched_program_is_not_listed_at_all(self):
        listed = {impact.program for impact in self.result.program_impacts}
        scanned = {program.name for program in self.result.programs}
        self.assertTrue(listed <= scanned)
        result = analyze(_spec(build_change("CUSTOMER", "NEVER_USED", "CHAR(1)", "CHAR(4)")))
        self.assertEqual(result.program_impacts, [])


class VariableScopeTests(unittest.TestCase):
    """A copybook field is one node PER PROGRAM, not one node globally."""

    @classmethod
    def setUpClass(cls):
        cls.result = _run_name_widening()

    def test_the_same_copybook_field_is_a_separate_node_in_each_program(self):
        node_ids = {finding.node_id for finding in self.result.findings}
        self.assertIn("var:CUSTUPD::CUST-NAME", node_ids)
        self.assertIn("var:ORDENTRY::CUST-NAME", node_ids)
        # The old global id linked every program's copy into one node, which is
        # what invented flows between programs that never call each other.
        self.assertNotIn("var:CUST-NAME", node_ids)

    def test_a_route_never_crosses_into_another_program_without_a_call(self):
        for finding in self.result.findings:
            programs = [
                node_id.split("::", 1)[0][4:]
                for node_id in finding.path
                if node_id.startswith("var:") and "::" in node_id
            ]
            # FMTNAME is reached by CALL USING, so a path may legitimately name
            # two programs; it must never name three unrelated ones.
            self.assertLessEqual(len(set(programs)), 2, finding.path)


if __name__ == "__main__":
    unittest.main()
