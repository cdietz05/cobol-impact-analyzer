"""The change specification: what the user wants to widen, and where to look."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from .models import ColumnChange
from .sqltypes import parse_sql_type


class SpecError(ValueError):
    """Raised when a change spec cannot be understood."""


@dataclass
class ChangeSpec:
    """Everything the analyzer needs for one run."""

    changes: list[ColumnChange] = field(default_factory=list)
    source_paths: list[Path] = field(default_factory=list)
    copybook_paths: list[Path] = field(default_factory=list)
    source_patterns: list[str] = field(default_factory=lambda: ["*.pco"])
    copybook_suffixes: list[str] = field(default_factory=list)
    source_format: Optional[str] = None
    max_depth: int = 0  # 0 means unlimited
    global_variable_scope: bool = False

    @property
    def tables(self) -> list[str]:
        seen: list[str] = []
        for change in self.changes:
            if change.table.upper() not in seen:
                seen.append(change.table.upper())
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "changes": [change.to_dict() for change in self.changes],
            "source_paths": [str(path) for path in self.source_paths],
            "copybook_paths": [str(path) for path in self.copybook_paths],
            "source_patterns": self.source_patterns,
            "copybook_suffixes": self.copybook_suffixes,
            "max_depth": self.max_depth,
        }


def build_change(table: str, column: str, old_type: str, new_type: str) -> ColumnChange:
    old = parse_sql_type(old_type)
    new = parse_sql_type(new_type)
    return ColumnChange(
        table=table.strip().upper(),
        column=column.strip().upper(),
        old_type=old.raw,
        new_type=new.raw,
        old_capacity=old.capacity,
        new_capacity=new.capacity,
    )


def load_spec(path: Path) -> ChangeSpec:
    """Read a JSON change spec.

    Two shapes are accepted.  The flat one names a single table::

        {"table": "CUSTOMER",
         "columns": [{"name": "CUST_NAME", "from": "VARCHAR2(30)", "to": "VARCHAR2(60)"}]}

    The general one lists changes across tables::

        {"changes": [{"table": "CUSTOMER", "column": "CUST_NAME",
                      "from": "VARCHAR2(30)", "to": "VARCHAR2(60)"}]}
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SpecError(f"{path}: invalid JSON ({error})") from error
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: top level must be an object")

    base = path.parent
    spec = ChangeSpec()

    entries: list[dict[str, Any]] = []
    if "changes" in raw:
        entries = list(raw.get("changes") or [])
    if "columns" in raw:
        table = raw.get("table")
        if not table:
            raise SpecError(f"{path}: 'columns' requires a top-level 'table'")
        for column in raw.get("columns") or []:
            merged = dict(column)
            merged.setdefault("table", table)
            entries.append(merged)
    if not entries:
        raise SpecError(f"{path}: no changes found (expected 'changes' or 'table'+'columns')")

    for entry in entries:
        table = entry.get("table")
        column = entry.get("column") or entry.get("name")
        old_type = entry.get("from") or entry.get("old_type") or entry.get("old")
        new_type = entry.get("to") or entry.get("new_type") or entry.get("new")
        missing = [
            label
            for label, value in (
                ("table", table),
                ("column/name", column),
                ("from/old_type", old_type),
                ("to/new_type", new_type),
            )
            if not value
        ]
        if missing:
            raise SpecError(f"{path}: change entry {entry!r} is missing {', '.join(missing)}")
        spec.changes.append(build_change(str(table), str(column), str(old_type), str(new_type)))

    spec.source_paths = _paths(raw.get("source_paths") or raw.get("sources") or [], base)
    spec.copybook_paths = _paths(raw.get("copybook_paths") or raw.get("copybooks") or [], base)
    patterns = raw.get("source_patterns")
    if patterns:
        spec.source_patterns = [str(pattern) for pattern in patterns]
    suffixes = raw.get("copybook_suffixes")
    if suffixes:
        spec.copybook_suffixes = [
            suffix if suffix.startswith(".") or suffix == "" else f".{suffix}"
            for suffix in (str(value).lower() for value in suffixes)
        ]
    source_format = raw.get("source_format")
    if source_format:
        spec.source_format = str(source_format).lower()
    spec.max_depth = int(raw.get("max_depth") or 0)
    spec.global_variable_scope = bool(raw.get("global_variable_scope"))
    return spec


def _paths(values: Sequence[Any], base: Path) -> list[Path]:
    result: list[Path] = []
    for value in values:
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = (base / candidate).resolve()
        result.append(candidate)
    return result
