"""SQL data type parsing and COBOL host-variable compatibility rules.

Oracle Pro*COBOL is the primary target (``.pco`` is its precompiler extension),
so the type vocabulary leans Oracle, but the common ANSI spellings used by DB2
and SQL Server are accepted too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Capacity, Kind

_TYPE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z0-9_ ]*?)\s*"
    r"(?:\(\s*(?P<p>\d+)\s*(?:,\s*(?P<s>-?\d+)\s*)?(?P<unit>BYTE|CHAR)?\s*\))?\s*$",
    re.IGNORECASE,
)

# Types whose length argument counts characters.
_TEXT_TYPES = {
    "VARCHAR2": 4000,
    "NVARCHAR2": 4000,
    "VARCHAR": 4000,
    "CHAR": 2000,
    "NCHAR": 2000,
    "CHARACTER": 2000,
    "CHARACTER VARYING": 4000,
    "STRING": 4000,
    "RAW": 2000,
    "LONG": 0,
    "TEXT": 0,
}

_NUMERIC_TYPES = {
    "NUMBER",
    "NUMERIC",
    "DECIMAL",
    "DEC",
    "FLOAT",
    "REAL",
    "DOUBLE PRECISION",
    "BINARY_FLOAT",
    "BINARY_DOUBLE",
}

# Integer aliases with a fixed implied precision.
_INTEGER_TYPES = {
    "INTEGER": 10,
    "INT": 10,
    "SMALLINT": 5,
    "BIGINT": 19,
    "TINYINT": 3,
}

_DATETIME_TYPES = {
    "DATE": 7,
    "TIMESTAMP": 11,
    "TIMESTAMP WITH TIME ZONE": 13,
    "TIMESTAMP WITH LOCAL TIME ZONE": 11,
    "DATETIME": 8,
    "TIME": 8,
    "INTERVAL": 11,
}

_LOB_TYPES = {"CLOB", "NCLOB", "BLOB", "BFILE", "LONG RAW", "XMLTYPE"}

# Oracle's default when NUMBER carries no precision at all.
_UNCONSTRAINED_NUMBER_DIGITS = 38


@dataclass(frozen=True)
class SqlType:
    """A parsed SQL column type."""

    raw: str
    base: str
    precision: int = 0
    scale: int = 0
    capacity: Capacity = Capacity()

    @property
    def is_text(self) -> bool:
        return self.base in _TEXT_TYPES

    @property
    def is_numeric(self) -> bool:
        return self.base in _NUMERIC_TYPES or self.base in _INTEGER_TYPES

    def render(self, capacity: Capacity) -> str:
        """This type respecified so it holds ``capacity``."""
        if self.is_text:
            return f"{self.base}({max(capacity.text_width, self.precision)})"
        if self.is_numeric:
            digits = max(capacity.total_digits, self.precision)
            scale = max(capacity.dec_digits, self.scale)
            if scale:
                return f"{self.base}({digits},{scale})"
            return f"{self.base}({digits})"
        return self.raw


def parse_sql_type(text: str) -> SqlType:
    """Parse ``VARCHAR2(30)``, ``NUMBER(9,2)``, ``DATE`` and friends."""
    raw = (text or "").strip().rstrip(",;")
    match = _TYPE_RE.match(raw)
    if not match:
        return SqlType(raw=raw, base=raw.upper(), capacity=Capacity())

    base = re.sub(r"\s+", " ", match.group("name") or "").strip().upper()
    precision = int(match.group("p")) if match.group("p") else 0
    scale = int(match.group("s")) if match.group("s") else 0

    if base in _LOB_TYPES:
        return SqlType(raw, base, precision, scale, Capacity(kind=Kind.LOB, chars=0))

    if base in _DATETIME_TYPES:
        chars = _DATETIME_TYPES[base]
        return SqlType(raw, base, precision, scale, Capacity(kind=Kind.DATETIME, chars=chars))

    if base in _TEXT_TYPES:
        chars = precision or _TEXT_TYPES[base] or 0
        return SqlType(raw, base, precision, scale, Capacity(kind=Kind.ALPHANUMERIC, chars=chars))

    if base in _INTEGER_TYPES:
        digits = precision or _INTEGER_TYPES[base]
        return SqlType(
            raw,
            base,
            precision,
            scale,
            Capacity(kind=Kind.NUMERIC, int_digits=digits, dec_digits=0, signed=True),
        )

    if base in _NUMERIC_TYPES:
        if precision == 0:
            digits = _UNCONSTRAINED_NUMBER_DIGITS
            return SqlType(
                raw,
                base,
                precision,
                scale,
                Capacity(kind=Kind.NUMERIC, int_digits=digits, dec_digits=0, signed=True),
            )
        int_digits = max(precision - max(scale, 0), 0)
        return SqlType(
            raw,
            base,
            precision,
            scale,
            Capacity(
                kind=Kind.NUMERIC,
                int_digits=int_digits,
                dec_digits=max(scale, 0),
                signed=True,
            ),
        )

    return SqlType(raw=raw, base=base, precision=precision, scale=scale, capacity=Capacity())


def host_variable_requirement(sql: SqlType) -> Capacity:
    """Capacity a COBOL host variable needs to round-trip this column safely.

    Numbers fetched into a ``PIC 9`` item need one digit per decimal digit; a
    signed column additionally needs a sign position, which the report surfaces
    as a separate finding rather than silently padding here.
    """
    cap = sql.capacity
    if cap.kind is Kind.DATETIME:
        # Pro*COBOL delivers DATE into an alphanumeric item; the default mask is
        # DD-MON-RR (9 characters) but shops routinely widen it.
        return Capacity(kind=Kind.ALPHANUMERIC, chars=max(cap.chars, 9))
    return cap


def describe_change(old: SqlType, new: SqlType) -> str:
    """Human sentence describing what the column change actually does."""
    if old.is_text and new.is_text:
        return f"character length {old.capacity.chars} -> {new.capacity.chars}"
    if old.is_numeric and new.is_numeric:
        return (
            f"precision {old.capacity.int_digits}.{old.capacity.dec_digits} -> "
            f"{new.capacity.int_digits}.{new.capacity.dec_digits}"
        )
    return f"{old.raw} -> {new.raw}"
