"""The breaks report: what one quality run found, as JSON (for machines) and Markdown (for people).

Sections from stages that did not run (because the run stopped earlier) are null, so a reader
can always tell "checked and fine" from "not checked".
"""

import json
from pathlib import Path

import pandas as pd

from riskplatform.quality.files import FileCheckReport
from riskplatform.quality.reconcile import Reconciliation
from riskplatform.schemas import RAW_TABLES


def table_of(file_name: str) -> str:
    """The raw table a file belongs to, from its path relative to the raw folder.

    Files under positions/ are positions; a top-level <table>.parquet is that table (rates, fx).
    """
    if file_name.startswith("positions/"):
        return "positions"
    return file_name.removesuffix(".parquet")


def _rows_by_table(frames: dict[str, pd.DataFrame]) -> dict[str, int]:
    counts = {table: 0 for table in RAW_TABLES}
    for name, frame in frames.items():
        counts[table_of(name)] += len(frame)
    return counts


def _records(frame: pd.DataFrame) -> list[dict]:
    """Rows as plain dicts, with empty values as None (so the JSON stays valid)."""
    return frame.astype(object).where(frame.notna(), None).to_dict(orient="records")


def _checks(results) -> list[dict]:
    return [{"name": r.name, "passed": r.passed, "offending_rows": r.failures} for r in results]


def build_report(
    *,
    run_id: str,
    run_at: pd.Timestamp,
    status: str,
    stop_reasons: list[str],
    raw_dir: Path,
    arrived: dict[str, pd.DataFrame],
    validated: dict[str, pd.DataFrame],
    quarantine: pd.DataFrame,
    reconciliation: Reconciliation | None,
    file_checks: FileCheckReport | None,
    outliers: pd.DataFrame | None,
) -> dict:
    """Everything one run found, as a JSON-ready dict."""
    by_check = (
        quarantine.groupby(["table", "check", "column"], dropna=False)
        .size()
        .rename("records")
        .reset_index()
    )
    report = {
        "run_id": run_id,
        "run_at": run_at.isoformat(),
        "status": status,
        "stop_reasons": stop_reasons,
        "raw_dir": Path(raw_dir).as_posix(),
        "files_arrived": len(arrived),
        "rows": {
            "arrived": _rows_by_table(arrived),
            "validated": _rows_by_table(validated),
            "quarantined": int(
                quarantine[["source_file", "row_number"]].drop_duplicates().shape[0]
            ),
            "quarantine_records": len(quarantine),
        },
        "quarantine_by_check": _records(by_check),
        "reconciliation": None,
        "file_checks": None,
        "outliers": None,
    }
    if reconciliation is not None:
        files = reconciliation.files
        balances = reconciliation.balances
        report["reconciliation"] = {
            "files_with_quarantine": _records(files[files["quarantined_rows"] != 0]),
            "balances_with_quarantine": _records(balances[balances["quarantined_mn"] != 0]),
        }
    if file_checks is not None:
        report["file_checks"] = {
            "stops": _checks(file_checks.stops),
            "warnings": _checks(file_checks.warnings),
        }
    if outliers is not None:
        report["outliers"] = {
            "count": len(outliers),
            "by_check": {k: int(v) for k, v in outliers["check"].value_counts().items()},
            "items": _records(outliers.assign(date=outliers["date"].astype(str))),
        }
    return report


def _md_table(rows: list[dict], columns: list[str], max_rows: int) -> list[str]:
    """A Markdown table of the first max_rows rows, with a note if any were cut."""
    if not rows:
        return ["None.", ""]
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows[:max_rows]:
        cells = [
            "" if row[c] is None else f"{row[c]:,.2f}" if isinstance(row[c], float) else str(row[c])
            for c in columns
        ]
        lines.append("| " + " | ".join(cells) + " |")
    if len(rows) > max_rows:
        lines.append(f"\n... and {len(rows) - max_rows} more (see breaks_report.json).")
    return [*lines, ""]


def _section(title: str, body: list[str] | None) -> list[str]:
    return [
        f"## {title}",
        "",
        *(body if body is not None else ["Not run (the run stopped earlier).", ""]),
    ]


def render_markdown(report: dict, max_rows: int) -> str:
    """A one-page, human-readable breaks report; each table shows at most max_rows rows."""
    rows = report["rows"]

    def table(table_rows: list[dict], columns: list[str]) -> list[str]:
        return _md_table(table_rows, columns, max_rows)

    lines = [
        f"# Breaks report: run {report['run_id']}",
        "",
        f"**Status: {report['status'].upper()}**  ",
        f"Run at {report['run_at']} on `{report['raw_dir']}` ({report['files_arrived']} files).",
        "",
    ]
    if report["stop_reasons"]:
        lines += ["Nothing was published. Reasons:", ""]
        lines += [f"- {reason}" for reason in report["stop_reasons"]] + [""]

    lines += _section(
        "Rows",
        table(
            [
                {"table": t, "arrived": rows["arrived"][t], "validated": rows["validated"][t]}
                for t in RAW_TABLES
            ],
            ["table", "arrived", "validated"],
        )
        + [f"Quarantined rows: {rows['quarantined']} ({rows['quarantine_records']} records).", ""],
    )
    lines += _section(
        "Quarantine by check",
        table(report["quarantine_by_check"], ["table", "check", "column", "records"]),
    )
    recon = report["reconciliation"]
    lines += _section(
        "Reconciliation to source (quarantined amounts)",
        None
        if recon is None
        else table(
            recon["balances_with_quarantine"],
            ["file", "entity", "currency", "side", "source_mn", "validated_mn", "quarantined_mn"],
        ),
    )
    checks = report["file_checks"]
    lines += _section(
        "File-level checks",
        None
        if checks is None
        else table(
            [{**c, "kind": kind} for kind in ("stops", "warnings") for c in checks[kind]],
            ["kind", "name", "passed", "offending_rows"],
        ),
    )
    outliers = report["outliers"]
    lines += _section(
        "Outlier warnings (for review; nothing quarantined)",
        None
        if outliers is None
        else table(outliers["items"], ["check", "date", "subject", "value", "threshold"]),
    )
    return "\n".join(lines)


def write_report(run_dir: Path, report: dict, max_markdown_rows: int) -> None:
    """Save breaks_report.json (every row) and breaks_report.md (tables cut) in the run's folder."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "breaks_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = render_markdown(report, max_markdown_rows)
    (run_dir / "breaks_report.md").write_text(markdown, encoding="utf-8")
