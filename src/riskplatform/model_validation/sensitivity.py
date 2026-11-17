"""LCR sensitivity to the retail run-off rates (test T3) and a simple benchmark (test T4).

T3 recalculates the LCR of every entity and the group, under both rulebooks, on the latest
validated day of the synthetic bank, with BOTH retail run-off rates replaced by one stress rate:
the worst 4-week outflow observed in T1. It reuses the metrics run's own code unchanged
(riskplatform.metrics.run.metric_groups for the entity and group positions, calculate_lcr for the
ratio); only a copy of each rulebook gets the new rates. Rated against the internal limit (110%,
config/limits.yaml) and the regulatory minimum (100%), as fixed in docs/validation_report.md.

Break-even rates: the run-off rate (both categories) at which the LCR reaches 110% and 100%,
found by bisection on the LCR itself, which falls as the rate rises. None if even a 100% run-off
keeps the LCR above the line.

T4: the given percentile of all 4-week H.8 outflows (a rise counts as 0), report only.
"""

import pandas as pd

from riskplatform.config import GeneratorConfig, LiquidityRules
from riskplatform.metrics.lcr import calculate_lcr
from riskplatform.metrics.run import metric_groups
from riskplatform.model_validation.backtest import (
    AMBER,
    GREEN,
    LESS_STABLE,
    RED,
    STABLE,
    forward_outflow,
)

LEVELS = ("entity", "group")
BISECTION_STEPS = 60  # interval [0, 1] halved 60 times: far below a basis point


def with_retail_run_off(rules: LiquidityRules, rate: float) -> LiquidityRules:
    """A copy of the rulebook with both retail run-off rates set to `rate`."""
    raw = rules.model_dump()
    for category in (STABLE, LESS_STABLE):
        raw["lcr"]["outflows"][category]["rate"] = rate
    return LiquidityRules.model_validate(raw)


def latest_groups(
    positions: pd.DataFrame, fx: pd.DataFrame, cfg: GeneratorConfig
) -> list[tuple[dict, pd.DataFrame]]:
    """The entity and group positions of the latest date (as the metrics run builds them)."""
    latest = positions[positions["date"] == positions["date"].max()]
    return [(key, group) for key, group in metric_groups(latest, fx, cfg) if key["level"] in LEVELS]


def _who(key: dict) -> str:
    return key["entity"] or "GROUP"


def lcr_at(group: pd.DataFrame, rules: LiquidityRules, rate: float | None) -> float:
    """The LCR with both retail run-off rates set to `rate` (None: the rulebook's own rates)."""
    return calculate_lcr(group, rules if rate is None else with_retail_run_off(rules, rate)).ratio


def break_even(group: pd.DataFrame, rules: LiquidityRules, target: float) -> float | None:
    """The run-off rate at which the LCR falls to `target`; 0 if already below, None if never."""
    if lcr_at(group, rules, 0.0) < target:
        return 0.0
    if lcr_at(group, rules, 1.0) >= target:
        return None
    low, high = 0.0, 1.0  # LCR(low) >= target > LCR(high)
    for _ in range(BISECTION_STEPS):
        middle = (low + high) / 2
        if lcr_at(group, rules, middle) >= target:
            low = middle
        else:
            high = middle
    return low


def rate_t3(stressed_lcrs: list[float], limit: float, minimum: float) -> str:
    """T3: green if every LCR stays at or above the limit, amber if at or above the minimum."""
    lowest = min(stressed_lcrs)
    if lowest < minimum:
        return RED
    return AMBER if lowest < limit else GREEN


def t3(
    groups: list[tuple[dict, pd.DataFrame]],
    regimes: dict[str, LiquidityRules],
    stress_rate: float,
    limit: float,
    rate_grid: list[float],
) -> dict:
    """T3 for every entity and the group under every rulebook (see the module notes)."""
    rows = []
    for key, group in groups:
        for name, rules in regimes.items():
            minimum = rules.lcr.minimum.value
            rows.append(
                {
                    "regime": name,
                    "who": _who(key),
                    "lcr": lcr_at(group, rules, None),
                    "stressed_lcr": lcr_at(group, rules, stress_rate),
                    "break_even_limit": break_even(group, rules, limit),
                    "break_even_minimum": break_even(group, rules, minimum),
                    **{f"lcr_at_{rate:.2f}": lcr_at(group, rules, rate) for rate in rate_grid},
                }
            )
    table = pd.DataFrame(rows)
    minimum = min(rules.lcr.minimum.value for rules in regimes.values())
    return {
        "stress_rate": stress_rate,
        "limit": limit,
        "minimum": minimum,
        "table": table,
        "rating": rate_t3(table["stressed_lcr"].tolist(), limit, minimum),
    }


def t4(deposits: pd.DataFrame, weeks: int, percentile: float) -> dict[str, float]:
    """The percentile of all `weeks`-week outflows (rises count as 0) for each H.8 series."""
    result = {}
    for name, rows in deposits.groupby("series"):
        levels = rows.set_index("date")["deposits_bn"].sort_index()
        result[name] = float(forward_outflow(levels, weeks).dropna().quantile(percentile))
    return result
