"""Independent checks of the generated raw files, written in SQL and run with DuckDB.

The generator builds the data with pandas. These checks read the Parquet files back from disk
and test them with different code, so a generator bug cannot hide inside its own checks.
They cover what row-by-row schema rules cannot: files, completeness, and links across rows
and days. Each check is a query that returns the offending rows; no rows means it passed.

Run with:  python -m riskplatform.validation.check_raw
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

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

# Named parameters each query uses (DuckDB rejects parameters a query does not use).
_TOLERANCES = {"balance_tolerance": BALANCE_TOLERANCE, "rate_tolerance": RATE_TOLERANCE}


@dataclass(frozen=True)
class CheckResult:
    name: str
    failures: int  # number of offending rows

    @property
    def passed(self) -> bool:
        return self.failures == 0


def _sql_path(path: Path) -> str:
    """A file path as a SQL string literal (forward slashes, quotes escaped)."""
    return "'" + path.as_posix().replace("'", "''") + "'"


def _load(con: duckdb.DuckDBPyConnection, raw_dir: Path, cfg: GeneratorConfig) -> None:
    """Expose the raw files and the config values as tables the checks can query."""
    positions_glob = _sql_path(raw_dir / "positions" / "date=*.parquet")
    con.execute(f"""
        CREATE VIEW positions AS
        SELECT * REPLACE (CAST(date AS DATE) AS date)
        FROM read_parquet({positions_glob}, filename = true)
    """)
    con.execute(f"""
        CREATE VIEW rates AS
        SELECT * REPLACE (CAST(date AS DATE) AS date)
        FROM read_parquet({_sql_path(raw_dir / "rates.parquet")})
    """)
    con.execute(
        "CREATE TABLE settings AS SELECT ? AS n_days, ? AS equity_ratio, ? AS curve_decay_years",
        [cfg.n_days, cfg.equity_ratio, cfg.rates.curve_decay_years],
    )
    con.execute("CREATE TABLE curve_config (currency VARCHAR, short_rate DOUBLE, long_rate DOUBLE)")
    con.executemany(
        "INSERT INTO curve_config VALUES (?, ?, ?)",
        [(ccy, c.short_rate, c.long_rate) for ccy, c in cfg.rates.curves.items()],
    )


def run_checks(raw_dir: str | Path, cfg: GeneratorConfig) -> list[CheckResult]:
    """Run every check against the raw files in raw_dir and return one result per check."""
    results = []
    with duckdb.connect() as con:  # in-memory database, gone when the block ends
        _load(con, Path(raw_dir), cfg)
        for name, sql in CHECKS.items():
            params = {key: value for key, value in _TOLERANCES.items() if f"${key}" in sql}
            count_sql = f"SELECT count(*) FROM ({sql})"
            (failures,) = con.execute(count_sql, params).fetchone()
            results.append(CheckResult(name, failures))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the generated raw files with DuckDB.")
    parser.add_argument("--config", type=Path, default=Path("config/generator.yaml"))
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    args = parser.parse_args(argv)

    results = run_checks(args.raw, load_generator_config(args.config))
    for result in results:
        detail = "" if result.passed else f"  ({result.failures} offending rows)"
        print(f"{'PASS' if result.passed else 'FAIL'}  {result.name}{detail}")
    failed = sum(not result.passed for result in results)
    print(f"{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
