"""COBOL data description parsing: copybooks and inline WORKING-STORAGE.

Both feed the same :class:`DataMap`, because a host variable declared inline in
a program and one pulled in with ``COPY`` behave identically once the
precompiler has run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import cobolsrc
from . import progress as progress_mod
from .cobolsrc import LogicalLine, Sentence, normalize_name
from .models import Capacity, Field, Kind, SourceRef
from .picture import normalize_usage, parse_picture
from .progress import Progress

# COBOL names may contain hyphens, so "\b" is not enough to isolate a keyword:
# it happily matches COMP inside WS-COMP-CODE. These guards demand that no name
# character sits on either side of the keyword.
_L = r"(?<![A-Za-z0-9_-])"
_R = r"(?![A-Za-z0-9_-])"

_LEVEL_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z0-9][A-Za-z0-9_\-]*)(.*)$", re.DOTALL)
_PIC_RE = re.compile(
    rf"{_L}(?:PIC|PICTURE){_R}\s+(?:IS\s+)?(?P<pic>[^\s]+(?:\s*\(\s*\d+\s*\))?)", re.I
)
_OCCURS_RE = re.compile(rf"{_L}OCCURS{_R}\s+(?:(\d+)\s+TO\s+)?(\d+)", re.I)
_REDEFINES_RE = re.compile(rf"{_L}REDEFINES{_R}\s+([A-Za-z0-9][A-Za-z0-9_\-]*)", re.I)
_VALUE_RE = re.compile(
    rf"{_L}VALUES?{_R}\s+(?:IS\s+|ARE\s+)?"
    r"(?P<val>'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[^\s.]+)",
    re.I,
)
_USAGE_RE = re.compile(
    rf"{_L}(?:USAGE\s+(?:IS\s+)?)?"
    r"(COMPUTATIONAL-[12345]|COMPUTATIONAL|COMP-[12345]|COMP|PACKED-DECIMAL|BINARY|"
    rf"DISPLAY-1|DISPLAY|INDEX|POINTER|NATIONAL){_R}",
    re.I,
)
_SIGN_SEPARATE_RE = re.compile(rf"{_L}SIGN{_R}.*?{_L}SEPARATE{_R}", re.I)
_VARYING_RE = re.compile(rf"{_L}VARYING{_R}", re.I)
_DEPENDING_RE = re.compile(rf"{_L}DEPENDING{_R}\s+(?:ON\s+)?([A-Za-z0-9][A-Za-z0-9_\-]*)", re.I)

_COPY_RE = re.compile(
    r"(?<![A-Za-z0-9_-])COPY(?![A-Za-z0-9_-])\s+"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9_\-]*|'[^']+'|\"[^\"]+\")"
    r"(?:\s+(?:OF|IN)\s+[A-Za-z0-9][A-Za-z0-9_\-]*)?"
    r"(?P<rest>.*)$",
    re.I | re.DOTALL,
)
_REPLACING_PAIR_RE = re.compile(
    r"(==(?P<lhs>.*?)==|(?P<lhs2>[^\s]+))\s+BY\s+(==(?P<rhs>.*?)==|(?P<rhs2>[^\s]+))",
    re.I | re.DOTALL,
)

# Extensions that plausibly hold a copybook. The empty string keeps
# extensionless members, which mainframe-derived trees are full of.
_COPYBOOK_SUFFIXES = (".cpy", ".cbl", ".cob", ".inc", ".copy", ".cpb", ".cbk", ".src", "")

# Directories never worth walking when looking for a copybook.
_PRUNED_DIRS = frozenset(
    {
        ".git", ".svn", ".hg", ".bzr", "CVS",
        "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".idea", ".vscode", ".venv", "venv",
        "target", "build", "dist", ".gradle",
    }
)


@dataclass
class DataMap:
    """All data items known in one compilation scope."""

    fields: dict[str, Field] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    origin: str = ""

    def add(self, item: Field) -> None:
        key = normalize_name(item.name)
        if key == "FILLER":
            # FILLER is unaddressable; keep it for sizing under a unique key.
            key = f"FILLER#{len(self.order)}"
            item.name = key
        if key in self.fields:
            # Duplicate names across copybooks are common; keep the first and
            # record the second only for size purposes under a suffixed key.
            key = f"{key}#{len(self.order)}"
        self.fields[key] = item
        self.order.append(key)

    def get(self, name: str) -> Optional[Field]:
        return self.fields.get(normalize_name(name))

    def merge(self, other: "DataMap") -> None:
        for key in other.order:
            item = other.fields[key]
            if key not in self.fields:
                self.fields[key] = item
                self.order.append(key)
        self.roots.extend(other.roots)

    def iter_fields(self) -> Iterable[Field]:
        for key in self.order:
            yield self.fields[key]


class CopybookResolver:
    """Finds copybook files on a search path, the way a precompiler would.

    The index is built once, lazily, on the first lookup — and only over files
    that could plausibly be copybooks. Walking every file under a shop's source
    tree up front costs minutes on a network filesystem before a single program
    is parsed, which is pure waste when the run may not need a copybook at all.

    If a name misses the filtered index, one full unfiltered scan runs as a
    fallback, so an unusual extension still resolves rather than silently
    failing. That fallback happens at most once per resolver.
    """

    def __init__(
        self,
        search_paths: Sequence[Path],
        suffixes: Optional[Iterable[str]] = None,
        progress: Optional[Progress] = None,
    ) -> None:
        self.search_paths = [Path(path) for path in search_paths]
        self.suffixes = frozenset(
            suffix.lower() for suffix in (suffixes if suffixes is not None else _COPYBOOK_SUFFIXES)
        )
        self.progress = progress_mod.resolve(progress)
        self._index: Optional[dict[str, Path]] = None
        self._fallback_index: Optional[dict[str, Path]] = None
        self._cache: dict[str, Optional[Path]] = {}
        self.files_indexed = 0

    def resolve(self, name: str) -> Optional[Path]:
        key = name.strip().strip("'\"").upper()
        if key in self._cache:
            return self._cache[key]
        found = self._lookup(key, self._ensure_index())
        if found is None:
            found = self._lookup(key, self._ensure_fallback())
        self._cache[key] = found
        return found

    def _lookup(self, key: str, index: dict[str, Path]) -> Optional[Path]:
        candidate = index.get(key)
        if candidate is not None:
            return candidate
        for suffix in sorted(self.suffixes):
            if not suffix:
                continue
            candidate = index.get(f"{key}{suffix.upper()}")
            if candidate is not None:
                return candidate
        return None

    def _ensure_index(self) -> dict[str, Path]:
        if self._index is None:
            self.progress.stage(
                "indexing copybooks under "
                + ", ".join(str(path) for path in self.search_paths)
            )
            self._index = self._scan(self._is_candidate)
            self.files_indexed = len(self._index)
            self.progress.stage(f"indexed {len(self._index)} copybook name(s)")
        return self._index

    def _ensure_fallback(self) -> dict[str, Path]:
        if self._fallback_index is None:
            self.progress.stage(
                "a copybook was not found by extension; scanning all files once"
            )
            self._fallback_index = self._scan(lambda _path: True)
        return self._fallback_index

    def _is_candidate(self, path: Path) -> bool:
        return path.suffix.lower() in self.suffixes

    def _scan(self, accept) -> dict[str, Path]:
        index: dict[str, Path] = {}
        for base in self.search_paths:
            if not base.exists():
                continue
            for root, dirs, files in os.walk(base):
                # Pruning in place is what makes os.walk cheaper than rglob.
                dirs[:] = sorted(name for name in dirs if name not in _PRUNED_DIRS)
                for filename in sorted(files):
                    path = Path(root) / filename
                    if not accept(path):
                        continue
                    index.setdefault(path.stem.upper(), path)
                    index.setdefault(path.name.upper(), path)
        return index


def parse_data_sentences(
    sentences: Iterable[Sentence],
    origin: str = "",
    program: str = "",
) -> DataMap:
    """Build a :class:`DataMap` from DATA DIVISION sentences."""
    data = DataMap(origin=origin)
    stack: list[tuple[int, str]] = []  # (level, key)

    for sentence in sentences:
        text = sentence.text.strip()
        match = _LEVEL_RE.match(text)
        if not match:
            continue
        level = int(match.group(1))
        name = match.group(2)
        rest = match.group(3) or ""
        if level == 0 or level > 88:
            continue
        if name.upper() in ("SECTION", "DIVISION"):
            continue

        item = _build_field(level, name, rest, sentence, program)

        if level in (1, 77, 66):
            stack = []
        elif level == 88:
            # Condition names attach to the item above but hold no storage.
            if stack:
                item.parent = stack[-1][1]
        else:
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                item.parent = stack[-1][1]

        key_before = len(data.order)
        data.add(item)
        key = data.order[key_before]

        if item.parent:
            parent = data.fields.get(item.parent)
            if parent is not None and level != 88:
                parent.children.append(key)

        if level not in (88, 66):
            if level in (1, 77):
                data.roots.append(key)
                stack = [(level, key)]
            else:
                stack.append((level, key))

    finalize(data)
    return data


def _build_field(level: int, name: str, rest: str, sentence: Sentence, program: str) -> Field:
    clauses = rest

    pic_match = _PIC_RE.search(clauses)
    picture = pic_match.group("pic").strip() if pic_match else ""
    # A picture is written without spaces, but "PIC X (10)" appears in the wild.
    if pic_match and picture and "(" not in picture:
        tail = clauses[pic_match.end():].lstrip()
        if tail.startswith("("):
            close = tail.find(")")
            if close != -1:
                picture += tail[: close + 1].replace(" ", "")

    usage_match = _USAGE_RE.search(clauses)
    usage = normalize_usage(usage_match.group(1)) if usage_match else "DISPLAY"

    occurs_match = _OCCURS_RE.search(clauses)
    occurs = int(occurs_match.group(2)) if occurs_match else 0

    redefines_match = _REDEFINES_RE.search(clauses)
    redefines = normalize_name(redefines_match.group(1)) if redefines_match else ""

    value_match = _VALUE_RE.search(clauses)
    value = value_match.group("val") if value_match else ""

    sign_separate = bool(_SIGN_SEPARATE_RE.search(clauses))
    varying = bool(_VARYING_RE.search(clauses))

    info = parse_picture(picture, usage, sign_separate) if (picture or usage != "DISPLAY") else None

    ref = SourceRef(
        path=sentence.path,
        line=sentence.line_no,
        program=program,
        text=sentence.text.strip()[:200],
    )

    item = Field(
        name=normalize_name(name),
        level=level,
        picture=picture,
        usage=usage,
        occurs=occurs,
        redefines=redefines,
        value=value,
        sign_separate=sign_separate,
        varying=varying,
        source=ref,
    )
    if info is not None:
        item.capacity = info.capacity
        item.storage_bytes = info.storage_bytes
    if varying and info is not None:
        # Pro*COBOL VARCHAR: a 2-byte length prefix plus the character array.
        item.storage_bytes = info.storage_bytes + 2
    return item


def finalize(data: DataMap) -> None:
    """Fill in group sizes and qualified names once the tree is complete."""
    for key in reversed(data.order):
        item = data.fields[key]
        if item.level == 88:
            parent = data.fields.get(item.parent or "")
            if parent is not None:
                item.capacity = parent.capacity
                item.storage_bytes = parent.storage_bytes
            continue
        if item.children and not item.picture:
            total = 0
            for child_key in item.children:
                child = data.fields[child_key]
                if child.redefines or child.level == 88:
                    continue
                total += child.storage_bytes * max(child.occurs, 1)
            item.storage_bytes = total
            item.capacity = Capacity(kind=Kind.GROUP, chars=total)

    for key in data.order:
        item = data.fields[key]
        item.qualified = _qualified_name(data, key)


def _qualified_name(data: DataMap, key: str) -> str:
    parts: list[str] = []
    current: Optional[str] = key
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        item = data.fields.get(current)
        if item is None:
            break
        parts.append(item.name)
        current = item.parent
    return " OF ".join(parts)


def load_copybook(
    path: Path,
    program: str = "",
    replacing: Optional[Sequence[tuple[str, str]]] = None,
    source_format: str | None = None,
) -> DataMap:
    """Parse a copybook file, applying COPY ... REPLACING substitutions."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if replacing:
        text = apply_replacing(text, replacing)
    lines = cobolsrc.parse_lines(text.splitlines(), str(path), source_format)
    sentences = list(cobolsrc.iter_sentences(lines))
    return parse_data_sentences(sentences, origin=str(path), program=program)


def apply_replacing(text: str, pairs: Sequence[tuple[str, str]]) -> str:
    """Apply ``COPY ... REPLACING ==A== BY ==B==`` pseudo-text substitution."""
    result = text
    for lhs, rhs in pairs:
        if not lhs:
            continue
        result = re.sub(re.escape(lhs), rhs, result, flags=re.IGNORECASE)
    return result


def parse_copy_statement(text: str) -> Optional[tuple[str, list[tuple[str, str]]]]:
    """Pull the copybook name and REPLACING pairs out of a COPY statement."""
    match = _COPY_RE.search(text)
    if not match:
        return None
    name = match.group("name").strip().strip("'\"")
    rest = match.group("rest") or ""
    pairs: list[tuple[str, str]] = []
    if re.search(r"\bREPLACING\b", rest, re.I):
        replacing_text = re.split(r"\bREPLACING\b", rest, maxsplit=1, flags=re.I)[1]
        for pair in _REPLACING_PAIR_RE.finditer(replacing_text):
            lhs = pair.group("lhs")
            if lhs is None:
                lhs = pair.group("lhs2") or ""
            rhs = pair.group("rhs")
            if rhs is None:
                rhs = pair.group("rhs2") or ""
            pairs.append((lhs.strip(), rhs.strip()))
    return name, pairs


def field_capacity(item: Field) -> Capacity:
    """Capacity of a field, treating groups as fixed-width character areas."""
    if item.capacity.kind is Kind.UNKNOWN and item.storage_bytes:
        return Capacity(kind=Kind.GROUP, chars=item.storage_bytes)
    return item.capacity


def find_by_suffix(data: DataMap, name: str) -> list[Field]:
    """Look up a field allowing the ``A OF B`` qualified form."""
    target = normalize_name(name)
    if " OF " in target or " IN " in target:
        parts = re.split(r"\s+(?:OF|IN)\s+", target)
        leaf = parts[0]
        chain = parts[1:]
        matches: list[Field] = []
        for item in data.iter_fields():
            if item.name != leaf:
                continue
            qualified = item.qualified.split(" OF ")
            if all(link in qualified for link in chain):
                matches.append(item)
        return matches
    item = data.get(target)
    return [item] if item else []
