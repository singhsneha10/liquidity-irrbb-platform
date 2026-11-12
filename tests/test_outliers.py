"""Outlier warnings: none on normal data; each planted outlier found with its details."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from riskplatform.config import OutlierThresholds, load_quality_config
from riskplatform.ingestion.generate import generate
from riskplatform.quality.outliers import find_outliers

QUALITY_PATH = Path(__file__).resolve().parents[1] / "config" / "quality.yaml"
LOAN = "BANK_UK_GBP_corporate_loans_001"
RESERVES = "BANK_UK_GBP_central_bank_reserves_001"


@pytest.fixture(scope="module")
def thresholds() -> OutlierThresholds:
    return load_quality_config(QUALITY_PATH).outliers


@pytest.fixture(scope="module")
def generated(cfg):
    return generate(cfg)


@pytest.fixture
def tables(generated):
    positions, rates = generated.positions, generated.rates
    return positions.copy(), rates.copy()


def _day(frame, n):
    return sorted(frame["date"].unique())[n]


def _rows(frame, **match):
    mask = frame.index == frame.index  # all True
    for column, value in match.items():
        mask &= frame[column] == value
    assert mask.any()
    return mask


def test_normal_data_has_no_outliers(tables, thresholds):
    assert find_outliers(*tables, thresholds).empty


def test_balance_jump_is_flagged_going_up_and_coming_back(tables, thresholds):
    positions, rates = tables
    day = _day(positions, 10)
    positions.loc[_rows(positions, position_id=LOAN, date=day), "balance_mn"] *= 1.5
    found = find_outliers(positions, rates, thresholds)
    jumps = found[found["check"] == "balance_jump"]
    assert jumps["subject"].tolist() == [LOAN, LOAN]
    assert jumps["date"].tolist() == [day, _day(positions, 11)]
    assert jumps["value"].iloc[0] == pytest.approx(0.5, abs=0.02)  # +50%, plus the normal drift


def test_rate_far_from_its_curve_is_flagged(tables, thresholds):
    positions, rates = tables
    day = _day(positions, 10)
    positions.loc[_rows(positions, position_id=LOAN, date=day), "interest_rate"] += 0.06
    found = find_outliers(positions, rates, thresholds)
    gaps = found[found["check"] == "rate_gap_to_curve"]
    assert (gaps["subject"].tolist(), gaps["date"].tolist()) == ([LOAN], [day])
    assert gaps["value"].iloc[0] > thresholds.rate_gap_to_curve


def test_concentrated_position_is_flagged(tables, thresholds):
    positions, rates = tables
    loan = _rows(positions, position_id=LOAN)
    positions.loc[loan, "balance_mn"] *= 50  # every day, so no balance jump within the window
    found = find_outliers(positions, rates, thresholds)
    concentrated = found[found["check"] == "concentration_share"]
    assert set(concentrated["subject"]) == {LOAN}
    assert len(concentrated) == positions["date"].nunique()


def test_excluded_categories_are_not_concentration_outliers(tables, thresholds):
    positions, rates = tables
    positions.loc[_rows(positions, position_id=RESERVES), "balance_mn"] *= 50
    found = find_outliers(positions, rates, thresholds)
    assert RESERVES not in set(found.loc[found["check"] == "concentration_share", "subject"])


def test_large_curve_move_is_flagged(tables, thresholds):
    positions, rates = tables
    day = _day(rates, 10)
    rates.loc[_rows(rates, currency="GBP", tenor_years=10.0, date=day), "rate"] += 0.006
    found = find_outliers(positions, rates, thresholds)
    moves = found[found["check"] == "curve_move"]
    assert moves["subject"].tolist() == ["GBP 10Y", "GBP 10Y"]  # the move and the move back
    assert moves["date"].tolist() == [day, _day(rates, 11)]


def test_thresholds_come_from_config(tables, thresholds):
    positions, rates = tables
    day = _day(positions, 10)
    positions.loc[_rows(positions, position_id=LOAN, date=day), "balance_mn"] *= 1.5
    relaxed = thresholds.model_copy(update={"balance_jump": 0.75})
    found = find_outliers(positions, rates, relaxed)
    assert "balance_jump" not in set(found["check"])


@pytest.mark.parametrize(("field", "value"), [("balance_jump", 0.0), ("concentration_share", 1.5)])
def test_config_rejects_impossible_thresholds(thresholds, field, value):
    raw = thresholds.model_dump()
    raw[field] = value
    with pytest.raises(ValidationError):
        OutlierThresholds.model_validate(raw)
