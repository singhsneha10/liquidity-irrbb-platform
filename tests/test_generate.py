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


def test_residual_days_count_down_or_roll_over(positions):
    ordered = positions.sort_values(["position_id", "date"])
    change = ordered.groupby("position_id")["residual_days"].diff().dropna()
    assert ((change == -1) | (change >= 0)).all()


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
