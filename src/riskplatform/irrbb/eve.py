"""Economic value of equity (EVE) under the prescribed shocks, aggregation and the outlier test.

For one currency (SRP31.95-31.129; UK ICAA 9.13-9.15, SS31/15 2.9B):

    delta EVE(scenario) = sum over cash flows of  amount x (DF_shocked(t) - DF_base(t))
    DF(t) = exp(-rate(t) x t)

A negative delta EVE is a loss. Cash flows come from repricing_flows (signed principal at the
bucket midpoint). The base rate at time t is read from the currency's curve by straight-line
interpolation, held flat beyond its first and last tenors; the shock and floor are then applied
at t (irrbb.shocks).

Across currencies, per scenario: losses are added in full and gains are added x gains_weight
(Basel 0: losses only, SRP31.129(5); UK 50%, SS31/15 2.9B(iv)). The EVE risk measure is the
worst aggregated loss across the scenarios, and 0 if every scenario is a gain.

Simplification: continuous compounding (the standardised framework's formulas are images on
bis.org, so their exact form was not checked in the text).
"""

import numpy as np
import pandas as pd

from riskplatform.config import IrrbbRules
from riskplatform.irrbb.shocks import shocked_rates


def base_rates_at(times: np.ndarray, tenors: np.ndarray, rates: np.ndarray) -> np.ndarray:
    """The base curve's rate at each time: straight-line interpolation, flat beyond the ends."""
    order = np.argsort(tenors)
    return np.interp(
        times, np.asarray(tenors, dtype=float)[order], np.asarray(rates, dtype=float)[order]
    )


def delta_eve(
    flows: pd.DataFrame,
    tenors: np.ndarray,
    base_rates: np.ndarray,
    rules: IrrbbRules,
    currency: str,
) -> dict[str, float]:
    """Delta EVE for each scenario, for one currency's repricing cash flows (time_years, amount)."""
    t = flows["time_years"].to_numpy(dtype=float)
    amount = flows["amount"].to_numpy(dtype=float)
    base = base_rates_at(t, tenors, base_rates)
    base_value = np.exp(-base * t)
    result = {}
    for scenario in rules.shocks.scenarios:
        shocked = shocked_rates(rules, currency, scenario, t, base)
        result[scenario] = float(np.sum(amount * (np.exp(-shocked * t) - base_value)))
    return result


def aggregated_losses(
    deltas_by_currency: dict[str, dict[str, float]], rules: IrrbbRules
) -> dict[str, float]:
    """Per scenario: losses added in full, gains added x gains_weight (a loss is positive)."""
    weight = rules.aggregation.gains_weight
    totals = {}
    for scenario in rules.shocks.scenarios:
        total = 0.0
        for deltas in deltas_by_currency.values():
            change = deltas[scenario]
            total += -change if change < 0 else -weight * change
        totals[scenario] = total
    return totals


def eve_risk(
    deltas_by_currency: dict[str, dict[str, float]], rules: IrrbbRules
) -> tuple[float, str | None]:
    """The EVE risk measure (worst aggregated loss, 0 if none) and the scenario that gives it."""
    totals = aggregated_losses(deltas_by_currency, rules)
    scenario = max(totals, key=totals.get)
    if totals[scenario] <= 0:
        return 0.0, None
    return totals[scenario], scenario


def is_outlier(loss: float, tier1: float, rules: IrrbbRules) -> bool:
    """True if the EVE loss exceeds the outlier threshold (15% of Tier 1 capital)."""
    return loss > rules.outlier_threshold.value * tier1
