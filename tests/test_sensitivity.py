"""LCR sensitivity to the retail run-off rates (T3) and the benchmark (T4)."""

from pathlib import Path

import pandas as pd
import pytest

from riskplatform.config import load_generator_config, load_ladder_config
from riskplatform.ingestion.generate import generate
from riskplatform.metrics.run import compute_metrics, load_liquidity_regimes
from riskplatform.model_validation.backtest import AMBER, GREEN, LESS_STABLE, RED, STABLE
from riskplatform.model_validation.sensitivity import (
    break_even,
    latest_groups,
    lcr_at,
    rate_t3,
    t3,
    t4,
    with_retail_run_off,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


@pytest.fixture(scope="module")
def bank():
    cfg = load_generator_config(CONFIG / "generator.yaml")
    small = cfg.model_validate({**cfg.model_dump(), "n_days": 2})
    raw = generate(small)
    regimes = load_liquidity_regimes(CONFIG / "liquidity")
    return small, raw.positions, raw.fx, regimes


def test_only_the_two_retail_rates_change(bank):
    _, _, _, regimes = bank
    rules = regimes["basel"]
    changed = with_retail_run_off(rules, 0.3)
    assert changed.lcr.outflows[STABLE].rate == changed.lcr.outflows[LESS_STABLE].rate == 0.3
    before, after = rules.model_dump(), changed.model_dump()
    for category in (STABLE, LESS_STABLE):
        before["lcr"]["outflows"][category]["rate"] = 0.3
    assert before == after  # everything else, references included, is untouched
    assert rules.lcr.outflows[STABLE].rate == 0.05  # the original is not modified


def test_with_the_rulebook_rates_it_reproduces_the_metrics_run(bank):
    cfg, positions, fx, regimes = bank
    official = compute_metrics(positions, fx, cfg, regimes, load_ladder_config(
        CONFIG / "liquidity" / "ladder.yaml")).lcr  # fmt: skip
    latest = official[(official["date"] == official["date"].max())
                      & official["level"].isin(["entity", "group"])]  # fmt: skip
    for key, group in latest_groups(positions, fx, cfg):
        for name, rules in regimes.items():
            row = latest[(latest["level"] == key["level"]) & (latest["regime"] == name)
                         & (latest["entity"].fillna("GROUP") == (key["entity"] or "GROUP"))]  # fmt: skip
            assert lcr_at(group, rules, None) == pytest.approx(row["lcr"].iloc[0], rel=1e-12)


def test_the_lcr_falls_as_the_run_off_rises(bank):
    cfg, positions, fx, regimes = bank
    for _, group in latest_groups(positions, fx, cfg):
        ratios = [lcr_at(group, regimes["pra"], rate) for rate in (0.0, 0.1, 0.3, 0.6, 1.0)]
        assert ratios == sorted(ratios, reverse=True)


def test_break_even_lands_on_the_target(bank):
    cfg, positions, fx, regimes = bank
    (_, group), *_ = latest_groups(positions, fx, cfg)
    rules = regimes["basel"]
    rate = break_even(group, rules, 1.10)
    assert rate is not None and 0 < rate < 1
    assert lcr_at(group, rules, rate) == pytest.approx(1.10, abs=1e-9)
    assert lcr_at(group, rules, rate + 1e-6) < 1.10
    assert break_even(group, rules, 100.0) == 0.0  # already below a 10,000% target
    assert break_even(group, rules, 0.0) is None  # never falls below 0%


@pytest.mark.parametrize(
    ("lcrs", "rating"),
    [([1.5, 1.10], GREEN), ([1.5, 1.0999], AMBER), ([1.5, 1.0], AMBER), ([1.5, 0.9999], RED)],
)
def test_t3_rating_bands_as_written_in_section_4(lcrs, rating):
    """Green at or above 110%; amber below 110% but at or above 100%; red below 100%."""
    assert rate_t3(lcrs, limit=1.10, minimum=1.00) == rating


def test_t3_covers_every_entity_and_the_group_in_both_rulebooks(bank):
    cfg, positions, fx, regimes = bank
    result = t3(latest_groups(positions, fx, cfg), regimes, 0.1069, 1.10, [0.05, 0.3])
    table = result["table"]
    assert len(table) == 4 * 2  # 3 entities + the group, 2 rulebooks
    assert set(table["who"]) == {"BANK_EU", "BANK_UK", "BANK_US", "GROUP"}
    assert (table["stressed_lcr"] < table["lcr"]).all()  # a harsher run-off always costs
    assert (table["lcr_at_0.30"] < table["lcr_at_0.05"]).all()
    assert result["rating"] == rate_t3(table["stressed_lcr"].tolist(), 1.10, 1.0)


def test_t4_percentile_by_hand():
    """Five 1-week outflows 0, 0, 2%, 4%, 10% (a rise counts as 0): the 50th percentile is 2%."""
    levels = [100.0, 101.0, 102.0, 99.96, 95.9616, 86.36544]
    deposits = pd.DataFrame(
        {
            "date": pd.date_range("2023-01-04", periods=6, freq="7D"),
            "series": "s",
            "fred_id": "X",
            "deposits_bn": levels,
        }
    )
    assert t4(deposits, weeks=1, percentile=0.5)["s"] == pytest.approx(0.02)
    assert t4(deposits, weeks=1, percentile=1.0)["s"] == pytest.approx(0.10)


def test_a_stress_never_lowers_a_rulebook_rate(bank):
    """A 6.43% stress raises the stable rate (5%) but keeps the less-stable rate at its 10%:
    a stress must not soften an assumption (the flaw found after the results, section 9)."""
    cfg, positions, fx, regimes = bank
    rules = regimes["basel"]
    stressed = with_retail_run_off(rules, 0.0643)
    assert stressed.lcr.outflows[STABLE].rate == 0.0643
    assert stressed.lcr.outflows[LESS_STABLE].rate == 0.10
    for _, group in latest_groups(positions, fx, cfg):
        for rate in (0.0, 0.03, 0.0643, 0.12):
            assert lcr_at(group, rules, rate) <= lcr_at(group, rules, None) + 1e-12


def test_t4_leaves_out_windows_spanning_a_confirmed_break():
    from riskplatform.config import SeriesBreak

    levels = [100.0, 100.0, 50.0, 50.0, 50.0]  # a 50% "outflow" that is a break
    deposits = pd.DataFrame({"date": pd.date_range("2023-01-04", periods=5, freq="7D"),
                             "series": "s", "fred_id": "X", "deposits_bn": levels})  # fmt: skip
    gap = SeriesBreak(series="s", last_before="2023-01-11", first_after="2023-01-18", evidence="t")
    assert t4(deposits, weeks=1, percentile=1.0)["s"] == pytest.approx(0.5)
    assert t4(deposits, weeks=1, percentile=1.0, breaks=[gap])["s"] == 0.0
