"""The DuckDB checks pass on clean generated files and catch each kind of planted fault."""

import shutil
from pathlib import Path

import pandas as pd
import pytest

from riskplatform.config import GeneratorConfig, load_generator_config
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.validation.check_raw import CHECKS, SYNTHETIC_ONLY, main, run_checks

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "generator.yaml"
SHORT_DAYS = 12  # faults are planted up to the 11th day; the last day must come after that


@pytest.fixture(scope="module")
def cfg(cfg) -> GeneratorConfig:
    """The same bank over 12 days (overrides conftest's 60-day cfg here): every test writes
    and checks fresh files, so the full 60 days made this module the slowest in the suite.
    test_all_checks_pass_on_the_full_bank still checks all 60 days once."""
    return GeneratorConfig.model_validate({**cfg.model_dump(), "n_days": SHORT_DAYS})


def _short_config_file(tmp_path: Path, *extra_lines: str) -> Path:
    """A config file for the command-line tests: the short bank, plus any extra lines."""
    path = tmp_path / "short.yaml"
    lines = [f"base: {CONFIG_PATH.as_posix()}", f"n_days: {SHORT_DAYS}", *extra_lines]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def generated(cfg):
    return generate(cfg)


@pytest.fixture
def raw_dir(generated, tmp_path):
    """A fresh, clean copy of the raw files for each test, so faults never leak between tests."""
    write_outputs(generated, tmp_path)
    return tmp_path


def _day_files(raw_dir: Path) -> list[Path]:
    return sorted((raw_dir / "positions").glob("date=*.parquet"))


def _edit(file: Path, edit) -> None:
    """Read a Parquet file, pass it through edit(frame) -> frame, and write the result back."""
    edit(pd.read_parquet(file)).to_parquet(file, index=False)


def _row(frame: pd.DataFrame, **match) -> pd.Series:
    """Boolean mask selecting rows whose columns equal the given values."""
    mask = pd.Series(True, index=frame.index)
    for column, value in match.items():
        mask &= frame[column] == value
    assert mask.any(), f"no row matches {match}"
    return mask


def _bump(column: str, amount: float, **match):
    def edit(frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.copy()
        frame.loc[_row(frame, **match), column] += amount
        return frame

    return edit


def _set(column: str, value, **match):
    def edit(frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.copy()
        frame.loc[_row(frame, **match), column] = value
        return frame

    return edit


BOND = "BANK_UK_GBP_govt_bonds_001"  # long-dated, so it never matures inside the test window


def _wrong_file_name(raw_dir):
    shutil.copy(_day_files(raw_dir)[0], raw_dir / "positions" / "date=2030-01-01.parquet")


def _missing_day(raw_dir):
    _day_files(raw_dir)[-1].unlink()


def _rates_missing_a_day(raw_dir):
    _edit(raw_dir / "rates.parquet", lambda frame: frame[frame["date"] != frame["date"].max()])


def _missing_row(raw_dir):
    _edit(_day_files(raw_dir)[1], lambda frame: frame.drop(index=0))


def _category_changes_mid_history(raw_dir):
    _edit(_day_files(raw_dir)[5], _set("category", "covered_bonds", position_id=BOND))


def _rate_changes_without_reset(raw_dir):
    _edit(_day_files(raw_dir)[5], _bump("interest_rate", 0.01, position_id=BOND))


def _unbalanced_first_day(raw_dir):
    _edit(_day_files(raw_dir)[0], _bump("balance_mn", 50.0, position_id=BOND))


def _countdown_skips(raw_dir):
    _edit(_day_files(raw_dir)[10], _bump("residual_days", 1, position_id=BOND))


def _one_tenor_moves_alone(raw_dir, day_index):
    rates = pd.read_parquet(raw_dir / "rates.parquet")
    day = sorted(rates["date"].unique())[day_index]
    bump = _bump("rate", 0.001, date=day, currency="GBP", tenor_years=10.0)
    _edit(raw_dir / "rates.parquet", bump)


def _fx_missing_a_rate(raw_dir):
    fx = pd.read_parquet(raw_dir / "fx.parquet")
    day = sorted(fx["date"].unique())[3]
    _edit(
        raw_dir / "fx.parquet",
        lambda frame: frame[~((frame["currency"] == "USD") & (frame["date"] == day))],
    )


def _curve_missing_for_a_currency(raw_dir):
    rates = pd.read_parquet(raw_dir / "rates.parquet")
    day = sorted(rates["date"].unique())[3]
    _edit(
        raw_dir / "rates.parquet",
        lambda frame: frame[~((frame["currency"] == "EUR") & (frame["date"] == day))],
    )


def _book_missing_on_a_day(raw_dir):
    _edit(
        _day_files(raw_dir)[4],
        lambda frame: frame[~((frame["entity"] == "BANK_EU") & (frame["currency"] == "EUR"))],
    )


FAULTS = {
    "files_hold_their_own_date": _wrong_file_name,
    "curves_cover_every_currency": _curve_missing_for_a_currency,
    "every_book_every_day": _book_missing_on_a_day,
    "fx_covers_every_date": _fx_missing_a_rate,
    "day_count_matches_config": _missing_day,
    "positions_and_rates_share_dates": _rates_missing_a_day,
    "every_position_every_day": _missing_row,
    "static_attributes_unchanged": _category_changes_mid_history,
    "rate_changes_only_at_reset": _rate_changes_without_reset,
    "balance_sheet_balances_day_one": _unbalanced_first_day,
    "day_counts_fall_by_calendar_days": _countdown_skips,
    "curves_move_in_parallel": lambda raw_dir: _one_tenor_moves_alone(raw_dir, day_index=10),
    "curve_shape_matches_config": lambda raw_dir: _one_tenor_moves_alone(raw_dir, day_index=0),
}


def test_every_check_has_a_planted_fault():
    assert set(FAULTS) == set(CHECKS)


def test_all_checks_pass_on_clean_files(cfg, raw_dir):
    failed = [r.name for r in run_checks(raw_dir, cfg) if not r.passed]
    assert failed == []


def test_all_checks_pass_on_the_full_bank(tmp_path):
    """The generator as configured (all 60 days) meets every check."""
    full = load_generator_config(CONFIG_PATH)
    write_outputs(generate(full), tmp_path)
    assert [r.name for r in run_checks(tmp_path, full) if not r.passed] == []


@pytest.mark.parametrize("check", list(FAULTS))
def test_check_catches_its_fault(cfg, raw_dir, check):
    FAULTS[check](raw_dir)
    results = {r.name: r for r in run_checks(raw_dir, cfg)}
    assert not results[check].passed


def test_command_line_exit_codes(raw_dir, tmp_path_factory, caplog):
    config = _short_config_file(tmp_path_factory.mktemp("config"))
    args = ["--config", str(config), "--raw", str(raw_dir)]
    assert main(args) == 0
    _missing_row(raw_dir)
    assert main(args) == 1
    failures = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any(m.startswith("FAIL  every_position_every_day") for m in failures)


# --- Real-rates mode: synthetic-only curve checks are skipped and reported -------------------


def _real_mode(cfg) -> GeneratorConfig:
    raw = cfg.model_dump()
    raw["rates"]["source"] = "real"
    raw["rates"]["real"] = {
        "curves_file": "unused.parquet",
        "fx_file": "unused.parquet",
        "max_fill_days": 4,
    }
    return GeneratorConfig.model_validate(raw)


@pytest.mark.parametrize("check", sorted(set(FAULTS) - SYNTHETIC_ONLY))
def test_real_mode_skips_only_synthetic_checks_and_still_catches_faults(cfg, raw_dir, check):
    real = _real_mode(cfg)
    clean = run_checks(raw_dir, real)
    assert {r.name for r in clean if r.skipped} == SYNTHETIC_ONLY
    assert all(r.passed for r in clean)
    FAULTS[check](raw_dir)
    assert not {r.name: r for r in run_checks(raw_dir, real)}[check].passed


def test_command_line_reports_skipped_checks(raw_dir, tmp_path_factory, caplog):
    real_config = _short_config_file(
        tmp_path_factory.mktemp("config"),
        "rates:",
        "  source: real",
        "  real: {curves_file: unused.parquet, fx_file: unused.parquet, max_fill_days: 4}",
    )
    assert main(["--config", str(real_config), "--raw", str(raw_dir)]) == 0
    out = caplog.text
    assert "SKIP  curves_move_in_parallel" in out
    assert f"{len(CHECKS) - 2} passed, 0 failed, 2 skipped" in out


def test_as_of_sets_the_expected_number_of_days(raw_dir, caplog):
    """The 12-day bank runs Mon 3 Aug to Tue 18 Aug: that as-of date matches it, one day later
    expects 13 days and the day-count check fails."""
    args = ["--config", str(CONFIG_PATH), "--raw", str(raw_dir)]
    assert main([*args, "--as-of", "2026-08-18"]) == 0
    assert main([*args, "--as-of", "2026-08-19"]) == 1
    failures = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any(m.startswith("FAIL  day_count_matches_config") for m in failures)
