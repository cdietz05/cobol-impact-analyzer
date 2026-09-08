import unittest

from cobol_impact_analyzer.models import Kind
from cobol_impact_analyzer.sqltypes import host_variable_requirement, parse_sql_type


class SqlTypeTests(unittest.TestCase):
    def test_varchar2(self):
        sql = parse_sql_type("VARCHAR2(30)")
        self.assertEqual(sql.base, "VARCHAR2")
        self.assertIs(sql.capacity.kind, Kind.ALPHANUMERIC)
        self.assertEqual(sql.capacity.chars, 30)

    def test_varchar2_char_semantics_suffix(self):
        self.assertEqual(parse_sql_type("VARCHAR2(30 CHAR)").capacity.chars, 30)

    def test_number_with_scale(self):
        sql = parse_sql_type("NUMBER(11,2)")
        self.assertEqual(sql.capacity.int_digits, 9)
        self.assertEqual(sql.capacity.dec_digits, 2)

    def test_number_without_precision_is_unconstrained(self):
        self.assertEqual(parse_sql_type("NUMBER").capacity.int_digits, 38)

    def test_integer_alias(self):
        self.assertEqual(parse_sql_type("INTEGER").capacity.int_digits, 10)

    def test_date_needs_an_alphanumeric_host_variable(self):
        requirement = host_variable_requirement(parse_sql_type("DATE"))
        self.assertIs(requirement.kind, Kind.ALPHANUMERIC)
        self.assertGreaterEqual(requirement.chars, 9)

    def test_clob_is_treated_as_unbounded(self):
        sql = parse_sql_type("CLOB")
        self.assertIs(sql.capacity.kind, Kind.LOB)

    def test_render_widens_in_place(self):
        sql = parse_sql_type("VARCHAR2(30)")
        widened = parse_sql_type("VARCHAR2(60)").capacity
        self.assertEqual(sql.render(widened), "VARCHAR2(60)")


if __name__ == "__main__":
    unittest.main()
