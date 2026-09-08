import unittest
from pathlib import Path

from cobol_impact_analyzer import cobolsrc
from cobol_impact_analyzer.copybook import (
    CopybookResolver,
    load_copybook,
    parse_copy_statement,
    parse_data_sentences,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _parse(text: str):
    lines = cobolsrc.parse_lines(text.splitlines(), "memory.cpy")
    return parse_data_sentences(cobolsrc.iter_sentences(lines))


class SourceFormatTests(unittest.TestCase):
    def test_fixed_format_strips_sequence_and_identification_areas(self):
        # Columns 1-6 sequence, 7 indicator, 8-72 code, 73-80 identification.
        raw = ["000100".ljust(7) + "01  WS-A     PIC X(10).".ljust(65) + "SEQ00010"]
        self.assertEqual(len(raw[0]), 80)
        lines = cobolsrc.parse_lines(raw, "x.cbl", "fixed")
        self.assertEqual(lines[0].text.strip(), "01  WS-A     PIC X(10).")

    def test_comment_lines_are_flagged(self):
        raw = ["      * THIS IS A COMMENT", "       01  WS-A PIC X."]
        lines = cobolsrc.parse_lines(raw, "x.cbl", "fixed")
        self.assertTrue(lines[0].is_comment)
        self.assertFalse(lines[1].is_comment)

    def test_period_inside_a_picture_does_not_end_a_sentence(self):
        lines = cobolsrc.parse_lines(["       05  WS-A PIC ZZ9.99."], "x.cbl", "fixed")
        sentences = list(cobolsrc.iter_sentences(lines))
        self.assertEqual(len(sentences), 1)
        self.assertEqual(sentences[0].text, "05  WS-A PIC ZZ9.99")


class CopybookParsingTests(unittest.TestCase):
    def test_hierarchy_and_group_size(self):
        data = _parse(
            "       01  REC.\n"
            "           05  A   PIC X(10).\n"
            "           05  B   PIC S9(5)  COMP-3.\n"
        )
        record = data.get("REC")
        self.assertIsNotNone(record)
        self.assertEqual(len(record.children), 2)
        # 10 bytes of A plus 3 bytes of packed B.
        self.assertEqual(record.storage_bytes, 13)

    def test_occurs_multiplies_group_size(self):
        data = _parse(
            "       01  REC.\n"
            "           05  T   PIC X(4)  OCCURS 5 TIMES.\n"
        )
        self.assertEqual(data.get("REC").storage_bytes, 20)

    def test_redefines_does_not_add_to_the_group(self):
        data = _parse(
            "       01  REC.\n"
            "           05  A   PIC X(10).\n"
            "           05  A-R REDEFINES A  PIC 9(10).\n"
        )
        self.assertEqual(data.get("REC").storage_bytes, 10)
        self.assertEqual(data.get("A-R").redefines, "A")

    def test_keyword_is_not_matched_inside_a_data_name(self):
        data = _parse("       05  WS-COMP-CODE  PIC X(4).\n")
        self.assertEqual(data.get("WS-COMP-CODE").usage, "DISPLAY")

    def test_condition_names_are_kept_but_hold_no_storage_of_their_own(self):
        data = _parse(
            "       01  WS-FLAGS.\n"
            "           05  WS-EOF-FLAG  PIC X(01)  VALUE 'N'.\n"
            "               88  WS-EOF               VALUE 'Y'.\n"
        )
        self.assertEqual(data.get("WS-FLAGS").storage_bytes, 1)
        self.assertEqual(data.get("WS-EOF").level, 88)

    def test_ibm_name_characters_are_accepted(self):
        # "#", "@" and "$" are legal in IBM COBOL user-defined words.
        data = _parse(
            "       01  CU01TB01-REC.\n"
            "           05  CUST#NAME    PIC X(30).\n"
            "           05  CUST@ID      PIC S9(9)  COMP-3.\n"
        )
        self.assertEqual(data.get("CUST#NAME").capacity.chars, 30)
        self.assertEqual(data.get("CUST@ID").usage, "COMP-3")
        self.assertEqual(len(data.get("CU01TB01-REC").children), 2)

    def test_filler_keys_cannot_collide_with_a_real_hash_name(self):
        data = _parse(
            "       01  REC.\n"
            "           05  FILLER   PIC X(2).\n"
            "           05  A#1      PIC X(3).\n"
        )
        # The synthetic FILLER key uses "!", which is not legal in a COBOL word.
        self.assertIsNotNone(data.get("A#1"))
        self.assertEqual(data.get("A#1").capacity.chars, 3)
        self.assertEqual(data.get("REC").storage_bytes, 5)

    def test_qualified_names(self):
        data = _parse(
            "       01  REC.\n"
            "           05  GRP.\n"
            "               10  A  PIC X(3).\n"
        )
        self.assertEqual(data.get("A").qualified, "A OF GRP OF REC")


class CopyStatementTests(unittest.TestCase):
    def test_plain_copy(self):
        parsed = parse_copy_statement("COPY WSCOMMON")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], "WSCOMMON")
        self.assertEqual(parsed[1], [])

    def test_copy_replacing(self):
        parsed = parse_copy_statement("COPY CUSTREC REPLACING ==:PFX:== BY ==WS==")
        self.assertEqual(parsed[0], "CUSTREC")
        self.assertEqual(parsed[1], [(":PFX:", "WS")])

    def test_copy_keyword_inside_a_name_is_ignored(self):
        self.assertIsNone(parse_copy_statement("MOVE WS-COPY-FLAG TO WS-A"))


class ResolverTests(unittest.TestCase):
    def test_finds_a_copybook_by_stem(self):
        resolver = CopybookResolver([EXAMPLES / "copybooks"])
        found = resolver.resolve("CUSTOMER")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "CUSTOMER.cpy")

    def test_example_copybook_parses(self):
        resolver = CopybookResolver([EXAMPLES / "copybooks"])
        data = load_copybook(resolver.resolve("CUSTOMER"))
        self.assertEqual(data.get("CUST-NAME").capacity.chars, 30)
        self.assertEqual(data.get("CUST-ID").usage, "COMP-3")
        self.assertEqual(data.get("CUST-BALANCE").capacity.dec_digits, 2)


if __name__ == "__main__":
    unittest.main()
