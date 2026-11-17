"""The run-off backtest (T1, T2): hand-worked series first, then the synthetic banks.

The hand-worked cases were written before the backtest was run on any real data.
"""

from pathlib import Path

import pandas as pd
import pytest

from riskplatform.config import load_generator_config, load_validation_config
from riskplatform.ingestion.generate import generate
from riskplatform.model_validation.backtest import (
    AMBER,
    GREEN,
    INCONCLUSIVE,
    RED,
    STABLE,
    RunOffRates,
    forward_outflow,
    overall,
    planted_size,
    rate_t1,
    retail_deposits,
    run_off_rates,
    spells,
    t1_series,
    t2,
)

ROOT = Path(__file__).resolve().parents[1]
CFG = load_validation_config(ROOT / "config" / "validation.yaml").backtest
RATES = RunOffRates(stable=0.05, less_stable=0.10)


def _weekly(values: list[float], start: str = "2023-01-04") -> pd.Series:
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="7D"))


def test_the_rates_come_from_both_rulebooks():
    assert run_off_rates(ROOT / "config" / "liquidity") == RATES


def test_forward_outflow_by_hand():
    """100 -> 90 two steps later is a 10% outflow; 90 -> 99 is a rise, so 0; no t+h -> NaN."""
    out = forward_outflow(_weekly([100.0, 95.0, 90.0, 99.0]), steps=2)
    assert out.iloc[0] == pytest.approx(0.10)  # 1 - 90/100
    assert out.iloc[1] == 0.0  # 95 -> 99 is a rise: no outflow (not -4%)
    assert out.iloc[2:].isna().all()


def test_a_rise_counts_as_no_outflow():
    out = forward_outflow(_weekly([100.0, 105.0, 110.0]), steps=1)
    assert out.dropna().tolist() == [0.0, 0.0]


@pytest.mark.parametrize(
    ("worst", "rating"),
    [(0.0, GREEN), (0.05, GREEN), (0.0501, AMBER), (0.10, AMBER), (0.1001, RED)],
)
def test_t1_rating_bands_as_written_in_section_4(worst, rating):
    """Green up to and including 5%, amber above 5% up to and including 10%, red above 10%."""
    assert rate_t1(worst, RATES) == rating


def test_spells_group_consecutive_dates_above_the_threshold():
    out = _weekly([0.01, 0.06, 0.08, 0.02, 0.07, 0.0])
    found = spells(out, 0.05)
    assert found["dates"].tolist() == [2, 1]
    assert found["worst"].tolist() == [0.08, 0.07]
    assert found["worst_date"].tolist() == [out.index[2], out.index[4]]
    assert spells(out, 0.5).empty


def test_t1_on_a_hand_made_series():
    """Flat at 100 except a fall to 92 in week 6 (and back): 4-week outflows from weeks 2-5 see
    the 8% fall; the worst 4-week outflow is 8% (amber), and the 5-week check sees it too."""
    levels = _weekly([100.0] * 6 + [92.0] + [100.0] * 10)
    result = t1_series(levels, CFG, RATES)
    assert result["main"]["worst"] == pytest.approx(0.08)
    assert result["main"]["worst_date"] == levels.index[2].date().isoformat()  # 4 weeks before
    assert result["conservative"]["worst"] == pytest.approx(0.08)
    assert result["above_stable"] == 1 and result["above_less_stable"] == 0
    assert result["rating"] == AMBER
    assert result["windows"] == len(levels) - CFG.h8_weeks
    assert len(result["spells_above_stable"]) == 1


def test_overall_is_the_worst_unless_detection_failed():
    assert overall([GREEN, AMBER], GREEN) == AMBER
    assert overall([GREEN, GREEN], GREEN, RED) == RED
    assert overall([GREEN, GREEN], RED) == INCONCLUSIVE


# --- T2 on the synthetic banks --------------------------------------------------------------------


@pytest.fixture(scope="module")
def banks():
    normal_cfg = load_generator_config(ROOT / CFG.normal_config)
    stress_cfg = load_generator_config(ROOT / CFG.stress_config)
    normal, stressed = generate(normal_cfg), generate(stress_cfg)
    return (normal.positions, normal.fx), (stressed.positions, stressed.fx), stress_cfg


def test_retail_deposits_use_fixed_fx(banks):
    (positions, fx), _, _ = banks
    table = retail_deposits(positions, fx)
    assert table.shape[1] == 3 * 2  # 3 entities x 2 retail categories
    first_day = positions[positions["date"] == positions["date"].min()]
    rate = fx[fx["date"] == fx["date"].min()].set_index("currency")["to_reporting"]
    uk = first_day[(first_day["entity"] == "BANK_UK") & (first_day["category"] == STABLE)]
    expected = (uk["balance_mn"] * uk["currency"].map(rate)).sum()
    assert table[("BANK_UK", STABLE)].iloc[0] == pytest.approx(expected)


def test_planted_size_is_the_sterling_share_of_the_stable_outflow(banks):
    normal, _, stress_cfg = banks
    size = planted_size(stress_cfg, *normal)
    assert 0.05 < size < stress_cfg.stress.outflows[STABLE]  # GBP only: a little under 10%


def test_t2_finds_the_planted_run_and_is_quiet_without_it(banks):
    normal, stressed, stress_cfg = banks
    result = t2(normal, stressed, stress_cfg, CFG, RATES)
    assert result["normal_quiet"] and result["found_run"] and result["rating"] == GREEN
    assert result["distance"] <= CFG.detection_tolerance


def test_t2_fails_when_the_backtest_cannot_see_the_run(banks):
    """The same check with the stressed bank replaced by the normal one must fail: a backtest
    that finds nothing where a run was planted cannot be trusted (T1 then inconclusive)."""
    normal, _, stress_cfg = banks
    result = t2(normal, normal, stress_cfg, CFG, RATES)
    assert not result["found_run"] and result["rating"] == RED


# --- Confirmed breaks (limitation 7) ---------------------------------------------------------------


def _break(series="s", before="2023-02-01", after="2023-02-08"):
    from riskplatform.config import SeriesBreak

    return SeriesBreak(series=series, last_before=before, first_after=after, evidence="test")


def test_without_breaks_blanks_exactly_the_windows_spanning_the_step():
    """Weekly from 4 Jan; the step is 1 Feb -> 8 Feb. 2-week windows spanning it start on
    25 Jan (to 8 Feb) and 1 Feb (to 15 Feb); the others are kept."""
    from riskplatform.model_validation.backtest import without_breaks

    out = forward_outflow(_weekly([100.0] * 8), steps=2)
    cleaned = without_breaks(out, 2, [_break()], "s")
    blanked = cleaned[cleaned.isna() & out.notna()].index.strftime("%m-%d").tolist()
    assert blanked == ["01-25", "02-01"]
    assert without_breaks(out, 2, [_break(series="other")], "s").equals(out)  # other series


def test_a_confirmed_break_leaves_t1_but_the_raw_rating_is_kept():
    """A 15% one-week step down at a confirmed break: RED before exclusions, GREEN after."""
    levels = _weekly([100.0] * 5 + [85.0] * 10)  # step between week 5 and week 6
    step_before, step_after = levels.index[4].date(), levels.index[5].date()
    cfg = CFG.model_copy(update={"confirmed_breaks": [_break("s", step_before, step_after)]})
    result = t1_series(levels, cfg, RATES, "s")
    assert result["raw"]["rating"] == RED and result["raw"]["worst"] == pytest.approx(0.15)
    assert result["rating"] == GREEN and result["main"]["worst"] == 0.0
    assert len(result["excluded_windows"]) == CFG.h8_weeks  # every 4-week window across the step
    assert len(result["spells_above_stable"]) == 1  # still listed for the check
