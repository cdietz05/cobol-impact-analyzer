import unittest

from cobol_impact_analyzer.models import Capacity, Kind
from cobol_impact_analyzer.picture import parse_picture, render_picture


class PictureTests(unittest.TestCase):
    def test_alphanumeric(self):
        info = parse_picture("X(30)")
        self.assertIs(info.capacity.kind, Kind.ALPHANUMERIC)
        self.assertEqual(info.capacity.chars, 30)
        self.assertEqual(info.storage_bytes, 30)

    def test_repeated_symbols_without_count(self):
        info = parse_picture("XXX")
        self.assertEqual(info.capacity.chars, 3)

    def test_signed_decimal_display(self):
        info = parse_picture("S9(7)V99")
        self.assertIs(info.capacity.kind, Kind.NUMERIC)
        self.assertEqual(info.capacity.int_digits, 7)
        self.assertEqual(info.capacity.dec_digits, 2)
        self.assertTrue(info.capacity.signed)
        self.assertEqual(info.storage_bytes, 9)

    def test_packed_decimal_bytes(self):
        info = parse_picture("S9(9)V99", "COMP-3")
        self.assertEqual(info.capacity.total_digits, 11)
        self.assertEqual(info.storage_bytes, 6)

    def test_binary_bytes_by_digit_count(self):
        self.assertEqual(parse_picture("S9(4)", "COMP").storage_bytes, 2)
        self.assertEqual(parse_picture("S9(9)", "COMP").storage_bytes, 4)
        self.assertEqual(parse_picture("S9(18)", "COMP").storage_bytes, 8)

    def test_index_and_pointer_carry_a_size_but_no_numeric_capacity(self):
        # An INDEX item is a subscript and a POINTER is a machine address.
        # Modelling either as a 15-digit signed number made every one of them
        # look like a huge value pouring into whatever field it touched, and
        # reported a truncation that cannot happen.
        for usage in ("INDEX", "POINTER"):
            info = parse_picture("", usage)
            self.assertIs(info.capacity.kind, Kind.UNKNOWN, usage)
            self.assertEqual(info.capacity.int_digits, 0, usage)
            self.assertGreater(info.storage_bytes, 0, f"{usage} still occupies storage")

    def test_an_unparseable_picture_does_not_inherit_a_usage_capacity(self):
        # _usage_only is also the fallback for a PICTURE the parser cannot
        # consume. It must not turn one of those into a confident number.
        for usage in ("DISPLAY", "COMP-3", "INDEX", "POINTER"):
            self.assertIs(parse_picture("()", usage).capacity.kind, Kind.UNKNOWN, usage)

    def test_floating_point_items_keep_their_significand(self):
        # COMP-1/COMP-2 do hold numbers, and a MOVE into a smaller field really
        # can lose precision, so these keep a digit count.
        self.assertEqual(parse_picture("", "COMP-1").capacity.int_digits, 7)
        self.assertEqual(parse_picture("", "COMP-2").capacity.int_digits, 15)
        self.assertEqual(parse_picture("", "COMP-2").storage_bytes, 8)

    def test_usage_synonyms(self):
        self.assertEqual(parse_picture("S9(5)", "PACKED-DECIMAL").storage_bytes, 3)
        self.assertEqual(parse_picture("S9(5)", "COMPUTATIONAL-3").storage_bytes, 3)

    def test_numeric_edited_display_width(self):
        info = parse_picture("ZZ,ZZZ,ZZ9.99-")
        self.assertIs(info.capacity.kind, Kind.NUMERIC_EDITED)
        # Z and 9 are both digit positions: ZZ + ZZZ + ZZ9 = 8 before the point.
        self.assertEqual(info.capacity.int_digits, 8)
        self.assertEqual(info.capacity.dec_digits, 2)
        # 10 digit positions, two commas, one point and one trailing sign.
        self.assertEqual(info.storage_bytes, 14)

    def test_trailing_sentence_period_is_not_part_of_the_picture(self):
        self.assertEqual(parse_picture("X(10).").capacity.chars, 10)
        self.assertEqual(parse_picture("ZZ9.99.").capacity.dec_digits, 2)

    def test_national_is_two_bytes_per_character(self):
        info = parse_picture("N(10)")
        self.assertIs(info.capacity.kind, Kind.NATIONAL)
        self.assertEqual(info.storage_bytes, 20)

    def test_render_alphanumeric(self):
        self.assertEqual(
            render_picture(Capacity(kind=Kind.ALPHANUMERIC, chars=60)), "X(60)"
        )

    def test_render_numeric_keeps_sign_and_scale(self):
        rendered = render_picture(
            Capacity(kind=Kind.NUMERIC, int_digits=11, dec_digits=2, signed=True)
        )
        self.assertEqual(rendered, "S9(11)V9(2)")


class CapacityTests(unittest.TestCase):
    def test_text_covers_shorter_text(self):
        wide = Capacity(kind=Kind.ALPHANUMERIC, chars=60)
        narrow = Capacity(kind=Kind.ALPHANUMERIC, chars=30)
        self.assertTrue(wide.covers(narrow))
        self.assertFalse(narrow.covers(wide))

    def test_numeric_needs_both_halves(self):
        target = Capacity(kind=Kind.NUMERIC, int_digits=9, dec_digits=0)
        value = Capacity(kind=Kind.NUMERIC, int_digits=9, dec_digits=2)
        self.assertFalse(target.covers(value))

    def test_numeric_into_alphanumeric_uses_text_width(self):
        target = Capacity(kind=Kind.ALPHANUMERIC, chars=9)
        value = Capacity(kind=Kind.NUMERIC, int_digits=7, dec_digits=2, signed=True)
        # 9 digits + decimal point + sign = 11 characters.
        self.assertFalse(target.covers(value))

    def test_unknown_never_reports_a_problem(self):
        self.assertTrue(Capacity().covers(Capacity(kind=Kind.ALPHANUMERIC, chars=999)))

    def test_grown_to_hold(self):
        grown = Capacity(kind=Kind.ALPHANUMERIC, chars=30).grown_to_hold(
            Capacity(kind=Kind.ALPHANUMERIC, chars=60)
        )
        self.assertEqual(grown.chars, 60)


if __name__ == "__main__":
    unittest.main()
