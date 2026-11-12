"""Net interest income (NII) sensitivity under the NII shock scenarios.

For one currency, with a constant balance sheet over the horizon H (config nii.horizon_months):

    delta NII(scenario) = sum over cash flows with t < H of
                          amount x (shocked rate(t) - base rate(t)) x (H - t)

A cash flow repricing within the horizon is replaced like-for-like and earns (asset, amount > 0)
or pays (liability, amount < 0) the new rate for the rest of the horizon. Later cash flows keep
their current rate, so their NII does not change. Shocked rates include the post-shock floor
(irrbb.shocks), and the base rate at t is read from the curve as for EVE.

Simplifications: constant balance sheet; principal only (rate changes apply to the repricing
amount, not to coupons); the scenarios (parallel up and down) and the 12-month horizon are
assumptions, because neither rulebook names the NII scenarios.
"""

import numpy as np
import pandas as pd

from riskplatform.config import IrrbbRules
from riskplatform.irrbb.eve import base_rates_at
from riskplatform.irrbb.shocks import shocked_rates


def delta_nii(
    flows: pd.DataFrame,
    tenors: np.ndarray,
    base_rates: np.ndarray,
    rules: IrrbbRules,
    currency: str,
) -> dict[str, float]:
    """Delta NII over the horizon for each NII scenario, for one currency's repricing flows."""
    horizon = rules.nii.horizon_months / 12.0
    t = flows["time_years"].to_numpy(dtype=float)
    amount = flows["amount"].to_numpy(dtype=float)
    within = t < horizon
    t, amount = t[within], amount[within]
    base = base_rates_at(t, tenors, base_rates)
    remaining = horizon - t
    result = {}
    for scenario in rules.nii.scenarios:
        change = shocked_rates(rules, currency, scenario, t, base) - base
        result[scenario] = float(np.sum(amount * change * remaining))
    return result
