"""Tableau-ready extracts: one CSV per result table of a run, plus one long table for trends.

Run with:  python -m riskplatform.extracts          (or: riskplatform-extracts; the latest run)
           python -m riskplatform.extracts --source s3     (the run in latest.json)
The pipeline also writes them for every run that gets this far (its extracts stage).

They are written from the riskplatform.query views, so they hold exactly the numbers the query
command, the dashboard and the alert email show. Files, in data/extracts/<run_id>/:

    lcr, nsfr, ladder               liquidity (metrics/<run_id>/)
    eve, nii, irrbb_risk, repricing_gap
                                    IRRBB (irrbb/<run_id>/; risk and gap renamed to say what
                                    they are)
    limits, limit_changes, balance_drivers
                                    internal limits and what changed since the day before
    quarantine                      rows the quality layer held back
    early_warning                   the early-warning indicators (retail outflow, break-even)
    liquidity_ratios                LCR and NSFR stacked (metric, value, regulatory_minimum,
                                    internal_limit): the shape a Tableau trend chart wants.
                                    internal_limit is empty per currency: the limits apply to
                                    entities and the group only (config/limits.yaml)
    manifest.json                   the run, and each file's rows and columns

Made for Tableau (and Excel): UTF-8 CSV with a header row; dates as YYYY-MM-DD (no time);
ratios as fractions (1.10 = 110%, format them as % in Tableau); amounts in GBP mn unless the
table has a currency column at the entity_currency level; an empty cell for a missing value. An
infinite value (a utilisation with nothing to divide by) is written empty too: one "inf" would
make Tableau read the whole column as text. Every table with an entity has `who` (the entity,
or GROUP for the group) so group rows are never a blank on a chart.

data/extracts/latest/ is then replaced by a copy, so a Tableau workbook can point at one fixed
folder. Labelled simplification: the copy is not atomic (Windows cannot swap folders in one
step), so a workbook refreshing at that moment can see a half-copied folder; refresh again.
"""

import argparse
import json
import logging
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from riskplatform.config import load_storage_config
from riskplatform.logs import setup_logging
from riskplatform.query import QueryInputError, connect, open_latest

logger = logging.getLogger(__name__)

GROUP = "GROUP"
LATEST = "latest"
# extract file name -> the query view it comes from (renamed where the view name is too short)
VIEWS = {
    "lcr": "lcr",
    "nsfr": "nsfr",
    "ladder": "ladder",
    "eve": "eve",
    "nii": "nii",
    "irrbb_risk": "risk",
    "repricing_gap": "gap",
    "limits": "limits",
    "limit_changes": "limit_changes",
    "balance_drivers": "balance_drivers",
    "quarantine": "quarantine",
    "early_warning": "early_warning",
}
LIQUIDITY_RATIOS = """
    WITH ratios AS (
        SELECT date, level, entity, currency, regime, 'lcr' AS metric, lcr AS value,
               minimum AS regulatory_minimum, meets_minimum FROM lcr
        UNION ALL
        SELECT date, level, entity, currency, regime, 'nsfr', nsfr, minimum, meets_minimum
        FROM nsfr
    ),
    internal AS (SELECT limit_name, max(limit_value) AS internal_limit FROM limits GROUP BY 1)
    SELECT CAST(r.date AS DATE) AS date, r.level, coalesce(r.entity, 'GROUP') AS who, r.entity,
           r.currency, r.regime, upper(r.metric) AS metric, r.value, r.regulatory_minimum,
           CASE WHEN r.level IN ('entity', 'group') THEN i.internal_limit END AS internal_limit,
           r.meets_minimum
    FROM ratios r LEFT JOIN internal i ON i.limit_name = r.metric
    ORDER BY date, metric, regime, level, who, currency
"""


def _view_sql(con: duckdb.DuckDBPyConnection, view: str) -> str:
    """SELECT every column of a view, dates as DATE, and `who` after level if it has entities."""
    columns = [row[0] for row in con.execute(f"DESCRIBE {view}").fetchall()]
    selected = []
    for column in columns:
        quoted = f'"{column}"'
        if column.endswith("date"):  # date, previous_date, latest_date
            selected.append(f"CAST({quoted} AS DATE) AS {quoted}")
        else:
            selected.append(quoted)
        if column == "entity":
            selected.append(f"coalesce(entity, '{GROUP}') AS who")
    order = ", ".join(c for c in ("date", "regime", "level", "entity") if c in columns) or "1"
    return f"SELECT {', '.join(selected)} FROM {view} ORDER BY {order}"


def _clean(table: pd.DataFrame) -> pd.DataFrame:
    """Infinities become missing (an empty cell), so a numeric column stays numeric."""
    numeric = table.select_dtypes(include="number").columns
    table[numeric] = table[numeric].replace([np.inf, -np.inf], np.nan)
    return table


def extract_tables(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    """Every extract, by file name, from a connection to one run's views."""
    tables = {name: _clean(con.execute(_view_sql(con, view)).df()) for name, view in VIEWS.items()}
    tables["liquidity_ratios"] = _clean(con.execute(LIQUIDITY_RATIOS).df())
    return tables


@dataclass(frozen=True)
class ExtractsRun:
    run_id: str
    out_dir: Path  # <out>/<run_id>/
    latest_dir: Path  # <out>/latest/
    manifest: dict


def write_extracts(con: duckdb.DuckDBPyConnection, run_id: str, out: str | Path) -> ExtractsRun:
    """Write the run's extracts to <out>/<run_id>/, then copy them to <out>/latest/."""
    out = Path(out)
    out_dir = out / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, table in extract_tables(con).items():
        table.to_csv(out_dir / f"{name}.csv", index=False, encoding="utf-8", date_format="%Y-%m-%d")
        files[f"{name}.csv"] = {"rows": len(table), "columns": list(table.columns)}
    dates = con.execute("SELECT max(date) FROM limits").fetchone()[0]
    manifest = {
        "run_id": run_id,
        "as_of": None if dates is None else pd.Timestamp(dates).date().isoformat(),
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "format": "UTF-8 CSV, header row, dates YYYY-MM-DD, ratios as fractions, empty = missing",
        "files": files,
    }
    text = json.dumps(manifest, indent=2, allow_nan=False)
    (out_dir / "manifest.json").write_text(text, encoding="utf-8")

    latest_dir = out / LATEST
    if latest_dir.exists():
        shutil.rmtree(latest_dir)
    shutil.copytree(out_dir, latest_dir)
    total = sum(f["rows"] for f in files.values())
    logger.info("Wrote %d extracts (%s rows) to %s and %s", len(files), f"{total:,}", out_dir,
                latest_dir)  # fmt: skip
    return ExtractsRun(run_id=run_id, out_dir=out_dir, latest_dir=latest_dir, manifest=manifest)


def run_extracts(data_dir: str | Path, run_id: str) -> ExtractsRun:
    """The pipeline's extracts stage: this run's results under data_dir, to data_dir/extracts/."""
    data_dir = Path(data_dir)
    return write_extracts(connect(data_dir, run_id), run_id, data_dir / "extracts")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write Tableau-ready CSVs of the latest run.")
    parser.add_argument("--source", choices=["local", "s3"], help="default: storage.yaml backend")
    parser.add_argument("--storage", type=Path, default=Path("config/storage.yaml"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--cache", type=Path, default=Path("data/cache"))
    parser.add_argument("--out", type=Path, default=Path("data/extracts"))
    args = parser.parse_args(argv)

    setup_logging()
    storage = load_storage_config(args.storage)
    try:
        con, run_id = open_latest(args.source or storage.backend, args.data, args.cache, storage)
    except QueryInputError as error:
        logger.error("%s", error)
        return 1
    run = write_extracts(con, run_id, args.out)
    print(run.latest_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
