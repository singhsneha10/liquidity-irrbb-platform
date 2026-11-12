"""Repricing cash flows and the repricing gap (IRRBB standardised framework, SRP31.96-31.112).

Each position's balance is placed in the time bucket where it next reprices (SRP31.96 Table 3,
from config/irrbb/<regime>.yaml):

- Non-maturity deposits (the categories under `nmd` in config) are split: the core share
  reprices at its core maturity, and the rest (non-core) reprices overnight (SRP31.111).
- Every other position reprices after reprice_days: at maturity if fixed-rate, at its next
  reset if floating (set by the generator).
- Amounts are signed: + for assets, - for liabilities. Equity is not a position, so it is
  excluded, as SRP31.97 requires.
- Interest rate swaps (the categories under `derivatives` in config) reprice as two legs of
  their notional: a pay-fixed swap is -notional at maturity (fixed leg paid) and +notional at
  the floating leg's next reset (received). The legs net to zero, so the gap still ends at
  assets minus liabilities. In the gap table the + leg counts as an asset, the - leg as a
  liability.

Simplifications: principal only (no coupons, commercial margins excluded, which SRP31.100
allows); off-balance-sheet commitments other than swaps are excluded (an undrawn line has no
repricing cash flow); a swap's next floating reset is one full reset period away (not in the data);
each bucket's cash flows sit at its midpoint; a non-maturity deposit's core part is one cash flow
at its core maturity rather than spread over several buckets.
"""

import numpy as np
import pandas as pd

from riskplatform.config import IrrbbRules, RepricingBucket

DAYS_PER_YEAR = 365.0
OVERNIGHT_YEARS = 1.0 / DAYS_PER_YEAR
REQUIRED_COLUMNS = ["category", "side", "balance_mn", "reprice_days"]
GAP_COLUMNS = ["bucket", "midpoint_years", "assets", "liabilities", "gap", "cumulative_gap"]


def bucket_index(years: np.ndarray, buckets: list[RepricingBucket]) -> np.ndarray:
    """The bucket number of each repricing time (0 = overnight)."""
    edges = [bucket.up_to_years for bucket in buckets[:-1]]  # upper edges; the last is open
    return np.searchsorted(edges, np.asarray(years, dtype=float), side="left")


def _slotted(positions: pd.DataFrame, rules: IrrbbRules) -> pd.DataFrame:
    """One row per repricing piece: bucket number and signed amount."""
    missing = [column for column in REQUIRED_COLUMNS if column not in positions]
    if missing:
        raise ValueError(f"positions are missing columns {missing}")
    on_sheet = positions[positions["side"].isin(["asset", "liability"])]
    sign = np.where(on_sheet["side"] == "asset", 1.0, -1.0)
    balance = on_sheet["balance_mn"].to_numpy(dtype=float) * sign
    nmd = rules.nmd.categories
    is_nmd = on_sheet["category"].isin(nmd).to_numpy()

    regular_years = on_sheet["reprice_days"].to_numpy(dtype=float)[~is_nmd] / DAYS_PER_YEAR
    pieces = [(regular_years, balance[~is_nmd])]
    if is_nmd.any():
        category = on_sheet["category"].to_numpy()[is_nmd]
        share = np.array([nmd[c].core_share for c in category])
        years = np.array([nmd[c].core_years for c in category])
        pieces.append((years, balance[is_nmd] * share))  # core: stays until core_years
        overnight = np.full(len(category), OVERNIGHT_YEARS)
        pieces.append((overnight, balance[is_nmd] * (1.0 - share)))  # non-core: overnight
    # Swaps (off balance sheet): two legs of the notional. Pay-fixed: -notional when the fixed
    # leg ends (maturity) and +notional at the floating leg's next reset; receive-fixed: the reverse.
    swaps = positions[positions["category"].isin(rules.derivatives)]
    for category, rows in swaps.groupby("category"):
        treatment = rules.derivatives[category]
        notional = rows["balance_mn"].to_numpy(dtype=float)
        fixed_sign = -1.0 if treatment.direction == "pay_fixed" else 1.0
        fixed_years = rows["reprice_days"].to_numpy(dtype=float) / DAYS_PER_YEAR
        reset_years = np.full(len(rows), treatment.floating_reset_days / DAYS_PER_YEAR)
        pieces.append((fixed_years, fixed_sign * notional))
        pieces.append((reset_years, -fixed_sign * notional))
    years = np.concatenate([p[0] for p in pieces])
    amounts = np.concatenate([p[1] for p in pieces])
    return pd.DataFrame({"bucket_no": bucket_index(years, rules.buckets), "amount": amounts})


def repricing_flows(positions: pd.DataFrame, rules: IrrbbRules) -> pd.DataFrame:
    """Net repricing cash flow per bucket that has one: bucket, time_years (midpoint), amount."""
    by_bucket = _slotted(positions, rules).groupby("bucket_no")["amount"].sum()
    by_bucket = by_bucket[by_bucket != 0]
    buckets = rules.buckets
    return pd.DataFrame(
        {
            "bucket": [buckets[i].label for i in by_bucket.index],
            "time_years": [buckets[i].midpoint_years for i in by_bucket.index],
            "amount": by_bucket.to_numpy(),
        }
    )


def repricing_gap(positions: pd.DataFrame, rules: IrrbbRules) -> pd.DataFrame:
    """Assets, liabilities, gap and cumulative gap for every bucket, in order."""
    slotted = _slotted(positions, rules)
    n = len(rules.buckets)
    index = slotted["bucket_no"].to_numpy()
    amount = slotted["amount"].to_numpy()
    assets = np.bincount(index, weights=np.where(amount > 0, amount, 0.0), minlength=n)
    liabilities = np.bincount(index, weights=np.where(amount < 0, -amount, 0.0), minlength=n)
    gap = assets - liabilities
    return pd.DataFrame(
        {
            "bucket": [bucket.label for bucket in rules.buckets],
            "midpoint_years": [bucket.midpoint_years for bucket in rules.buckets],
            "assets": assets,
            "liabilities": liabilities,
            "gap": gap,
            "cumulative_gap": np.cumsum(gap),
        },
        columns=GAP_COLUMNS,
    )
