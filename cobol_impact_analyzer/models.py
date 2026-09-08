"""Core data model shared by every stage of the analyzer.

The central abstraction is :class:`Capacity`: a medium-independent description of
"how much data can this thing hold".  A COBOL ``PIC X(30)`` field, an Oracle
``VARCHAR2(30)`` column and a literal ``'ABC'`` all reduce to a ``Capacity``,
which lets the impact engine compare storage across language boundaries without
special-casing every pair.
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional


class Kind(str, enum.Enum):
    """Broad storage class of a field or column."""

    ALPHANUMERIC = "alphanumeric"
    NUMERIC = "numeric"
    NUMERIC_EDITED = "numeric-edited"
    ALPHANUMERIC_EDITED = "alphanumeric-edited"
    NATIONAL = "national"
    GROUP = "group"
    DATETIME = "datetime"
    LOB = "lob"
    UNKNOWN = "unknown"

    def is_numeric(self) -> bool:
        return self in (Kind.NUMERIC, Kind.NUMERIC_EDITED)

    def is_textual(self) -> bool:
        return self in (
            Kind.ALPHANUMERIC,
            Kind.ALPHANUMERIC_EDITED,
            Kind.NATIONAL,
            Kind.GROUP,
        )


@dataclass(frozen=True)
class Capacity:
    """How much data a field or column can hold.

    ``chars`` is the number of character positions available for textual data.
    ``int_digits``/``dec_digits`` describe numeric precision.  A numeric field
    also reports a ``chars`` value, because COBOL happily moves a numeric field
    into an alphanumeric one and vice versa.
    """

    kind: Kind = Kind.UNKNOWN
    chars: int = 0
    int_digits: int = 0
    dec_digits: int = 0
    signed: bool = False

    @property
    def total_digits(self) -> int:
        return self.int_digits + self.dec_digits

    @property
    def text_width(self) -> int:
        """Character positions needed to render this value as text."""
        if self.kind.is_numeric():
            width = self.total_digits
            if self.dec_digits:
                width += 1  # decimal point when rendered
            if self.signed:
                width += 1
            return max(width, self.chars)
        return self.chars

    def covers(self, other: "Capacity") -> bool:
        """True when a value described by ``other`` fits here without loss."""
        if other.kind is Kind.UNKNOWN or self.kind is Kind.UNKNOWN:
            return True  # cannot prove a problem; do not cry wolf
        if self.kind.is_numeric() and other.kind.is_numeric():
            return (
                self.int_digits >= other.int_digits
                and self.dec_digits >= other.dec_digits
            )
        if self.kind is Kind.LOB:
            return True
        return self.chars >= other.text_width

    def grown_to_hold(self, other: "Capacity") -> "Capacity":
        """This capacity, enlarged just enough to hold ``other``."""
        if self.covers(other):
            return self
        if self.kind.is_numeric() and other.kind.is_numeric():
            return Capacity(
                kind=self.kind,
                chars=0,
                int_digits=max(self.int_digits, other.int_digits),
                dec_digits=max(self.dec_digits, other.dec_digits),
                signed=self.signed or other.signed,
            )
        return Capacity(
            kind=self.kind,
            chars=max(self.chars, other.text_width),
            int_digits=self.int_digits,
            dec_digits=self.dec_digits,
            signed=self.signed,
        )

    def describe(self) -> str:
        if self.kind.is_numeric():
            body = f"{self.int_digits}"
            if self.dec_digits:
                body += f".{self.dec_digits}"
            sign = "signed " if self.signed else ""
            return f"{sign}numeric({body})"
        if self.kind is Kind.UNKNOWN:
            return "unknown"
        return f"{self.kind.value}({self.chars})"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.describe()


UNKNOWN_CAPACITY = Capacity()


def widest(caps: Iterable[Capacity]) -> Capacity:
    """Smallest capacity that covers every capacity in ``caps``."""
    result: Optional[Capacity] = None
    for cap in caps:
        if result is None:
            result = cap
            continue
        result = result.grown_to_hold(cap)
    return result if result is not None else UNKNOWN_CAPACITY


class Severity(str, enum.Enum):
    """How badly a finding will hurt if it is ignored."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def rank(self) -> int:
        order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
            Severity.INFO: 4,
        }
        return order[self]


@dataclass
class SourceRef:
    """Where in the source tree something was found."""

    path: str
    line: int
    program: str = ""
    paragraph: str = ""
    text: str = ""

    def location(self) -> str:
        """``path:line``, shortened to a relative path when that is readable.

        Reports are read next to the source tree, so an absolute path adds noise
        without adding information. The raw path is preserved in ``to_dict`` for
        machine consumers.
        """
        display = self.path
        try:
            relative = os.path.relpath(self.path)
            if not relative.startswith(".."):
                display = relative
        except (ValueError, OSError):
            pass  # different drive on Windows, or a path that no longer resolves
        return f"{display}:{self.line}"

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "program": self.program,
            "paragraph": self.paragraph,
            "text": self.text,
        }


@dataclass
class Field:
    """One data item declared in a copybook or in WORKING-STORAGE."""

    name: str
    level: int
    picture: str = ""
    usage: str = "DISPLAY"
    occurs: int = 0
    redefines: str = ""
    value: str = ""
    sign_separate: bool = False
    varying: bool = False
    parent: Optional[str] = None
    children: list[str] = field(default_factory=list)
    capacity: Capacity = UNKNOWN_CAPACITY
    storage_bytes: int = 0
    source: Optional[SourceRef] = None
    qualified: str = ""

    @property
    def is_group(self) -> bool:
        return bool(self.children) and not self.picture

    @property
    def is_condition(self) -> bool:
        return self.level == 88

    def declaration(self) -> str:
        """Reconstructed COBOL declaration, used in report output."""
        parts = [f"{self.level:02d}", self.name]
        if self.redefines:
            parts.append(f"REDEFINES {self.redefines}")
        if self.occurs:
            parts.append(f"OCCURS {self.occurs}")
        if self.picture:
            parts.append(f"PIC {self.picture}")
        if self.usage and self.usage != "DISPLAY":
            parts.append(self.usage)
        if self.varying:
            parts.append("VARYING")
        return " ".join(parts) + "."

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "level": self.level,
            "picture": self.picture,
            "usage": self.usage,
            "occurs": self.occurs,
            "redefines": self.redefines,
            "parent": self.parent,
            "capacity": self.capacity.describe(),
            "storage_bytes": self.storage_bytes,
            "declaration": self.declaration(),
            "source": self.source.to_dict() if self.source else None,
        }


@dataclass
class Column:
    """One column of a SQL table, as declared in the change spec or inferred."""

    table: str
    name: str
    sql_type: str
    capacity: Capacity = UNKNOWN_CAPACITY

    @property
    def key(self) -> str:
        return f"{self.table.upper()}.{self.name.upper()}"


@dataclass
class ColumnChange:
    """A requested widening of one column."""

    table: str
    column: str
    old_type: str
    new_type: str
    old_capacity: Capacity = UNKNOWN_CAPACITY
    new_capacity: Capacity = UNKNOWN_CAPACITY

    @property
    def key(self) -> str:
        return f"{self.table.upper()}.{self.column.upper()}"

    def is_widening(self) -> bool:
        return not self.old_capacity.covers(self.new_capacity)

    def alter_statement(self) -> str:
        return (
            f"ALTER TABLE {self.table.upper()} "
            f"MODIFY {self.column.upper()} {self.new_type};"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "table": self.table.upper(),
            "column": self.column.upper(),
            "old_type": self.old_type,
            "new_type": self.new_type,
            "old_capacity": self.old_capacity.describe(),
            "new_capacity": self.new_capacity.describe(),
            "alter": self.alter_statement(),
        }


class EdgeKind(str, enum.Enum):
    """Why data flows from one node to another."""

    SQL_FETCH = "sql-fetch"          # column -> host variable (SELECT/FETCH INTO)
    SQL_BIND = "sql-bind"            # host variable -> column (INSERT/UPDATE)
    SQL_PREDICATE = "sql-predicate"  # host variable compared against a column
    MOVE = "move"
    STRING = "string"
    UNSTRING = "unstring"
    COMPUTE = "compute"
    ARITHMETIC = "arithmetic"
    CALL_ARG = "call-arg"
    WRITE_FROM = "write-from"
    READ_INTO = "read-into"
    COMPARE = "compare"
    GROUP_PARENT = "group-parent"
    REDEFINES = "redefines"
    INITIALIZE = "initialize"
    DISPLAY = "display"
    REFMOD = "reference-modification"

    @property
    def truncates(self) -> bool:
        """Edges where an undersized destination silently loses data."""
        return self in (
            EdgeKind.SQL_FETCH,
            EdgeKind.MOVE,
            EdgeKind.STRING,
            EdgeKind.UNSTRING,
            EdgeKind.COMPUTE,
            EdgeKind.ARITHMETIC,
            EdgeKind.WRITE_FROM,
            EdgeKind.READ_INTO,
            EdgeKind.REFMOD,
        )


@dataclass
class Edge:
    """A directed data-flow edge discovered in the source."""

    source_id: str
    target_id: str
    kind: EdgeKind
    ref: SourceRef
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "from": self.source_id,
            "to": self.target_id,
            "kind": self.kind.value,
            "note": self.note,
            "source": self.ref.to_dict(),
        }


class NodeKind(str, enum.Enum):
    COLUMN = "column"
    VARIABLE = "variable"
    FILE_RECORD = "file-record"
    PROGRAM_ARG = "program-arg"
    LITERAL = "literal"


@dataclass
class Node:
    """A vertex of the data-flow graph: a column, a variable, or a record."""

    node_id: str
    kind: NodeKind
    name: str
    capacity: Capacity = UNKNOWN_CAPACITY
    declared_in: Optional[SourceRef] = None
    field_ref: Optional[Field] = None
    programs: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "kind": self.kind.value,
            "name": self.name,
            "capacity": self.capacity.describe(),
            "programs": sorted(self.programs),
            "declared_in": self.declared_in.to_dict() if self.declared_in else None,
        }


@dataclass
class Finding:
    """One thing a human has to do (or at least look at) before shipping."""

    severity: Severity
    category: str
    node_id: str
    title: str
    detail: str
    current: str = ""
    required: str = ""
    remediation: str = ""
    distance: int = 0
    path: list[str] = field(default_factory=list)
    refs: list[SourceRef] = field(default_factory=list)
    # Other ways this same field is used that the widening disturbs - a REFMOD
    # with a hard-coded length, a VALUE clause, a literal comparison. Kept as
    # separate lines rather than concatenated into `detail`, so each renderer
    # can lay them out: the text report gives them a line each, HTML a list.
    # Squashing them into one string made a single cell hundreds of characters
    # wide and pushed everything after it off the page.
    notes: list[str] = field(default_factory=list)

    def sort_key(self) -> tuple[int, int, str]:
        return (self.severity.rank, self.distance, self.node_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity.value,
            "category": self.category,
            "node": self.node_id,
            "title": self.title,
            "detail": self.detail,
            "notes": self.notes,
            "current": self.current,
            "required": self.required,
            "remediation": self.remediation,
            "hops_from_change": self.distance,
            "propagation_path": self.path,
            "references": [ref.to_dict() for ref in self.refs],
        }
