"""The dashboard's data: one small SQL query per chart over the riskplatform.query views.

No Streamlit here, so every number the dashboard shows is tested without a browser
(tests/test_dashboard.py). Every function takes a DuckDB connection from open_dashboard, which
opens the latest good run with the same rule as the query command: data/validated/_run.json
locally, latest.json in S3. Nothing is recalculated: the figures are the pipeline's own.

Levels: the entity and group rows (the levels the internal limits apply to); `who` is the entity,
or GROUP for the group. Ratios are fractions (1.10 = 110%); amounts are GBP mn.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from riskplatform.config import StorageConfig
from riskplatform.query import Source, open_latest

GROUP = "GROUP"
WHO = f"coalesce(entity, '{GROUP}') AS who"
LEVELS = "level IN ('entity', 'group')"
RATIOS = {"lcr": "lcr", "nsfr": "nsfr"}  # metric -> its view (the column has the same name)


@dataclass(frozen=True)
class Dashboard:
    con: duckdb.DuckDBPyConnection
    run_id: str
    results_dir: Path  # data/ locally, the download cache for S3 (same layout)
    source: Source


def open_dashboard(
    source: Source, data_dir: str | Path, cache_dir: str | Path, storage: StorageConfig
) -> Dashboard:
    """The latest good run, from data/ or from S3 (downloaded once into cache_dir)."""
    con, run_id = open_latest(source, data_dir, cache_dir, storage)
    results_dir = Path(data_dir if source == "local" else cache_dir)
    return Dashboard(con=con, run_id=run_id, results_dir=results_dir, source=source)


def _df(con: duckdb.DuckDBPyConnection, sql: str, *params) -> pd.DataFrame:
    return con.execute(sql, list(params)).df()


def business_days(con: duckdb.DuckDBPyConnection) -> int:
    """How many business days the run covers."""
    return int(con.execute("SELECT count(DISTINCT date) FROM limits").fetchone()[0])


def latest_date(con: duckdb.DuckDBPyConnection) -> pd.Timestamp:
    return pd.Timestamp(con.execute("SELECT max(date) FROM limits").fetchone()[0])


def limit_status(con: duckdb.DuckDBPyConnection, regime: str) -> pd.DataFrame:
    """Every internal limit on the latest date, with its movement since the day before."""
    return _df(
        con,
        f"""
        SELECT l.limit_name, coalesce(l.entity, '{GROUP}') AS who, l.kind, l.actual,
               l.limit_value, l.utilisation, l.status,
               coalesce(c.movement, 'unchanged') AS movement, c.previous_status
        FROM limits l
        LEFT JOIN limit_changes c
          ON c.level = l.level AND c.regime = l.regime AND c.limit_name = l.limit_name
         AND c.currency = l.currency AND c.entity IS NOT DISTINCT FROM l.entity
        WHERE l.regime = ? AND l.date = (SELECT max(date) FROM limits)
        ORDER BY l.limit_name, l.level DESC, who
        """,
        regime,
    )


def status_counts(con: duckdb.DuckDBPyConnection, regime: str) -> dict[str, int]:
    """How many limits are ok, at warning and in breach on the latest date."""
    counts = limit_status(con, regime)["status"].value_counts()
    return {status: int(counts.get(status, 0)) for status in ("ok", "warning", "breach")}


def ratio_trend(con: duckdb.DuckDBPyConnection, metric: str, regime: str) -> pd.DataFrame:
    """LCR or NSFR per entity and the group over every date, with the regulatory minimum."""
    view = RATIOS[metric]  # a fixed name, never user text, so safe in the SQL
    return _df(
        con,
        f"""
        SELECT date, {WHO}, {view} AS value, minimum
        FROM {view} WHERE {LEVELS} AND regime = ?
        ORDER BY date, who
        """,
        regime,
    )


def limit_value(con: duckdb.DuckDBPyConnection, limit_name: str) -> float:
    """The internal limit (config/limits.yaml), e.g. 1.10 for the LCR floor."""
    row = con.execute(
        "SELECT max(limit_value) FROM limits WHERE limit_name = ?", [limit_name]
    ).fetchone()
    return float(row[0])


def eve_by_scenario(con: duckdb.DuckDBPyConnection, regime: str) -> pd.DataFrame:
    """The EVE loss per scenario on the latest date (aggregated_loss: positive = a loss)."""
    return _df(
        con,
        f"""
        SELECT {WHO}, scenario, aggregated_loss AS eve_loss
        FROM eve WHERE {LEVELS} AND regime = ? AND date = (SELECT max(date) FROM eve)
        ORDER BY who, scenario
        """,
        regime,
    )


def nii_by_scenario(con: duckdb.DuckDBPyConnection, regime: str) -> pd.DataFrame:
    """The change in NII per scenario on the latest date (positive = more income)."""
    return _df(
        con,
        f"""
        SELECT {WHO}, scenario, delta_nii
        FROM nii WHERE {LEVELS} AND regime = ? AND date = (SELECT max(date) FROM nii)
        ORDER BY who, scenario
        """,
        regime,
    )


def eve_against_tier1(con: duckdb.DuckDBPyConnection, regime: str) -> pd.DataFrame:
    """The worst EVE loss as a share of Tier 1, against the outlier test (SRP31.82)."""
    return _df(
        con,
        f"""
        SELECT {WHO}, eve_risk, tier1, eve_risk / tier1 AS share, threshold / tier1 AS test,
               worst_scenario, outlier
        FROM risk WHERE {LEVELS} AND regime = ? AND date = (SELECT max(date) FROM risk)
        ORDER BY level DESC, who
        """,
        regime,
    )


def limit_movements(con: duckdb.DuckDBPyConnection, regime: str) -> pd.DataFrame:
    """Every limit whose status moved since the day before (new, worse, resolved, better)."""
    return _df(
        con,
        f"""
        SELECT movement, limit_name, {WHO}, previous_status, latest_status,
               previous_utilisation, latest_utilisation
        FROM limit_changes WHERE regime = ? AND movement <> 'unchanged'
        """,
        regime,
    )


def compared_dates(con: duckdb.DuckDBPyConnection) -> tuple[pd.Timestamp | None, pd.Timestamp]:
    """(the day before, the latest day) of the changes; (None, latest) for a one-day run."""
    row = con.execute("SELECT max(previous_date), max(latest_date) FROM limit_changes").fetchone()
    if row[1] is None:
        return None, latest_date(con)
    return pd.Timestamp(row[0]), pd.Timestamp(row[1])


def top_drivers(con: duckdb.DuckDBPyConnection, n: int) -> pd.DataFrame:
    """The n largest balance changes since the day before, split into volume and FX."""
    return _df(
        con,
        """
        SELECT entity, category, side, change_gbp, volume_gbp, fx_gbp
        FROM balance_drivers ORDER BY abs(change_gbp) DESC LIMIT ?
        """,
        n,
    )


def quarantine_by_check(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Quarantined rows of the run's quality layer, per table and check."""
    return _df(
        con,
        """
        SELECT "table", "check", count(*) AS rows
        FROM quarantine GROUP BY ALL ORDER BY rows DESC, "table", "check"
        """,
    )


def breaks_report(dashboard: Dashboard) -> dict:
    """The quality run's breaks report (rows arrived, validated, quarantined; file checks)."""
    path = dashboard.results_dir / "quality_runs" / dashboard.run_id / "breaks_report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def file_checks(report: dict) -> pd.DataFrame:
    """The quality run's file checks: name, whether it stops the run, passed, rows."""
    rows = [
        {"check": c["name"], "stops_the_run": kind == "stops", "passed": c["passed"],
         "offending_rows": c["offending_rows"]}
        for kind in ("stops", "warnings")
        for c in report["file_checks"][kind]
    ]  # fmt: skip
    return pd.DataFrame(rows, columns=["check", "stops_the_run", "passed", "offending_rows"])


def group_headline(con: duckdb.DuckDBPyConnection, regime: str) -> pd.DataFrame:
    """The group's LCR, NSFR and EVE loss / Tier 1 on the latest date, the change since the
    day before (NaN for a one-day run), and each limit's status."""
    return _df(
        con,
        """
        SELECT l.limit_name, l.kind, l.actual, l.limit_value, l.status, m.change
        FROM limits l
        LEFT JOIN metric_changes m
          ON m.level = l.level AND m.regime = l.regime AND m.limit_name = l.limit_name
        WHERE l.level = 'group' AND l.regime = ? AND l.date = (SELECT max(date) FROM limits)
        ORDER BY l.limit_name
        """,
        regime,
    )


def thresholds(dashboard: Dashboard) -> dict[str, float]:
    """The warning and breach levels of utilisation (config/limits.yaml, via the run)."""
    path = dashboard.results_dir / "limits" / dashboard.run_id / "summary.json"
    return json.loads(path.read_text(encoding="utf-8"))["thresholds"]


def early_warning(con: duckdb.DuckDBPyConnection, regime: str) -> pd.DataFrame:
    """The early-warning indicators on the latest date: retail outflows (no rulebook) and the
    break-even run-off for this rulebook (config/early_warning.yaml)."""
    return _df(
        con,
        """
        SELECT indicator, who, category, regime, value, threshold, status
        FROM early_warning WHERE regime IS NULL OR regime = ?
        ORDER BY status = 'alert' DESC, indicator, level DESC, who, category
        """,
        regime,
    )


def early_warning_alerts(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Every early-warning indicator in alert on the latest date, under both rulebooks."""
    return _df(
        con,
        """
        SELECT indicator, who, category, regime, value, threshold
        FROM early_warning WHERE status = 'alert'
        ORDER BY indicator, value, who, regime
        """,
    )


def eve_share_trend(con: duckdb.DuckDBPyConnection, regime: str) -> pd.DataFrame:
    """The worst EVE loss as a share of Tier 1, per entity and the group, over every date."""
    return _df(
        con,
        f"""
        SELECT date, {WHO}, eve_risk / tier1 AS value, threshold / tier1 AS test
        FROM risk WHERE {LEVELS} AND regime = ?
        ORDER BY date, who
        """,
        regime,
    )


def rulebook_comparison(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """LCR, NSFR and EVE loss / Tier 1 under both rulebooks on the latest date, per entity and
    the group, with the difference (UK PRA minus Basel, a fraction: 0.014 = 1.4 points)."""
    latest = "date = (SELECT max(date) FROM {view})"
    parts = " UNION ALL ".join(
        f"SELECT '{name}' AS metric, {order} AS ord, {WHO}, regime, {value} AS value "
        f"FROM {view} WHERE {LEVELS} AND {latest.format(view=view)}"
        for name, order, view, value in (
            ("LCR", 1, "lcr", "lcr"),
            ("NSFR", 2, "nsfr", "nsfr"),
            ("EVE loss / Tier 1 (proxy)", 3, "risk", "eve_risk / tier1"),
        )
    )
    return _df(
        con,
        f"""
        SELECT metric, who,
               max(value) FILTER (WHERE regime = 'basel') AS basel,
               max(value) FILTER (WHERE regime = 'pra') AS pra,
               max(value) FILTER (WHERE regime = 'pra')
                 - max(value) FILTER (WHERE regime = 'basel') AS difference
        FROM ({parts}) GROUP BY metric, ord, who
        ORDER BY ord, who = '{GROUP}', who
        """,
    )


def ladder_whos(con: duckdb.DuckDBPyConnection) -> list[str]:
    """The entities and the group that have a maturity ladder (group last)."""
    rows = con.execute(
        f"SELECT DISTINCT {WHO} FROM ladder WHERE {LEVELS} ORDER BY who = '{GROUP}', who"
    ).fetchall()
    return [row[0] for row in rows]


def maturity_ladder(con: duckdb.DuckDBPyConnection, who: str) -> pd.DataFrame:
    """The contractual cash-flow ladder of one entity (or the group) on the latest date (GBP mn
    for the group, the entity's own currency otherwise; the currency column says which)."""
    return _df(
        con,
        f"""
        SELECT bucket, from_days, currency, inflows, outflows, net, cumulative_net, contingent
        FROM ladder WHERE {LEVELS} AND coalesce(entity, '{GROUP}') = ?
          AND date = (SELECT max(date) FROM ladder)
        ORDER BY from_days
        """,
        who,
    )


def gap_books(con: duckdb.DuckDBPyConnection, regime: str) -> pd.DataFrame:
    """The entity-currency books that have a repricing gap, under one rulebook."""
    return _df(
        con,
        "SELECT DISTINCT entity, currency FROM gap WHERE regime = ? ORDER BY entity, currency",
        regime,
    )


def repricing_gap(
    con: duckdb.DuckDBPyConnection, regime: str, entity: str, currency: str
) -> pd.DataFrame:
    """The repricing gap of one entity's book in one currency on the latest date."""
    return _df(
        con,
        """
        SELECT bucket, midpoint_years, assets, liabilities, gap, cumulative_gap
        FROM gap WHERE regime = ? AND entity = ? AND currency = ?
          AND date = (SELECT max(date) FROM gap)
        ORDER BY midpoint_years
        """,
        regime,
        entity,
        currency,
    )
