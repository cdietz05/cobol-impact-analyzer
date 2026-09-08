"""cobol-impact-analyzer: SQL column widening impact tracing for Pro*COBOL.

Typical use from Python::

    from pathlib import Path
    from cobol_impact_analyzer import ChangeSpec, analyze, build_change, to_text

    spec = ChangeSpec(
        changes=[build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)")],
        source_paths=[Path("src")],
        copybook_paths=[Path("copybooks")],
    )
    print(to_text(analyze(spec)))
"""

from __future__ import annotations

from .analyzer import AnalysisResult, ImpactAnalyzer, ImpactGraph, analyze
from .models import Capacity, ColumnChange, EdgeKind, Finding, Kind, Severity
from .picture import PictureInfo, parse_picture, render_picture
from .report import to_html, to_json, to_text, write_csv, write_html, write_json
from .spec import ChangeSpec, SpecError, build_change, load_spec
from .sqltypes import SqlType, parse_sql_type

__version__ = "0.1.0"

__all__ = [
    "AnalysisResult",
    "Capacity",
    "ChangeSpec",
    "ColumnChange",
    "EdgeKind",
    "Finding",
    "ImpactAnalyzer",
    "ImpactGraph",
    "Kind",
    "PictureInfo",
    "Severity",
    "SpecError",
    "SqlType",
    "__version__",
    "analyze",
    "build_change",
    "load_spec",
    "parse_picture",
    "parse_sql_type",
    "render_picture",
    "to_html",
    "to_json",
    "to_text",
    "write_csv",
    "write_html",
    "write_json",
]
