"""Interest rate shock scenarios (IRRBB): the six prescribed shocks applied to a base curve.

Shock at tenor t (years) for one currency; sizes in basis points from config/irrbb/<regime>.yaml:

    parallel up / down   +/- parallel
    short up / down      +/- short(t)
    steepener            steepener.short x short(t) + steepener.long x long(t)   (-0.65, +0.9)
    flattener            flattener.short x short(t) + flattener.long x long(t)   (+0.8, -0.6)

    short(t) = short x exp(-t / x)       long(t) = long x (1 - exp(-t / x)),   x = decay_years

(SRP31.90-31.92; UK ICAA 9.7-9.11.) The formulas reproduce the Basel example in SRP31.92.

Optional post-shock floor (UK, SS31/15 2.9B(iii)): start_bp at tenor 0, rising in a straight
line to 0 at zero_at_years and staying at 0 beyond. A shocked rate below the floor is raised to
it, but never above the base rate, so an observed rate that is already lower is kept.

Pure functions on numpy arrays; rates are decimals (0.01 = 1%).
"""

import numpy as np

from riskplatform.config import IrrbbRules

BP = 1e-4  # one basis point as a decimal


def _sizes(rules: IrrbbRules, currency: str):
    try:
        return rules.shocks.sizes_bp[currency]
    except KeyError:
        raise ValueError(f"{rules.regime}: no shock sizes for currency {currency}") from None


def shock(rules: IrrbbRules, currency: str, scenario: str, tenors: np.ndarray) -> np.ndarray:
    """The rate change at each tenor under one scenario, as a decimal."""
    if scenario not in rules.shocks.scenarios:
        raise ValueError(f"{rules.regime}: unknown scenario {scenario!r}")
    sizes = _sizes(rules, currency)
    t = np.asarray(tenors, dtype=float)
    decay = np.exp(-t / rules.shocks.decay_years.value)
    short = sizes.short * BP * decay
    long = sizes.long * BP * (1.0 - decay)
    steep, flat = rules.shocks.steepener, rules.shocks.flattener
    shapes = {
        "parallel_up": np.full_like(t, sizes.parallel * BP),
        "parallel_down": np.full_like(t, -sizes.parallel * BP),
        "short_up": short,
        "short_down": -short,
        "steepener": steep.short * short + steep.long * long,
        "flattener": flat.short * short + flat.long * long,
    }
    return shapes[scenario]


def floor_curve(rules: IrrbbRules, tenors: np.ndarray) -> np.ndarray | None:
    """The post-shock floor at each tenor, or None if the regime applies no floor."""
    floor = rules.shocks.floor
    if not floor.applied:
        return None
    t = np.minimum(np.asarray(tenors, dtype=float), floor.zero_at_years)
    return floor.start_bp * BP * (1.0 - t / floor.zero_at_years)


def shocked_rates(
    rules: IrrbbRules,
    currency: str,
    scenario: str,
    tenors: np.ndarray,
    base_rates: np.ndarray,
) -> np.ndarray:
    """Base rates plus the scenario's shock, then the floor (if any) at each tenor."""
    base = np.asarray(base_rates, dtype=float)
    rates = base + shock(rules, currency, scenario, tenors)
    floor = floor_curve(rules, tenors)
    if floor is not None:
        rates = np.maximum(rates, np.minimum(floor, base))  # keep observed rates below the floor
    return rates
