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


def _slotted(positions: pd.DataFrame, rules: IrrbbRules) -> tuple[np.ndarray, np.ndarray]:
    """Every repricing piece, as two arrays: bucket number and signed amount."""
    missing = [column for column in REQUIRED_COLUMNS if column not in positions]
    if missing:
        raise ValueError(f"positions are missing columns {missing}")
    category = positions["category"].to_numpy()
    side = positions["side"].to_numpy()
    balance = positions["balance_mn"].to_numpy(dtype=float)
    reprice_years = positions["reprice_days"].to_numpy(dtype=float) / DAYS_PER_YEAR
    on_sheet = (side == "asset") | (side == "liability")
    signed = np.where(side == "asset", balance, -balance)
    nmd = rules.nmd.categories
    is_nmd = np.zeros(len(category), dtype=bool)
    for name in nmd:
        is_nmd |= category == name

    regular = on_sheet & ~is_nmd
    pieces = [(reprice_years[regular], signed[regular])]
    for name, treatment in nmd.items():
        rows = on_sheet & (category == name)
        if rows.any():
            amount = signed[rows]
            core = np.full(len(amount), treatment.core_years)
            pieces.append((core, amount * treatment.core_share))  # core: stays until core_years
            overnight = np.full(len(amount), OVERNIGHT_YEARS)
            pieces.append((overnight, amount * (1.0 - treatment.core_share)))  # non-core
    # Swaps (off balance sheet): two legs of the notional. Pay-fixed: -notional when the fixed
    # leg ends (maturity) and +notional at the floating leg's next reset; receive-fixed: the reverse.
    for name, treatment in rules.derivatives.items():
        rows = category == name
        if not rows.any():
            continue
        notional = balance[rows]
        fixed_sign = -1.0 if treatment.direction == "pay_fixed" else 1.0
        reset_years = np.full(len(notional), treatment.floating_reset_days / DAYS_PER_YEAR)
        pieces.append((reprice_years[rows], fixed_sign * notional))
        pieces.append((reset_years, -fixed_sign * notional))
    years = np.concatenate([p[0] for p in pieces])
    amounts = np.concatenate([p[1] for p in pieces])
    return bucket_index(years, rules.buckets), amounts


def repricing_flows(positions: pd.DataFrame, rules: IrrbbRules) -> pd.DataFrame:
    """Net repricing cash flow per bucket that has one: bucket, time_years (midpoint), amount."""
    index, amount = _slotted(positions, rules)
    totals = np.bincount(index, weights=amount, minlength=len(rules.buckets))
    used = np.flatnonzero(totals != 0)
    buckets = rules.buckets
    return pd.DataFrame(
        {
            "bucket": [buckets[i].label for i in used],
            "time_years": [buckets[i].midpoint_years for i in used],
            "amount": totals[used],
        }
    )


def repricing_gap(positions: pd.DataFrame, rules: IrrbbRules) -> pd.DataFrame:
    """Assets, liabilities, gap and cumulative gap for every bucket, in order."""
    index, amount = _slotted(positions, rules)
    n = len(rules.buckets)
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
