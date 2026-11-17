"""The planted deposit run (config/generator_stress.yaml): off by default, exact, funded, refused
when it cannot be paid, and validated. A test instrument for model validation test T2."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from riskplatform.config import load_generator_config
from riskplatform.ingestion.generate import generate

ROOT = Path(__file__).resolve().parents[1]
NORMAL = ROOT / "config" / "generator.yaml"
STRESS = ROOT / "config" / "generator_stress.yaml"
DEPOSITS = ["retail_stable_deposits", "retail_less_stable_deposits"]
PAYERS = ["central_bank_reserves", "govt_bonds"]
KEY = ["date", "entity", "currency", "category"]


@pytest.fixture(scope="module")
def runs():
    """The normal bank and the stressed bank (60 business days each)."""
    normal = generate(load_generator_config(NORMAL)).positions
    stressed = generate(load_generator_config(STRESS)).positions
    return normal, stressed


@pytest.fixture(scope="module")
def cfg():
    return load_generator_config(STRESS)


def _totals(positions: pd.DataFrame) -> pd.Series:
    return positions.groupby(KEY)["balance_mn"].sum()


def test_the_default_bank_has_no_episode():
    assert load_generator_config(NORMAL).stress is None


def test_days_before_the_start_are_identical(runs, cfg):
    normal, stressed = runs
    before = normal["date"] < pd.Timestamp(cfg.stress.start)
    assert before.any()
    pd.testing.assert_frame_equal(normal[before], stressed[before])


def test_only_the_stressed_books_change(runs, cfg):
    normal, stressed = runs
    change = (_totals(stressed) - _totals(normal)).abs()
    moved = change[change > 1e-9].reset_index()
    assert set(moved["entity"]) == {cfg.stress.entity}
    assert set(moved["currency"]) == set(cfg.stress.currencies)
    assert set(moved["category"]) <= set(DEPOSITS) | set(PAYERS)
    # Every other column is untouched: rates, maturities and repricing do not depend on balance.
    other = stressed.columns.drop("balance_mn")
    pd.testing.assert_frame_equal(normal[other], stressed[other])


def test_each_category_loses_exactly_its_share_over_the_episode(runs, cfg):
    normal, stressed = runs
    stress = cfg.stress
    ratio = _totals(stressed) / _totals(normal)
    book = ratio.xs((stress.entity, "GBP"), level=("entity", "currency"))
    dates = sorted(normal["date"].unique())
    start = dates.index(np.datetime64(stress.start))
    end = dates[start + stress.business_days - 1]
    for category, outflow in stress.outflows.items():
        path = book.xs(category, level="category")
        assert path.loc[dates[start - 1]] == pytest.approx(1.0)
        first_day = (1 - outflow) ** (1 / stress.business_days)  # the same fraction each day
        assert path.loc[dates[start]] == pytest.approx(first_day)
        assert path.loc[end] == pytest.approx(1 - outflow)
        assert path.loc[dates[-1]] == pytest.approx(1 - outflow)  # it does not come back


def test_the_withdrawals_are_paid_from_reserves_then_bonds(runs, cfg):
    """Assets fall by exactly what the deposits lost, every day, so the books still balance;
    reserves go first, down to their floor, before any bond is sold."""
    normal, stressed = runs
    change = (_totals(stressed) - _totals(normal)).xs((cfg.stress.entity, "GBP"),
                                                     level=("entity", "currency"))  # fmt: skip
    by_category = change.unstack("category").fillna(0.0)
    withdrawn = -by_category[DEPOSITS].sum(axis=1)
    paid = -by_category[PAYERS].sum(axis=1)
    assert np.allclose(paid, withdrawn)
    reserves_normal = _totals(normal).xs(
        (cfg.stress.entity, "GBP", "central_bank_reserves"),
        level=("entity", "currency", "category"),
    )
    bonds_sold = -by_category["govt_bonds"]
    reserves_left = reserves_normal + by_category["central_bank_reserves"]
    floor = reserves_normal * cfg.stress.keep_at_least
    selling = bonds_sold > 1e-9
    assert selling.any(), "precondition: this episode is larger than the reserves"
    assert np.allclose(reserves_left[selling], floor[selling])  # reserves at their floor first
    assert (stressed["balance_mn"] > 0).all()  # the data contract still holds


def test_a_run_too_large_to_pay_is_refused(cfg):
    big = cfg.model_validate(
        {**cfg.model_dump(), "stress": {**cfg.stress.model_dump(), "currencies": None}}
    )  # the euro book holds too few liquid assets for this run
    with pytest.raises(ValueError, match="stress episode too large: BANK_UK EUR cannot pay"):
        generate(big)


def test_an_as_of_date_before_the_start_gives_the_normal_bank(cfg):
    early = date(2026, 9, 4)
    normal = generate(load_generator_config(NORMAL, as_of=early)).positions
    stressed = generate(load_generator_config(STRESS, as_of=early)).positions
    pd.testing.assert_frame_equal(normal, stressed)
    assert load_generator_config(STRESS, as_of=early).stress == cfg.stress  # kept by through()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"start": "2026-09-13"}, "not a business day"),
        ({"start": "2026-07-01"}, "before start_date"),
        ({"entity": "BANK_XX"}, "stress entity BANK_XX"),
        ({"currencies": ["JPY"]}, "stress currencies"),
        ({"outflows": {"retail_stable_deposits": 1.0}}, "between 0 and 1"),
        ({"outflows": {"retail_stable_deposits": 0.0}}, "between 0 and 1"),
        ({"outflows": {"retail_mortgages": 0.1}}, "must be a liability category"),
        ({"paid_from": ["retail_stable_deposits"]}, "must be an asset category"),
        ({"keep_at_least": 0.0}, "keep_at_least"),
        ({"business_days": 0}, "business_days"),
    ],
)
def test_a_bad_episode_is_refused(cfg, change, message):
    raw = {**cfg.model_dump(), "stress": {**cfg.stress.model_dump(), **change}}
    with pytest.raises(ValidationError, match=message):
        cfg.model_validate(raw)
