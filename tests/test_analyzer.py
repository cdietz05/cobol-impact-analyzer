import json
import tempfile
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
        self.assertEqual(
            payload["summary"]["programs_scanned"],
            len(list((EXAMPLES / "src").glob("*.pco"))),
        )
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

    def test_html_tiles_link_to_severity_sections(self):
        html = report.to_html(self.result)
        for level in ("critical", "high", "medium", "low", "info"):
            self.assertIn(f"href='#sev-{level}'", html)
            self.assertIn(f"id='sev-{level}'", html)

    def test_html_has_the_three_reader_sections(self):
        html = report.to_html(self.result)
        self.assertIn("<h2>Flow</h2>", html)
        self.assertIn("<h2>Changes by module</h2>", html)

    def test_html_explains_every_severity(self):
        html = report.to_html(self.result)
        self.assertIn("What the severities mean", html)
        for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
            self.assertIn(f"<dt style='color:", html)
            self.assertIn(report._SEVERITY_BLURB[Severity(level)][:24], html)

    def test_text_report_explains_every_severity(self):
        text = report.to_text(self.result)
        self.assertIn("SEVERITY", text)
        for level in Severity:
            self.assertIn(report._SEVERITY_BLURB[level][:24], text)

    def test_text_report_has_flow_and_per_module_sections(self):
        text = report.to_text(self.result)
        self.assertIn("FLOW", text)
        self.assertIn("CHANGES BY MODULE", text)
        self.assertIn("CUSTOMER.CUST_NAME", text)

    def test_summary_lists_each_edited_file_once(self):
        summary = report.to_summary(self.result)
        self.assertIn("# CUSTOMER widening", summary)
        self.assertIn("## Files to edit", summary)
        self.assertIn("CUSTOMER.cpy", summary)
        # A copybook field reached through several programs is one edit line.
        self.assertEqual(summary.count("**CUST-NAME**"), 1)

    def test_out_writes_the_markdown_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            main(
                [
                    "--spec",
                    str(EXAMPLES / "change_spec.json"),
                    "--out",
                    str(out),
                    "--quiet",
                    "--no-progress",
                ]
            )
            self.assertTrue((out / "CUSTOMER.md").exists())


class CliTests(unittest.TestCase):
    def test_end_to_end_writes_all_three_formats(self):
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
            # Named for the table in the spec, not a fixed "impact".
            for name in ("CUSTOMER.json", "CUSTOMER.csv", "CUSTOMER.html"):
                self.assertTrue((out / name).exists(), name)
            payload = json.loads((out / "CUSTOMER.json").read_text(encoding="utf-8"))
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


class OutputNamingTests(unittest.TestCase):
    def test_one_table_names_the_files_after_it(self):
        spec = _spec(build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)"))
        self.assertEqual(report.output_basename(spec), "CUSTOMER")

    def test_several_tables_are_joined(self):
        spec = _spec(
            build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)"),
            build_change("ORDER_SHIP", "SHIP_NAME", "VARCHAR2(30)", "VARCHAR2(60)"),
        )
        self.assertEqual(report.output_basename(spec), "CUSTOMER_ORDER_SHIP")

    def test_many_tables_say_how_many_rather_than_listing_them(self):
        spec = _spec(
            *[
                build_change(f"TABLE{index}", "COL", "VARCHAR2(30)", "VARCHAR2(60)")
                for index in range(6)
            ]
        )
        self.assertEqual(report.output_basename(spec), "TABLE0_and_5_more")

    def test_a_schema_qualified_name_does_not_become_an_extension(self):
        spec = _spec(build_change("SALES.CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)"))
        self.assertEqual(report.output_basename(spec), "SALES_CUSTOMER")

    def test_out_writes_files_named_for_the_table(self):
        with tempfile.TemporaryDirectory() as directory:
            code = main(
                [
                    "--spec",
                    str(EXAMPLES / "change_spec.json"),
                    "--out",
                    directory,
                    "--quiet",
                    "--no-progress",
                ]
            )
            self.assertEqual(code, 0)
            written = {path.name for path in Path(directory).iterdir()}
            self.assertNotIn("impact.json", written)
            self.assertTrue(
                any(name.endswith(".html") for name in written), written
            )
            stems = {Path(name).stem for name in written}
            self.assertEqual(len(stems), 1, written)
            self.assertNotEqual(stems.pop(), "impact")


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
        # CUSTPURG includes CUSTOMER.cpy and only ever DELETEs. No widened value
        # flows through CUST-NAME here - it widens only because the copybook is
        # edited - but the copybook it compiles against still changes shape, so
        # it still has to be rebuilt. These are the programs most likely to be
        # missed, because nothing in them looks different.
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
        self.assertEqual(
            includers,
            {"CUSTUPD", "ORDENTRY", "CUSTLIST", "CUSTPURG", "CUSTARCH", "CUSTTBL"},
        )

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


class SubscriptTests(unittest.TestCase):
    """A subscript is a position, not somewhere the value lands.

    Widened with a NUMBER, not a VARCHAR2, on purpose. An index is numeric, and
    covers() compares only digit counts when both sides are numeric - so an
    alphanumeric widening never reached the index and would not have shown this
    at all. A numeric one hands it the extra digits directly.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = analyze(
            _spec(build_change("CUSTOMER", "CUST_BALANCE", "NUMBER(11,2)", "NUMBER(13,2)"))
        )

    def test_the_table_entry_receiving_the_value_is_reported(self):
        findings = _find(self.result, "var:CUSTTBL::WS-BAL-ENTRY")
        self.assertTrue(findings)

    def test_the_index_variable_is_not_reported(self):
        # MOVE CUST-BALANCE TO WS-BAL-ENTRY(WS-BAL-IDX) used to read WS-BAL-IDX
        # as a second target, so every index in the shop came back as needing
        # to grow. No amount of widening a value makes a position bigger.
        self.assertFalse(_find(self.result, "var:CUSTTBL::WS-BAL-IDX"))

    def test_the_index_is_not_listed_as_work_for_its_program(self):
        for impact in self.result.program_impacts:
            if impact.program == "CUSTTBL":
                self.assertFalse(
                    [item for item in impact.own_work if "WS-BAL-IDX" in item],
                    impact.own_work,
                )


class DynamicCallTests(unittest.TestCase):
    """CALL WS-PGM-NAME, resolved through the literals moved into it."""

    @classmethod
    def setUpClass(cls):
        cls.result = _run_name_widening()

    def test_the_widened_value_reaches_the_called_modules_linkage(self):
        # CUSTARCH does MOVE 'IOCUSTNM' TO WS-PGM-NAME then CALL WS-PGM-NAME.
        # Without resolving that, the call is a dead end and nothing downstream
        # of it is ever reported.
        self.assertTrue(_find(self.result, "var:IOCUSTNM::LK-IO-NAME"))

    def test_a_table_only_written_by_the_io_module_is_reported(self):
        # The whole point: CUST_ARCHIVE is never named in the calling program,
        # only in the IO module the caller reaches indirectly.
        findings = _find(self.result, "col:CUST_ARCHIVE.ARCH_NAME")
        self.assertTrue(findings)
        self.assertIn("VARCHAR2(60)", findings[0].remediation)

    def test_the_route_names_the_program_the_variable_resolved_to(self):
        findings = _find(self.result, "var:IOCUSTNM::LK-IO-NAME")
        trail = findings[0].path
        self.assertIn("col:CUSTOMER.CUST_NAME", trail)
        self.assertIn("var:CUSTARCH::WS-ARCH-NAME", trail)

    def test_an_unresolvable_dynamic_call_says_why(self):
        warnings = "\n".join(self.result.warnings)
        # Every dynamic call in the examples resolves, so the bare "no literal
        # was ever moved into it" wording must not be firing spuriously.
        self.assertNotIn("no literal program name was ever moved into it", warnings)


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



def _both_columns():
    return analyze(
        _spec(
            build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)"),
            build_change("CUSTOMER", "CUST_BALANCE", "NUMBER(11,2)", "NUMBER(13,2)"),
        )
    )


def _layout(result, node_id):
    return [f for f in _find(result, node_id) if f.category == "record-layout"]


class RecordLengthTests(unittest.TestCase):
    """Record lengths are bytes, summed over every member that grows."""

    @classmethod
    def setUpClass(cls):
        cls.result = _both_columns()

    def test_a_record_counts_every_member_that_grows(self):
        # 114 bytes, plus 30 for CUST-NAME, plus 1 for CUST-BALANCE. Keeping
        # only the larger increase reported 144.
        [finding] = _layout(self.result, "var:CUSTUPD::CUSTOMER-REC")
        self.assertEqual(finding.required, "145 bytes")

    def test_one_copybook_has_one_record_length_in_every_program(self):
        # CUSTTBL only fetches the balance and CUSTLIST only the name, but they
        # compile against the same edited CUSTOMER.cpy.
        for program in ("CUSTARCH", "CUSTLIST", "CUSTPURG", "CUSTTBL", "CUSTUPD", "ORDENTRY"):
            [finding] = _layout(self.result, f"var:{program}::CUSTOMER-REC")
            self.assertEqual(finding.required, "145 bytes", program)

    def test_a_packed_field_grows_its_record_in_bytes_not_digits(self):
        result = analyze(
            _spec(build_change("CUSTOMER", "CUST_BALANCE", "NUMBER(11,2)", "NUMBER(13,2)"))
        )
        # S9(9)V99 COMP-3 is 6 bytes, S9(11)V99 COMP-3 is 7.
        [finding] = _layout(result, "var:CUSTTBL::CUSTOMER-REC")
        self.assertEqual(finding.required, "115 bytes")

    def test_a_table_grows_by_every_entry(self):
        # 100 entries, one more byte each.
        [finding] = _layout(self.result, "var:CUSTTBL::WS-BAL-TABLE")
        self.assertEqual(finding.required, "700 bytes")

    def test_an_edited_field_adds_its_punctuation_to_the_record(self):
        # 83 bytes, plus 30 for RL-CUST-NAME, plus 4 for RL-BALANCE growing
        # from ZZ,ZZZ,ZZ9.99- to ZZ,ZZZ,ZZZ,ZZ9.99-.
        [finding] = _layout(self.result, "var:CUSTUPD::WS-REPORT-LINE")
        self.assertEqual(finding.required, "117 bytes")

    def test_a_location_is_listed_once(self):
        [finding] = _find(self.result, "var:CUSTUPD::WS-REPORT-FLAT")
        locations = [(ref.path, ref.line) for ref in finding.refs]
        self.assertEqual(len(locations), len(set(locations)), locations)


class CopybookFieldTests(unittest.TestCase):
    """A copybook field widened by another program's need for it."""

    @classmethod
    def setUpClass(cls):
        cls.result = _run_name_widening()

    def test_it_is_reported_as_a_rebuild(self):
        [finding] = _find(self.result, "var:CUSTPURG::CUST-NAME")
        self.assertEqual(finding.category, "copybook-field")
        self.assertIs(finding.severity, Severity.LOW)
        self.assertIn("X(60)", finding.remediation)

    def test_the_route_goes_through_the_copybook(self):
        [finding] = _find(self.result, "var:CUSTPURG::CUST-NAME")
        self.assertTrue(any(node.startswith("decl:") for node in finding.path), finding.path)

    def test_the_program_is_still_recompile_only(self):
        [impact] = [i for i in self.result.program_impacts if i.program == "CUSTPURG"]
        self.assertTrue(impact.recompile_only)

    def test_a_later_copybook_link_does_not_downgrade_a_truncation(self):
        # CSCUSTINQ MOVEs its own fetched balance into CS-RL-BALANCE, which
        # truncates. CSBILLCYC then needs the same copybook field wider still,
        # and that non-truncating link grew it last - which used to turn the
        # CRITICAL into HIGH.
        result = analyze(load_spec(EXAMPLES / "css" / "change_spec.json"))
        [finding] = _find(result, "var:CSCUSTINQ::CS-RL-BALANCE")
        self.assertIs(finding.severity, Severity.CRITICAL)

    def test_a_field_widened_by_fetching_it_is_still_critical(self):
        [finding] = _find(self.result, "var:CUSTLIST::CUST-NAME")
        self.assertEqual(finding.category, "host-variable")
        self.assertIs(finding.severity, Severity.CRITICAL)


_LEAK_COPYBOOK = """\
       01  CUST-REC.
           05  CUST-NAME            PIC X(30).
           05  CUST-CITY            PIC X(20).
           05  CUST-BAL             PIC S9(7)V99.
"""

_LEAK_WIDENER = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. WIDENER.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE CUSTREC END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT CUST_NAME INTO :CUST-NAME FROM CUSTOMER WHERE ID = 1
           END-EXEC
           MOVE CUST-NAME TO CUST-CITY
           GOBACK.
"""

_LEAK_BYSTANDER = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. BYSTAND.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE CUSTREC END-EXEC.
       01  WS-CITY-OUT              PIC X(20).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT CUST_BAL INTO :CUST-BAL FROM CUSTOMER WHERE ID = 1
           END-EXEC
           MOVE CUST-BAL TO CUST-CITY
           MOVE CUST-CITY TO WS-CITY-OUT
           GOBACK.
"""


class CopybookSizeDoesNotFlowTests(unittest.TestCase):
    """A copybook field's new size is not data moving through other programs.

    WIDENER moves the widened name into CUST-CITY, so CUSTREC.cpy's CUST-CITY
    has to grow. BYSTAND only ever puts a balance in CUST-CITY. Its copy of
    the field grows with the copybook, but moving it on moves a balance, and
    must not flag WS-CITY-OUT as needing room for a 60-character name.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "cpy").mkdir()
        (root / "src").mkdir()
        (root / "cpy" / "CUSTREC.cpy").write_text(_LEAK_COPYBOOK)
        (root / "src" / "widener.pco").write_text(_LEAK_WIDENER)
        (root / "src" / "bystand.pco").write_text(_LEAK_BYSTANDER)
        cls.result = analyze(
            ChangeSpec(
                changes=[
                    build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)"),
                    build_change("CUSTOMER", "CUST_BAL", "NUMBER(9,2)", "NUMBER(11,2)"),
                ],
                source_paths=[root / "src"],
                copybook_paths=[root / "cpy"],
                source_patterns=["*.pco"],
            )
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_program_that_moves_the_name_in_is_flagged(self):
        [finding] = _find(self.result, "var:WIDENER::CUST-CITY")
        self.assertIs(finding.severity, Severity.CRITICAL)

    def test_the_copybook_size_does_not_travel_through_another_programs_move(self):
        self.assertFalse(_find(self.result, "var:BYSTAND::WS-CITY-OUT"))

    def test_the_other_programs_copy_is_a_rebuild(self):
        [finding] = _find(self.result, "var:BYSTAND::CUST-CITY")
        self.assertEqual(finding.category, "copybook-field")


def _analyze_sources(sources: dict[str, str], copybooks: dict[str, str], *changes):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "cpy").mkdir()
        for name, text in sources.items():
            (root / "src" / name).write_text(text)
        for name, text in copybooks.items():
            (root / "cpy" / name).write_text(text)
        return analyze(
            ChangeSpec(
                changes=list(changes),
                source_paths=[root / "src"],
                copybook_paths=[root / "cpy"],
                source_patterns=["*.pco"],
            )
        )


_AT_COPYBOOK = """\
       01  AT-REC.
           05  AT-ID                PIC S9(9) COMP-3.
           05  AT-DB                PIC S9(9)V99 COMP-3.
           05  AT-REMN-DB           PIC S9(9)V99 COMP-3.
           05  AT-DATE              PIC 9(8).
"""

_AT_WIDENING = (
    build_change("AT_TBL", "AT_REMN_DB", "NUMBER(11,2)", "NUMBER(13,2)"),
)


class CursorDeclaredAfterFetchTests(unittest.TestCase):
    """A program's own DECLARE wins, wherever it sits in the source."""

    @classmethod
    def setUpClass(cls):
        other = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. AOTHER.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE ATREC END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               DECLARE C1 CURSOR FOR
               SELECT AT_ID, AT_DATE, AT_REMN_DB FROM AT_TBL
           END-EXEC
           GOBACK.
"""
        program = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. PROG.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE ATREC END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 9000-DECLARE
           EXEC SQL OPEN C1 END-EXEC
           EXEC SQL
               FETCH C1 INTO :AT-ID, :AT-REMN-DB, :AT-DB
           END-EXEC
           GOBACK.
       9000-DECLARE.
           EXEC SQL
               DECLARE C1 CURSOR FOR
               SELECT AT_ID, AT_REMN_DB, AT_DB FROM AT_TBL
           END-EXEC.
"""
        cls.result = _analyze_sources(
            {"aother.pco": other, "prog.pco": program}, {"ATREC.cpy": _AT_COPYBOOK}, *_AT_WIDENING
        )

    def test_the_fetch_uses_this_programs_own_select_list(self):
        [finding] = _find(self.result, "var:PROG::AT-REMN-DB")
        self.assertEqual(finding.path, ["col:AT_TBL.AT_REMN_DB", "var:PROG::AT-REMN-DB"])

    def test_a_same_named_cursor_elsewhere_is_not_borrowed(self):
        self.assertFalse([w for w in self.result.warnings if "borrowed" in w and "prog.pco" in w])
        self.assertFalse(
            [f for f in _find(self.result, "var:PROG::AT-DB") if f.category != "copybook-field"]
        )


class SharedSubprogramTests(unittest.TestCase):
    """A value one caller passes in does not come back out to another caller."""

    @classmethod
    def setUpClass(cls):
        caller_a = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. PA.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE ATREC END-EXEC.
       01  WS-OUT                   PIC X(12).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT AT_REMN_DB INTO :AT-REMN-DB FROM AT_TBL
           END-EXEC
           CALL 'UTIL' USING AT-REMN-DB WS-OUT
           GOBACK.
"""
        caller_b = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. PB.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE ATREC END-EXEC.
       01  WS-OUT                   PIC X(12).
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL 'UTIL' USING AT-DATE WS-OUT
           GOBACK.
"""
        util = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. UTIL.
       DATA DIVISION.
       LINKAGE SECTION.
       01  LK-VALUE                 PIC S9(11)V99.
       01  LK-TEXT                  PIC X(12).
       PROCEDURE DIVISION USING LK-VALUE LK-TEXT.
       0000-MAIN.
           MOVE LK-VALUE TO LK-TEXT
           GOBACK.
"""
        cls.result = _analyze_sources(
            {"pa.pco": caller_a, "pb.pco": caller_b, "util.pco": util},
            {"ATREC.cpy": _AT_COPYBOOK},
            *_AT_WIDENING,
        )

    def test_another_callers_argument_is_not_flagged(self):
        # PB passes a date in the same position PA passes the balance.
        self.assertFalse(_find(self.result, "var:PB::AT-DATE"))
        self.assertFalse(_find(self.result, "var:PB::WS-OUT"))

    def test_a_value_the_callee_moves_to_another_parameter_reaches_its_own_caller(self):
        # UTIL moves LK-VALUE into LK-TEXT, so PA's WS-OUT receives PA's
        # widened balance, through the callee's own MOVE.
        [finding] = _find(self.result, "var:PA::WS-OUT")
        self.assertIn("X(", finding.remediation)


_QUALIFIED_PROGRAM = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. CUBCE100.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE CU02TB04 END-EXEC.
           EXEC SQL INCLUDE CU02TB05 END-EXEC.
       01  VS-CD-DB-STAT            PIC X(02).
       01  VS-CD-BUS                PIC X(02).
       01  WS-UNBILL-TOTAL          PIC S9(11)V99 COMP-3.
       01  WS-DEBIT-DATE            PIC X(10).
       PROCEDURE DIVISION.
       A8000-OPEN.
           MOVE VS-CD-DB-STAT TO CD-DB-STAT OF CU02TB04.
           MOVE VS-CD-BUS     TO CD-BUS OF CU02TB04.
           EXEC SQL
               DECLARE CEP_DEBIT CURSOR FOR
               SELECT   AT_REMN_DB,
                        CD_CITY_CNTY,
                        AT_DB,
                        DT_DB,
                        KY_PROD_ORDNO,
                        CD_BUS,
                        CD_PROD,
                        CD_BILL_TYPE,
                        KY_DB_SEQ_NO
                 FROM   DB_ACTIVITY
                WHERE   KY_BA        = :CU02TB04.KY-BA        AND
                        AT_REMN_DB   > :CU02TB04.AT-REMN-DB   AND
                        CD_DB_STAT   = :CU02TB04.CD-DB-STAT   AND
                        CD_BUS       = :CU02TB04.CD-BUS
             ORDER BY   CD_BUS,
                        KY_PROD_ORDNO,
                        KY_DB_SEQ_NO
           END-EXEC.
           EXEC SQL
               OPEN CEP_DEBIT
           END-EXEC.
       A8120-FETCH.
           EXEC SQL
               FETCH  CEP_DEBIT
               INTO   :CU02TB04.AT-REMN-DB,
                      :CU02TB04.CD-CITY-CNTY,
                      :CU02TB04.AT-DB,
                      :CU02TB04.DT-DB,
                      :CU02TB04.KY-PROD-ORDNO,
                      :CU02TB04.CD-BUS,
                      :CU02TB04.CD-PROD,
                      :CU02TB04.CD-BILL-TYPE,
                      :CU02TB04.KY-DB-SEQ-NO
           END-EXEC.
           ADD AT-REMN-DB OF CU02TB04 TO WS-UNBILL-TOTAL.
           IF DT-DB OF CU02TB04 > WS-DEBIT-DATE
               MOVE DT-DB OF CU02TB04 TO WS-DEBIT-DATE
           END-IF.
           MOVE AT-DB OF CU02TB05 TO AT-DB OF CU02TB04.
"""

_QUALIFIED_TB04 = """\
       01  CU02TB04.
           05  KY-BA                PIC X(10).
           05  AT-REMN-DB           PIC S9(9)V99 COMP-3.
           05  CD-CITY-CNTY         PIC X(04).
           05  AT-DB                PIC S9(9)V99 COMP-3.
           05  DT-DB                PIC X(10).
           05  KY-PROD-ORDNO        PIC X(12).
           05  CD-BUS               PIC X(02).
           05  CD-PROD              PIC X(04).
           05  CD-BILL-TYPE         PIC X(02).
           05  KY-DB-SEQ-NO         PIC S9(5) COMP-3.
           05  CD-DB-STAT           PIC X(02).
"""

_QUALIFIED_TB05 = """\
       01  CU02TB05.
           05  AT-DB                PIC S9(9)V99 COMP-3.
           05  DT-DB                PIC X(10).
"""


class QualifiedReferenceTests(unittest.TestCase):
    """:CU02TB04.AT-DB and AT-DB OF CU02TB04 name one field, in one record.

    Shaped after a real Pro*COBOL program: every host variable is written
    :RECORD.FIELD, every COBOL reference FIELD OF RECORD, and a second record
    declares some of the same field names. Before, the whole FETCH was dropped
    (the qualified host variables matched nothing), AT_DB was reported as never
    referenced, the record CU02TB04 was read as an operand of IF DT-DB OF
    CU02TB04 > WS-DEBIT-DATE - flagging the date for the record's width - and
    CU02TB05's AT-DB and DT-DB did not exist at all.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = _analyze_sources(
            {"cubce100.pco": _QUALIFIED_PROGRAM},
            {"CU02TB04.cpy": _QUALIFIED_TB04, "CU02TB05.cpy": _QUALIFIED_TB05},
            build_change("DB_ACTIVITY", "AT_REMN_DB", "NUMBER(11,2)", "NUMBER(13,2)"),
            build_change("DB_ACTIVITY", "AT_DB", "NUMBER(11,2)", "NUMBER(13,2)"),
        )

    def test_qualified_host_variables_are_traced(self):
        self.assertFalse([w for w in self.result.warnings if "is not declared" in w])
        self.assertTrue(_find(self.result, "var:CUBCE100::AT-REMN-DB"))

    def test_a_column_fetched_through_a_qualified_host_variable_is_found(self):
        self.assertFalse([f for f in self.result.findings if f.category == "coverage-gap"])
        [finding] = _find(self.result, "var:CUBCE100::AT-DB OF CU02TB04")
        self.assertEqual(finding.path[0], "col:DB_ACTIVITY.AT_DB")

    def test_the_record_is_not_an_operand_of_a_qualified_comparison(self):
        self.assertFalse(_find(self.result, "var:CUBCE100::WS-DEBIT-DATE"))

    def test_the_other_records_same_named_fields_stay_separate(self):
        self.assertFalse(_find(self.result, "var:CUBCE100::AT-DB OF CU02TB05"))
        self.assertFalse(_find(self.result, "var:CUBCE100::DT-DB OF CU02TB05"))

    def test_the_accumulator_fed_by_a_qualified_add_is_found(self):
        self.assertTrue(_find(self.result, "var:CUBCE100::WS-UNBILL-TOTAL"))

    def test_the_html_trace_starts_at_the_sql_and_names_each_statement(self):
        page = report.to_html(self.result)
        flow = page[page.index("<h2>Flow</h2>") : page.index("<h2>Changes by module</h2>")]
        # The whole FETCH, not a 200-character stub of it.
        self.assertIn(":CU02TB04.KY-DB-SEQ-NO", flow)
        self.assertIn("cursor CEP_DEBIT, declared at", flow)
        self.assertIn("ADD AT-REMN-DB OF CU02TB04 TO WS-UNBILL-TOTAL", flow)
        # Every finding carries its own hop-by-hop trace as well.
        self.assertIn("class='trace'", page)


class ComparisonDoesNotFlowTests(unittest.TestCase):
    """A field compared with a widened value is flagged, but holds none of it.

    Shaped after a real trace: WHERE AT_REMN_DB > :HWS-UNBIL-BAL gave
    HWS-UNBIL-BAL the balance width, which then flowed through COMPUTE into
    HWS-AT-MPAY2, through UPDATE into COLLECTION_INFO.AT_MPAY_TWO, back out of
    that column into another program's AT-MPAY-TWO, and round again - none of
    which ever held a balance.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = _analyze_sources(
            {
                "cubcl053.pco": """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. CUBCL053.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE CU02TB34 END-EXEC.
           EXEC SQL INCLUDE CU05TB02 END-EXEC.
       01  HWS-AT-MPAY2             PIC S9(10)V9(2) COMP-3.
       01  HWS-KY-BA                PIC X(10).
       01  HWS-UNBIL-BAL            PIC S9(9)V9(2) COMP-3.
       01  WS-CNT                   PIC S9(9) COMP.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT COUNT(*) INTO :WS-CNT FROM DB_ACTIVITY
                WHERE KY_BA = :CU05TB02.KY-BA
                  AND AT_REMN_DB > :HWS-UNBIL-BAL
           END-EXEC
           COMPUTE HWS-AT-MPAY2 = HWS-UNBIL-BAL
                                + AT-MPAY-TWO OF CU02TB34
           EXEC SQL
               UPDATE COLLECTION_INFO
                  SET AT_MPAY_TWO = :HWS-AT-MPAY2
                WHERE KY_BA = :HWS-KY-BA
           END-EXEC
           GOBACK.
""",
                "cubcl001.pco": """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. CUBCL001.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE CU02TB34 END-EXEC.
       01  HWV-LAST-KY-BA           PIC X(10).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT AT_MPAY_ONE, AT_MPAY_TWO
                 INTO :CU02TB34.AT-MPAY-ONE, :CU02TB34.AT-MPAY-TWO
                 FROM COLLECTION_INFO WHERE KY_BA = :HWV-LAST-KY-BA
           END-EXEC
           GOBACK.
""",
            },
            {
                "CU02TB34.cpy": """\
       01  CU02TB34.
           11  KY-CUST-NO           PIC X(10).
           11  AT-MPAY-ONE          PIC S9(7)V9(2) COMP-3.
           11  AT-MPAY-TWO          PIC S9(7)V9(2) COMP-3.
""",
                "CU05TB02.cpy": """\
       01  CU05TB02.
           05  KY-BA                PIC X(10).
           05  AT-UNBIL-BAL         PIC S9(9)V9(2) COMP-3.
""",
            },
            build_change("DB_ACTIVITY", "AT_REMN_DB", "NUMBER(11,2)", "NUMBER(13,2)"),
        )

    def test_the_compared_field_is_flagged_as_a_comparison(self):
        [finding] = _find(self.result, "var:CUBCL053::HWS-UNBIL-BAL")
        self.assertEqual(finding.category, "comparison")

    def test_nothing_flows_on_from_the_compared_field(self):
        flagged = {f.node_id for f in self.result.findings if f.category != "requested-change"}
        self.assertEqual(flagged, {"var:CUBCL053::HWS-UNBIL-BAL"})


class ReferenceModificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _run_name_widening()

    def test_a_fixed_length_slice_does_not_widen_its_target(self):
        # MOVE CUST-NAME (1:20) TO WS-SNAPSHOT-NAME moves 20 characters however
        # wide CUST-NAME grows.
        self.assertFalse(_find(self.result, "var:CUSTUPD::WS-SNAPSHOT-NAME"))

    def test_nor_the_column_that_target_is_written_to(self):
        self.assertFalse(_find(self.result, "col:ORDER_AUDIT.AUDIT_CUST_NAME"))
        self.assertNotIn("ORDER_AUDIT", "\n".join(self.result.ddl))


class StringTargetTests(unittest.TestCase):
    def test_a_target_with_room_to_spare_is_not_reported(self):
        # STRING WS-WORK-NAME DELIMITED BY SIZE INTO LK-FULL-ADDRESS puts at
        # most 60 characters into 70.
        result = _run_name_widening()
        self.assertFalse(_find(result, "var:FMTNAME::LK-FULL-ADDRESS"))
        self.assertFalse(_find(result, "var:CUSTUPD::WS-FULL-ADDRESS"))


_FIXTURE_CALLER = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. WIDTHS.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-A                     PIC X(30).
       01  WS-B                     PIC X(30).
       01  WS-PAIR                  PIC X(60).
       01  WS-TAIL                  PIC X(30).
       01  WS-GRP.
           05  G-A                  PIC X(30).
           05  G-B                  PIC X(30).
       01  WS-TBL.
           05  T-ENT                PIC X(30) OCCURS 10 TIMES.
       01  WS-IDX                   PIC S9(4) COMP.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT NAME_A, NAME_B INTO :WS-A, :WS-B
                 FROM T1 WHERE ID = 1
           END-EXEC
           STRING WS-A DELIMITED BY SIZE
                  '-' DELIMITED BY SIZE
                  WS-B DELIMITED BY SIZE INTO WS-PAIR END-STRING
           MOVE WS-A (11:) TO WS-TAIL
           MOVE WS-A TO G-A
           MOVE WS-B TO G-B
           MOVE WS-A TO T-ENT (WS-IDX)
           CALL 'WIDTHSUB' USING BY CONTENT 'X' WS-A
           GOBACK.
"""

_FIXTURE_CALLEE = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. WIDTHSUB.
       DATA DIVISION.
       LINKAGE SECTION.
       01  LK-FLAG                  PIC X(01).
       01  LK-NAME                  PIC X(30).
       PROCEDURE DIVISION USING LK-FLAG LK-NAME.
       0000-MAIN.
           GOBACK.
"""


class WidthArithmeticTests(unittest.TestCase):
    """Two columns widening together, through every combining shape."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "widths.pco").write_text(_FIXTURE_CALLER)
        (root / "widthsub.pco").write_text(_FIXTURE_CALLEE)
        cls.result = analyze(
            ChangeSpec(
                changes=[
                    build_change("T1", "NAME_A", "VARCHAR2(30)", "VARCHAR2(60)"),
                    build_change("T1", "NAME_B", "VARCHAR2(30)", "VARCHAR2(60)"),
                ],
                source_paths=[root],
                copybook_paths=[],
                source_patterns=["*.pco"],
            )
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_string_target_holds_every_source_plus_its_literals(self):
        # 60 + 1 + 60. Keeping only the larger increase gave 90.
        [finding] = _find(self.result, "var:WIDTHS::WS-PAIR")
        self.assertIn("X(121)", finding.remediation)

    def test_an_open_ended_slice_carries_the_rest_of_the_field(self):
        # WS-A (11:) is 50 characters once WS-A is 60.
        [finding] = _find(self.result, "var:WIDTHS::WS-TAIL")
        self.assertIn("X(50)", finding.remediation)

    def test_a_group_adds_up_its_members(self):
        [finding] = _layout(self.result, "var:WIDTHS::WS-GRP")
        self.assertEqual(finding.required, "120 bytes")

    def test_a_table_multiplies_by_its_occurs(self):
        [finding] = _layout(self.result, "var:WIDTHS::WS-TBL")
        self.assertEqual(finding.required, "600 bytes")

    def test_a_literal_argument_does_not_shift_the_rest(self):
        self.assertTrue(_find(self.result, "var:WIDTHSUB::LK-NAME"))
        self.assertFalse(_find(self.result, "var:WIDTHSUB::LK-FLAG"))


if __name__ == "__main__":
    unittest.main()
