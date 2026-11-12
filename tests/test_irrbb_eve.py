"""Delta EVE, cross-currency aggregation (Basel vs UK) and the outlier test."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from riskplatform.config import load_irrbb_rules
from riskplatform.irrbb.eve import aggregated_losses, delta_eve, eve_risk, is_outlier

RULES_DIR = Path(__file__).resolve().parents[1] / "config" / "irrbb"
BASEL = load_irrbb_rules(RULES_DIR / "basel.yaml")
PRA = load_irrbb_rules(RULES_DIR / "pra.yaml")
SCENARIOS = BASEL.shocks.scenarios
TENORS, FLAT = np.array([0.0833, 30.0]), np.array([0.03, 0.03])


def _deltas(**changes) -> dict[str, float]:
    """A delta EVE per scenario: 0 except where given."""
    return {scenario: changes.get(scenario, 0.0) for scenario in SCENARIOS}


def _flows(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["time_years", "amount"]).astype(float)


def test_basel_counts_losses_only_and_the_uk_offsets_gains_at_half():
    by_currency = {"GBP": _deltas(parallel_up=-10.0), "EUR": _deltas(parallel_up=6.0)}
    assert aggregated_losses(by_currency, BASEL)["parallel_up"] == pytest.approx(10.0)
    assert aggregated_losses(by_currency, PRA)["parallel_up"] == pytest.approx(10.0 - 0.5 * 6.0)


def test_losses_add_up_in_full_in_both_regimes():
    by_currency = {"GBP": _deltas(steepener=-10.0), "USD": _deltas(steepener=-4.0)}
    for rules in (BASEL, PRA):
        assert eve_risk(by_currency, rules) == (pytest.approx(14.0), "steepener")


def test_worst_scenario_is_the_largest_aggregated_loss():
    by_currency = {"GBP": _deltas(parallel_up=-5.0, short_up=-8.0, parallel_down=3.0)}
    assert eve_risk(by_currency, BASEL) == (pytest.approx(8.0), "short_up")


def test_no_loss_in_any_scenario_means_zero_risk():
    gains = {"GBP": {scenario: 1.0 for scenario in SCENARIOS}}
    assert eve_risk(gains, BASEL) == (0.0, None)
    assert eve_risk(gains, PRA) == (0.0, None)


def test_no_cash_flows_means_no_change():
    result = delta_eve(_flows(), TENORS, FLAT, BASEL, "GBP")
    assert result == {scenario: 0.0 for scenario in SCENARIOS}


def test_a_single_asset_loses_when_rates_rise_and_gains_when_they_fall():
    result = delta_eve(_flows((5.0, 100.0)), TENORS, FLAT, BASEL, "GBP")
    assert result["parallel_up"] < 0 < result["parallel_down"]
    expected_up = 100 * (np.exp(-(0.03 + 0.0275) * 5) - np.exp(-0.03 * 5))
    assert result["parallel_up"] == pytest.approx(expected_up)


def test_cash_flow_beyond_the_curve_uses_the_flat_end_rate():
    curve = np.array([0.02, 0.04])
    result = delta_eve(_flows((40.0, 100.0)), TENORS, curve, BASEL, "GBP")
    expected = 100 * (np.exp(-(0.04 + 0.0275) * 40) - np.exp(-0.04 * 40))
    assert result["parallel_up"] == pytest.approx(expected)


def test_a_matched_book_has_no_eve_risk():
    matched = _flows((3.5, 100.0), (3.5, -100.0))
    for rules in (BASEL, PRA):
        result = delta_eve(matched, TENORS, FLAT, rules, "EUR")
        assert all(change == pytest.approx(0.0, abs=1e-12) for change in result.values())


def test_outlier_boundary():
    assert not is_outlier(15.0, 100.0, BASEL)  # exactly 15%: not an outlier
    assert is_outlier(15.01, 100.0, BASEL)
