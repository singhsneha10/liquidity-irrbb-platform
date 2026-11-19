"""The executive summary: status rules, key messages built from the figures, and headroom."""

import dataclasses
from pathlib import Path

import pandas as pd
import pytest

from riskplatform.config import load_storage_config
from riskplatform.dashboard import data, executive
from riskplatform.dashboard.executive import (
    AMBER,
    GREEN,
    RED,
    early_warning_card,
    liquidity_card,
    overall,
    rates_card,
)
from riskplatform.pipeline import load_inputs, run_pipeline

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


def _limits(rows):
    return pd.DataFrame(rows, columns=["limit_name", "who", "kind", "actual", "limit_value",
                                       "utilisation", "status"])  # fmt: skip


OK_LIQUIDITY = [("lcr", "A", "floor", 1.6, 1.1, 0.69, "ok"),
                ("nsfr", "A", "floor", 1.3, 1.05, 0.81, "ok")]  # fmt: skip


def test_liquidity_card_rules():
    assert liquidity_card(_limits(OK_LIQUIDITY), minimum_missed=False).status == GREEN
    warning = [*OK_LIQUIDITY, ("lcr", "B", "floor", 1.2, 1.1, 0.92, "warning")]
    assert liquidity_card(_limits(warning), False).status == AMBER
    breach = [*OK_LIQUIDITY, ("nsfr", "B", "floor", 1.0, 1.05, 1.05, "breach")]
    assert liquidity_card(_limits(breach), False).status == RED
    card = liquidity_card(_limits(OK_LIQUIDITY), minimum_missed=True)  # always red
    assert card.status == RED and card.reason.startswith("A regulatory minimum is missed")
    assert "limits 110% and 105%" in card.reason  # limits as whole percentages


def test_rates_card_rules():
    eve = [("eve_tier1", "A", "cap", 0.10, 0.15, 0.67, "ok")]
    no_outlier = pd.DataFrame({"who": ["A"], "outlier": [False]})
    assert rates_card(_limits(eve), no_outlier).status == GREEN
    warned = [("eve_tier1", "A", "cap", 0.14, 0.15, 0.93, "warning")]
    assert rates_card(_limits(warned), no_outlier).status == AMBER
    outlier = pd.DataFrame({"who": ["A"], "outlier": [True]})
    card = rates_card(_limits(eve), outlier)  # an IRRBB outlier is red even below the limit
    assert card.status == RED and card.reason.endswith("IRRBB outlier: A")


def test_early_warnings_are_amber_at_most():
    indicators = pd.DataFrame({"who": ["A", "B"], "status": ["alert", "alert"]})
    card = early_warning_card(indicators)
    assert card.status == AMBER and card.reason == "2 in alert (A, B)"
    assert early_warning_card(indicators.assign(status="ok")).status == GREEN


def test_overall_is_the_worst_card():
    cards = [executive.Card("x", GREEN, ""), executive.Card("y", AMBER, "")]
    assert overall(cards) == AMBER
    assert overall([*cards, executive.Card("z", RED, "")]) == RED


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    folder = tmp_path_factory.mktemp("executive")
    run_pipeline(dataclasses.replace(loaded, generator=small), folder)
    return data.open_dashboard("local", folder, folder / "cache",
                               load_storage_config(CONFIG / "storage.yaml", env={}))  # fmt: skip


@pytest.mark.parametrize("regime", ["basel", "pra"])
def test_on_a_real_run_cards_and_messages_follow_the_figures(board, regime):
    limits = data.limit_status(board.con, regime)
    cards = executive.cards(board.con, regime)
    assert [c.area for c in cards] == ["Liquidity", "Interest rate risk", "Early warnings"]
    rates = cards[1]
    if (limits.loc[limits["limit_name"] == "eve_tier1", "status"] == "breach").any():
        assert rates.status == RED
    messages = executive.key_messages(board.con, regime)
    assert 1 <= len(messages) <= 5
    for _, row in limits[limits["status"] == "breach"].iterrows():  # every breach is named
        assert any(row["who"] in m and f"{row['actual']:.1%}" in m for m in messages)
    lowest = limits[limits["limit_name"] == "lcr"]["actual"].min()
    assert any(f"lowest LCR is {lowest:.1%}" in m for m in messages)


def test_headroom_is_the_tightest_entity_per_limit(board):
    limits = data.limit_status(board.con, "basel")
    room = executive.headroom(board.con, "basel")
    assert list(room["limit_name"]) == ["lcr", "nsfr", "eve_tier1"]
    for _, row in room.iterrows():
        assert row["utilisation"] == limits.loc[
            limits["limit_name"] == row["limit_name"], "utilisation"].max()  # fmt: skip
