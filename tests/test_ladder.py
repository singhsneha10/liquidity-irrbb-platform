"""Maturity ladder: the hand-worked example, bucket edges, and the equity cross-check."""

from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError
from test_liquidity_worked_example import PORTFOLIO  # one definition of the hand-worked bank

from riskplatform.config import LadderConfig, load_ladder_config
from riskplatform.ingestion.generate import generate
from riskplatform.metrics.ladder import build_ladder

LADDER = load_ladder_config(
    Path(__file__).resolve().parents[1] / "config" / "liquidity" / "ladder.yaml"
)

# The ladder section of docs/liquidity_worked_example.md: (bucket, inflows, outflows, cumulative)
HAND_LADDER = [
    ("overnight", 100, 1800, -1700),
    ("2-7 days", 0, 80, -1780),
    ("8-30 days", 850, 450, -1380),
    ("31-90 days", 0, 100, -1480),
    ("91-180 days", 0, 0, -1480),
    ("181-365 days", 0, 50, -1530),
    ("1-2 years", 200, 150, -1480),
    ("2-5 years", 500, 0, -980),
    ("over 5 years", 1500, 0, 520),
]


def _positions(*rows) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["category", "side", "balance_mn", "residual_days"])
    return frame.astype({"balance_mn": float})


def test_worked_example_ladder_matches_the_hand_calculation():
    portfolio = pd.DataFrame(
        PORTFOLIO, columns=["id", "category", "side", "balance_mn", "residual_days"]
    ).astype({"balance_mn": float})
    ladder = build_ladder(portfolio, LADDER)
    table = ladder.table
    assert table["bucket"].tolist() == [row[0] for row in HAND_LADDER]
    assert table["inflows"].tolist() == [row[1] for row in HAND_LADDER]
    assert table["outflows"].tolist() == [row[2] for row in HAND_LADDER]
    assert table["cumulative_net"].tolist() == [row[3] for row in HAND_LADDER]
    assert (table["net"] == table["inflows"] - table["outflows"]).all()
    assert ladder.contingent == 200


@pytest.mark.parametrize(
    ("days", "bucket"),
    [
        (1, "overnight"),
        (7, "2-7 days"),
        (8, "8-30 days"),
        (30, "8-30 days"),
        (31, "31-90 days"),
        (1825, "2-5 years"),
        (1826, "over 5 years"),
    ],
)
def test_bucket_edges(days, bucket):
    table = build_ladder(_positions(("govt_bonds", "asset", 100, days)), LADDER).table
    assert table.loc[table["inflows"] == 100, "bucket"].tolist() == [bucket]


def test_cumulative_gap_equals_equity_on_the_generated_bank(cfg):
    positions = generate(cfg).positions
    day = positions[positions["date"] == positions["date"].min()]
    for _, group in day.groupby(["entity", "currency"]):
        on_sheet = group.groupby("side")["balance_mn"].sum()
        equity = on_sheet["asset"] - on_sheet["liability"]
        final_gap = build_ladder(group, LADDER).table["cumulative_net"].iloc[-1]
        assert final_gap == pytest.approx(equity)


def test_every_bucket_is_present_even_when_empty():
    empty = build_ladder(_positions(), LADDER)
    assert len(empty.table) == len(LADDER.buckets)
    assert (empty.table[["inflows", "outflows", "net", "cumulative_net"]] == 0).all().all()
    assert empty.contingent == 0


def test_off_balance_sheet_items_are_contingent_only():
    ladder = build_ladder(
        _positions(("undrawn_committed_lines", "off_balance_sheet", 200, 1)), LADDER
    )
    assert ladder.contingent == 200
    assert (ladder.table[["inflows", "outflows"]] == 0).all().all()


@pytest.mark.parametrize(
    "buckets",
    [
        [{"label": "a"}, {"label": "b", "up_to_days": 5}],  # open-ended bucket not last
        [{"label": "a", "up_to_days": 5}, {"label": "b", "up_to_days": 5}, {"label": "c"}],
        [{"label": "a", "up_to_days": 5}, {"label": "a"}],  # duplicate label
    ],
)
def test_bad_ladder_configs_are_rejected(buckets):
    with pytest.raises(ValidationError):
        LadderConfig.model_validate({"buckets": buckets})
