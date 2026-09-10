"""Diagnose why a REDEFINES target is not being reported.

Ships inside the package so it travels with the folder. Run it against one
real source file and paste the whole output:

    python -m cobol_impact_analyzer.diagnose <FILE.pco> [--copybook DIR]... [--name NAME]

It prints:
  1. which cobol_impact_analyzer is loaded, and whether it carries the
     REDEFINES-note code (so a stale copy is obvious)
  2. every 01-level item the parser extracted, with its PICTURE and REDEFINES
  3. for each REDEFINES, whether its target name resolved to a parsed item
  4. the raw source lines around each REDEFINES and around the item it names,
     with tabs and line ends made visible
  5. parser warnings
  6. the detected source format

--name limits sections 3-4 to REDEFINES whose target matches NAME (case- and
hyphen/underscore-insensitive). Nothing is sent anywhere; this only prints.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _rule(title: str) -> None:
    print("\n" + "=" * 70 + "\n" + title + "\n" + "=" * 70)


def _visible(text: str) -> str:
    return text.replace("\t", "<TAB>").rstrip("\n") + "$"


def _loose(name: str) -> str:
    return name.upper().replace("_", "-")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cobol_impact_analyzer.diagnose")
    ap.add_argument("file", type=Path)
    ap.add_argument("--copybook", action="append", default=[], type=Path)
    ap.add_argument("--name", default="")
    args = ap.parse_args(argv)

    _rule("1. loaded package")
    from . import analyzer, cobolsrc, report
    from .copybook import CopybookResolver
    from .pco import ProgramParser

    print("analyzer :", analyzer.__file__)
    print("report   :", report.__file__)
    print(
        "analyzer builds the overlap note      :",
        hasattr(analyzer.ImpactAnalyzer, "_redefines_notes")
        and "overlays the same storage"
        in Path(analyzer.__file__).read_text(encoding="utf-8", errors="replace"),
    )
    try:
        report_src = Path(report.__file__).read_text(encoding="utf-8", errors="replace")
        print(
            "report shows notes in every section  :",
            'edit.get("notes")' in report_src and "ul class='notes'" in report_src,
        )
    except OSError as exc:
        print("could not read report.py:", exc)

    if not args.file.exists():
        print("\nfile not found:", args.file)
        return 2

    raw = args.file.read_text(encoding="utf-8", errors="replace").splitlines()

    _rule("2. items the parser extracted (01-level, plus anything with REDEFINES)")
    resolver = (
        CopybookResolver([Path(p) for p in args.copybook]) if args.copybook else None
    )
    program = ProgramParser(resolver).parse(args.file)
    fields = program.data.fields
    names = {f.name for f in fields.values()}
    loose_names = {_loose(n) for n in names}
    want = _loose(args.name) if args.name else ""

    for key in program.data.order:
        f = fields[key]
        if f.level == 1 or f.redefines:
            src = str(f.source.line) if f.source else "?"
            print(
                f"  line {src:>5}  {f.level:>2}  {f.name:<30} "
                f"pic={f.picture!r:<16} redefines={f.redefines or '-'}"
            )

    _rule("3. does each REDEFINES target resolve to a parsed item?")
    redefiners = [
        fields[k]
        for k in program.data.order
        if fields[k].redefines and (not want or _loose(fields[k].redefines) == want)
    ]
    if not redefiners:
        print("  (no REDEFINES matched)")
    for f in redefiners:
        target = f.redefines
        print(f"  {f.name} REDEFINES {target}")
        print(f"      target parsed (exact name)            : {target in names}")
        print(f"      target parsed (hyphen/underscore loose): {_loose(target) in loose_names}")
        tgt = next(
            (fields[k] for k in program.data.order if fields[k].name == target), None
        )
        if tgt is not None:
            print(
                f"      target: level {tgt.level}  pic={tgt.picture!r}  "
                f"storage_bytes={tgt.storage_bytes}  children={len(tgt.children)}"
            )

    _rule("4. raw source around each REDEFINES and its target")
    for f in redefiners:
        for label, ident in (
            (f"REDEFINER {f.name}", f.name),
            (f"TARGET {f.redefines}", f.redefines),
        ):
            hits = [
                i
                for i, ln in enumerate(raw)
                if f" {ident} " in f" {ln.strip()} "
                or ln.strip().endswith(f" {ident}")
                or ln.strip().endswith(f" {ident}.")
                or ln.strip().split()[1:2] == [ident]
            ]
            first = hits[0] if hits else None
            where = f"line {first + 1}" if first is not None else "NOT FOUND in raw text"
            print(f"\n  {label}  (first match: {where})")
            if first is not None:
                lo, hi = max(0, first - 2), min(len(raw), first + 3)
                for i in range(lo, hi):
                    mark = ">>" if i == first else "  "
                    print(f"    {mark} {i + 1:>5} |{_visible(raw[i])}")

    _rule("5. parser warnings")
    for w in program.warnings or ["(none)"]:
        print("  " + w)

    _rule("6. detected source format")
    print("  autodetect says:", cobolsrc.detect_format(raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
