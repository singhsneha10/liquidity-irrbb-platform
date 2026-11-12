"""Synthetic data generator: writes the raw layer (positions and market rates).

The generator is deterministic: the same config always produces identical output.
Run it with:  python -m riskplatform.ingestion.generate
"""

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from riskplatform.config import GeneratorConfig, load_generator_config
from riskplatform.controls import write_control_totals
from riskplatform.schemas import market_curves_schema, positions_schema, rates_schema

logger = logging.getLogger(__name__)

# Static attributes copied onto every daily row, in the order the table presents them.
POSITION_COLUMNS = [
    "date",
    "position_id",
    "entity",
    "currency",
    "category",
    "side",
    "balance_mn",
    "rate_type",
    "interest_rate",
    "residual_days",
    "reprice_days",
]


def _position_count(n_positions: int, weight: float) -> int:
    """Scale a position count by a currency weight, keeping at least one position."""
    return max(1, round(n_positions * weight))


def _balance_sheet(cfg: GeneratorConfig, book: pd.DataFrame) -> pd.DataFrame:
    """Scale asset balances so assets = liabilities + equity for every entity and currency.

    Equity is not a position in the table: it is the gap, equal to equity_ratio of assets.
    """
    balance = book["start_balance_mn"]
    groups = [book["entity"], book["currency"]]
    liabilities = balance.where(book["side"] == "liability", 0.0).groupby(groups).transform("sum")
    assets = balance.where(book["side"] == "asset", 0.0).groupby(groups).transform("sum")
    factor = np.where(book["side"] == "asset", liabilities / (1.0 - cfg.equity_ratio) / assets, 1.0)
    return book.assign(start_balance_mn=balance * factor)


def build_book(cfg: GeneratorConfig, rng: np.random.Generator) -> pd.DataFrame:
    """Create the banking book: one row per position with its fixed attributes.

    Every combination of entity, currency and product category gets at least one position.
    """
    reprice_low, reprice_high = cfg.floating_reprice_days
    frames = []
    for entity in cfg.entities:
        for currency in cfg.currencies:
            weight = 1.0 if currency == entity.home_currency else cfg.foreign_currency_weight
            for category in cfg.categories:
                n = _position_count(category.n_positions, weight)
                tenor_low, tenor_high = category.tenor_days
                tenor = rng.integers(tenor_low, tenor_high + 1, n)
                period = rng.integers(reprice_low, reprice_high + 1, n)
                ids = [f"{entity.name}_{currency}_{category.name}_{k:03d}" for k in range(1, n + 1)]
                frames.append(
                    pd.DataFrame(
                        {
                            "position_id": ids,
                            "entity": entity.name,
                            "currency": currency,
                            "category": category.name,
                            "side": category.side,
                            "rate_type": category.rate_type,
                            "spread": rng.normal(category.spread, cfg.spread_sd, n),
                            "start_balance_mn": rng.lognormal(
                                np.log(category.mean_balance_mn * weight), cfg.balance_log_sigma, n
                            ),
                            "tenor_days": tenor,
                            "age_days": rng.integers(0, tenor),
                            "reprice_period_days": period,
                            "reprice_phase_days": rng.integers(0, period),
                        }
                    )
                )
    return _balance_sheet(cfg, pd.concat(frames, ignore_index=True))


def _curve_rate(
    cfg: GeneratorConfig,
    curves: np.ndarray,
    day_index: np.ndarray,
    currency_index: np.ndarray,
    tenor_years: np.ndarray,
) -> np.ndarray:
    """Read curve rates by straight-line interpolation between tenors (flat beyond the ends).

    curves has shape (n_days, n_currencies, n_tenors); the index arrays broadcast together.
    """
    tenors = np.array(cfg.rates.tenors_years)
    t = np.clip(tenor_years, tenors[0], tenors[-1])
    upper = np.searchsorted(tenors, t).clip(1, len(tenors) - 1)
    lower = upper - 1
    weight = (t - tenors[lower]) / (tenors[upper] - tenors[lower])
    low_rate = curves[day_index, currency_index, lower]
    high_rate = curves[day_index, currency_index, upper]
    return low_rate + weight * (high_rate - low_rate)


def build_positions(
    cfg: GeneratorConfig,
    book: pd.DataFrame,
    dates: pd.DatetimeIndex,
    curves: np.ndarray,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Turn the static book into a daily positions table (one row per position per date)."""
    n_days, n_positions = len(dates), len(book)
    # Calendar days since the first date, as a column vector (n_days, 1) that broadcasts against
    # positions. Maturities count in calendar days (as LCR and NSFR do): a weekend is 3 days.
    day_offsets = (dates - dates[0]).days.to_numpy()
    elapsed = day_offsets[:, None]

    # Balances: each position starts at its start balance and takes a small random step each day.
    # The first day has no step, so day one matches the balanced starting book exactly.
    steps = rng.normal(cfg.daily_balance_drift, cfg.daily_balance_vol, (n_days, n_positions))
    steps[0] = 0.0
    balance = book["start_balance_mn"].to_numpy() * np.cumprod(1.0 + steps, axis=0)

    # Days to maturity count down by the calendar days elapsed; a matured position is reissued
    # at full tenor.
    tenor = book["tenor_days"].to_numpy()
    age = book["age_days"].to_numpy()
    residual = (tenor - 1 - age - elapsed) % tenor + 1

    # Floating-rate positions reset on a repeating cycle; fixed-rate ones only reprice at maturity.
    period = book["reprice_period_days"].to_numpy()
    phase = book["reprice_phase_days"].to_numpy()
    days_to_reset = (period - 1 - phase - elapsed) % period + 1
    is_fixed = (book["rate_type"] == "fixed").to_numpy()
    reprice = np.where(is_fixed, residual, np.minimum(days_to_reset, residual))

    # Interest rates: a rate is fixed on the position's last fixing date, which is its last
    # rollover or (floating only) its last reset, whichever is later. The rate is the curve rate
    # for its currency at its reference tenor on that date, plus its spread, floored.
    last_rollover = elapsed + residual - tenor
    last_reset = elapsed + days_to_reset - period
    fixing = np.where(is_fixed, last_rollover, np.maximum(last_rollover, last_reset))
    # Business-day row of the fixing date: the latest date on or before it. Fixings before the
    # first date use the first day's curve (simplification: there is no earlier curve history).
    fixing_day = (np.searchsorted(day_offsets, fixing, side="right") - 1).clip(0)
    # Reference tenor: fixed rates use the full tenor; floating rates the reset cycle, which is
    # never longer than the position itself (a 1-day deposit resets daily).
    reference_days = np.where(is_fixed, tenor, np.minimum(period, tenor))
    currency_index = book["currency"].map({c: i for i, c in enumerate(cfg.currencies)})
    curve_rate = _curve_rate(
        cfg, curves, fixing_day, currency_index.to_numpy(), reference_days / 365.0
    )
    rate = np.maximum(curve_rate + book["spread"].to_numpy(), cfg.interest_rate_floor)

    # Stack the days: repeat the book once per day and attach that day's date and values.
    rows = np.tile(np.arange(n_positions), n_days)
    long = book.iloc[rows].reset_index(drop=True)
    long["date"] = dates.repeat(n_positions)
    long["interest_rate"] = rate.ravel()
    long["balance_mn"] = balance.ravel()
    long["residual_days"] = residual.ravel()
    long["reprice_days"] = reprice.ravel()
    return long[POSITION_COLUMNS]


def build_curves(cfg: GeneratorConfig, n_days: int, rng: np.random.Generator) -> np.ndarray:
    """Create daily yield curves as an array of shape (n_days, n_currencies, n_tenors).

    Each currency's curve starts from a smooth short-to-long shape and then moves in parallel
    day by day. Real curves also twist and bend; that is a labelled simplification.
    """
    rates_cfg = cfg.rates
    tenors = np.array(rates_cfg.tenors_years)

    # One random walk of daily parallel shifts per currency (n_days x n_currencies).
    shifts = rng.normal(0.0, rates_cfg.daily_vol, (n_days, len(cfg.currencies))).cumsum(axis=0)

    approach = 1.0 - np.exp(-tenors / rates_cfg.curve_decay_years)  # 0 at short end -> 1 long
    short = np.array([rates_cfg.curves[c].short_rate for c in cfg.currencies])
    long = np.array([rates_cfg.curves[c].long_rate for c in cfg.currencies])
    base = short[:, None] + (long - short)[:, None] * approach  # (n_currencies, n_tenors)
    return base[None, :, :] + shifts[:, :, None]


def load_real_curves(cfg: GeneratorConfig, dates: pd.DatetimeIndex) -> np.ndarray:
    """Read saved real curves and lay them on the generator's dates and tenor grid.

    Returns the same shape as build_curves: (n_days, n_currencies, n_tenors). Each source day's
    curve is interpolated onto rates.tenors_years (straight lines between the source's points,
    flat beyond its shortest and longest). A date with no curve (a holiday) uses the latest
    earlier curve, if it is at most rates.real.max_fill_days calendar days old.
    """
    real = cfg.rates.real
    path = Path(real.curves_file)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Save it first with: python -m riskplatform.ingestion.market_rates"
        )
    stored = pd.read_parquet(path)
    stored = stored[stored["currency"].isin(cfg.currencies)]
    stored = market_curves_schema(cfg.currencies).validate(stored)

    grid = np.array(cfg.rates.tenors_years)
    max_age = pd.Timedelta(days=real.max_fill_days)
    curves = np.empty((len(dates), len(cfg.currencies), len(grid)))
    for j, currency in enumerate(cfg.currencies):
        by_day = stored[stored["currency"] == currency].sort_values("tenor_years").groupby("date")
        if by_day.ngroups == 0:
            raise ValueError(f"{path}: no curves for {currency}")
        days = [(day, np.interp(grid, g["tenor_years"], g["rate"])) for day, g in by_day]
        source_dates = pd.DatetimeIndex([day for day, _ in days])
        on_grid = np.array([curve for _, curve in days])

        latest = source_dates.searchsorted(dates, side="right") - 1  # -1: nothing on or before
        age = dates - source_dates[latest.clip(0)]
        missing = (latest < 0) | (age > max_age)
        if missing.any():
            listed = ", ".join(day.date().isoformat() for day in dates[missing])
            raise ValueError(
                f"{currency}: no curve within {real.max_fill_days} days before: {listed}"
            )
        curves[:, j, :] = on_grid[latest]
    return curves


def build_rates(cfg: GeneratorConfig, dates: pd.DatetimeIndex, curves: np.ndarray) -> pd.DataFrame:
    """Lay the curves out as a table (one row per date, currency and tenor)."""
    tenors = np.array(cfg.rates.tenors_years)
    frames = [
        pd.DataFrame(
            {
                "date": dates.repeat(len(tenors)),
                "currency": currency,
                "tenor_years": np.tile(tenors, len(dates)),
                "rate": curves[:, j, :].ravel(),
            }
        )
        for j, currency in enumerate(cfg.currencies)
    ]
    return pd.concat(frames, ignore_index=True)


def generate(cfg: GeneratorConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build positions and rates from a config, checked against the data contracts."""
    # Independent random streams, so changing one part never shifts the others' numbers.
    seeds = np.random.SeedSequence(cfg.seed).spawn(3)
    book_rng, path_rng, rates_rng = (np.random.default_rng(s) for s in seeds)

    dates = pd.bdate_range(start=pd.Timestamp(cfg.start_date), periods=cfg.n_days)
    if cfg.rates.source == "real":
        curves = load_real_curves(cfg, dates)
    else:
        curves = build_curves(cfg, len(dates), rates_rng)
    book = build_book(cfg, book_rng)
    positions = build_positions(cfg, book, dates, curves, path_rng)
    positions = positions_schema(cfg).validate(positions)
    rates = rates_schema(cfg).validate(build_rates(cfg, dates, curves))
    return positions, rates


def write_outputs(positions: pd.DataFrame, rates: pd.DataFrame, out_dir: Path) -> None:
    """Write one positions file per date, a rates file and their control totals.

    Safe to run repeatedly: stale position files are removed and the control file is replaced.
    """
    out_dir = Path(out_dir)
    positions_dir = out_dir / "positions"
    positions_dir.mkdir(parents=True, exist_ok=True)
    for stale in positions_dir.glob("date=*.parquet"):
        stale.unlink()
    sent = {}  # relative path -> (table, frame), for the control totals
    for day, frame in positions.groupby("date"):
        name = f"date={day:%Y-%m-%d}.parquet"
        frame.to_parquet(positions_dir / name, index=False)
        sent[f"positions/{name}"] = ("positions", frame)
    rates.to_parquet(out_dir / "rates.parquet", index=False)
    sent["rates.parquet"] = ("rates", rates)
    # Like a source system, send control totals with the files (see riskplatform.controls).
    write_control_totals(out_dir, sent)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic positions and market rates.")
    parser.add_argument("--config", type=Path, default=Path("config/generator.yaml"))
    parser.add_argument("--out", type=Path, default=Path("data/raw"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_generator_config(args.config)
    positions, rates = generate(cfg)
    write_outputs(positions, rates, args.out)
    logger.info(
        "Wrote %d position rows over %d days and %d rate rows to %s",
        len(positions),
        cfg.n_days,
        len(rates),
        args.out,
    )


if __name__ == "__main__":
    main()
