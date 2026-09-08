"""Pro*COBOL program parsing: declarations, embedded SQL, and data flow.

This module answers "what moves where" for one ``.pco`` (or plain ``.cbl``)
program.  It is deliberately a *pattern* parser rather than a full COBOL grammar:
the statements that matter for width impact (MOVE, STRING, COMPUTE, CALL, the
arithmetic verbs and embedded SQL) have shapes regular enough to recognise
reliably, and everything else is recorded as a plain usage so a human still sees
it in the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import cobolsrc, copybook as cb
from .cobolsrc import LogicalLine, normalize_name
from .copybook import CopybookResolver, DataMap
from .models import EdgeKind, SourceRef
from .sqlparse import SqlAnalyzer, SqlStatement

_NAME = r"[A-Za-z][A-Za-z0-9_\-]*"
_NAME_RE = re.compile(_NAME)
_REFMOD_RE = re.compile(rf"({_NAME})\s*\((?P<args>[^()]*:[^()]*)\)")
_SUBSCRIPT_RE = re.compile(rf"({_NAME})\s*\(([^():]*)\)")

_PROGRAM_ID_RE = re.compile(r"PROGRAM-ID\s*\.?\s*([A-Za-z0-9][A-Za-z0-9_\-]*)", re.I)
_EXEC_SQL_START_RE = re.compile(r"(?<![A-Za-z0-9_-])EXEC\s+SQL(?![A-Za-z0-9_-])", re.I)
_EXEC_SQL_END_RE = re.compile(r"(?<![A-Za-z0-9_-])END-EXEC(?![A-Za-z0-9_-])", re.I)
# Paragraph and section names routinely start with a digit ("1000-READ-CUST").
_PARAGRAPH_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_\-]*)\s*\.?\s*$")

# Statement verbs used to chop a sentence into individual statements.
_VERBS = [
    "ACCEPT", "ADD", "ALTER", "CALL", "CANCEL", "CLOSE", "COMPUTE", "CONTINUE",
    "DELETE", "DISPLAY", "DIVIDE", "ELSE", "ENTRY", "EVALUATE", "EXIT",
    "GENERATE", "GOBACK", "GO", "IF", "INITIALIZE", "INITIATE", "INSPECT",
    "INVOKE", "MERGE", "MOVE", "MULTIPLY", "OPEN", "PERFORM", "READ", "RELEASE",
    "RETURN", "REWRITE", "SEARCH", "SET", "SORT", "START", "STOP", "STRING",
    "SUBTRACT", "TERMINATE", "UNLOCK", "UNSTRING", "WHEN", "WRITE", "XML",
    "END-IF", "END-EVALUATE", "END-PERFORM", "END-READ", "END-STRING",
    "END-UNSTRING", "END-CALL", "END-COMPUTE", "END-ADD", "END-SUBTRACT",
    "END-MULTIPLY", "END-DIVIDE", "END-SEARCH", "END-WRITE",
]
_VERB_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(sorted(_VERBS, key=len, reverse=True)) + r")(?![A-Za-z0-9_-])",
    re.I,
)

# Reserved words that must never be mistaken for a data name.
_RESERVED = frozenset(
    """
    ADVANCING AFTER ALL ALPHABETIC ALSO ALTERNATE AND ANY ARE AREA ASCENDING
    AT BEFORE BY CHARACTER CHARACTERS COMMA CORR CORRESPONDING COUNT
    DELIMITED DELIMITER DEPENDING DESCENDING DOWN ELSE END EQUAL ERROR EXCEPTION
    FALSE FIRST FOR FROM GIVING GREATER HIGH-VALUE HIGH-VALUES IN INITIAL INTO
    INVALID IS KEY LEADING LENGTH LESS LINE LINES LOW-VALUE LOW-VALUES NEXT NO
    NOT NULL NULLS NUMERIC OF OFF ON OR ORDER OTHER OVERFLOW POINTER POSITIVE
    PROCEDURE QUOTE QUOTES RECORD REFERENCE REMAINDER REPLACING RETURNING
    ROUNDED RUN SEPARATE SIZE SPACE SPACES STANDARD STATUS TALLYING TEST THAN
    THEN THROUGH THRU TIMES TO TRAILING TRUE UNTIL UP UPON USING VARYING WHEN
    WITH ZERO ZEROS ZEROES TRANSFORM PROGRAM SECTION EXIT GOBACK CONTINUE
    """.split()
)


@dataclass
class Flow:
    """One data movement discovered in the PROCEDURE DIVISION."""

    sources: list[str]
    target: str
    kind: EdgeKind
    ref: SourceRef
    note: str = ""
    combines: bool = False  # sources concatenate rather than each fitting alone


@dataclass
class Usage:
    """A place a name is mentioned without a clear width-carrying flow."""

    name: str
    category: str
    ref: SourceRef
    detail: str = ""


@dataclass
class CallSite:
    """A ``CALL 'X' USING ...`` with its argument list in order."""

    target: str
    args: list[str]
    ref: SourceRef


@dataclass
class Program:
    """Everything the analyzer learned about one source program."""

    name: str
    path: str
    data: DataMap = field(default_factory=DataMap)
    sql: list[SqlStatement] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    linkage_using: list[str] = field(default_factory=list)
    copybooks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def knows(self, name: str) -> bool:
        return self.data.get(name) is not None


class ProgramParser:
    """Parses one program; reuses a copybook resolver across the whole scan."""

    def __init__(self, resolver: Optional[CopybookResolver] = None) -> None:
        self.resolver = resolver
        self.sql_analyzer = SqlAnalyzer()

    def parse(self, path: Path, source_format: str | None = None) -> Program:
        lines = cobolsrc.read_lines(path, source_format)
        name = _program_id(lines) or path.stem.upper()
        program = Program(name=name, path=str(path))

        sql_blocks, stripped = _extract_exec_sql(lines)
        for text, ref in sql_blocks:
            ref.program = name
            statement = self.sql_analyzer.parse(text, ref)
            program.sql.append(statement)
            if statement.kind == "INCLUDE" and statement.include_name:
                self._include(program, statement.include_name, ref)

        division = ""
        section = ""
        paragraph = ""
        data_sentences: list[cobolsrc.Sentence] = []
        procedure_sentences: list[tuple[cobolsrc.Sentence, str]] = []

        for sentence in cobolsrc.iter_sentences(stripped):
            text = sentence.text.strip()
            found_division = cobolsrc.find_division(text)
            if found_division:
                division = found_division
                section = ""
                using = _PROGRAM_USING_RE.search(text)
                if using:
                    program.linkage_using = [
                        normalize_name(word)
                        for word in re.split(r"[\s,]+", using.group(1))
                        if word and word.upper() not in _RESERVED
                    ]
                continue
            found_section = cobolsrc.find_section(text)
            if found_section:
                section = found_section
                continue

            if division == "DATA":
                copy = cb.parse_copy_statement(text)
                if copy is not None:
                    self._copy(program, copy[0], copy[1], sentence.path, sentence.line_no)
                    continue
                data_sentences.append(sentence)
                continue

            if division == "PROCEDURE":
                paragraph_match = _PARAGRAPH_RE.match(text)
                if paragraph_match and not _VERB_RE.match(text.strip()):
                    paragraph = paragraph_match.group(1).upper()
                    continue
                procedure_sentences.append((sentence, paragraph))

        # The declarations have to be complete before any statement is read:
        # flow detection only accepts names that resolve to a declared item, and
        # inline WORKING-STORAGE is parsed here rather than pulled in by COPY.
        inline = cb.parse_data_sentences(data_sentences, origin=str(path), program=name)
        program.data.merge(inline)
        cb.finalize(program.data)

        for sentence, paragraph in procedure_sentences:
            self._procedure(program, sentence, paragraph)
        return program

    # -- copybook plumbing ------------------------------------------------

    def _copy(
        self,
        program: Program,
        name: str,
        replacing: Sequence[tuple[str, str]],
        path: str,
        line: int,
    ) -> None:
        resolved = self.resolver.resolve(name) if self.resolver else None
        if resolved is None:
            program.warnings.append(f"{path}:{line}: copybook {name} not found on the search path")
            return
        program.copybooks.append(str(resolved))
        data = cb.load_copybook(resolved, program=program.name, replacing=list(replacing))
        program.data.merge(data)

    def _include(self, program: Program, name: str, ref: SourceRef) -> None:
        if name.upper() in ("SQLCA", "ORACA", "SQLDA"):
            return
        resolved = self.resolver.resolve(name) if self.resolver else None
        if resolved is None:
            program.warnings.append(
                f"{ref.location()}: EXEC SQL INCLUDE {name} not found on the search path"
            )
            return
        program.copybooks.append(str(resolved))
        program.data.merge(cb.load_copybook(resolved, program=program.name))

    # -- procedure division ----------------------------------------------

    def _procedure(self, program: Program, sentence: cobolsrc.Sentence, paragraph: str) -> None:
        for verb, chunk, line_no in _split_statements(sentence):
            ref = SourceRef(
                path=sentence.path,
                line=line_no,
                program=program.name,
                paragraph=paragraph,
                text=chunk.strip()[:200],
            )
            handler = _HANDLERS.get(verb)
            if handler is not None:
                handler(program, chunk, ref)
            _record_refmods(program, chunk, ref)


_PROGRAM_USING_RE = re.compile(r"PROCEDURE\s+DIVISION\s+USING\s+(.+)$", re.I)


def _program_id(lines: Sequence[LogicalLine]) -> str:
    for line in lines:
        if line.is_comment:
            continue
        match = _PROGRAM_ID_RE.search(line.text)
        if match:
            return match.group(1).upper()
    return ""


def _extract_exec_sql(
    lines: Sequence[LogicalLine],
) -> tuple[list[tuple[str, SourceRef]], list[LogicalLine]]:
    """Pull EXEC SQL blocks out and hand back the remaining COBOL lines.

    Extracted regions are replaced by ``CONTINUE`` so that the surrounding
    sentence structure (an ``IF`` around an ``EXEC SQL``, for example) still
    parses.
    """
    blocks: list[tuple[str, SourceRef]] = []
    remaining: list[LogicalLine] = []
    buffer: list[str] = []
    start_line = 0
    start_path = ""
    inside = False

    for line in lines:
        if line.is_comment:
            remaining.append(line)
            continue
        text = line.text
        if not inside:
            match = _EXEC_SQL_START_RE.search(text)
            if not match:
                remaining.append(line)
                continue
            inside = True
            start_line = line.line_no
            start_path = line.path
            head = text[: match.start()]
            tail = text[match.end():]
            end = _EXEC_SQL_END_RE.search(tail)
            if end:
                buffer.append(tail[: end.start()])
                rest = tail[end.end():]
                blocks.append(_finish_block(buffer, start_line, start_path))
                buffer = []
                inside = False
                remaining.append(
                    LogicalLine(
                        text=f"{head} CONTINUE {rest}".strip(),
                        line_no=line.line_no,
                        path=line.path,
                        raw=line.raw,
                    )
                )
            else:
                buffer.append(tail)
                remaining.append(
                    LogicalLine(
                        text=f"{head} CONTINUE".strip(),
                        line_no=line.line_no,
                        path=line.path,
                        raw=line.raw,
                    )
                )
            continue

        end = _EXEC_SQL_END_RE.search(text)
        if end:
            buffer.append(text[: end.start()])
            rest = text[end.end():]
            blocks.append(_finish_block(buffer, start_line, start_path))
            buffer = []
            inside = False
            remaining.append(
                LogicalLine(text=rest.strip(), line_no=line.line_no, path=line.path, raw=line.raw)
            )
            continue
        buffer.append(text)
        remaining.append(LogicalLine(text="", line_no=line.line_no, path=line.path, raw=line.raw))

    if inside and buffer:
        blocks.append(_finish_block(buffer, start_line, start_path))
    return blocks, remaining


def _finish_block(buffer: list[str], line_no: int, path: str) -> tuple[str, SourceRef]:
    text = " ".join(part.strip() for part in buffer if part.strip()).strip().rstrip(";")
    ref = SourceRef(path=path, line=line_no, text=text[:200])
    return text, ref


def _split_statements(sentence: cobolsrc.Sentence) -> list[tuple[str, str, int]]:
    """Chop a sentence into ``(verb, text, line)`` statement chunks."""
    text = sentence.text
    matches = list(_VERB_RE.finditer(text))
    if not matches:
        return []
    result: list[tuple[str, str, int]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[match.start():end]
        result.append((match.group(1).upper(), chunk, _line_for_offset(sentence, match.start())))
    return result


def _line_for_offset(sentence: cobolsrc.Sentence, offset: int) -> int:
    """Map a character offset in the joined sentence back to a source line."""
    consumed = 0
    for line in sentence.lines:
        length = len(line.text.strip()) + 1
        if offset < consumed + length:
            return line.line_no
        consumed += length
    return sentence.line_no


def identifiers(text: str) -> list[str]:
    """Data-name-looking tokens, with literals, numbers and keywords removed."""
    without_literals = re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", " ", text)
    names: list[str] = []
    for match in _NAME_RE.finditer(without_literals):
        token = match.group(0).upper()
        if token in _RESERVED or token in {verb.upper() for verb in _VERBS}:
            continue
        if token.isdigit():
            continue
        names.append(token)
    return names


def known_identifiers(program: Program, text: str) -> list[str]:
    """Identifiers that actually resolve to a declared data item."""
    seen: set[str] = set()
    result: list[str] = []
    for name in identifiers(text):
        if name in seen:
            continue
        seen.add(name)
        if program.data.get(name) is not None:
            result.append(name)
    return result


def _split_on_keyword(text: str, keyword: str) -> tuple[str, str]:
    pattern = re.compile(rf"(?<![A-Za-z0-9_-]){keyword}(?![A-Za-z0-9_-])", re.I)
    match = pattern.search(text)
    if not match:
        return text, ""
    return text[: match.start()], text[match.end():]


# -- statement handlers ---------------------------------------------------


def _handle_move(program: Program, chunk: str, ref: SourceRef) -> None:
    body = re.sub(r"^\s*MOVE\s+", "", chunk, flags=re.I)
    corresponding = bool(re.match(r"\s*(CORR|CORRESPONDING)\b", body, re.I))
    body = re.sub(r"^\s*(CORR|CORRESPONDING)\s+", "", body, flags=re.I)
    source_text, target_text = _split_on_keyword(body, "TO")
    if not target_text:
        return
    sources = known_identifiers(program, source_text)
    targets = known_identifiers(program, target_text)
    if not targets:
        return
    if corresponding:
        _move_corresponding(program, sources, targets, ref)
        return
    note = "" if sources else "literal or figurative constant source"
    for target in targets:
        program.flows.append(
            Flow(
                sources=sources[:1],
                target=target,
                kind=EdgeKind.MOVE,
                ref=ref,
                note=note,
            )
        )
    if _REFMOD_RE.search(source_text) or _REFMOD_RE.search(target_text):
        for target in targets:
            program.usages.append(
                Usage(
                    name=target,
                    category="reference-modification",
                    ref=ref,
                    detail="MOVE uses a hard-coded offset/length that will not follow a width change",
                )
            )


def _move_corresponding(
    program: Program,
    sources: list[str],
    targets: list[str],
    ref: SourceRef,
) -> None:
    """MOVE CORRESPONDING pairs children by unqualified name."""
    if not sources:
        return
    source_group = program.data.get(sources[0])
    for target_name in targets:
        target_group = program.data.get(target_name)
        if source_group is None or target_group is None:
            continue
        source_children = {
            program.data.fields[key].name: key for key in source_group.children
        }
        for key in target_group.children:
            child = program.data.fields[key]
            match = source_children.get(child.name)
            if match is None:
                continue
            program.flows.append(
                Flow(
                    sources=[program.data.fields[match].name],
                    target=child.name,
                    kind=EdgeKind.MOVE,
                    ref=ref,
                    note="MOVE CORRESPONDING",
                )
            )


def _handle_string(program: Program, chunk: str, ref: SourceRef) -> None:
    body = re.sub(r"^\s*STRING\s+", "", chunk, flags=re.I)
    source_text, target_text = _split_on_keyword(body, "INTO")
    if not target_text:
        return
    # DELIMITED BY clauses name delimiters, not data being concatenated.
    concatenated = re.sub(
        r"(?<![A-Za-z0-9_-])DELIMITED\s+BY\s+(SIZE|[^\s]+)", " ", source_text, flags=re.I
    )
    sources = known_identifiers(program, concatenated)
    target_text, _ = _split_on_keyword(target_text, "WITH")
    targets = known_identifiers(program, target_text)
    for target in targets[:1]:
        program.flows.append(
            Flow(
                sources=sources,
                target=target,
                kind=EdgeKind.STRING,
                ref=ref,
                note="concatenated result",
                combines=True,
            )
        )


def _handle_unstring(program: Program, chunk: str, ref: SourceRef) -> None:
    body = re.sub(r"^\s*UNSTRING\s+", "", chunk, flags=re.I)
    source_text, target_text = _split_on_keyword(body, "INTO")
    if not target_text:
        return
    source_text = re.sub(
        r"(?<![A-Za-z0-9_-])DELIMITED\s+BY\s+(ALL\s+)?(SIZE|[^\s]+)", " ", source_text, flags=re.I
    )
    sources = known_identifiers(program, source_text)
    target_text, _ = _split_on_keyword(target_text, "WITH")
    target_text, _ = _split_on_keyword(target_text, "TALLYING")
    for target in known_identifiers(program, target_text):
        program.flows.append(
            Flow(
                sources=sources[:1],
                target=target,
                kind=EdgeKind.UNSTRING,
                ref=ref,
                note="one delimited field of the source",
            )
        )


def _handle_compute(program: Program, chunk: str, ref: SourceRef) -> None:
    body = re.sub(r"^\s*COMPUTE\s+", "", chunk, flags=re.I)
    if "=" not in body:
        return
    lhs, rhs = body.split("=", 1)
    sources = known_identifiers(program, rhs)
    for target in known_identifiers(program, lhs):
        program.flows.append(
            Flow(
                sources=sources,
                target=target,
                kind=EdgeKind.COMPUTE,
                ref=ref,
                note="arithmetic result",
                combines=True,
            )
        )


def _handle_add(program: Program, chunk: str, ref: SourceRef) -> None:
    _arithmetic(program, chunk, ref, "ADD", "TO")


def _handle_subtract(program: Program, chunk: str, ref: SourceRef) -> None:
    _arithmetic(program, chunk, ref, "SUBTRACT", "FROM")


def _handle_multiply(program: Program, chunk: str, ref: SourceRef) -> None:
    _arithmetic(program, chunk, ref, "MULTIPLY", "BY")


def _handle_divide(program: Program, chunk: str, ref: SourceRef) -> None:
    _arithmetic(program, chunk, ref, "DIVIDE", "INTO|BY")


def _arithmetic(program: Program, chunk: str, ref: SourceRef, verb: str, joiner: str) -> None:
    """Shared shape for ADD/SUBTRACT/MULTIPLY/DIVIDE.

    ``GIVING`` splits off first because it always names the true destination;
    without it the operand after the joiner is both a source and a destination
    (``ADD 1 TO WS-COUNT`` grows WS-COUNT).
    """
    body = re.sub(rf"^\s*{verb}\s+", "", chunk, flags=re.I)
    main, giving_text = _split_on_keyword(body, "GIVING")
    giving_text, _ = _split_on_keyword(giving_text, "REMAINDER")
    joiner_re = re.compile(rf"(?<![A-Za-z0-9_-])(?:{joiner})(?![A-Za-z0-9_-])", re.I)
    match = joiner_re.search(main)
    head = main[: match.start()] if match else main
    tail = main[match.end():] if match else ""

    left = known_identifiers(program, head)
    right = known_identifiers(program, tail)

    if giving_text.strip():
        targets = known_identifiers(program, giving_text)
        sources = left + right
    else:
        targets = right
        sources = left + right

    for target in targets:
        program.flows.append(
            Flow(
                sources=sources,
                target=target,
                kind=EdgeKind.ARITHMETIC,
                ref=ref,
                note=f"{verb} result",
                combines=True,
            )
        )


def _handle_call(program: Program, chunk: str, ref: SourceRef) -> None:
    match = re.match(rf"\s*CALL\s+('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|{_NAME})", chunk, re.I)
    if not match:
        return
    target = match.group(1).strip("'\"").upper()
    _, using_text = _split_on_keyword(chunk, "USING")
    if not using_text:
        program.calls.append(CallSite(target=target, args=[], ref=ref))
        return
    using_text, _ = _split_on_keyword(using_text, "RETURNING")
    using_text, _ = _split_on_keyword(using_text, "ON")
    args = known_identifiers(program, using_text)
    program.calls.append(CallSite(target=target, args=args, ref=ref))


def _handle_write(program: Program, chunk: str, ref: SourceRef) -> None:
    body = re.sub(r"^\s*(WRITE|REWRITE|RELEASE)\s+", "", chunk, flags=re.I)
    record_text, from_text = _split_on_keyword(body, "FROM")
    records = known_identifiers(program, record_text)
    if not from_text:
        for record in records:
            program.usages.append(
                Usage(name=record, category="file-write", ref=ref, detail="record written to a file")
            )
        return
    from_text, _ = _split_on_keyword(from_text, "AFTER")
    from_text, _ = _split_on_keyword(from_text, "BEFORE")
    sources = known_identifiers(program, from_text)
    for record in records[:1]:
        program.flows.append(
            Flow(sources=sources[:1], target=record, kind=EdgeKind.WRITE_FROM, ref=ref)
        )


def _handle_read(program: Program, chunk: str, ref: SourceRef) -> None:
    body = re.sub(r"^\s*(READ|RETURN)\s+", "", chunk, flags=re.I)
    record_text, into_text = _split_on_keyword(body, "INTO")
    if not into_text:
        return
    into_text, _ = _split_on_keyword(into_text, "KEY")
    into_text, _ = _split_on_keyword(into_text, "AT")
    sources = known_identifiers(program, record_text)
    for target in known_identifiers(program, into_text)[:1]:
        program.flows.append(
            Flow(sources=sources[:1], target=target, kind=EdgeKind.READ_INTO, ref=ref)
        )


def _handle_initialize(program: Program, chunk: str, ref: SourceRef) -> None:
    for name in known_identifiers(program, chunk):
        program.usages.append(Usage(name=name, category="initialize", ref=ref))


_RELATION_RE = re.compile(
    r"(?P<left>'(?:[^']|'')*'|[A-Za-z][A-Za-z0-9_\-]*(?:\s*\([^)]*\))?)\s*"
    r"(?P<op>=|<>|>=|<=|>|<|(?:IS\s+)?(?:NOT\s+)?(?:GREATER|LESS|EQUAL)(?:\s+THAN)?"
    r"(?:\s+OR\s+EQUAL(?:\s+TO)?)?)\s*"
    r"(?P<right>'(?:[^']|'')*'|[A-Za-z][A-Za-z0-9_\-]*(?:\s*\([^)]*\))?)",
    re.I,
)


def _handle_condition(program: Program, chunk: str, ref: SourceRef) -> None:
    """Relational conditions only: operands on either side of one operator.

    Pairing every identifier in the sentence would invent edges between
    unrelated flags, so each comparison is matched explicitly.
    """
    for match in _RELATION_RE.finditer(chunk):
        left = match.group("left").strip()
        right = match.group("right").strip()
        left_names = known_identifiers(program, left)
        right_names = known_identifiers(program, right)
        for source in left_names:
            for target in right_names:
                program.flows.append(
                    Flow(
                        sources=[source],
                        target=target,
                        kind=EdgeKind.COMPARE,
                        ref=ref,
                        note="operands compared; widths must stay consistent",
                    )
                )
        literal_side = ""
        if left.startswith("'") or left.startswith('"'):
            literal_side = left
        elif right.startswith("'") or right.startswith('"'):
            literal_side = right
        if literal_side:
            for name in left_names + right_names:
                program.usages.append(
                    Usage(
                        name=name,
                        category="literal-comparison",
                        ref=ref,
                        detail=f"compared against literal {literal_side}",
                    )
                )


def _handle_display(program: Program, chunk: str, ref: SourceRef) -> None:
    for name in known_identifiers(program, chunk):
        program.usages.append(
            Usage(name=name, category="display", ref=ref, detail="written to a report or log line")
        )


def _handle_inspect(program: Program, chunk: str, ref: SourceRef) -> None:
    for name in known_identifiers(program, chunk):
        program.usages.append(
            Usage(
                name=name,
                category="inspect",
                ref=ref,
                detail="INSPECT counts or replaces over the whole field width",
            )
        )


def _handle_set(program: Program, chunk: str, ref: SourceRef) -> None:
    for name in known_identifiers(program, chunk):
        program.usages.append(Usage(name=name, category="set", ref=ref))


_HANDLERS = {
    "MOVE": _handle_move,
    "STRING": _handle_string,
    "UNSTRING": _handle_unstring,
    "COMPUTE": _handle_compute,
    "ADD": _handle_add,
    "SUBTRACT": _handle_subtract,
    "MULTIPLY": _handle_multiply,
    "DIVIDE": _handle_divide,
    "CALL": _handle_call,
    "WRITE": _handle_write,
    "REWRITE": _handle_write,
    "RELEASE": _handle_write,
    "READ": _handle_read,
    "RETURN": _handle_read,
    "INITIALIZE": _handle_initialize,
    "IF": _handle_condition,
    "WHEN": _handle_condition,
    "EVALUATE": _handle_condition,
    "DISPLAY": _handle_display,
    "INSPECT": _handle_inspect,
    "SET": _handle_set,
}


def _record_refmods(program: Program, chunk: str, ref: SourceRef) -> None:
    """Flag reference modification, which pins offsets that a widening breaks."""
    for match in _REFMOD_RE.finditer(chunk):
        name = match.group(1).upper()
        if program.data.get(name) is None:
            continue
        program.usages.append(
            Usage(
                name=name,
                category="reference-modification",
                ref=ref,
                detail=f"hard-coded offset/length ({match.group('args').strip()})",
            )
        )


def discover_sources(
    roots: Sequence[Path],
    patterns: Sequence[str] = ("*.pco", "*.PCO"),
) -> list[Path]:
    """Every source file under ``roots`` matching any of ``patterns``."""
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if root.is_file():
            if root.resolve() not in seen:
                seen.add(root.resolve())
                found.append(root)
            continue
        for pattern in patterns:
            for path in root.rglob(pattern):
                resolved = path.resolve()
                if path.is_file() and resolved not in seen:
                    seen.add(resolved)
                    found.append(path)
    return sorted(found)
