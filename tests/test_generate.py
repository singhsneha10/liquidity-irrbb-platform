from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from riskplatform.config import GeneratorConfig
from riskplatform.ingestion.generate import generate, main, write_outputs
from riskplatform.schemas import positions_schema, rates_schema

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "generator.yaml"


@pytest.fixture(scope="module")
def generated(cfg):
    return generate(cfg)


@pytest.fixture(scope="module")
def positions(generated):
    return generated[0]


@pytest.fixture(scope="module")
def rates(generated):
    return generated[1]


def test_output_passes_both_contracts(cfg, positions, rates):
    positions_schema(cfg).validate(positions)
    rates_schema(cfg).validate(rates)


def test_one_row_per_position_per_day(cfg, positions):
    assert positions["date"].nunique() == cfg.n_days
    assert len(positions) == cfg.n_days * positions["position_id"].nunique()


def test_dates_are_consecutive_business_days(cfg, positions):
    dates = pd.DatetimeIndex(sorted(positions["date"].unique()))
    assert (dates.day_of_week < 5).all()
    assert dates[0] >= pd.Timestamp(cfg.start_date)
    assert len(dates) == cfg.n_days


def test_same_positions_every_day(positions):
    ids_per_day = positions.groupby("date")["position_id"].apply(frozenset)
    assert ids_per_day.nunique() == 1


def test_every_entity_currency_category_is_present(cfg, positions):
    combos = positions[["entity", "currency", "category"]].drop_duplicates()
    assert len(combos) == len(cfg.entities) * len(cfg.currencies) * len(cfg.categories)


def test_same_config_gives_identical_data(cfg, positions, rates):
    again_positions, again_rates = generate(cfg)
    pd.testing.assert_frame_equal(positions, again_positions)
    pd.testing.assert_frame_equal(rates, again_rates)


def test_different_seed_gives_different_data(cfg, positions):
    other = GeneratorConfig.model_validate({**cfg.model_dump(), "seed": cfg.seed + 1})
    other_positions, _ = generate(other)
    assert not positions["balance_mn"].equals(other_positions["balance_mn"])


def test_balances_move_gradually(positions):
    ordered = positions.sort_values(["position_id", "date"])
    daily_change = ordered.groupby("position_id")["balance_mn"].pct_change().abs()
    assert daily_change.max() < 0.05


@pytest.mark.parametrize("column", ["residual_days", "reprice_days"])
def test_day_counts_fall_by_calendar_days_or_reset(positions, column):
    # Each row, the count falls by the calendar days since the previous row (3 over a weekend),
    # unless the date it counted down to fell within that gap, in which case it resets.
    ordered = positions.sort_values(["position_id", "date"])
    by_position = ordered.groupby("position_id")
    gap = by_position["date"].diff().dt.days
    previous = by_position[column].shift()
    change = ordered[column] - previous
    counted_down = change == -gap
    reset = previous <= gap
    assert (counted_down | reset)[gap.notna()].all()
    assert (gap == 3).any()  # the data must include a weekend, or this test proves nothing


def test_rates_follow_their_currency_curve(cfg, positions):
    # Every currency has the same product mix, so a lower curve must mean lower rates in every
    # category: order the currencies by their curves, and check each category keeps that order.
    by_curve = sorted(cfg.currencies, key=lambda c: cfg.rates.curves[c].short_rate)
    means = positions.groupby(["category", "currency"])["interest_rate"].mean().unstack()
    assert (means[by_curve].diff(axis=1).iloc[:, 1:] >= 0).all().all()
    assert (means[by_curve[-1]] > means[by_curve[0]]).all()


def test_deposits_pay_less_than_loans(positions):
    means = positions.groupby(["currency", "category"])["interest_rate"].mean().unstack()
    assert (means["retail_stable_deposits"] < means["corporate_loans"]).all()


def test_floating_rates_reset_during_the_window(positions):
    ordered = positions.sort_values(["position_id", "date"])
    changed = ordered.groupby("position_id")["interest_rate"].diff().fillna(0) != 0
    assert ordered.loc[changed, "rate_type"].eq("floating").any()


def test_rates_respect_the_floor(cfg, positions):
    assert (positions["interest_rate"] >= cfg.interest_rate_floor).all()


def _equity_ratio(positions):
    totals = positions.pivot_table(
        index=["date", "entity", "currency"], columns="side", values="balance_mn", aggfunc="sum"
    )
    return (totals["asset"] - totals["liability"]) / totals["asset"]


def test_balance_sheet_balances_on_first_day(cfg, positions):
    first_day = positions[positions["date"] == positions["date"].min()]
    assert np.allclose(_equity_ratio(first_day), cfg.equity_ratio)


def test_equity_stays_positive_as_balances_drift(positions):
    assert _equity_ratio(positions).between(0.0, 0.15).all()


def test_row_count_of_rates(cfg, rates):
    expected = cfg.n_days * len(cfg.currencies) * len(cfg.rates.tenors_years)
    assert len(rates) == expected


def test_curves_move_in_parallel_and_slope_upward(cfg, rates):
    gbp = rates[rates["currency"] == "GBP"]
    curve = gbp.pivot(index="date", columns="tenor_years", values="rate")
    slope = curve.iloc[:, -1] - curve.iloc[:, 0]
    assert slope.iloc[0] > 0
    assert np.allclose(slope, slope.iloc[0])


def test_write_outputs_round_trips(cfg, positions, rates, tmp_path):
    write_outputs(positions, rates, tmp_path)
    files = sorted((tmp_path / "positions").glob("date=*.parquet"))
    assert len(files) == cfg.n_days
    back_positions = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    pd.testing.assert_frame_equal(back_positions, positions, check_dtype=False)
    back_rates = pd.read_parquet(tmp_path / "rates.parquet")
    pd.testing.assert_frame_equal(back_rates, rates, check_dtype=False)


def test_rerun_removes_stale_files(cfg, positions, rates, tmp_path):
    write_outputs(positions, rates, tmp_path)
    stale = tmp_path / "positions" / "date=1999-01-01.parquet"
    stale.write_bytes(b"old")
    write_outputs(positions, rates, tmp_path)
    assert not stale.exists()
    assert len(list((tmp_path / "positions").glob("date=*.parquet"))) == cfg.n_days


def test_command_line_entry_point(cfg, tmp_path):
    main(["--config", str(CONFIG_PATH), "--out", str(tmp_path)])
    assert (tmp_path / "rates.parquet").exists()
    assert len(list((tmp_path / "positions").glob("date=*.parquet"))) == cfg.n_days
