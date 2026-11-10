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
from riskplatform.schemas import positions_schema, rates_schema

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
                            "interest_rate": rng.normal(
                                cfg.interest_rate_mean, cfg.interest_rate_sd, n
                            ),
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


def build_positions(
    cfg: GeneratorConfig, book: pd.DataFrame, dates: pd.DatetimeIndex, rng: np.random.Generator
) -> pd.DataFrame:
    """Turn the static book into a daily positions table (one row per position per date)."""
    n_days, n_positions = len(dates), len(book)
    day = np.arange(n_days)[:, None]  # column vector (n_days, 1); broadcasts against positions

    # Balances: each position starts at its start balance and takes a small random step each day.
    # The first day has no step, so day one matches the balanced starting book exactly.
    steps = rng.normal(cfg.daily_balance_drift, cfg.daily_balance_vol, (n_days, n_positions))
    steps[0] = 0.0
    balance = book["start_balance_mn"].to_numpy() * np.cumprod(1.0 + steps, axis=0)

    # Days to maturity count down by one per day; a matured position is reissued at full tenor.
    tenor = book["tenor_days"].to_numpy()
    age = book["age_days"].to_numpy()
    residual = (tenor - 1 - age - day) % tenor + 1

    # Floating-rate positions reset on a repeating cycle; fixed-rate ones only reprice at maturity.
    period = book["reprice_period_days"].to_numpy()
    phase = book["reprice_phase_days"].to_numpy()
    days_to_reset = (period - 1 - phase - day) % period + 1
    is_fixed = (book["rate_type"] == "fixed").to_numpy()
    reprice = np.where(is_fixed, residual, np.minimum(days_to_reset, residual))

    # Stack the days: repeat the book once per day and attach that day's date and values.
    rows = np.tile(np.arange(n_positions), n_days)
    long = book.iloc[rows].reset_index(drop=True)
    long["date"] = dates.repeat(n_positions)
    long["balance_mn"] = balance.ravel()
    long["residual_days"] = residual.ravel()
    long["reprice_days"] = reprice.ravel()
    return long[POSITION_COLUMNS]


def build_rates(
    cfg: GeneratorConfig, dates: pd.DatetimeIndex, rng: np.random.Generator
) -> pd.DataFrame:
    """Create daily yield curves (one row per date, currency and tenor).

    Each currency's curve starts from a smooth short-to-long shape and then moves in parallel
    day by day. Real curves also twist and bend; that is a labelled simplification.
    """
    rates_cfg = cfg.rates
    tenors = np.array(rates_cfg.tenors_years)
    n_days = len(dates)

    # One random walk of daily parallel shifts per currency (n_days x n_currencies).
    shifts = rng.normal(0.0, rates_cfg.daily_vol, (n_days, len(cfg.currencies))).cumsum(axis=0)

    frames = []
    for j, currency in enumerate(cfg.currencies):
        curve = rates_cfg.curves[currency]
        approach = 1.0 - np.exp(-tenors / rates_cfg.curve_decay_years)  # 0 at short end -> 1 long
        base = curve.short_rate + (curve.long_rate - curve.short_rate) * approach
        rate = base[None, :] + shifts[:, [j]]  # (n_days, n_tenors)
        frames.append(
            pd.DataFrame(
                {
                    "date": dates.repeat(len(tenors)),
                    "currency": currency,
                    "tenor_years": np.tile(tenors, n_days),
                    "rate": rate.ravel(),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def generate(cfg: GeneratorConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build positions and rates from a config, checked against the data contracts."""
    # Independent random streams, so changing one part never shifts the others' numbers.
    seeds = np.random.SeedSequence(cfg.seed).spawn(3)
    book_rng, path_rng, rates_rng = (np.random.default_rng(s) for s in seeds)

    dates = pd.bdate_range(start=pd.Timestamp(cfg.start_date), periods=cfg.n_days)
    book = build_book(cfg, book_rng)
    positions = positions_schema(cfg).validate(build_positions(cfg, book, dates, path_rng))
    rates = rates_schema(cfg).validate(build_rates(cfg, dates, rates_rng))
    return positions, rates


def write_outputs(positions: pd.DataFrame, rates: pd.DataFrame, out_dir: Path) -> None:
    """Write one positions file per date plus a rates file; safe to run repeatedly."""
    out_dir = Path(out_dir)
    positions_dir = out_dir / "positions"
    positions_dir.mkdir(parents=True, exist_ok=True)
    for stale in positions_dir.glob("date=*.parquet"):
        stale.unlink()
    for day, frame in positions.groupby("date"):
        frame.to_parquet(positions_dir / f"date={day:%Y-%m-%d}.parquet", index=False)
    rates.to_parquet(out_dir / "rates.parquet", index=False)


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
