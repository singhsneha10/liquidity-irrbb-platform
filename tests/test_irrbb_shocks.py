"""IRRBB shock scenarios: shapes, symmetry, the UK floor, errors, and the worked example's rates."""

from pathlib import Path

import numpy as np
import pytest

from riskplatform.config import load_irrbb_rules
from riskplatform.irrbb.shocks import floor_curve, shock, shocked_rates

RULES_DIR = Path(__file__).resolve().parents[1] / "config" / "irrbb"
BASEL = load_irrbb_rules(RULES_DIR / "basel.yaml")
PRA = load_irrbb_rules(RULES_DIR / "pra.yaml")
TENORS = np.array([0.0, 0.5, 1.0, 5.0, 10.0, 20.0, 30.0])


def test_parallel_shock_is_the_same_at_every_tenor():
    assert np.allclose(shock(BASEL, "GBP", "parallel_up", TENORS), 0.0275)
    assert np.allclose(shock(BASEL, "GBP", "parallel_down", TENORS), -0.0275)


def test_short_shock_fades_with_tenor():
    short = shock(BASEL, "GBP", "short_up", TENORS)
    assert short[0] == pytest.approx(0.0425)  # full size at t = 0
    assert np.all(np.diff(short) < 0)  # always fading
    assert short[TENORS == 20.0][0] < 0.003


def test_long_shock_is_zero_at_the_short_end():
    # At t = 0 the long shape is 0, so the twists are just their short weights.
    assert shock(BASEL, "GBP", "steepener", np.array([0.0]))[0] == pytest.approx(-0.65 * 0.0425)
    assert shock(BASEL, "GBP", "flattener", np.array([0.0]))[0] == pytest.approx(0.80 * 0.0425)


@pytest.mark.parametrize("pair", [("parallel_up", "parallel_down"), ("short_up", "short_down")])
def test_up_and_down_are_mirror_images(pair):
    up, down = (shock(BASEL, "EUR", scenario, TENORS) for scenario in pair)
    assert np.allclose(up, -down)


def test_uk_floor_shape():
    floor = floor_curve(PRA, np.array([0.0, 10.0, 20.0, 40.0]))
    assert floor == pytest.approx([-0.01, -0.005, 0.0, 0.0])
    assert floor_curve(BASEL, TENORS) is None


def test_uk_floor_binds_only_where_needed():
    base = np.full_like(TENORS, 0.01)
    uk = shocked_rates(PRA, "GBP", "parallel_down", TENORS, base)
    assert np.allclose(uk, np.maximum(0.01 - 0.0275, floor_curve(PRA, TENORS)))
    up = shocked_rates(PRA, "GBP", "parallel_up", TENORS, base)
    assert np.allclose(up, 0.01 + 0.0275)  # far above the floor: untouched


def test_observed_rate_below_the_floor_is_kept():
    base = np.array([-0.02])  # already below the -1% floor at t = 0
    assert shocked_rates(PRA, "GBP", "parallel_down", np.array([0.0]), base)[0] == pytest.approx(
        -0.02
    )


def test_basel_applies_no_floor():
    rates = shocked_rates(BASEL, "GBP", "parallel_down", TENORS, np.zeros_like(TENORS))
    assert np.allclose(rates, -0.0275)


def test_unknown_currency_or_scenario_is_an_error():
    with pytest.raises(ValueError, match="JPY"):
        shock(BASEL, "JPY", "parallel_up", TENORS)
    with pytest.raises(ValueError, match="twist"):
        shock(BASEL, "GBP", "twist", TENORS)


# docs/irrbb_worked_example.md section 2: shocked rates at t = 0.0028, 3.5, 4.5 (base 1%).
WORKED_RATES = {
    ("basel", "parallel_down"): [-0.017500, -0.017500, -0.017500],
    ("pra", "parallel_down"): [-0.009999, -0.008250, -0.007750],
    ("basel", "steepener"): [-0.017590, 0.011605, 0.016227],
    ("pra", "steepener"): [-0.009999, 0.011605, 0.016227],
    ("basel", "flattener"): [0.043966, 0.015426, 0.010908],
    ("basel", "short_up"): [0.052470, 0.027717, 0.023798],
    ("pra", "short_down"): [-0.009999, -0.007717, -0.003798],
}


@pytest.mark.parametrize(("key", "expected"), list(WORKED_RATES.items()), ids=str)
def test_worked_example_rates(key, expected):
    regime, scenario = key
    rules = {"basel": BASEL, "pra": PRA}[regime]
    times = np.array([0.0028, 3.5, 4.5])
    rates = shocked_rates(rules, "GBP", scenario, times, np.full(3, 0.01))
    assert rates == pytest.approx(expected, abs=1e-6)
