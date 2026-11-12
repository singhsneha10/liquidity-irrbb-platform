"""Cash-flow maturity ladder for one group of positions (e.g. one date, entity, currency).

A contractual view: each asset's principal flows in, and each liability's flows out, in the
bucket where it matures (by residual_days). Off-balance-sheet commitments have no contractual
maturity cash flow, so they are reported separately as contingent outflows.

Every asset and liability falls in exactly one bucket, so the cumulative gap after the last
bucket equals assets minus liabilities (the implied equity).

Simplifications: principal only (no interest cash flows); contractual maturities only (no
behavioural run-off; the LCR covers stressed behaviour); non-maturity deposits fall overnight.
Buckets come from config/liquidity/ladder.yaml.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from riskplatform.config import LadderBucket, LadderConfig
from riskplatform.metrics.lcr import REQUIRED_COLUMNS

LADDER_COLUMNS = ["bucket", "from_days", "to_days", "inflows", "outflows", "net", "cumulative_net"]


@dataclass(frozen=True)
class MaturityLadder:
    table: pd.DataFrame  # one row per bucket, in order, including empty buckets
    contingent: float  # off-balance-sheet commitments (undrawn), outside the buckets


def bucket_of(residual_days: pd.Series, buckets: list[LadderBucket]) -> np.ndarray:
    """The bucket number of each residual maturity (0 = first bucket)."""
    edges = [bucket.up_to_days for bucket in buckets[:-1]]  # last day included in each bucket
    return np.searchsorted(edges, residual_days.to_numpy(), side="left")


def build_ladder(positions: pd.DataFrame, ladder: LadderConfig) -> MaturityLadder:
    """The maturity ladder and contingent total, for one group of positions."""
    missing = [column for column in REQUIRED_COLUMNS if column not in positions]
    if missing:
        raise ValueError(f"positions are missing columns {missing}")
    buckets = ladder.buckets
    side = positions["side"]
    balance = positions["balance_mn"].astype(float)
    index = bucket_of(positions["residual_days"], buckets)

    def by_bucket(mask: pd.Series) -> np.ndarray:
        return np.bincount(index[mask.to_numpy()], weights=balance[mask], minlength=len(buckets))

    inflows = by_bucket(side == "asset")
    outflows = by_bucket(side == "liability")
    net = inflows - outflows
    starts = [1] + [bucket.up_to_days + 1 for bucket in buckets[:-1]]
    table = pd.DataFrame(
        {
            "bucket": [bucket.label for bucket in buckets],
            "from_days": starts,
            "to_days": pd.array([bucket.up_to_days for bucket in buckets], dtype="Int64"),
            "inflows": inflows,
            "outflows": outflows,
            "net": net,
            "cumulative_net": np.cumsum(net),
        },
        columns=LADDER_COLUMNS,
    )
    contingent = float(balance[side == "off_balance_sheet"].sum())
    return MaturityLadder(table=table, contingent=contingent)
