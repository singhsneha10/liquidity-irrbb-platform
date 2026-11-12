"""Net Stable Funding Ratio (NSFR) for one group of positions (e.g. one date, entity, currency).

NSFR = available stable funding (ASF) / required stable funding (RSF), over a one-year horizon.
Must be at least 100%.

- ASF: each liability x its ASF factor, plus capital. Capital is the implied equity of the group
  (on-balance-sheet assets minus liabilities), which has no maturity and takes its open-ended
  band (100%).
- RSF: each asset x its RSF factor, plus off-balance-sheet items (undrawn commitments) x their
  factor.
- Factors depend on residual maturity through bands in the rulebook: a row takes the first band
  whose below_days is above its residual_days, otherwise the open-ended last band.

Pure functions: no files are read. Every factor comes from config/liquidity/<regime>.yaml; see
docs/liquidity_worked_example.md for the method worked by hand.

Simplifications: a negative implied equity (a deficit) is not stable funding, so it counts as
zero ASF; it is still reported in `equity`. Book value is used for every amount; all assets are
assumed unencumbered.
"""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from riskplatform.config import FactorBand, LiquidityRules
from riskplatform.metrics.lcr import REQUIRED_COLUMNS

EQUITY = "equity"


@dataclass(frozen=True)
class NsfrResult:
    asf: float
    rsf: float
    ratio: float  # inf if RSF is 0 with ASF; nan if both are 0
    equity: float  # implied equity (may be negative); ASF counts max(equity, 0)
    by_category: pd.DataFrame  # category, side, amount, weighted (ASF or RSF contribution)


def band_factors(rows: pd.DataFrame, table: dict[str, list[FactorBand]]) -> pd.Series:
    """Each row's factor from its category's maturity bands (NaN if the category is unmapped)."""
    factors = pd.Series(np.nan, index=rows.index, dtype=float)
    for category, bands in table.items():
        mask = rows["category"] == category
        if not mask.any():
            continue
        if len(bands) == 1:  # one factor whatever the maturity (np.select needs a condition)
            factors[mask] = bands[0].factor
            continue
        days = rows.loc[mask, "residual_days"].to_numpy()
        conditions = [days < band.below_days for band in bands[:-1]]
        choices = [band.factor for band in bands[:-1]]
        factors[mask] = np.select(conditions, choices, default=bands[-1].factor)
    return factors


def _check(positions: pd.DataFrame, rules: LiquidityRules) -> None:
    missing = [column for column in REQUIRED_COLUMNS if column not in positions]
    if missing:
        raise ValueError(f"positions are missing columns {missing}")
    is_liability = positions["side"] == "liability"
    unknown = set(positions.loc[is_liability, "category"]) - set(rules.nsfr.asf)
    unknown |= set(positions.loc[~is_liability, "category"]) - set(rules.nsfr.rsf)
    if unknown:
        raise ValueError(f"{rules.regime}: no NSFR factor for categories {sorted(unknown)}")


def calculate_nsfr(positions: pd.DataFrame, rules: LiquidityRules) -> NsfrResult:
    """The NSFR and its components, for one group of positions."""
    _check(positions, rules)
    nsfr = rules.nsfr
    side = positions["side"]
    is_liability = side == "liability"
    balance = positions["balance_mn"].astype(float)

    equity = float(balance[side == "asset"].sum() - balance[is_liability].sum())
    equity_asf = max(equity, 0.0) * nsfr.asf[EQUITY][-1].factor  # no maturity: open-ended band

    factor = pd.Series(np.nan, index=positions.index, dtype=float)
    factor[is_liability] = band_factors(positions[is_liability], nsfr.asf)
    factor[~is_liability] = band_factors(positions[~is_liability], nsfr.rsf)
    weighted = balance * factor

    by_category = (
        pd.DataFrame(
            {
                "category": positions["category"],
                "side": side,
                "amount": balance,
                "weighted": weighted,
            }
        )
        .groupby(["category", "side"], as_index=False)[["amount", "weighted"]]
        .sum()
    )
    by_category = pd.concat(
        [
            by_category,
            pd.DataFrame(
                [{"category": EQUITY, "side": "equity", "amount": equity, "weighted": equity_asf}]
            ),
        ],
        ignore_index=True,
    )

    asf = float(weighted[is_liability].sum()) + equity_asf
    rsf = float(weighted[~is_liability].sum())
    if rsf > 0:
        ratio = asf / rsf
    else:
        ratio = math.inf if asf > 0 else math.nan
    return NsfrResult(asf=asf, rsf=rsf, ratio=ratio, equity=equity, by_category=by_category)
