"""Embedded SQL parsing for Pro*COBOL ``EXEC SQL`` blocks.

The goal is not a complete SQL grammar; it is to answer one question reliably:
*which column is bound to which host variable, and in which direction*.  That is
what makes column-to-variable impact tracing possible.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import SourceRef


class Direction(str, enum.Enum):
    """Which way data moves between a column and a host variable."""

    OUT = "out"          # column -> host variable (SELECT INTO, FETCH INTO)
    IN = "in"            # host variable -> column (INSERT, UPDATE SET)
    PREDICATE = "pred"   # host variable compared against a column in WHERE


@dataclass
class Binding:
    """One column/host-variable pairing inside a SQL statement."""

    host_var: str
    column: str = ""
    table: str = ""
    direction: Direction = Direction.OUT
    indicator: str = ""
    expression: str = ""
    note: str = ""

    @property
    def column_key(self) -> str:
        if not self.column:
            return ""
        if self.table:
            return f"{self.table.upper()}.{self.column.upper()}"
        return self.column.upper()


@dataclass
class SqlStatement:
    """One ``EXEC SQL ... END-EXEC`` block, decomposed."""

    kind: str
    text: str
    ref: SourceRef
    tables: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    bindings: list[Binding] = field(default_factory=list)
    cursor: str = ""
    include_name: str = ""
    host_vars: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "tables": self.tables,
            "cursor": self.cursor,
            "bindings": [
                {
                    "host_var": b.host_var,
                    "column": b.column_key,
                    "direction": b.direction.value,
                    "expression": b.expression,
                    "note": b.note,
                }
                for b in self.bindings
            ],
            "source": self.ref.to_dict(),
        }


_HOST_VAR_RE = re.compile(r":\s*([A-Za-z][A-Za-z0-9_\-#@$]*(?:\s*\.\s*[A-Za-z][A-Za-z0-9_\-#@$]*)*)")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_IDENT = r"[A-Za-z][A-Za-z0-9_$#]*"
_QUALIFIED = rf"(?:{_IDENT}\s*\.\s*)?{_IDENT}"

_KEYWORDS = frozenset(
    """
    SELECT INSERT UPDATE DELETE MERGE DECLARE OPEN FETCH CLOSE COMMIT ROLLBACK
    EXECUTE PREPARE INCLUDE WHENEVER CONNECT BEGIN END VAR TYPE CALL SET ALTER
    CREATE DROP TRUNCATE LOCK SAVEPOINT CONTEXT ENABLE FREE ALLOCATE DESCRIBE
    """.split()
)

_NON_COLUMN_TOKENS = frozenset(
    """
    NULL SYSDATE SYSTIMESTAMP USER CURRENT_DATE CURRENT_TIMESTAMP DEFAULT
    AND OR NOT IN IS LIKE BETWEEN EXISTS FROM WHERE VALUES SET SELECT INTO
    """.split()
)


def strip_sql_comments(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    return _LINE_COMMENT_RE.sub(" ", text)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", strip_sql_comments(text)).strip()


def split_top_level(text: str, separator: str = ",") -> list[str]:
    """Split on ``separator`` while ignoring anything inside parentheses or quotes."""
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def find_keyword(text: str, keyword: str, start: int = 0) -> int:
    """Index of ``keyword`` at paren depth zero, or -1."""
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(keyword)}(?![A-Za-z0-9_])", re.I)
    depth = 0
    quote = ""
    for index, char in enumerate(text):
        if index < start:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(depth - 1, 0)
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif depth == 0:
            match = pattern.match(text, index)
            if match:
                return index
    return -1


def host_vars_in(text: str) -> list[str]:
    """All ``:HOST-VAR`` references in order of appearance."""
    return [re.sub(r"\s+", "", name).upper() for name in _HOST_VAR_RE.findall(text)]


def _host_var_pairs(fragment: str) -> list[tuple[str, str]]:
    """Split an INTO list into (host variable, indicator variable) pairs."""
    pairs: list[tuple[str, str]] = []
    for item in split_top_level(fragment):
        names = host_vars_in(item)
        if not names:
            continue
        indicator = names[1] if len(names) > 1 else ""
        pairs.append((names[0], indicator))
    return pairs


def _parse_from_clause(fragment: str) -> tuple[list[str], dict[str, str]]:
    """Table names and alias mapping from a FROM / UPDATE target list."""
    tables: list[str] = []
    aliases: dict[str, str] = {}
    for item in split_top_level(fragment):
        item = item.strip()
        if not item or item.startswith("("):
            continue
        words = [word for word in re.split(r"\s+", item) if word]
        if not words:
            continue
        table = words[0].split("@")[0].strip()
        if not re.fullmatch(_QUALIFIED, table):
            continue
        tables.append(table.upper())
        rest = [word for word in words[1:] if word.upper() != "AS"]
        if rest and re.fullmatch(_IDENT, rest[0]) and rest[0].upper() not in _KEYWORDS:
            aliases[rest[0].upper()] = table.upper()
        aliases.setdefault(table.upper(), table.upper())
    return tables, aliases


def _column_of(expression: str, aliases: dict[str, str], default_table: str) -> tuple[str, str, str]:
    """Resolve a select-list or SET item to (table, column, expression note)."""
    expr = expression.strip()
    # Strip a trailing column alias: "CUST_NAME AS NM" or "CUST_NAME NM".
    aliased = re.fullmatch(rf"({_QUALIFIED})\s+(?:AS\s+)?({_IDENT})", expr, re.I)
    if aliased and aliased.group(2).upper() not in _KEYWORDS:
        expr = aliased.group(1).strip()
    simple = re.fullmatch(rf"({_IDENT})\s*\.\s*({_IDENT})", expr)
    if simple:
        alias = simple.group(1).upper()
        return aliases.get(alias, default_table), simple.group(2).upper(), ""
    bare = re.fullmatch(_IDENT, expr)
    if bare and expr.upper() not in _NON_COLUMN_TOKENS:
        return default_table, expr.upper(), ""
    # An expression: report the columns it touches so the caller can widen them
    # all, but flag that the mapping is not one-to-one.
    return default_table, "", expr


def columns_in_expression(expr: str, aliases: dict[str, str], default_table: str) -> list[tuple[str, str]]:
    """Every column-looking identifier inside an expression."""
    found: list[tuple[str, str]] = []
    for match in re.finditer(rf"(?<![:.\w])({_IDENT})\s*\.\s*({_IDENT})|(?<![:.\w])({_IDENT})", expr):
        if match.group(1):
            table = aliases.get(match.group(1).upper(), default_table)
            found.append((table, match.group(2).upper()))
            continue
        name = (match.group(3) or "").upper()
        if not name or name in _NON_COLUMN_TOKENS or name in _KEYWORDS:
            continue
        # Skip function names, which are always followed by an open paren.
        tail = expr[match.end():].lstrip()
        if tail.startswith("("):
            continue
        found.append((default_table, name))
    return found


class SqlAnalyzer:
    """Parses EXEC SQL blocks, remembering cursor declarations along the way."""

    def __init__(self) -> None:
        self.cursors: dict[str, SqlStatement] = {}

    def parse(self, raw: str, ref: SourceRef) -> SqlStatement:
        text = normalize(raw)
        upper = text.upper()
        first = upper.split(" ", 1)[0] if upper else ""

        if upper.startswith("INCLUDE "):
            name = text.split(None, 1)[1].strip().strip("'\";")
            return SqlStatement(kind="INCLUDE", text=text, ref=ref, include_name=name)
        if upper.startswith("BEGIN DECLARE SECTION") or upper.startswith("END DECLARE SECTION"):
            return SqlStatement(kind="DECLARE-SECTION", text=text, ref=ref)
        if upper.startswith("DECLARE "):
            return self._parse_declare(text, ref)
        if upper.startswith("SELECT"):
            return self._parse_select(text, ref)
        if upper.startswith("INSERT"):
            return self._parse_insert(text, ref)
        if upper.startswith("UPDATE"):
            return self._parse_update(text, ref)
        if upper.startswith("DELETE"):
            return self._parse_delete(text, ref)
        if upper.startswith("FETCH"):
            return self._parse_fetch(text, ref)
        if upper.startswith("OPEN"):
            return self._parse_open(text, ref)

        statement = SqlStatement(kind=first or "UNKNOWN", text=text, ref=ref)
        statement.host_vars = host_vars_in(text)
        return statement

    # -- individual statement shapes -------------------------------------

    def _parse_declare(self, text: str, ref: SourceRef) -> SqlStatement:
        match = re.match(rf"DECLARE\s+({_IDENT})\s+(CURSOR|STATEMENT|TABLE)\b", text, re.I)
        if not match:
            return SqlStatement(kind="DECLARE", text=text, ref=ref)
        name = match.group(1).upper()
        what = match.group(2).upper()
        if what != "CURSOR":
            return SqlStatement(kind=f"DECLARE-{what}", text=text, ref=ref, cursor=name)
        for_index = find_keyword(text, "FOR")
        body = text[for_index + 3:].strip() if for_index != -1 else ""
        statement = self._parse_select(body, ref) if body.upper().startswith("SELECT") else SqlStatement(
            kind="DECLARE-CURSOR", text=text, ref=ref
        )
        statement.kind = "DECLARE-CURSOR"
        statement.cursor = name
        statement.text = text
        self.cursors[name] = statement
        return statement

    def _parse_select(self, text: str, ref: SourceRef) -> SqlStatement:
        statement = SqlStatement(kind="SELECT", text=text, ref=ref)
        into_index = find_keyword(text, "INTO")
        from_index = find_keyword(text, "FROM")

        select_end = into_index if 0 <= into_index < (from_index if from_index != -1 else len(text)) else from_index
        select_list = text[6:select_end if select_end != -1 else len(text)]

        if from_index != -1:
            where_index = find_keyword(text, "WHERE", from_index)
            tail_markers = [
                idx
                for idx in (
                    where_index,
                    find_keyword(text, "GROUP", from_index),
                    find_keyword(text, "ORDER", from_index),
                    find_keyword(text, "HAVING", from_index),
                    find_keyword(text, "FOR", from_index),
                )
                if idx != -1
            ]
            from_end = min(tail_markers) if tail_markers else len(text)
            tables, aliases = _parse_from_clause(text[from_index + 4 : from_end])
            statement.tables = tables
            statement.aliases = aliases
        else:
            where_index = find_keyword(text, "WHERE")

        default_table = statement.tables[0] if statement.tables else ""

        if into_index != -1:
            into_end = from_index if from_index > into_index else len(text)
            pairs = _host_var_pairs(text[into_index + 4 : into_end])
            items = split_top_level(select_list)
            for position, (host, indicator) in enumerate(pairs):
                expression = items[position].strip() if position < len(items) else ""
                if expression == "*":
                    statement.unresolved.append(
                        "SELECT * cannot be mapped to columns without table DDL"
                    )
                    continue
                table, column, expr_note = _column_of(expression, statement.aliases, default_table)
                binding = Binding(
                    host_var=host,
                    column=column,
                    table=table,
                    direction=Direction.OUT,
                    indicator=indicator,
                    expression=expr_note,
                )
                if expr_note:
                    binding.note = "value derived from an expression"
                statement.bindings.append(binding)

        if where_index != -1:
            statement.bindings.extend(
                _predicate_bindings(text[where_index:], statement.aliases, default_table)
            )
        statement.host_vars = host_vars_in(text)
        return statement

    def _parse_insert(self, text: str, ref: SourceRef) -> SqlStatement:
        statement = SqlStatement(kind="INSERT", text=text, ref=ref)
        match = re.match(rf"INSERT\s+INTO\s+({_QUALIFIED})", text, re.I)
        table = match.group(1).upper() if match else ""
        statement.tables = [table] if table else []
        statement.aliases = {table: table} if table else {}

        columns: list[str] = []
        after_table = match.end() if match else 0
        remainder = text[after_table:].lstrip()
        if remainder.startswith("("):
            close = _matching_paren(remainder, 0)
            columns = [col.strip().upper() for col in split_top_level(remainder[1:close])]
            remainder = remainder[close + 1 :].lstrip()

        values_index = find_keyword(remainder, "VALUES")
        if values_index != -1:
            list_start = remainder.find("(", values_index)
            if list_start != -1:
                close = _matching_paren(remainder, list_start)
                values = split_top_level(remainder[list_start + 1 : close])
                for position, value in enumerate(values):
                    names = host_vars_in(value)
                    if not names:
                        continue
                    column = columns[position] if position < len(columns) else ""
                    if not column and not columns:
                        statement.unresolved.append(
                            "INSERT without a column list; positional mapping needs table DDL"
                        )
                    statement.bindings.append(
                        Binding(
                            host_var=names[0],
                            column=column,
                            table=table,
                            direction=Direction.IN,
                            indicator=names[1] if len(names) > 1 else "",
                        )
                    )
        else:
            # INSERT ... SELECT: the select list feeds the column list.
            select_index = find_keyword(remainder, "SELECT")
            if select_index != -1:
                inner = self._parse_select(remainder[select_index:], ref)
                for position, item in enumerate(split_top_level(inner.text[6:])):
                    names = host_vars_in(item)
                    if not names:
                        continue
                    column = columns[position] if position < len(columns) else ""
                    statement.bindings.append(
                        Binding(
                            host_var=names[0],
                            column=column,
                            table=table,
                            direction=Direction.IN,
                        )
                    )
                statement.bindings.extend(inner.bindings)
                statement.tables.extend(inner.tables)
        statement.host_vars = host_vars_in(text)
        return statement

    def _parse_update(self, text: str, ref: SourceRef) -> SqlStatement:
        statement = SqlStatement(kind="UPDATE", text=text, ref=ref)
        set_index = find_keyword(text, "SET")
        target = text[6:set_index if set_index != -1 else len(text)]
        tables, aliases = _parse_from_clause(target)
        statement.tables = tables
        statement.aliases = aliases
        default_table = tables[0] if tables else ""

        where_index = find_keyword(text, "WHERE", max(set_index, 0))
        if set_index != -1:
            set_end = where_index if where_index != -1 else len(text)
            for assignment in split_top_level(text[set_index + 3 : set_end]):
                if "=" not in assignment:
                    continue
                lhs, rhs = assignment.split("=", 1)
                table, column, _ = _column_of(lhs, aliases, default_table)
                names = host_vars_in(rhs)
                if not names:
                    continue
                binding = Binding(
                    host_var=names[0],
                    column=column,
                    table=table,
                    direction=Direction.IN,
                    indicator=names[1] if len(names) > 1 else "",
                )
                if not re.fullmatch(r"\s*:\s*[A-Za-z][A-Za-z0-9_\-#@$]*\s*", rhs):
                    binding.expression = rhs.strip()
                    binding.note = "assigned through an expression"
                statement.bindings.append(binding)

        if where_index != -1:
            statement.bindings.extend(
                _predicate_bindings(text[where_index:], aliases, default_table)
            )
        statement.host_vars = host_vars_in(text)
        return statement

    def _parse_delete(self, text: str, ref: SourceRef) -> SqlStatement:
        statement = SqlStatement(kind="DELETE", text=text, ref=ref)
        match = re.match(rf"DELETE\s+(?:FROM\s+)?({_QUALIFIED})", text, re.I)
        table = match.group(1).upper() if match else ""
        statement.tables = [table] if table else []
        statement.aliases = {table: table} if table else {}
        where_index = find_keyword(text, "WHERE")
        if where_index != -1:
            statement.bindings.extend(
                _predicate_bindings(text[where_index:], statement.aliases, table)
            )
        statement.host_vars = host_vars_in(text)
        return statement

    def _parse_fetch(self, text: str, ref: SourceRef) -> SqlStatement:
        statement = SqlStatement(kind="FETCH", text=text, ref=ref)
        match = re.match(rf"FETCH\s+({_IDENT})", text, re.I)
        cursor = match.group(1).upper() if match else ""
        statement.cursor = cursor
        declaration = self.cursors.get(cursor)
        into_index = find_keyword(text, "INTO")
        if into_index == -1:
            return statement
        pairs = _host_var_pairs(text[into_index + 4 :])
        if declaration is None:
            statement.unresolved.append(f"cursor {cursor} declared outside the scanned sources")
            for host, indicator in pairs:
                statement.bindings.append(
                    Binding(host_var=host, direction=Direction.OUT, indicator=indicator)
                )
            return statement

        statement.tables = list(declaration.tables)
        select_list = _cursor_select_list(declaration)
        default_table = declaration.tables[0] if declaration.tables else ""
        for position, (host, indicator) in enumerate(pairs):
            expression = select_list[position] if position < len(select_list) else ""
            if expression == "*":
                statement.unresolved.append("cursor selects *; column mapping needs table DDL")
                continue
            table, column, expr_note = _column_of(expression, declaration.aliases, default_table)
            binding = Binding(
                host_var=host,
                column=column,
                table=table,
                direction=Direction.OUT,
                indicator=indicator,
                expression=expr_note,
            )
            if expr_note:
                binding.note = "value derived from an expression"
            statement.bindings.append(binding)
        return statement

    def _parse_open(self, text: str, ref: SourceRef) -> SqlStatement:
        statement = SqlStatement(kind="OPEN", text=text, ref=ref)
        match = re.match(rf"OPEN\s+({_IDENT})", text, re.I)
        cursor = match.group(1).upper() if match else ""
        statement.cursor = cursor
        declaration = self.cursors.get(cursor)
        if declaration is not None:
            statement.tables = list(declaration.tables)
            # USING host variables feed the cursor's WHERE clause predicates.
            for binding in declaration.bindings:
                if binding.direction is Direction.PREDICATE:
                    statement.bindings.append(binding)
        statement.host_vars = host_vars_in(text)
        return statement


def _cursor_select_list(declaration: SqlStatement) -> list[str]:
    body = declaration.text
    select_index = find_keyword(body, "SELECT")
    if select_index == -1:
        return []
    from_index = find_keyword(body, "FROM", select_index)
    end = from_index if from_index != -1 else len(body)
    return split_top_level(body[select_index + 6 : end])


def _matching_paren(text: str, open_index: int) -> int:
    depth = 0
    quote = ""
    for index in range(open_index, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(text) - 1


_PREDICATE_RE = re.compile(
    rf"({_QUALIFIED})\s*(=|<>|!=|>=|<=|>|<|(?:NOT\s+)?LIKE)\s*:\s*([A-Za-z][A-Za-z0-9_\-#@$]*)",
    re.I,
)
_PREDICATE_REVERSED_RE = re.compile(
    rf":\s*([A-Za-z][A-Za-z0-9_\-#@$]*)\s*(=|<>|!=|>=|<=|>|<)\s*({_QUALIFIED})",
    re.I,
)


def _predicate_bindings(
    where_text: str,
    aliases: dict[str, str],
    default_table: str,
) -> list[Binding]:
    """Host variables compared against columns in a WHERE clause."""
    bindings: list[Binding] = []
    for match in _PREDICATE_RE.finditer(where_text):
        table, column, _ = _column_of(match.group(1), aliases, default_table)
        if not column:
            continue
        bindings.append(
            Binding(
                host_var=match.group(3).upper(),
                column=column,
                table=table,
                direction=Direction.PREDICATE,
                note=f"compared with {match.group(2).upper()}",
            )
        )
    for match in _PREDICATE_REVERSED_RE.finditer(where_text):
        table, column, _ = _column_of(match.group(3), aliases, default_table)
        if not column:
            continue
        bindings.append(
            Binding(
                host_var=match.group(1).upper(),
                column=column,
                table=table,
                direction=Direction.PREDICATE,
                note=f"compared with {match.group(2).upper()}",
            )
        )
    return bindings


def dedupe_bindings(bindings: Iterable[Binding]) -> list[Binding]:
    seen: set[tuple[str, str, str]] = set()
    result: list[Binding] = []
    for binding in bindings:
        key = (binding.host_var, binding.column_key, binding.direction.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(binding)
    return result
