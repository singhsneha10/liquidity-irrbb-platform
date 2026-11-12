"""Repricing cash flows and gap: bucket edges, deposit split, fixed vs floating, real-data identity."""

from pathlib import Path

import pandas as pd
import pytest

from riskplatform.config import load_irrbb_rules
from riskplatform.ingestion.generate import generate
from riskplatform.irrbb.repricing import repricing_flows, repricing_gap

BASEL = load_irrbb_rules(Path(__file__).resolve().parents[1] / "config" / "irrbb" / "basel.yaml")


def _positions(*rows) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["category", "side", "balance_mn", "reprice_days"])
    return frame.astype({"balance_mn": float})


def _bucket_of(days: int) -> str:
    flows = repricing_flows(_positions(("govt_bonds", "asset", 100, days)), BASEL)
    return flows["bucket"].item()


@pytest.mark.parametrize(
    ("days", "bucket"),
    [
        (1, "overnight"),
        (2, "O/N-1M"),
        (30, "O/N-1M"),
        (31, "1M-3M"),
        (365, "9M-1Y"),
        (366, "1Y-1.5Y"),
        (30 * 365, "over 20Y"),
    ],
)
def test_bucket_edges(days, bucket):
    assert _bucket_of(days) == bucket


def test_non_maturity_deposits_split_into_core_and_non_core():
    flows = repricing_flows(
        _positions(
            ("retail_stable_deposits", "liability", 1000, 1),  # 70% core at 4 years
            ("retail_less_stable_deposits", "liability", 400, 1),  # 50% core at 3 years
        ),
        BASEL,
    ).set_index("bucket")["amount"]
    assert flows["3Y-4Y"] == pytest.approx(-700)  # 4.0 years is in the 3Y-4Y bucket
    assert flows["2Y-3Y"] == pytest.approx(-200)  # 3.0 years is in the 2Y-3Y bucket
    assert flows["overnight"] == pytest.approx(-300 - 200)
    assert flows.sum() == pytest.approx(-1400)  # nothing lost or added


def test_floating_reprices_at_its_reset_and_fixed_at_maturity():
    # Same 5-year loan: floating resets in 60 days; fixed reprices at maturity (generator rule).
    floating = repricing_flows(_positions(("corporate_loans", "asset", 100, 60)), BASEL)
    fixed = repricing_flows(_positions(("corporate_loans", "asset", 100, 5 * 365)), BASEL)
    assert (floating["bucket"].item(), fixed["bucket"].item()) == ("1M-3M", "4Y-5Y")


def test_off_balance_sheet_items_are_ignored():
    flows = repricing_flows(
        _positions(("undrawn_committed_lines", "off_balance_sheet", 200, 400)), BASEL
    )
    assert flows.empty


def test_gap_ends_at_assets_minus_liabilities_on_the_generated_bank(cfg):
    positions = generate(cfg).positions
    day = positions[positions["date"] == positions["date"].min()]
    for _, group in day.groupby(["entity", "currency"]):
        gap = repricing_gap(group, BASEL)
        on_sheet = group.groupby("side")["balance_mn"].sum()
        assert gap["cumulative_gap"].iloc[-1] == pytest.approx(
            on_sheet["asset"] - on_sheet["liability"]
        )
        assert (gap["gap"] == gap["assets"] - gap["liabilities"]).all()
        assert len(gap) == len(BASEL.buckets)


def test_missing_column_is_an_error():
    with pytest.raises(ValueError, match="reprice_days"):
        repricing_flows(_positions().drop(columns="reprice_days"), BASEL)
