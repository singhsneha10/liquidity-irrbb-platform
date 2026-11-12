"""Delta NII: horizon cut-off, time remaining, signs, horizon from config, UK floor effect."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from riskplatform.config import IrrbbRules, load_irrbb_rules
from riskplatform.irrbb.nii import delta_nii

RULES_DIR = Path(__file__).resolve().parents[1] / "config" / "irrbb"
BASEL = load_irrbb_rules(RULES_DIR / "basel.yaml")
PRA = load_irrbb_rules(RULES_DIR / "pra.yaml")
TENORS, FLAT = np.array([0.0028, 30.0]), np.array([0.03, 0.03])


def _flows(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["time_years", "amount"]).astype(float)


def _nii(flows, rules=BASEL, rates=FLAT):
    return delta_nii(flows, TENORS, rates, rules, "GBP")


def test_only_cash_flows_inside_the_horizon_count():
    assert _nii(_flows((1.0, 100.0), (4.5, 100.0))) == {"parallel_up": 0.0, "parallel_down": 0.0}
    assert _nii(_flows((0.99, 100.0)))["parallel_up"] > 0


def test_repricing_halfway_through_counts_for_half_a_year():
    assert _nii(_flows((0.5, 100.0)))["parallel_up"] == pytest.approx(100 * 0.0275 * 0.5)


def test_asset_sensitive_gains_liability_sensitive_loses_matched_is_flat():
    assets = _nii(_flows((0.25, 100.0)))
    liabilities = _nii(_flows((0.25, -100.0)))
    matched = _nii(_flows((0.25, 100.0), (0.25, -100.0)))
    assert assets["parallel_up"] > 0 > liabilities["parallel_up"]
    assert assets["parallel_down"] < 0 < liabilities["parallel_down"]
    assert matched == pytest.approx({"parallel_up": 0.0, "parallel_down": 0.0})


def test_horizon_comes_from_config():
    raw = BASEL.model_dump()
    raw["nii"]["horizon_months"] = 24
    two_years = IrrbbRules.model_validate(raw)
    result = _nii(_flows((1.5, 100.0)), rules=two_years)
    assert result["parallel_up"] == pytest.approx(100 * 0.0275 * 0.5)


def test_uk_floor_limits_the_parallel_down_benefit_on_short_liabilities():
    deposits = _flows((0.0028, -100.0))
    low = np.array([0.01, 0.01])  # 1%: parallel down would take rates to -1.75%
    basel = _nii(deposits, BASEL, low)["parallel_down"]
    uk = _nii(deposits, PRA, low)["parallel_down"]
    assert basel > uk > 0  # the UK floor (-1% at the short end) cuts the saving
