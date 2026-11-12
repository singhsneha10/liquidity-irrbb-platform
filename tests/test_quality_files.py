"""File-level checks for a quality run: STOP checks stop it, WARNING checks are only reported."""

import pandas as pd
import pytest

from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.quality.files import (
    STOP_CHECKS,
    WARNING_CHECKS,
    FileChecksFailed,
    check_files,
)
from riskplatform.quality.rows import FileLevelError
from riskplatform.validation.check_raw import run_checks, run_checks_on_tables

BOND = "BANK_UK_GBP_govt_bonds_001"  # long-dated, so it never matures inside the window


@pytest.fixture(scope="module")
def generated(cfg):
    return generate(cfg)


def _with_filenames(positions: pd.DataFrame) -> pd.DataFrame:
    """Add the source-file column the checks need, as the quality run will."""
    names = "positions/date=" + positions["date"].dt.strftime("%Y-%m-%d") + ".parquet"
    return positions.assign(filename=names)


@pytest.fixture
def tables(generated):
    """(positions with filenames, rates, fx): fresh copies for each test."""
    return _with_filenames(generated.positions), generated.rates.copy(), generated.fx.copy()


def _nth_date(frame: pd.DataFrame, n: int) -> pd.Timestamp:
    return sorted(frame["date"].unique())[n]


def _change(frame, column, value, **match):
    mask = pd.Series(True, index=frame.index)
    for key, wanted in match.items():
        mask &= frame[key] == wanted
    assert mask.any()
    frame.loc[mask, column] = value(frame.loc[mask, column]) if callable(value) else value


def test_checks_on_tables_match_checks_on_files(cfg, generated, tmp_path):
    write_outputs(generated, tmp_path)
    on_files = run_checks(tmp_path, cfg)
    on_tables = run_checks_on_tables(
        _with_filenames(generated.positions), generated.rates, generated.fx, cfg
    )
    assert on_tables == on_files


def test_clean_data_passes_every_stop_and_warning_check(cfg, tables):
    report = check_files(*tables, cfg)
    assert [r.name for r in report.stops] == list(STOP_CHECKS)
    assert [r.name for r in report.warnings] == list(WARNING_CHECKS)
    assert all(r.passed for r in report.stops + report.warnings)


# Each fault takes (positions, rates, fx) and returns the damaged (positions, rates, fx).


def _wrong_file_name(positions, rates, fx):
    _change(
        positions, "filename", "positions/date=2030-01-01.parquet", date=_nth_date(positions, 3)
    )
    return positions, rates, fx


def _missing_day(positions, rates, fx):
    day = _nth_date(positions, 3)
    return tuple(frame[frame["date"] != day] for frame in (positions, rates, fx))


def _rates_missing_a_day(positions, rates, fx):
    return positions, rates[rates["date"] != _nth_date(rates, 3)], fx


def _fx_missing_a_rate(positions, rates, fx):
    gone = (fx["currency"] == "USD") & (fx["date"] == _nth_date(fx, 3))
    return positions, rates, fx[~gone]


STOP_FAULTS = {
    "files_hold_their_own_date": _wrong_file_name,
    "day_count_matches_config": _missing_day,
    "positions_and_rates_share_dates": _rates_missing_a_day,
    "fx_covers_every_date": _fx_missing_a_rate,
}


def test_every_stop_check_has_a_planted_fault():
    assert set(STOP_FAULTS) == set(STOP_CHECKS)


@pytest.mark.parametrize("check", list(STOP_FAULTS))
def test_stop_check_failure_stops_the_run(cfg, tables, check):
    with pytest.raises(FileChecksFailed, match=check) as stopped:
        check_files(*STOP_FAULTS[check](*tables), cfg)
    assert isinstance(stopped.value, FileLevelError)  # the run catches every stop in one place


WARNING_FAULTS = {
    "static_attributes_unchanged": ("category", "covered_bonds"),
    "day_counts_fall_by_calendar_days": ("residual_days", lambda days: days + 1),
    "rate_changes_only_at_reset": ("interest_rate", lambda rate: rate + 0.01),
}


@pytest.mark.parametrize("check", list(WARNING_FAULTS))
def test_warning_check_failure_is_reported_but_does_not_stop(cfg, tables, check):
    positions, rates, fx = tables
    column, value = WARNING_FAULTS[check]
    _change(positions, column, value, position_id=BOND, date=_nth_date(positions, 5))
    report = check_files(positions, rates, fx, cfg)
    failed = [r.name for r in report.warnings if not r.passed]
    assert failed == [check]
    assert all(r.passed for r in report.stops)


def test_quarantined_row_neither_stops_nor_warns(cfg, tables):
    positions, rates, fx = tables
    gone = (positions["position_id"] == BOND) & (positions["date"] == _nth_date(positions, 5))
    report = check_files(positions[~gone], rates, fx, cfg)
    assert all(r.passed for r in report.stops + report.warnings)


def test_stopped_run_still_carries_the_warnings(cfg, tables):
    positions, rates, fx = tables
    _change(positions, "category", "covered_bonds", position_id=BOND, date=_nth_date(positions, 5))
    with pytest.raises(FileChecksFailed) as stopped:
        check_files(*_missing_day(positions, rates, fx), cfg)
    warnings = {r.name: r.passed for r in stopped.value.report.warnings}
    assert warnings["static_attributes_unchanged"] is False


def test_unknown_check_name_is_an_error(cfg, tables):
    with pytest.raises(ValueError, match="no_such_check"):
        run_checks_on_tables(*tables, cfg, only=["no_such_check"])
