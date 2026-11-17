"""Backtest of the LCR's retail deposit run-off rates: tests T1 (real H.8 deposits) and T2
(detection check on the synthetic bank, normal and with a planted run).

What is tested, how outflows are measured and the green / amber / red criteria were fixed in
docs/validation_report.md (sections 2 and 4) before any result; this module applies them as
written. Settings: config/validation.yaml (backtest). The run-off rates come from
config/liquidity/ (both rulebooks, which must agree), not from this module.

An outflow over h steps: outflow(t) = max(0, 1 - D(t + h) / D(t)), a rise counting as none.
Steps are rows: H.8 weeks are exactly 7 days apart (riskplatform.model_validation.h8 checks), and
the synthetic bank has one row per business day.
"""

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from riskplatform.config import BacktestSettings, GeneratorConfig, SeriesBreak
from riskplatform.metrics.run import load_liquidity_regimes

STABLE, LESS_STABLE = "retail_stable_deposits", "retail_less_stable_deposits"
GREEN, AMBER, RED, INCONCLUSIVE = "green", "amber", "red", "inconclusive"
ORDER = [GREEN, AMBER, RED]  # worse to the right


@dataclass(frozen=True)
class RunOffRates:
    stable: float  # 5%: LCR40.7 / UK LCR Art 24(1)
    less_stable: float  # 10%: LCR40.13 / UK LCR Art 25(1)


def run_off_rates(liquidity_dir: str | Path) -> RunOffRates:
    """The retail run-off rates from the rulebooks; both rulebooks must give the same rates."""
    rates = {
        name: (rules.lcr.outflows[STABLE].rate, rules.lcr.outflows[LESS_STABLE].rate)
        for name, rules in load_liquidity_regimes(liquidity_dir).items()
    }
    if len(set(rates.values())) != 1:
        raise ValueError(f"the rulebooks give different retail run-off rates: {rates}")
    stable, less_stable = next(iter(rates.values()))
    return RunOffRates(stable=stable, less_stable=less_stable)


def forward_outflow(levels: pd.Series, steps: int) -> pd.Series:
    """The share of the level that has left `steps` rows later (0 for a rise; NaN at the end)."""
    after = levels.shift(-steps)
    return (1.0 - after / levels).clip(lower=0.0)


def without_breaks(
    outflow: pd.Series, steps: int, breaks: list[SeriesBreak], series: str
) -> pd.Series:
    """The outflows with every window that spans a confirmed break of `series` set to NaN.

    A window from t to the date `steps` rows later spans a break if it starts on or before the
    break's last_before and ends on or after its first_after (limitation 7 of the report).
    """
    cleaned = outflow.copy()
    ends = pd.Series(outflow.index, index=outflow.index).shift(-steps)
    for found in breaks:
        if found.series != series:
            continue
        spans = (outflow.index <= pd.Timestamp(found.last_before)) & (
            ends >= pd.Timestamp(found.first_after)
        )
        cleaned[spans.to_numpy()] = float("nan")
    return cleaned


def rate_t1(worst: float, rates: RunOffRates) -> str:
    """T1: green up to the stable rate, amber up to the less-stable rate, red above it."""
    if worst > rates.less_stable:
        return RED
    return AMBER if worst > rates.stable else GREEN


def spells(outflow: pd.Series, threshold: float) -> pd.DataFrame:
    """Runs of consecutive dates with an outflow above threshold: first and last date, number
    of dates, and the worst outflow with its date. These are checked against the H.8 notes."""
    above = outflow > threshold
    if not above.any():
        return pd.DataFrame(columns=["first_date", "last_date", "dates", "worst", "worst_date"])
    run_id = (above != above.shift()).cumsum()[above]
    rows = []
    for _, run in outflow[above].groupby(run_id):
        rows.append(
            {
                "first_date": run.index.min(),
                "last_date": run.index.max(),
                "dates": len(run),
                "worst": float(run.max()),
                "worst_date": run.idxmax(),
            }
        )
    return pd.DataFrame(rows)


def _worst(outflow: pd.Series) -> dict:
    valid = outflow.dropna()
    return {"worst": float(valid.max()), "worst_date": valid.idxmax().date().isoformat()}


def t1_series(
    levels: pd.Series, cfg: BacktestSettings, rates: RunOffRates, series: str = ""
) -> dict:
    """T1 for one H.8 series (levels indexed by date): the numbers and the rating.

    Windows spanning a confirmed break (cfg.confirmed_breaks) are left out of every figure and
    the rating; `raw` keeps the worst outflow, the counts and the rating without exclusions, and
    the spells are listed before exclusions (they are what the limitation 7 check examines).
    """
    raw = forward_outflow(levels, cfg.h8_weeks)
    main = without_breaks(raw, cfg.h8_weeks, cfg.confirmed_breaks, series)
    conservative = without_breaks(
        forward_outflow(levels, cfg.h8_conservative_weeks),
        cfg.h8_conservative_weeks, cfg.confirmed_breaks, series,
    )  # fmt: skip
    measured = main.dropna()
    raw_measured = raw.dropna()
    excluded = raw_measured.index.difference(measured.index)
    episodes = {}
    for episode in cfg.episodes:
        window = main.loc[pd.Timestamp(episode.start) : pd.Timestamp(episode.end)].dropna()
        episodes[episode.name] = _worst(window) if len(window) else None
    worst = _worst(main)
    return {
        "first_date": levels.index.min().date().isoformat(),
        "last_date": levels.index.max().date().isoformat(),
        "windows": len(measured),
        "main": worst,
        "conservative": _worst(conservative),
        "above_stable": int((measured > rates.stable).sum()),
        "above_less_stable": int((measured > rates.less_stable).sum()),
        "episodes": episodes,
        "spells_above_stable": spells(raw_measured, rates.stable),
        "excluded_windows": [day.date().isoformat() for day in excluded],
        "raw": {
            **_worst(raw),
            "above_stable": int((raw_measured > rates.stable).sum()),
            "above_less_stable": int((raw_measured > rates.less_stable).sum()),
            "rating": rate_t1(_worst(raw)["worst"], rates),
        },
        "rating": rate_t1(worst["worst"], rates),
    }


def t1(deposits: pd.DataFrame, cfg: BacktestSettings, rates: RunOffRates) -> dict[str, dict]:
    """T1 for every H.8 series in the deposits table (date, series, fred_id, deposits_bn)."""
    return {
        name: t1_series(rows.set_index("date")["deposits_bn"].sort_index(), cfg, rates, name)
        for name, rows in deposits.groupby("series")
    }


# --- T2: the detection check on the synthetic bank ------------------------------------------------


def retail_deposits(positions: pd.DataFrame, fx: pd.DataFrame) -> pd.DataFrame:
    """Retail deposits per date, entity and category, all currencies added up at the FIRST day's
    FX rates: a fixed rate, so exchange-rate moves are not mistaken for withdrawals."""
    first = fx[fx["date"] == fx["date"].min()].set_index("currency")["to_reporting"]
    retail = positions[positions["category"].isin([STABLE, LESS_STABLE])]
    gbp = retail["balance_mn"] * retail["currency"].map(first)
    totals = gbp.groupby([retail["date"], retail["entity"], retail["category"]]).sum()
    return totals.unstack(["entity", "category"]).sort_index()


def planted_size(cfg: GeneratorConfig, normal: pd.DataFrame, fx: pd.DataFrame) -> float:
    """The share of the stressed entity's total stable deposits that the episode removes: the
    configured stable outflow times the stressed currencies' share of those deposits on the
    business day before the episode (at the first day's FX, as in retail_deposits)."""
    stress = cfg.stress
    first = fx[fx["date"] == fx["date"].min()].set_index("currency")["to_reporting"]
    day_before = normal.loc[normal["date"] < pd.Timestamp(stress.start), "date"].max()
    rows = normal[
        (normal["date"] == day_before)
        & (normal["entity"] == stress.entity)
        & (normal["category"] == STABLE)
    ]
    gbp = rows["balance_mn"] * rows["currency"].map(first)
    in_scope = rows["currency"].isin(stress.currencies or cfg.currencies)
    return float(stress.outflows[STABLE] * gbp[in_scope].sum() / gbp.sum())


def t2(
    normal: tuple[pd.DataFrame, pd.DataFrame],
    stressed: tuple[pd.DataFrame, pd.DataFrame],
    stress_cfg: GeneratorConfig,
    cfg: BacktestSettings,
    rates: RunOffRates,
) -> dict:
    """T2: quiet on the normal bank, and finds the planted run (positions, fx for each bank)."""
    quiet = retail_deposits(*normal).apply(forward_outflow, steps=cfg.synthetic_days)
    worst_normal = quiet.max()
    normal_ok = bool((worst_normal <= rates.stable).all())

    entity = stress_cfg.stress.entity
    levels = retail_deposits(*stressed)[(entity, STABLE)]
    found = _worst(forward_outflow(levels, cfg.synthetic_days))
    conservative = _worst(forward_outflow(levels, cfg.synthetic_conservative_days))
    planted = planted_size(stress_cfg, *normal)
    distance = abs(found["worst"] - planted)
    stressed_ok = found["worst"] > rates.stable and distance <= cfg.detection_tolerance
    return {
        "normal_worst": {f"{e} {c}": float(v) for (e, c), v in worst_normal.items()},
        "normal_quiet": normal_ok,
        "stressed_entity": entity,
        "planted": planted,
        "found": found,
        "conservative": conservative,
        "distance": distance,
        "found_run": bool(stressed_ok),
        "rating": GREEN if normal_ok and stressed_ok else RED,
    }


def overall(t1_ratings: list[str], t2_rating: str, t3_rating: str | None = None) -> str:
    """The worst of T1 (and T3, once run) if T2 is green; inconclusive if T2 is red."""
    if t2_rating != GREEN:
        return INCONCLUSIVE
    ratings = [*t1_ratings, *([t3_rating] if t3_rating else [])]
    return max(ratings, key=ORDER.index)
