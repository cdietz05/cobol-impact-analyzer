"""COBOL PICTURE and USAGE clause parsing.

Turns strings like ``S9(7)V99`` or ``-ZZ,ZZ9.99`` into a :class:`Capacity` plus a
byte count, honouring the USAGE clause (``DISPLAY``, ``COMP-3``, ``COMP``, ...)
because packed and binary items store the same digits in far fewer bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Capacity, Kind

# CR and DB are two-character symbols and must be matched before the single ones.
_SYMBOL_RE = re.compile(
    r"(CR|DB|[9AXNZSVP*+\-.,/BE0])(?:\s*\((\d+)\))?",
    re.IGNORECASE,
)

_DIGIT_SYMBOLS = frozenset("9")
_FLOAT_DIGIT_SYMBOLS = frozenset("Z*")
_CHAR_SYMBOLS = frozenset("AX")
_EDIT_SYMBOLS = frozenset(".,/B0+-$")

# Normalised USAGE spellings.
_USAGE_ALIASES = {
    "COMPUTATIONAL": "COMP",
    "COMPUTATIONAL-1": "COMP-1",
    "COMPUTATIONAL-2": "COMP-2",
    "COMPUTATIONAL-3": "COMP-3",
    "COMPUTATIONAL-4": "COMP-4",
    "COMPUTATIONAL-5": "COMP-5",
    "PACKED-DECIMAL": "COMP-3",
    "BINARY": "COMP-4",
    "INDEX": "INDEX",
    "POINTER": "POINTER",
    "DISPLAY-1": "DISPLAY-1",
    "NATIONAL": "NATIONAL",
}


def normalize_usage(usage: str) -> str:
    """Fold USAGE synonyms onto one canonical spelling."""
    token = (usage or "DISPLAY").strip().upper()
    token = token.removeprefix("USAGE ").strip()
    token = token.removeprefix("IS ").strip()
    return _USAGE_ALIASES.get(token, token or "DISPLAY")


@dataclass(frozen=True)
class PictureInfo:
    """Everything the analyzer needs to know about one PICTURE clause."""

    raw: str
    usage: str
    capacity: Capacity
    storage_bytes: int
    display_size: int

    def render(self, capacity: Capacity) -> str:
        """A PICTURE string that would hold ``capacity``, keeping this shape."""
        return render_picture(capacity, self.usage, template=self.raw)


def _strip_terminator(pic: str) -> str:
    """Drop the COBOL sentence period that usually trails a PICTURE clause.

    ``PIC ZZ9.99.`` ends with two periods: the first belongs to the picture, the
    second terminates the sentence.  A period is only a terminator when nothing
    follows it.
    """
    text = pic.strip()
    while text.endswith("."):
        candidate = text[:-1]
        # A picture may legitimately end in a period only when it is an editing
        # character with digit positions on both sides, which cannot happen at
        # the very end of the string.
        text = candidate.strip()
        if not text:
            break
    return text


def parse_picture(pic: str, usage: str = "DISPLAY", sign_separate: bool = False) -> PictureInfo:
    """Parse a PICTURE clause into a capacity and a byte count."""
    raw = (pic or "").strip()
    usage_norm = normalize_usage(usage)
    body = _strip_terminator(raw).upper()

    if not body:
        return _usage_only(raw, usage_norm)

    int_digits = 0
    dec_digits = 0
    char_positions = 0
    edit_positions = 0
    signed = False
    has_char = False
    has_national = False
    has_edit = False
    has_digit = False
    seen_v = False
    consumed = 0

    for match in _SYMBOL_RE.finditer(body):
        symbol = match.group(1).upper()
        count = int(match.group(2)) if match.group(2) else 1
        consumed += len(match.group(0))

        if symbol == "S":
            signed = True
        elif symbol == "V":
            seen_v = True
        elif symbol == "P":
            # Scaling positions imply magnitude without occupying storage.
            if seen_v:
                dec_digits += count
            else:
                int_digits += count
        elif symbol in _DIGIT_SYMBOLS:
            has_digit = True
            if seen_v:
                dec_digits += count
            else:
                int_digits += count
        elif symbol in _FLOAT_DIGIT_SYMBOLS:
            has_digit = True
            has_edit = True
            edit_positions += count
            if seen_v:
                dec_digits += count
            else:
                int_digits += count
        elif symbol == "N":
            has_national = True
            char_positions += count
        elif symbol in _CHAR_SYMBOLS:
            has_char = True
            char_positions += count
        elif symbol in ("CR", "DB"):
            has_edit = True
            signed = True
            edit_positions += 2 * count
        elif symbol == ".":
            has_edit = True
            edit_positions += count
            seen_v = True
        elif symbol in _EDIT_SYMBOLS:
            has_edit = True
            edit_positions += count
            if symbol in "+-":
                signed = True
        elif symbol == "E":
            has_edit = True
            edit_positions += count

    if consumed == 0:
        return _usage_only(raw, usage_norm)

    if has_char:
        kind = Kind.ALPHANUMERIC_EDITED if has_edit else Kind.ALPHANUMERIC
        chars = char_positions + edit_positions
        display = chars
        capacity = Capacity(kind=kind, chars=chars)
    elif has_national:
        kind = Kind.NATIONAL
        chars = char_positions
        display = chars
        capacity = Capacity(kind=kind, chars=chars)
    elif has_digit:
        kind = Kind.NUMERIC_EDITED if has_edit else Kind.NUMERIC
        display = int_digits + dec_digits
        if has_edit:
            # Editing symbols such as "," and "." take their own positions on
            # top of the digit positions they punctuate.
            display = int_digits + dec_digits + _pure_edit_positions(body)
        elif signed and sign_separate:
            display += 1
        capacity = Capacity(
            kind=kind,
            chars=display,
            int_digits=int_digits,
            dec_digits=dec_digits,
            signed=signed,
        )
    else:
        return _usage_only(raw, usage_norm)

    storage = _storage_bytes(capacity, usage_norm, display, signed, sign_separate)
    return PictureInfo(
        raw=raw,
        usage=usage_norm,
        capacity=capacity,
        storage_bytes=storage,
        display_size=display,
    )


def _pure_edit_positions(body: str) -> int:
    """Count editing characters that add display width but no digit position."""
    total = 0
    for match in _SYMBOL_RE.finditer(body):
        symbol = match.group(1).upper()
        count = int(match.group(2)) if match.group(2) else 1
        if symbol in ("CR", "DB"):
            total += 2 * count
        elif symbol in ".,/B0$+-":
            total += count
    return total


# Items with no PICTURE whose size the USAGE clause fixes on its own. Only the
# floating-point ones carry a decimal capacity: COMP-1 and COMP-2 hold numbers,
# and their significand is what a MOVE into a smaller field would truncate.
#
# INDEX and POINTER are here for their byte count ONLY. An INDEX item is a
# subscript and a POINTER is a machine address; neither holds a decimal quantity
# and neither participates in the arithmetic MOVEs this tool reasons about.
# Giving them a numeric capacity made every one of them look like a 15-digit
# signed number pouring into whatever it touched, and reported a truncation that
# cannot happen - and it was also the silent fallback for any PICTURE the parser
# failed to consume on such a field. Unknown is the honest answer, and covers()
# already treats unknown as "cannot prove a problem".
_USAGE_ONLY_BYTES = {"COMP-1": 4, "COMP-2": 8, "INDEX": 4, "POINTER": 8}
# Significant decimal digits in an IEEE single/double significand. Approximate
# by nature - these are binary floats, not fixed-point decimals - so they size
# the value without claiming an exact digit count.
_FLOAT_DIGITS = {"COMP-1": 7, "COMP-2": 15}


def _usage_only(raw: str, usage: str) -> PictureInfo:
    """Items that carry no PICTURE, e.g. ``COMP-1``, ``INDEX``, group items."""
    size = _USAGE_ONLY_BYTES.get(usage)
    if size is None:
        return PictureInfo(raw=raw, usage=usage, capacity=Capacity(), storage_bytes=0, display_size=0)

    digits = _FLOAT_DIGITS.get(usage)
    capacity = (
        Capacity(kind=Kind.NUMERIC, int_digits=digits, dec_digits=0, signed=True)
        if digits is not None
        else Capacity()
    )
    return PictureInfo(
        raw=raw,
        usage=usage,
        capacity=capacity,
        storage_bytes=size,
        display_size=size,
    )


def _binary_bytes(digits: int) -> int:
    """IBM/Micro Focus default COMP sizing by declared digit count."""
    if digits <= 4:
        return 2
    if digits <= 9:
        return 4
    return 8


def _storage_bytes(
    capacity: Capacity,
    usage: str,
    display: int,
    signed: bool,
    sign_separate: bool,
) -> int:
    if usage == "COMP-3":
        return (capacity.total_digits // 2) + 1
    if usage in ("COMP", "COMP-4", "COMP-5"):
        return _binary_bytes(capacity.total_digits)
    if usage == "COMP-1":
        return 4
    if usage == "COMP-2":
        return 8
    if capacity.kind is Kind.NATIONAL:
        return capacity.chars * 2
    if capacity.kind is Kind.NUMERIC and signed and sign_separate:
        return display
    return display


def render_picture(capacity: Capacity, usage: str = "DISPLAY", template: str = "") -> str:
    """Build a PICTURE clause large enough to hold ``capacity``.

    ``template`` lets an edited picture keep its punctuation; when the shape is
    too exotic to rebuild safely the caller gets a plain picture back plus the
    original for reference.
    """
    if capacity.kind in (Kind.ALPHANUMERIC, Kind.ALPHANUMERIC_EDITED, Kind.GROUP):
        return f"X({capacity.chars})"
    if capacity.kind is Kind.NATIONAL:
        return f"N({capacity.chars})"
    if capacity.kind.is_numeric():
        sign = "S" if capacity.signed else ""
        body = f"9({capacity.int_digits})" if capacity.int_digits else ""
        if capacity.dec_digits:
            body += f"V9({capacity.dec_digits})"
        rendered = f"{sign}{body}" or "9"
        if capacity.kind is Kind.NUMERIC_EDITED and template:
            return _widen_edited(template, capacity)
        return rendered
    return template or "X(1)"


def _widen_edited(template: str, capacity: Capacity) -> str:
    """Grow an edited picture by padding its leading digit positions.

    ``ZZ,ZZ9.99`` widened to 8 integer digits becomes ``ZZZ,ZZZ,ZZ9.99`` in a
    real shop; reproducing comma grouping exactly is more trouble than it is
    worth, so the leading float positions are padded and the punctuation left
    alone.  The report always shows the original next to the suggestion.
    """
    body = _strip_terminator(template).upper()
    current = 0
    for match in _SYMBOL_RE.finditer(body):
        symbol = match.group(1).upper()
        count = int(match.group(2)) if match.group(2) else 1
        if symbol in ("9", "Z", "*"):
            current += count
    deficit = capacity.total_digits - current
    if deficit <= 0:
        return template
    return "Z" * deficit + body


def picture_for_capacity(capacity: Capacity, usage: str = "DISPLAY") -> str:
    """Convenience wrapper used by the report layer."""
    return render_picture(capacity, usage)
