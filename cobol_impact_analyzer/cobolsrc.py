"""Reading COBOL source text: fixed vs free format, comments, continuations.

Every downstream parser works on :class:`LogicalLine` values so none of them has
to care whether the shop writes card-image fixed format with sequence numbers in
columns 1-6 or modern free format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

FIXED = "fixed"
FREE = "free"

# IBM COBOL allows "#", "@" and "$" inside user-defined words, and mainframe
# shops use them freely in copybook and field names. Because a plain "\b" would
# then match a keyword sitting inside a name, every keyword pattern in this
# package brackets itself with these two guards instead.
NAME_CHARS = r"A-Za-z0-9_\-#@$"
COBOL_NAME = r"[A-Za-z0-9][A-Za-z0-9_\-#@$]*"
BOUNDARY_L = r"(?<![A-Za-z0-9_\-#@$])"
BOUNDARY_R = r"(?![A-Za-z0-9_\-#@$])"

_SEQ_AREA = slice(0, 6)
_INDICATOR = 6
_CODE_AREA = slice(7, 72)

_COMMENT_INDICATORS = frozenset("*/")
_DEBUG_INDICATOR = "D"
_CONTINUATION_INDICATOR = "-"

# A level number whose digits begin left of column 8, followed by whitespace and
# the start of a name. Card-image fixed format keeps only columns 8-72, so a
# data item written like this - "01   WV-DATA  PIC X." starting in column 1 -
# has its level number sitting in the sequence-number area and is silently lost
# when the file is read as fixed. Seeing these is a strong free-format signal,
# and worth a warning when the format was forced to fixed anyway.
_MARGIN_LEVEL_RE = re.compile(r"^ {0,6}(\d{1,2})[ \t]+[A-Za-z0-9]")
_CODE_HAS_LEVEL_RE = re.compile(r"^\s*\d{1,2}[ \t.]")


def _is_margin_declaration(line: str) -> bool:
    """True when ``line`` starts a data item with its level left of column 8."""
    match = _MARGIN_LEVEL_RE.match(line)
    if match is None:
        return False
    level = int(match.group(1))
    return 1 <= level <= 49 or level in (66, 77, 88)


@dataclass
class LogicalLine:
    """One line of code with the noise stripped off."""

    text: str
    line_no: int
    path: str
    is_comment: bool = False
    is_continuation: bool = False
    raw: str = ""

    def upper(self) -> str:
        return self.text.upper()


@dataclass
class Sentence:
    """A COBOL sentence: text up to a terminating period."""

    text: str
    line_no: int
    end_line: int
    path: str
    lines: list[LogicalLine] = field(default_factory=list)

    def tokens(self) -> list[str]:
        return tokenize(self.text)

    def upper(self) -> str:
        return self.text.upper()


def detect_format(raw_lines: Sequence[str]) -> str:
    """Guess whether a file is fixed-format card image or free format."""
    considered = 0
    fixed_votes = 0
    margin_decls = 0
    for line in raw_lines:
        stripped = line.rstrip("\r\n")
        if not stripped.strip():
            continue
        considered += 1
        if considered > 200:
            break
        # A level number left of column 8 is never valid card image. One could
        # be a fluke; a handful means the file is free format (or hand-shifted),
        # and reading it as fixed would quietly drop those declarations.
        if _is_margin_declaration(stripped):
            margin_decls += 1
        if len(stripped) < 7:
            continue
        sequence = stripped[_SEQ_AREA]
        indicator = stripped[_INDICATOR]
        seq_ok = all(ch.isdigit() or ch == " " for ch in sequence)
        ind_ok = indicator in " *-/D$"
        # Free-format source usually starts a statement well before column 8.
        starts_left = bool(stripped[:6].strip()) and not sequence.strip().isdigit()
        if seq_ok and ind_ok and not starts_left:
            fixed_votes += 1
    if considered == 0:
        return FREE
    if margin_decls >= 2 or (margin_decls and margin_decls * 10 >= considered):
        return FREE
    return FIXED if fixed_votes / considered >= 0.75 else FREE


def _expand_tabs(line: str) -> str:
    return line.replace("\t", "    ")


def read_lines(
    path: Path,
    source_format: str | None = None,
    warnings: list[str] | None = None,
) -> list[LogicalLine]:
    """Read a source file into logical lines, dropping comments and card noise."""
    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return parse_lines(raw, str(path), source_format, warnings)


def parse_lines(
    raw_lines: Sequence[str],
    path: str,
    source_format: str | None = None,
    warnings: list[str] | None = None,
) -> list[LogicalLine]:
    """Normalise raw text lines. Exposed separately so tests can skip the disk."""
    fmt = source_format or detect_format(raw_lines)
    result: list[LogicalLine] = []
    salvaged_margin: list[int] = []
    for index, original in enumerate(raw_lines, start=1):
        line = _expand_tabs(original.rstrip("\r\n"))
        if fmt == FIXED:
            if len(line) <= _INDICATOR:
                result.append(
                    LogicalLine(text="", line_no=index, path=path, raw=original)
                )
                continue
            indicator = line[_INDICATOR]
            code = line[_CODE_AREA].rstrip()
            is_comment = indicator in _COMMENT_INDICATORS
            is_continuation = indicator == _CONTINUATION_INDICATOR
            if indicator == _DEBUG_INDICATOR:
                is_comment = True
            # A data item whose level number was typed left of column 8 loses
            # that level to the sequence area and vanishes downstream, taking any
            # REDEFINES or PICTURE on it with it. It is never valid card image,
            # so read it as free format rather than drop it, and note that we
            # did in case the file really was meant to be fixed.
            if (
                not is_comment
                and _is_margin_declaration(line)
                and not _CODE_HAS_LEVEL_RE.match(code)
            ):
                code = line.strip()
                is_continuation = False
                salvaged_margin.append(index)
        else:
            stripped = line.lstrip()
            is_comment = stripped.startswith("*")
            is_continuation = False
            code = line.rstrip()
            if not is_comment:
                # Free format still allows a trailing identification area in
                # some shops; only trim when the line is clearly card width.
                if len(code) > 72 and code[72:].strip() and _looks_like_ident(code[72:]):
                    code = code[:72].rstrip()
        result.append(
            LogicalLine(
                text=code,
                line_no=index,
                path=path,
                is_comment=is_comment,
                is_continuation=is_continuation,
                raw=original,
            )
        )
    if warnings is not None and salvaged_margin:
        warnings.append(
            f"{path}: {len(salvaged_margin)} line(s) carry a level number before "
            f"column 8 (first at line {salvaged_margin[0]}); read as free format so "
            f"the declaration is not lost. If this file really is fixed-format, "
            f"pass --format fixed."
        )
    return result


def _looks_like_ident(tail: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9 _.\-]{1,8}", tail.strip()))


def code_lines(lines: Iterable[LogicalLine]) -> list[LogicalLine]:
    """Only the lines that carry executable or declarative code."""
    return [line for line in lines if not line.is_comment and line.text.strip()]


_TOKEN_RE = re.compile(
    r"""
    '(?:[^']|'')*'              |   # single quoted literal
    "(?:[^"]|"")*"              |   # double quoted literal
    [A-Za-z0-9][A-Za-z0-9_\-#@$]*  |
    ::                          |
    [(),.:;=<>+*/&-]
    """,
    re.VERBOSE,
)


def tokenize(text: str) -> list[str]:
    """Split COBOL text into tokens, keeping quoted literals whole."""
    return _TOKEN_RE.findall(text)


_PERIOD_END_RE = re.compile(r"\.(?=\s|$)")


def iter_sentences(lines: Sequence[LogicalLine]) -> Iterator[Sentence]:
    """Group logical lines into period-terminated sentences.

    A period only terminates a sentence when whitespace or end-of-line follows
    it, which keeps ``PIC ZZ9.99`` and qualified names like ``A.B`` intact.
    """
    buffer: list[str] = []
    buffer_lines: list[LogicalLine] = []
    start_line = 0

    for line in lines:
        if line.is_comment or not line.text.strip():
            continue
        text = line.text.strip()
        if not buffer:
            start_line = line.line_no
        position = 0
        while True:
            match = _PERIOD_END_RE.search(text, position)
            if not match:
                buffer.append(text[position:])
                buffer_lines.append(line)
                break
            head = text[position : match.start()]
            buffer.append(head)
            buffer_lines.append(line)
            body = " ".join(part for part in buffer if part.strip()).strip()
            if body:
                yield Sentence(
                    text=body,
                    line_no=start_line,
                    end_line=line.line_no,
                    path=line.path,
                    lines=list(buffer_lines),
                )
            buffer = []
            buffer_lines = []
            position = match.end()
            start_line = line.line_no
            if position >= len(text):
                break

    body = " ".join(part for part in buffer if part.strip()).strip()
    if body and buffer_lines:
        yield Sentence(
            text=body,
            line_no=start_line,
            end_line=buffer_lines[-1].line_no,
            path=buffer_lines[-1].path,
            lines=list(buffer_lines),
        )


_DIVISION_RE = re.compile(r"\b(IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", re.I)
_SECTION_RE = re.compile(
    r"\b(WORKING-STORAGE|LOCAL-STORAGE|LINKAGE|FILE|COMMUNICATION|REPORT|SCREEN)\s+SECTION\b",
    re.I,
)


def find_division(text: str) -> str | None:
    match = _DIVISION_RE.search(text)
    return match.group(1).upper() if match else None


def find_section(text: str) -> str | None:
    match = _SECTION_RE.search(text)
    return match.group(1).upper() if match else None


def normalize_name(name: str) -> str:
    """COBOL names are case-insensitive; underscores and hyphens are distinct."""
    return name.strip().upper()
