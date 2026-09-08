import json
import unittest
from pathlib import Path

from cobol_impact_analyzer import report
from cobol_impact_analyzer.analyzer import analyze
from cobol_impact_analyzer.cli import main
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
        findings = _find(self.result, "var:CUST-NAME")
        self.assertTrue(findings)
        self.assertIs(findings[0].severity, Severity.CRITICAL)
        self.assertIn("X(60)", findings[0].remediation)

    def test_one_hop_move_target_is_reported(self):
        findings = _find(self.result, "var:RL-CUST-NAME")
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
            for finding in _find(self.result, "var:CUSTOMER-REC")
            if finding.category == "record-layout"
        ]
        self.assertTrue(findings)
        self.assertIn("bytes", findings[0].required)

    def test_reference_modification_is_surfaced(self):
        refmod = [
            finding
            for finding in self.result.findings
            if finding.category == "reference-modification"
        ]
        self.assertTrue(refmod)
        self.assertIs(refmod[0].severity, Severity.HIGH)

    def test_every_finding_carries_a_path_back_to_a_change(self):
        for finding in self.result.findings:
            self.assertTrue(finding.path, finding.title)

    def test_paths_start_at_the_changed_column(self):
        findings = _find(self.result, "var:RL-CUST-NAME")
        self.assertEqual(findings[0].path[0], "col:CUSTOMER.CUST_NAME")


class NumericPropagationTests(unittest.TestCase):
    def test_widening_a_number_grows_the_packed_host_variable(self):
        result = analyze(
            _spec(build_change("CUSTOMER", "CUST_BALANCE", "NUMBER(11,2)", "NUMBER(13,2)"))
        )
        findings = _find(result, "var:CUST-BALANCE")
        self.assertTrue(findings)
        self.assertIn("9(11)", findings[0].remediation)

    def test_edited_report_field_is_flagged_too(self):
        result = analyze(
            _spec(build_change("CUSTOMER", "CUST_BALANCE", "NUMBER(11,2)", "NUMBER(13,2)"))
        )
        self.assertTrue(_find(result, "var:RL-BALANCE"))


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
        self.assertEqual(payload["summary"]["programs_scanned"], 3)
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
            ]
        )
        self.assertEqual(code, 0)

    def test_fail_on_returns_non_zero(self):
        code = main(
            [
                "--spec",
                str(EXAMPLES / "change_spec.json"),
                "--quiet",
                "--fail-on",
                "CRITICAL",
            ]
        )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
