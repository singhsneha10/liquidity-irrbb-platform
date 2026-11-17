"""Test T6: single-bank deposit outflows across every FDIC-insured institution, 2001 onwards.

Follows the plan fixed before the data was downloaded (docs/validation_report.md, section 6.9;
committed in 876ee97). Report only: T6 has no rating and cannot change the overall rating.

For each bank b and quarter t (consecutive quarters only):
    quarterly outflow   q = max(0, 1 - DEP(b,t) / DEP(b,t-1))
    implied monthly     m = 1 - (1 - q) ** (1/3)     at least one month fell by m or more
m is what is compared with the LCR's 30-day run-off rates (5% and 10%).

A bank-quarter is left out (column `excluded`, first reason that applies):
- "first": the bank's first quarter in the data (no previous quarter);
- "gap": the previous quarter is missing;
- "structure_event": a deposit-moving structure event (the codes in config/validation.yaml, fdic)
  for the bank, as the bank itself, the acquirer or the bank acquired, with an effective date
  after the previous quarter end and on or before this one;
- "small": previous-quarter deposits below the threshold ($10 million);
- "hand_check": named after the hand check of the largest outflows (config, all_banks).
Deposits are in thousands of dollars, as the FDIC reports them.
"""

import numpy as np
import pandas as pd
from pandas.tseries.offsets import QuarterEnd

SIZES = [  # previous-quarter deposits, thousands of dollars
    ("under $1bn", 0, 1_000_000),
    ("$1bn to $10bn", 1_000_000, 10_000_000),
    ("over $10bn", 10_000_000, float("inf")),
]
PERCENTILES = [0.5, 0.9, 0.99, 0.999]


def event_quarters(events: pd.DataFrame, quarters_before: int = 0) -> pd.DataFrame:
    """(cert, quarter) for every bank an event touches, in the quarter that contains it, and in
    the `quarters_before` quarters before it (0 in the plan; 1 for the supplementary figure,
    because banks often move deposits the quarter before the legal date of a merger or closing).

    An event effective on a quarter end belongs to that quarter: (previous end, this end]."""
    quarter = events["effective"] + QuarterEnd(0)  # rolls forward; a quarter end stays put
    touched = [
        pd.DataFrame({"cert": events[column], "quarter": quarter - QuarterEnd(back)})
        for column in ("cert", "acq_cert", "out_cert")
        for back in range(quarters_before + 1)
    ]
    pairs = pd.concat(touched, ignore_index=True).dropna(subset=["cert"])
    pairs["cert"] = pairs["cert"].astype("int64")
    return pairs.drop_duplicates().reset_index(drop=True)


def quarterly_outflows(
    deposits: pd.DataFrame,
    events: pd.DataFrame,
    min_previous_thousands: float,
    hand_excluded: set = frozenset(),
    quarters_before: int = 0,
) -> pd.DataFrame:
    """Every bank-quarter with q, m, the previous deposits and why it is excluded (if it is)."""
    table = deposits.sort_values(["cert", "quarter"]).reset_index(drop=True)
    by_bank = table.groupby("cert")
    table["previous_quarter"] = by_bank["quarter"].shift()
    table["previous_thousands"] = by_bank["deposits_thousands"].shift()
    ratio = table["deposits_thousands"] / table["previous_thousands"]
    table["q"] = (1.0 - ratio).clip(lower=0.0)
    table["m"] = 1.0 - (1.0 - table["q"]) ** (1.0 / 3.0)

    flagged = event_quarters(events, quarters_before).assign(event=True)
    table = table.merge(flagged, on=["cert", "quarter"], how="left")
    table["event"] = table["event"].fillna(False).astype(bool)
    expected_previous = table["quarter"] - QuarterEnd(1)
    by_hand = {(int(c), pd.Timestamp(q)) for c, q in hand_excluded}  # dates or timestamps
    hand = pd.Series(
        [(c, q) in by_hand for c, q in zip(table["cert"], table["quarter"], strict=True)],
        index=table.index,
    )
    conditions = [
        table["previous_quarter"].isna(),
        table["previous_quarter"] != expected_previous,
        table["event"],
        table["previous_thousands"] < min_previous_thousands,
        hand,
    ]
    reasons = ["first", "gap", "structure_event", "small", "hand_check"]
    table["excluded"] = np.select(conditions, reasons, default="")
    return table.drop(columns=["event"])


def size_of(previous_thousands: pd.Series) -> pd.Series:
    labels = pd.Series("", index=previous_thousands.index)
    for label, low, high in SIZES:
        labels[(previous_thousands >= low) & (previous_thousands < high)] = label
    return labels


def _stats(m: pd.Series, rates: tuple[float, float]) -> dict:
    if m.empty:
        return {"bank_quarters": 0}
    return {
        "bank_quarters": len(m),
        **{f"p{p * 100:g}": float(m.quantile(p)) for p in PERCENTILES},
        "share_above_stable": float((m > rates[0]).mean()),
        "share_above_less_stable": float((m > rates[1]).mean()),
    }


def summarise(table: pd.DataFrame, rates: tuple[float, float], stress_years: list[int]) -> dict:
    """The statistics fixed in the plan: by size and for all banks, overall and by period."""
    kept = table[table["excluded"] == ""].copy()
    kept["size"] = size_of(kept["previous_thousands"])
    stress = kept["quarter"].dt.year.isin(stress_years)
    groups = [("all banks", kept.index == kept.index)] + [
        (label, kept["size"] == label) for label, _, _ in SIZES
    ]
    result = {}
    for name, rows in groups:
        result[name] = {
            "all years": _stats(kept.loc[rows, "m"], rates),
            "stress years": _stats(kept.loc[rows & stress, "m"], rates),
            "other years": _stats(kept.loc[rows & ~stress, "m"], rates),
        }
    result["excluded"] = {k: int(v) for k, v in table["excluded"].value_counts().items() if k}
    result["kept"] = len(kept)
    return result


def largest(table: pd.DataFrame, n: int, min_previous_thousands: float) -> pd.DataFrame:
    """The n largest m among kept bank-quarters with previous deposits at least the minimum."""
    kept = table[
        (table["excluded"] == "") & (table["previous_thousands"] >= min_previous_thousands)
    ]
    return kept.nlargest(n, "m")[
        ["cert", "quarter", "previous_thousands", "deposits_thousands", "q", "m"]
    ].reset_index(drop=True)
