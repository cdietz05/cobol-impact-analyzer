"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import report
from .analyzer import analyze
from .models import Severity
from .progress import Progress
from .spec import ChangeSpec, SpecError, build_change, load_spec

_EXIT_OK = 0
_EXIT_FINDINGS = 1
_EXIT_USAGE = 2

_SEVERITY_NAMES = [level.value for level in Severity]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cobol-impact",
        description=(
            "Trace the impact of widening SQL columns through Pro*COBOL (.pco) "
            "sources: which host variables, derived working-storage fields, record "
            "layouts and other database columns must change, and to what."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  cobol-impact --spec change.json --html report.html\n"
            "  cobol-impact --table CUSTOMER --column CUST_NAME \\\n"
            "               --from 'VARCHAR2(30)' --to 'VARCHAR2(60)' \\\n"
            "               --source src/ --copybook copybooks/\n"
        ),
    )

    source = parser.add_argument_group("what to change")
    source.add_argument("--spec", type=Path, help="JSON change specification")
    source.add_argument("--table", help="SQL table name (with --column/--from/--to)")
    source.add_argument(
        "--column",
        action="append",
        default=[],
        metavar="NAME",
        help="column to widen; repeatable, paired positionally with --from/--to",
    )
    source.add_argument(
        "--from",
        dest="old_types",
        action="append",
        default=[],
        metavar="TYPE",
        help="current SQL type of the matching --column, e.g. 'VARCHAR2(30)'",
    )
    source.add_argument(
        "--to",
        dest="new_types",
        action="append",
        default=[],
        metavar="TYPE",
        help="new SQL type of the matching --column, e.g. 'VARCHAR2(60)'",
    )

    scan = parser.add_argument_group("where to look")
    scan.add_argument(
        "--source",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="directory or file of COBOL sources; repeatable",
    )
    scan.add_argument(
        "--copybook",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="directory holding copybooks; repeatable",
    )
    scan.add_argument(
        "--pattern",
        action="append",
        default=[],
        metavar="GLOB",
        help="source filename pattern (default: *.pco and *.PCO)",
    )
    scan.add_argument(
        "--copybook-ext",
        action="append",
        default=[],
        metavar="EXT",
        help=(
            "extension to treat as a copybook when indexing, e.g. --copybook-ext .cpb; "
            "repeatable. Defaults cover .cpy/.cbl/.cob/.inc/.copy/.cpb/.cbk/.src and "
            "extensionless files"
        ),
    )
    scan.add_argument(
        "--format",
        dest="source_format",
        choices=["fixed", "free"],
        help="force COBOL source format instead of auto-detecting it",
    )
    scan.add_argument(
        "--max-depth",
        type=int,
        default=0,
        metavar="N",
        help="stop propagating after N hops (default: unlimited)",
    )
    scan.add_argument(
        "--global-vars",
        action="store_true",
        help=(
            "treat identically named fields in different programs as one node, "
            "even when declared inline rather than in a shared copybook"
        ),
    )

    out = parser.add_argument_group("output")
    out.add_argument("--json", type=Path, metavar="FILE", help="write JSON findings")
    out.add_argument("--csv", type=Path, metavar="FILE", help="write CSV findings")
    out.add_argument("--html", type=Path, metavar="FILE", help="write an HTML report")
    out.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help="write impact.json, impact.csv and impact.html into DIR",
    )
    out.add_argument(
        "--include-graph",
        action="store_true",
        help="embed the full data-flow graph in the JSON output",
    )
    out.add_argument("--quiet", action="store_true", help="suppress the terminal report")
    out.add_argument(
        "--no-progress",
        action="store_true",
        help=(
            "suppress the progress lines on stderr; they are on by default because a "
            "large scan is otherwise silent for minutes"
        ),
    )
    out.add_argument("--verbose", action="store_true", help="include the reasoning per finding")
    out.add_argument(
        "--fail-on",
        choices=_SEVERITY_NAMES + ["NONE"],
        default="NONE",
        help="exit non-zero when a finding of this severity or worse exists",
    )
    return parser


def _spec_from_args(args: argparse.Namespace) -> ChangeSpec:
    if args.spec:
        spec = load_spec(args.spec)
    else:
        if not args.table:
            raise SpecError("give either --spec FILE or --table with --column/--from/--to")
        if not (len(args.column) == len(args.old_types) == len(args.new_types)):
            raise SpecError(
                "--column, --from and --to must be repeated the same number of times "
                f"(got {len(args.column)}, {len(args.old_types)}, {len(args.new_types)})"
            )
        if not args.column:
            raise SpecError("--table needs at least one --column/--from/--to triple")
        spec = ChangeSpec(
            changes=[
                build_change(args.table, column, old, new)
                for column, old, new in zip(args.column, args.old_types, args.new_types)
            ]
        )

    if args.source:
        spec.source_paths = [Path(path).resolve() for path in args.source]
    if args.copybook:
        spec.copybook_paths = [Path(path).resolve() for path in args.copybook]
    if args.pattern:
        spec.source_patterns = list(args.pattern)
    elif spec.source_patterns == ["*.pco"]:
        spec.source_patterns = ["*.pco", "*.PCO"]
    if args.copybook_ext:
        spec.copybook_suffixes = [
            ext if ext.startswith(".") or ext == "" else f".{ext}"
            for ext in (value.lower() for value in args.copybook_ext)
        ]
    if args.source_format:
        spec.source_format = args.source_format
    if args.max_depth:
        spec.max_depth = args.max_depth
    if args.global_vars:
        spec.global_variable_scope = True

    if not spec.source_paths:
        raise SpecError("no source paths given; use --source or 'source_paths' in the spec")
    if not spec.copybook_paths:
        # Copybooks often live alongside the sources.
        spec.copybook_paths = list(spec.source_paths)
    return spec


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        spec = _spec_from_args(args)
    except SpecError as error:
        parser.error(str(error))
        return _EXIT_USAGE

    result = analyze(spec, Progress(enabled=not args.no_progress))

    if not args.quiet:
        sys.stdout.write(report.to_text(result, verbose=args.verbose))

    targets: list[tuple[str, Path]] = []
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        targets.extend(
            [
                ("json", args.out / "impact.json"),
                ("csv", args.out / "impact.csv"),
                ("html", args.out / "impact.html"),
            ]
        )
    if args.json:
        targets.append(("json", args.json))
    if args.csv:
        targets.append(("csv", args.csv))
    if args.html:
        targets.append(("html", args.html))

    for kind, path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "json":
            report.write_json(result, path, include_graph=args.include_graph)
        elif kind == "csv":
            report.write_csv(result, path)
        else:
            report.write_html(result, path)
        if not args.quiet:
            print(f"wrote {kind}: {path}")

    if args.fail_on != "NONE":
        threshold = Severity(args.fail_on).rank
        if any(finding.severity.rank <= threshold for finding in result.findings):
            return _EXIT_FINDINGS
    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
