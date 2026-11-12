"""Independent checks of the generated raw files, written in SQL and run with DuckDB.

The generator builds the data with pandas. These checks read the Parquet files back from disk
and test them with different code, so a generator bug cannot hide inside its own checks.
They cover what row-by-row schema rules cannot: files, completeness, and links across rows
and days. Each check is a query that returns the offending rows; no rows means it passed.

Run with:  python -m riskplatform.validation.check_raw
"""

import argparse
import sys
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from riskplatform.config import GeneratorConfig, load_generator_config

# Tolerances for comparing floating-point numbers that should be equal.
BALANCE_TOLERANCE = 1e-9  # relative to total assets
RATE_TOLERANCE = 1e-12  # absolute, in rate units (decimal)

CHECKS = {
    "files_hold_their_own_date": """
        SELECT filename
        FROM positions
        GROUP BY filename
        HAVING count(DISTINCT date) <> 1
            OR strftime(min(date), '%Y-%m-%d')
               <> regexp_extract(filename, 'date=(\\d{4}-\\d{2}-\\d{2})\\.parquet$', 1)
    """,
    "day_count_matches_config": """
        SELECT count(DISTINCT date) AS days
        FROM positions
        HAVING count(DISTINCT date) <> (SELECT n_days FROM settings)
    """,
    "positions_and_rates_share_dates": """
        SELECT p.date AS position_date, r.date AS rate_date
        FROM (SELECT DISTINCT date FROM positions) AS p
        FULL OUTER JOIN (SELECT DISTINCT date FROM rates) AS r USING (date)
        WHERE p.date IS NULL OR r.date IS NULL
    """,
    "fx_covers_every_date": """
        WITH expected AS (
            SELECT d.date, c.currency
            FROM (SELECT DISTINCT date FROM positions) AS d
            CROSS JOIN currencies AS c
        )
        SELECT e.date, e.currency
        FROM expected AS e
        LEFT JOIN fx ON fx.date = e.date AND fx.currency = e.currency
        WHERE fx.to_reporting IS NULL
    """,
    "every_position_every_day": """
        SELECT position_id
        FROM positions
        GROUP BY position_id
        HAVING count(*) <> (SELECT count(DISTINCT date) FROM positions)
            OR count(DISTINCT date) <> count(*)
    """,
    "static_attributes_unchanged": """
        SELECT position_id
        FROM positions
        GROUP BY position_id
        HAVING greatest(
            count(DISTINCT entity), count(DISTINCT currency), count(DISTINCT category),
            count(DISTINCT side), count(DISTINCT rate_type)
        ) > 1
    """,
    "rate_changes_only_at_reset": """
        WITH steps AS (
            SELECT
                position_id,
                date,
                date_diff('day', lag(date) OVER w, date) AS gap,
                interest_rate,
                lag(interest_rate) OVER w AS previous_rate,
                lag(reprice_days) OVER w AS previous_reprice
            FROM positions
            WINDOW w AS (PARTITION BY position_id ORDER BY date)
        )
        SELECT *
        FROM steps
        WHERE gap IS NOT NULL
          AND interest_rate <> previous_rate
          AND previous_reprice > gap
    """,
    "balance_sheet_balances_day_one": """
        WITH totals AS (
            SELECT
                entity,
                currency,
                sum(balance_mn) FILTER (WHERE side = 'asset') AS assets,
                sum(balance_mn) FILTER (WHERE side = 'liability') AS liabilities
            FROM positions
            WHERE date = (SELECT min(date) FROM positions)
            GROUP BY entity, currency
        )
        SELECT totals.*
        FROM totals, settings
        WHERE abs(assets - liabilities / (1 - settings.equity_ratio))
              > $balance_tolerance * assets
    """,
    "day_counts_fall_by_calendar_days": """
        WITH steps AS (
            SELECT
                position_id,
                date,
                date_diff('day', lag(date) OVER w, date) AS gap,
                residual_days,
                lag(residual_days) OVER w AS previous_residual,
                reprice_days,
                lag(reprice_days) OVER w AS previous_reprice
            FROM positions
            WINDOW w AS (PARTITION BY position_id ORDER BY date)
        )
        SELECT *
        FROM steps
        WHERE gap IS NOT NULL
          AND NOT (
                (residual_days = previous_residual - gap OR previous_residual <= gap)
            AND (reprice_days = previous_reprice - gap OR previous_reprice <= gap)
          )
    """,
    "curves_move_in_parallel": """
        WITH moves AS (
            SELECT
                date,
                currency,
                rate - lag(rate) OVER (PARTITION BY currency, tenor_years ORDER BY date) AS move
            FROM rates
        )
        SELECT date, currency, max(move) - min(move) AS spread
        FROM moves
        WHERE move IS NOT NULL
        GROUP BY date, currency
        HAVING max(move) - min(move) > $rate_tolerance
    """,
    "curve_shape_matches_config": """
        WITH first_day AS (
            SELECT
                currency,
                tenor_years,
                rate - first_value(rate) OVER by_tenor AS actual_rise,
                first_value(tenor_years) OVER by_tenor AS shortest_tenor
            FROM rates
            WHERE date = (SELECT min(date) FROM rates)
            WINDOW by_tenor AS (PARTITION BY currency ORDER BY tenor_years)
        )
        SELECT f.currency, f.tenor_years, f.actual_rise
        FROM first_day AS f
        JOIN curve_config AS c USING (currency)
        CROSS JOIN settings AS s
        WHERE abs(
            f.actual_rise
            - (c.long_rate - c.short_rate)
              * (exp(-f.shortest_tenor / s.curve_decay_years)
                 - exp(-f.tenor_years / s.curve_decay_years))
        ) > $rate_tolerance
    """,
}

# Checks of how the synthetic curves are built. Real curves twist and do not follow the
# config's shape, so these are skipped (and reported as skipped) when rates.source is 'real'.
SYNTHETIC_ONLY = {"curves_move_in_parallel", "curve_shape_matches_config"}

# Named parameters each query uses (DuckDB rejects parameters a query does not use).
_TOLERANCES = {"balance_tolerance": BALANCE_TOLERANCE, "rate_tolerance": RATE_TOLERANCE}


@dataclass(frozen=True)
class CheckResult:
    name: str
    failures: int  # number of offending rows
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.failures == 0


def _sql_path(path: Path) -> str:
    """A file path as a SQL string literal (forward slashes, quotes escaped)."""
    return "'" + path.as_posix().replace("'", "''") + "'"


def _view(con: duckdb.DuckDBPyConnection, name: str, source_sql: str) -> None:
    """Create a view the checks query, with timestamps turned into plain dates."""
    con.execute(f"CREATE VIEW {name} AS SELECT * REPLACE (CAST(date AS DATE) AS date) {source_sql}")


def _load_config(con: duckdb.DuckDBPyConnection, cfg: GeneratorConfig) -> None:
    """Expose the config values the checks compare against as small tables."""
    con.execute(
        "CREATE TABLE settings AS SELECT ? AS n_days, ? AS equity_ratio, ? AS curve_decay_years",
        [cfg.n_days, cfg.equity_ratio, cfg.rates.curve_decay_years],
    )
    con.execute("CREATE TABLE currencies (currency VARCHAR)")
    con.executemany("INSERT INTO currencies VALUES (?)", [(c,) for c in cfg.currencies])
    con.execute("CREATE TABLE curve_config (currency VARCHAR, short_rate DOUBLE, long_rate DOUBLE)")
    con.executemany(
        "INSERT INTO curve_config VALUES (?, ?, ?)",
        [(ccy, c.short_rate, c.long_rate) for ccy, c in cfg.rates.curves.items()],
    )


def _run(
    con: duckdb.DuckDBPyConnection, cfg: GeneratorConfig, only: Collection[str] | None
) -> list[CheckResult]:
    """Run the checks (all, or only the named ones) and return one result per check."""
    unknown = set(only or ()) - set(CHECKS)
    if unknown:
        raise ValueError(f"unknown checks: {sorted(unknown)}")
    _load_config(con, cfg)
    results = []
    for name, sql in CHECKS.items():
        if only is not None and name not in only:
            continue
        if name in SYNTHETIC_ONLY and cfg.rates.source != "synthetic":
            results.append(CheckResult(name, failures=0, skipped=True))
            continue
        params = {key: value for key, value in _TOLERANCES.items() if f"${key}" in sql}
        (failures,) = con.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()
        results.append(CheckResult(name, failures))
    return results


def run_checks(
    raw_dir: str | Path, cfg: GeneratorConfig, only: Collection[str] | None = None
) -> list[CheckResult]:
    """Run the checks against the raw files in raw_dir and return one result per check."""
    raw_dir = Path(raw_dir)
    positions_glob = _sql_path(raw_dir / "positions" / "date=*.parquet")
    with duckdb.connect() as con:  # in-memory database, gone when the block ends
        _view(con, "positions", f"FROM read_parquet({positions_glob}, filename = true)")
        _view(con, "rates", f"FROM read_parquet({_sql_path(raw_dir / 'rates.parquet')})")
        _view(con, "fx", f"FROM read_parquet({_sql_path(raw_dir / 'fx.parquet')})")
        return _run(con, cfg, only)


def run_checks_on_tables(
    positions: pd.DataFrame,
    rates: pd.DataFrame,
    fx: pd.DataFrame,
    cfg: GeneratorConfig,
    only: Collection[str] | None = None,
) -> list[CheckResult]:
    """Run the same checks on tables in memory (e.g. rows that passed row-level validation).

    positions needs a 'filename' column naming each row's source file (as read_parquet's
    filename option adds), so the file-name check can run.
    """
    with duckdb.connect() as con:
        for name, frame in (("positions", positions), ("rates", rates), ("fx", fx)):
            con.register(f"{name}_input", frame)
            _view(con, name, f"FROM {name}_input")
        return _run(con, cfg, only)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the generated raw files with DuckDB.")
    parser.add_argument("--config", type=Path, default=Path("config/generator.yaml"))
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    args = parser.parse_args(argv)

    results = run_checks(args.raw, load_generator_config(args.config))
    for result in results:
        if result.skipped:
            print(f"SKIP  {result.name}  (synthetic curves only)")
            continue
        detail = "" if result.passed else f"  ({result.failures} offending rows)"
        print(f"{'PASS' if result.passed else 'FAIL'}  {result.name}{detail}")
    skipped = sum(result.skipped for result in results)
    failed = sum(not result.passed for result in results)
    print(f"{len(results) - failed - skipped} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
