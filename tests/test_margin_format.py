"""Data items whose level number sits left of column 8.

Written like ``01   WV-DATA  PIC X(4096).`` starting in column 1, the level
number lands in the card-image sequence area and, read as fixed format, the
whole declaration is dropped - taking any REDEFINES on it with it. The reader
should recognise the shape as free format, and warn if the format was forced
to fixed anyway.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cobol_impact_analyzer.analyzer import analyze
from cobol_impact_analyzer.cobolsrc import FIXED, FREE, detect_format
from cobol_impact_analyzer.models import Severity
from cobol_impact_analyzer.spec import ChangeSpec, build_change

_COPYBOOK = """\
01           WV-DATA                    PIC X(4096).
01           WV-CUTBLA-VIEW REDEFINES WV-DATA.
      03        WV-CUTBLA-KEY.
               05     WV-VALUE       PIC S9(10)V9(2).
"""

_PROGRAM = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. MARGIN.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL BEGIN DECLARE SECTION END-EXEC.
           EXEC SQL INCLUDE MARGINCB END-EXEC.
           EXEC SQL END DECLARE SECTION END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL SELECT CB_VALUE INTO :WV-VALUE FROM CUTBLA END-EXEC
           STOP RUN.
"""


class DetectFormatTests(unittest.TestCase):
    def test_margin_level_numbers_read_as_free(self):
        self.assertEqual(detect_format(_COPYBOOK.splitlines()), FREE)

    def test_ordinary_fixed_card_image_still_reads_as_fixed(self):
        fixed = [
            "000100 IDENTIFICATION DIVISION.",
            "000200 PROGRAM-ID. X.",
            "       01  WS-REC.",
            "           05  WS-NAME     PIC X(30).",
        ]
        self.assertEqual(detect_format(fixed), FIXED)


class MarginRedefinesTests(unittest.TestCase):
    def _run(self, fmt: str | None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MARGINCB.cpy").write_text(_COPYBOOK, encoding="utf-8")
            (root / "margin.pco").write_text(_PROGRAM, encoding="utf-8")
            spec = ChangeSpec(
                changes=[build_change("CUTBLA", "CB_VALUE", "NUMBER(12,2)", "NUMBER(15,2)")],
                source_paths=[root],
                copybook_paths=[root],
                source_patterns=["*.pco"],
            )
            if fmt:
                spec.source_format = fmt
            return analyze(spec)

    def test_redefined_buffer_is_reported_on_autodetect(self):
        result = self._run(None)
        nodes = {f.node_id for f in result.findings}
        self.assertIn("var:MARGIN::WV-VALUE", nodes)
        # The whole point: the sized buffer the view redefines is reached.
        self.assertIn("var:MARGIN::WV-DATA", nodes)
        redefines = [
            (edge.source_id, edge.target_id)
            for edges in result.graph.out_edges.values()
            for edge in edges
            if edge.kind.value == "redefines"
        ]
        self.assertTrue(
            any("WV-DATA" in a and "WV-CUTBLA-VIEW" in b for a, b in redefines),
            redefines,
        )

    def test_margin_lines_are_salvaged_even_when_fixed_is_forced(self):
        # The level number left of column 8 is never valid card image, so the
        # declaration is recovered rather than dropped - with a warning.
        result = self._run(FIXED)
        self.assertIn(
            "var:MARGIN::WV-DATA", {f.node_id for f in result.findings}
        )
        self.assertTrue(
            any("column 8" in w for w in result.warnings),
            result.warnings,
        )


_BIG_BUFFER = """\
       01  WV-DATA                     PIC X(4096).
       01  WV-VIEW  REDEFINES  WV-DATA.
           03  WV-KEY.
               05  WV-VALUE            PIC S9(10)V9(2).
"""


class RedefinesNoteTests(unittest.TestCase):
    """A buffer big enough to still fit is named in a note, not left silent."""

    def _run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MARGINCB.cpy").write_text(_BIG_BUFFER, encoding="utf-8")
            (root / "margin.pco").write_text(_PROGRAM, encoding="utf-8")
            spec = ChangeSpec(
                changes=[build_change("CUTBLA", "CB_VALUE", "NUMBER(12,2)", "NUMBER(15,2)")],
                source_paths=[root],
                copybook_paths=[root],
                source_patterns=["*.pco"],
            )
            return analyze(spec)

    def test_record_layout_finding_names_the_redefined_buffer(self):
        result = self._run()
        layout = [
            f for f in result.findings
            if f.node_id == "var:MARGIN::WV-VIEW" and f.category == "record-layout"
        ]
        self.assertTrue(layout, [f.node_id for f in result.findings])
        joined = " ".join(layout[0].notes)
        self.assertIn("WV-DATA", joined)
        self.assertIn("still fits", joined)


if __name__ == "__main__":
    unittest.main()
