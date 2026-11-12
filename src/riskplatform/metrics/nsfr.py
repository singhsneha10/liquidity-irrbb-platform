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
from dataclasses import dataclass, field

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
    # Each row's (category, side, amount, weighted), kept to build by_category only on request:
    # the metrics run never needs it, and building it for every group is slow.
    rows: pd.DataFrame = field(repr=False)
    equity_asf: float = field(repr=False)

    @property
    def by_category(self) -> pd.DataFrame:
        """category, side, amount, weighted (ASF or RSF contribution); equity as its own row."""
        table = self.rows.groupby(["category", "side"], as_index=False)[["amount", "weighted"]]
        equity = {"category": EQUITY, "side": "equity", "amount": self.equity,
                  "weighted": self.equity_asf}  # fmt: skip
        return pd.concat([table.sum(), pd.DataFrame([equity])], ignore_index=True)


def _factors(
    categories: np.ndarray, days: np.ndarray, table: dict[str, list[FactorBand]]
) -> np.ndarray:
    """Each row's factor from its category's maturity bands (NaN if the category is unmapped).

    A row takes the first band whose below_days is above its days, else the open-ended last
    band. The limits rise strictly (checked in config), so searchsorted finds that band: the
    number of limits at or below the row's days is the position of the first limit above them.
    """
    factors = np.full(len(categories), np.nan)
    for category, bands in table.items():
        mask = categories == category
        if not mask.any():
            continue
        limits = np.array([band.below_days for band in bands[:-1]], dtype=float)
        choices = np.array([band.factor for band in bands])
        factors[mask] = choices[np.searchsorted(limits, days[mask], side="right")]
    return factors


def calculate_nsfr(positions: pd.DataFrame, rules: LiquidityRules) -> NsfrResult:
    """The NSFR and its components, for one group of positions."""
    missing = [column for column in REQUIRED_COLUMNS if column not in positions]
    if missing:
        raise ValueError(f"positions are missing columns {missing}")
    nsfr = rules.nsfr
    categories = positions["category"].to_numpy()
    side = positions["side"].to_numpy()
    is_liability = side == "liability"
    balance = positions["balance_mn"].to_numpy(dtype=float)
    days = positions["residual_days"].to_numpy()

    unknown = set(categories[is_liability]) - set(nsfr.asf)
    unknown |= set(categories[~is_liability]) - set(nsfr.rsf)
    if unknown:
        raise ValueError(f"{rules.regime}: no NSFR factor for categories {sorted(unknown)}")

    equity = float(balance[side == "asset"].sum() - balance[is_liability].sum())
    equity_asf = max(equity, 0.0) * nsfr.asf[EQUITY][-1].factor  # no maturity: open-ended band

    # Liabilities take ASF factors; assets and off-balance-sheet items take RSF factors.
    factor = np.where(
        is_liability, _factors(categories, days, nsfr.asf), _factors(categories, days, nsfr.rsf)
    )
    weighted = balance * factor

    asf = float(weighted[is_liability].sum()) + equity_asf
    rsf = float(weighted[~is_liability].sum())
    if rsf > 0:
        ratio = asf / rsf
    else:
        ratio = math.inf if asf > 0 else math.nan
    rows = pd.DataFrame(
        {"category": categories, "side": side, "amount": balance, "weighted": weighted}
    )
    return NsfrResult(
        asf=asf, rsf=rsf, ratio=ratio, equity=equity, rows=rows, equity_asf=equity_asf
    )
