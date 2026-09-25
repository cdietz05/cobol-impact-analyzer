"""Propagation must settle, whatever shape the data flow takes.

Every rule that ADDS to a width - STRING sums its sources, a group sums its
members times OCCURS, arithmetic grows by its operand's delta - feeds on itself
around a cycle. Before these guards, MOVE CUST-REC TO a member of CUST-REC grew
the member to the record, the record by the member, and so on until the
relaxation budget ran out, and the report told someone to change a PIC X(8) to
a width hundreds of digits long.

The hand-written cases pin the shapes that did it. The generated ones throw
random mixes of copybooks, numbers, X(8) fields, REDEFINES, OCCURS, MOVE, ADD,
COMPUTE, STRING and CALL at the analyzer; the seeds listed first all diverged.
"""

import random
import re
import tempfile
import unittest
from pathlib import Path

from cobol_impact_analyzer.analyzer import analyze
from cobol_impact_analyzer.spec import ChangeSpec, build_change

# Far above anything the change can legitimately ask for in these fixtures, and
# far below what a diverging walk produces.
_SANE = 1000

_PROGRAM = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. {name}.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           COPY CB.
{working}
{linkage}       PROCEDURE DIVISION{using}.
       0000-MAIN.
{statements}
           GOBACK.
"""


def _analyze(copybook: str, programs: dict[str, str]):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "cpy").mkdir()
        (root / "src").mkdir()
        (root / "cpy" / "CB.cpy").write_text(copybook)
        for name, text in programs.items():
            (root / "src" / f"{name.lower()}.pco").write_text(text)
        return analyze(
            ChangeSpec(
                changes=[build_change("T1", "AMT", "NUMBER(9,2)", "NUMBER(11,2)")],
                source_paths=[root / "src"],
                copybook_paths=[root / "cpy"],
                source_patterns=["*.pco"],
            )
        )


def _program(name, statements, working="", linkage="", using=""):
    return _PROGRAM.format(
        name=name,
        working=working,
        linkage=linkage,
        using=using,
        statements="\n".join(f"           {line}" for line in statements),
    )


def _widest(result) -> int:
    widest = 0
    for finding in result.findings:
        text = f"{finding.remediation} {finding.required}"
        for match in re.findall(r"(?:X|9)\((\d+)\)|(\d+) bytes", text):
            widest = max(widest, int(match[0] or match[1]))
    return widest


class _Settles:
    def assertSettles(self, result):
        self.assertFalse(
            [w for w in result.warnings if "stopped early" in w],
            "propagation ran out of budget instead of converging",
        )
        self.assertLess(_widest(result), _SANE)
        # Every trace begins at a changed column. A route rewritten around a
        # loop used to start mid-loop, at a field nothing had widened.
        seeds = {f"col:{c.table.upper()}.{c.column.upper()}" for c in result.spec.changes}
        for finding in result.findings:
            self.assertIn(finding.path[0], seeds, finding.path)


class HandWrittenCycleTests(unittest.TestCase, _Settles):
    def test_a_record_moved_into_its_own_member(self):
        copybook = (
            "       01  CREC.\n"
            "           05  CAMT                 PIC S9(9)V99 COMP-3.\n"
            "           05  CKEY                 PIC X(8).\n"
        )
        result = _analyze(
            copybook,
            {
                "P0": _program(
                    "P0",
                    [
                        "EXEC SQL SELECT AMT INTO :CAMT FROM T1 WHERE ID = 1 END-EXEC",
                        "MOVE CREC TO CKEY",
                    ],
                )
            },
        )
        self.assertSettles(result)

    def test_a_record_moved_into_its_own_occurs_member(self):
        # The member repeats three times, so any rule that multiplies by
        # OCCURS inside the loop triples the record on every pass.
        copybook = (
            "       01  CREC.\n"
            "           05  CAMT                 PIC S9(9)V99 COMP-3.\n"
            "           05  CKEY                 PIC X(8) OCCURS 3 TIMES.\n"
        )
        result = _analyze(
            copybook,
            {
                "P0": _program(
                    "P0",
                    [
                        "EXEC SQL SELECT AMT INTO :CAMT FROM T1 WHERE ID = 1 END-EXEC",
                        "MOVE CAMT TO CKEY",
                        "MOVE CREC TO CKEY",
                    ],
                )
            },
        )
        self.assertSettles(result)

    def test_an_accumulator_fed_back_into_its_operand(self):
        # ADD grows TOTAL by AMT's delta; MOVE hands TOTAL back to AMT. The
        # delta used to include TOTAL's head start over AMT on every pass.
        copybook = (
            "       01  CREC.\n"
            "           05  CAMT                 PIC S9(7)V99 COMP-3.\n"
            "           05  CTOTAL               PIC S9(11)V99 COMP-3.\n"
            "           05  CKEY                 PIC X(8).\n"
        )
        statements = [
            "EXEC SQL SELECT AMT INTO :CAMT FROM T1 WHERE ID = 1 END-EXEC",
            "ADD CAMT TO CTOTAL",
            "MOVE CTOTAL TO CAMT",
            "MOVE CTOTAL TO CKEY",
        ]
        result = _analyze(copybook, {"P0": _program("P0", statements)})
        self.assertSettles(result)
        [total] = [f for f in result.findings if f.node_id == "var:P0::CTOTAL"]
        # 11 integer digits, plus the 2 the column added - once.
        self.assertIn("S9(13)V9(2)", total.remediation)

    def test_a_string_that_feeds_its_own_source(self):
        copybook = "       01  CREC.\n           05  CAMT                 PIC S9(9)V99.\n"
        working = "       01  WS-LINE                  PIC X(20).\n       01  WS-OUT                   PIC X(8).\n"
        statements = [
            "EXEC SQL SELECT AMT INTO :CAMT FROM T1 WHERE ID = 1 END-EXEC",
            "MOVE CAMT TO WS-LINE",
            "STRING WS-LINE DELIMITED BY SIZE CAMT DELIMITED BY SIZE INTO WS-OUT END-STRING",
            "MOVE WS-OUT TO WS-LINE",
        ]
        result = _analyze(copybook, {"P0": _program("P0", statements, working=working)})
        self.assertSettles(result)

    def test_a_redefining_item_larger_than_its_base(self):
        # X(20) over a 6-byte packed field: the base growing used to push the
        # overlay up by the base's delta, which pushed the base up again.
        copybook = (
            "       01  CREC.\n"
            "           05  CAMT                 PIC S9(9)V99 COMP-3.\n"
            "       01  CRED REDEFINES CREC PIC X(20).\n"
        )
        statements = [
            "EXEC SQL SELECT AMT INTO :CAMT FROM T1 WHERE ID = 1 END-EXEC",
            "MOVE CRED TO CAMT",
        ]
        result = _analyze(copybook, {"P0": _program("P0", statements)})
        self.assertSettles(result)


_NUMERIC = ["S9(7)V99 COMP-3", "S9(9)V99 COMP-3", "9(8)", "S9(5) COMP", "9(6)", "S9(11)V99"]


def _random_fields(rng, prefix):
    fields = []
    for index in range(rng.randint(3, 6)):
        if rng.random() < 0.5:
            fields.append((f"{prefix}N{index}", "PIC " + rng.choice(_NUMERIC)))
        else:
            width = 8 if rng.random() < 0.6 else rng.choice([4, 10, 20])
            fields.append((f"{prefix}X{index}", f"PIC X({width})"))
    return fields


def _random_case(seed):
    rng = random.Random(seed)
    shared = _random_fields(rng, "C")
    lines = ["       01  CREC."]
    for name, pic in shared:
        occurs = " OCCURS 3 TIMES" if rng.random() < 0.15 else ""
        lines.append(f"           05  {name:<20} {pic}{occurs}.")
    if rng.random() < 0.5:
        rng.choice(shared)  # keeps the seeds in DIVERGED reproducing the same shapes
        lines.append(f"       01  CRED REDEFINES CREC PIC X({rng.choice([8, 20, 60, 200])}).")
    copybook = "\n".join(lines) + "\n"

    names = [name for name, _ in shared]
    program_names = [f"P{index}" for index in range(rng.randint(2, 4))]
    programs = {}
    for position, program in enumerate(program_names):
        local = _random_fields(rng, f"W{position}")
        working = [f"       01  {name:<24} {pic}." for name, pic in local]
        if rng.random() < 0.4:
            base = rng.choice(local)[0]
            working.append(f"       01  {base}-R REDEFINES {base} PIC X(8).")
            local.append((base + "-R", ""))
        known = names + [name for name, _ in local]
        statements = []
        if rng.random() < 0.8:
            host = rng.choice(known)
            statements.append(f"EXEC SQL SELECT AMT INTO :{host} FROM T1 WHERE ID = 1 END-EXEC")
        for _ in range(rng.randint(3, 8)):
            source, target = rng.choice(known), rng.choice(known)
            if source == target:
                continue
            roll = rng.random()
            if roll < 0.4:
                statements.append(f"MOVE {source} TO {target}")
            elif roll < 0.6:
                statements.append(f"ADD {source} TO {target}")
            elif roll < 0.7:
                statements.append(f"COMPUTE {target} = {source} * 2")
            elif roll < 0.85:
                other = rng.choice(known)
                statements.append(
                    f"STRING {source} DELIMITED BY SIZE {other} DELIMITED BY SIZE "
                    f"INTO {target} END-STRING"
                )
            elif roll < 0.95 and position + 1 < len(program_names):
                statements.append(f"CALL '{program_names[position + 1]}' USING {source}")
            else:
                statements.append(f"MOVE CREC TO {target}")
        linkage, using = "", ""
        if position > 0:
            parameter = f"LK{position}"
            pic = rng.choice(["X(8)", "S9(9)V99 COMP-3", "9(8)"])
            linkage = f"       LINKAGE SECTION.\n       01  {parameter:<24} PIC {pic}.\n"
            using = f" USING {parameter}"
            statements.append(f"MOVE {parameter} TO {rng.choice(known)}")
        programs[program] = _program(
            program, statements, working="\n".join(working), linkage=linkage, using=using
        )
    return copybook, programs


class GeneratedCycleTests(unittest.TestCase, _Settles):
    # Every one of these diverged; the range after them is a broader net.
    DIVERGED = [19, 20, 34, 38, 50, 115, 118, 128, 129, 132, 134, 152, 163, 182, 185, 198, 298, 332]

    def test_known_diverging_shapes_settle(self):
        for seed in self.DIVERGED:
            with self.subTest(seed=seed):
                self.assertSettles(_analyze(*_random_case(seed)))

    def test_random_shapes_settle(self):
        for seed in range(400, 520):
            with self.subTest(seed=seed):
                self.assertSettles(_analyze(*_random_case(seed)))


if __name__ == "__main__":
    unittest.main()
