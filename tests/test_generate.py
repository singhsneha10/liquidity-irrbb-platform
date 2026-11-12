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


# --- Real-rates mode (curves read from a saved file) ------------------------------------------

REAL_TENORS = [0.25, 1.0, 10.0]
CURVE_DAYS = pd.bdate_range("2026-07-31", "2026-08-14")  # a Friday before start, then 2 weeks


def _real_rate(currency_index: int, day_index: int, tenor: float) -> float:
    """Simple known curves: higher per currency, per tenor and per day, so each is traceable."""
    return 0.02 + 0.01 * currency_index + 0.001 * tenor + 0.0001 * day_index


def _write_curves(cfg, path, drop=()):
    """Save curves for every currency and CURVE_DAYS, leaving out (currency, date) in drop."""
    rows = [
        {
            "date": day,
            "currency": currency,
            "tenor_years": tenor,
            "rate": _real_rate(c, d, tenor),
            "source": "boe",
        }
        for c, currency in enumerate(cfg.currencies)
        for d, day in enumerate(CURVE_DAYS)
        for tenor in REAL_TENORS
        if (currency, day) not in {(ccy, pd.Timestamp(when)) for ccy, when in drop}
    ]
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _real_cfg(cfg, curves_file, n_days=10):
    raw = cfg.model_dump()
    raw["n_days"] = n_days
    raw["rates"]["source"] = "real"
    raw["rates"]["real"] = {"curves_file": curves_file, "max_fill_days": 4}
    return GeneratorConfig.model_validate(raw)


def _rate_on(rates, currency, day, tenor):
    match = (
        (rates["currency"] == currency)
        & (rates["date"] == pd.Timestamp(day))
        & (rates["tenor_years"] == tenor)
    )
    return rates.loc[match, "rate"].item()


def test_real_curves_are_laid_on_the_tenor_grid(cfg, tmp_path):
    real = _real_cfg(cfg, _write_curves(cfg, tmp_path / "curves.parquet"))
    _, rates = generate(real)
    day = "2026-08-05"
    d = list(CURVE_DAYS).index(pd.Timestamp(day))
    for c, currency in enumerate(cfg.currencies):
        assert _rate_on(rates, currency, day, 0.5) == pytest.approx(_real_rate(c, d, 0.5))
        assert _rate_on(rates, currency, day, 0.0833) == pytest.approx(_real_rate(c, d, 0.25))
        assert _rate_on(rates, currency, day, 30.0) == pytest.approx(_real_rate(c, d, 10.0))


def test_real_curve_missing_on_a_holiday_uses_the_previous_day(cfg, tmp_path):
    path = _write_curves(cfg, tmp_path / "curves.parquet", drop=[("GBP", "2026-08-10")])
    _, rates = generate(_real_cfg(cfg, path))
    assert _rate_on(rates, "GBP", "2026-08-10", 1.0) == _rate_on(rates, "GBP", "2026-08-07", 1.0)
    assert _rate_on(rates, "USD", "2026-08-10", 1.0) != _rate_on(rates, "USD", "2026-08-07", 1.0)


def test_real_curve_gap_longer_than_allowed_is_an_error(cfg, tmp_path):
    week = [("USD", day) for day in pd.bdate_range("2026-08-04", "2026-08-10")]
    path = _write_curves(cfg, tmp_path / "curves.parquet", drop=week)
    with pytest.raises(ValueError, match="USD.*2026-08-10"):
        generate(_real_cfg(cfg, path))


def test_real_curves_missing_a_currency_is_an_error(cfg, tmp_path):
    no_eur = [("EUR", day) for day in CURVE_DAYS]
    path = _write_curves(cfg, tmp_path / "curves.parquet", drop=no_eur)
    with pytest.raises(ValueError, match="EUR"):
        generate(_real_cfg(cfg, path))


def test_real_mode_without_saved_file_explains_how_to_get_it(cfg, tmp_path):
    with pytest.raises(FileNotFoundError, match="market_rates"):
        generate(_real_cfg(cfg, tmp_path / "missing.parquet"))
