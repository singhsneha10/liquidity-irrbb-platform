"""The hand-worked IRRBB example in docs/irrbb_worked_example.md: the code must reproduce it.

Expected values were worked out before any IRRBB code existed and rechecked with plain
arithmetic (which also reproduces SRP31.92). Each test was strict xfail until its calculation
was built (steps 5.3-5.6); every one reproduced these answers and is now enforced.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from riskplatform.config import IrrbbRules, load_irrbb_rules

RULES_DIR = Path(__file__).resolve().parents[1] / "config" / "irrbb"
RULES = {regime: load_irrbb_rules(RULES_DIR / f"{regime}.yaml") for regime in ("basel", "pra")}

# Flat 1% base curve, and the book's repricing cash flows (+ asset, - liability).
TENORS = np.array([0.0028, 30.0])
BASE_RATES = np.array([0.01, 0.01])
FLOWS = pd.DataFrame({"time_years": [4.5, 3.5, 0.0028], "amount": [1000.0, -560.0, -240.0]})

EXPECTED_EVE = {
    "basel": {
        "parallel_up": -61.639071,
        "parallel_down": 71.284659,
        "steepener": -23.405629,
        "flattener": 6.297434,
        "short_up": -25.012091,
        "short_down": 26.618959,
    },
    "pra": {
        "parallel_up": -61.639071,
        "parallel_down": 43.812659,
        "steepener": -23.400527,
        "flattener": 6.297434,
        "short_up": -25.012091,
        "short_down": 26.634061,
    },
}
EXPECTED_NII = {
    "basel": {"parallel_up": -6.581520, "parallel_down": 6.581520},
    "pra": {"parallel_up": -6.581520, "parallel_down": 4.786225},
}


def test_shapes_reproduce_the_basel_example_srp31_92():
    from riskplatform.irrbb.shocks import shocked_rates

    raw = RULES["basel"].model_dump()
    raw["shocks"]["sizes_bp"]["GBP"] = {"parallel": 100, "short": 100, "long": 100}  # as SRP31.92
    rules = IrrbbRules.model_validate(raw)
    tenor, zero = np.array([3.5]), np.array([0.0])
    steepener = shocked_rates(rules, "GBP", "steepener", tenor, zero)[0]
    flattener = shocked_rates(rules, "GBP", "flattener", tenor, zero)[0]
    assert round(steepener * 1e4, 1) == 25.4  # basis points
    assert round(flattener * 1e4, 1) == -1.6


@pytest.mark.parametrize("regime", ["basel", "pra"])
def test_delta_eve_matches_the_hand_calculation(regime):
    from riskplatform.irrbb.eve import delta_eve

    result = delta_eve(FLOWS, TENORS, BASE_RATES, RULES[regime], "GBP")
    assert result == pytest.approx(EXPECTED_EVE[regime], abs=1e-6)


@pytest.mark.parametrize("regime", ["basel", "pra"])
def test_worst_loss_and_outlier_test(regime):
    from riskplatform.irrbb.eve import eve_risk

    loss, scenario = eve_risk({"GBP": EXPECTED_EVE[regime]}, RULES[regime])
    assert (loss, scenario) == (pytest.approx(61.639071, abs=1e-6), "parallel_up")
    assert loss > RULES[regime].outlier_threshold.value * 200  # equity 200: an outlier


@pytest.mark.parametrize("regime", ["basel", "pra"])
def test_delta_nii_matches_the_hand_calculation(regime):
    from riskplatform.irrbb.nii import delta_nii

    result = delta_nii(FLOWS, TENORS, BASE_RATES, RULES[regime], "GBP")
    assert result == pytest.approx(EXPECTED_NII[regime], abs=1e-6)


def test_positions_give_the_bucketed_cash_flows():
    from riskplatform.irrbb.repricing import repricing_flows

    positions = pd.DataFrame(
        [
            ("corporate_loans", "asset", 1000.0, 1700, 1700, "fixed"),
            ("retail_stable_deposits", "liability", 800.0, 1, 1, "floating"),
        ],
        columns=["category", "side", "balance_mn", "residual_days", "reprice_days", "rate_type"],
    )
    flows = repricing_flows(positions, RULES["basel"])
    by_time = flows.groupby("time_years")["amount"].sum().round(6).to_dict()
    assert by_time == {0.0028: -240.0, 3.5: -560.0, 4.5: 1000.0}
