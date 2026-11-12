"""The whole quality run: publish on good data, quarantine bad rows, stop on file-level problems."""

import json
from pathlib import Path

import pandas as pd
import pytest

from riskplatform.config import GeneratorConfig, load_quality_config
from riskplatform.controls import CONTROL_FILE, write_control_totals
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.quality.report import render_markdown, table_of
from riskplatform.quality.run import MANIFEST, _rename, main, run_quality

ROOT = Path(__file__).resolve().parents[1]
QUALITY_PATH = ROOT / "config" / "quality.yaml"
FIRST_DAY = "positions/date=2026-08-03.parquet"
SECOND_DAY = "positions/date=2026-08-04.parquet"


@pytest.fixture(scope="module")
def small_cfg(cfg) -> GeneratorConfig:
    """The same bank over 5 days, so whole runs stay fast."""
    return GeneratorConfig.model_validate({**cfg.model_dump(), "n_days": 5})


@pytest.fixture(scope="module")
def quality_cfg():
    return load_quality_config(QUALITY_PATH)


@pytest.fixture(scope="module")
def generated(small_cfg):
    return generate(small_cfg)


@pytest.fixture
def raw_dir(generated, tmp_path):
    raw = tmp_path / "raw"
    write_outputs(generated, raw)
    return raw


def _run(raw_dir, small_cfg, quality_cfg):
    return run_quality(raw_dir, raw_dir.parent / "out", small_cfg, quality_cfg)


def _edit(raw_dir, name, edit):
    path = raw_dir / name
    edit(pd.read_parquet(path)).to_parquet(path, index=False)


def _resend_control_totals(raw_dir):
    """Recalculate the control totals from the files as they are now: the source sent this."""
    files = {
        path.relative_to(raw_dir).as_posix(): (
            table_of(path.relative_to(raw_dir).as_posix()),
            pd.read_parquet(path),
        )
        for path in raw_dir.rglob("*.parquet")
    }
    write_control_totals(raw_dir, files)


def _manifest(raw_dir):
    return json.loads((raw_dir.parent / "out" / "validated" / MANIFEST).read_text(encoding="utf-8"))


def _markdown(result) -> str:
    return (result.run_dir / "breaks_report.md").read_text(encoding="utf-8")


def test_clean_run_publishes_everything(raw_dir, small_cfg, quality_cfg, generated):
    result = _run(raw_dir, small_cfg, quality_cfg)
    assert result.status == "published"
    validated = raw_dir.parent / "out" / "validated"
    assert len(list((validated / "positions").glob("*.parquet"))) == small_cfg.n_days
    assert _manifest(raw_dir)["run_id"] == result.run_id
    rows = result.report["rows"]
    assert rows["validated"] == {
        "positions": len(generated.positions),
        "rates": len(generated.rates),
        "fx": len(generated.fx),
    }
    assert (validated / "fx.parquet").exists()
    assert rows["quarantined"] == 0
    assert pd.read_parquet(result.run_dir / "quarantine.parquet").empty
    assert "**Status: PUBLISHED**" in _markdown(result)


def _negative_balance_and_unknown_currency(frame):
    frame = frame.copy()
    frame.loc[3, "balance_mn"] = -5.0
    frame.loc[4, "currency"] = "JPY"
    return frame


def test_bad_rows_from_the_source_are_quarantined_and_the_run_publishes(
    raw_dir, small_cfg, quality_cfg, generated
):
    _edit(raw_dir, FIRST_DAY, _negative_balance_and_unknown_currency)
    _resend_control_totals(raw_dir)
    result = _run(raw_dir, small_cfg, quality_cfg)

    assert result.status == "published"
    report = result.report
    assert report["rows"]["quarantined"] == 2
    assert report["rows"]["validated"]["positions"] == len(generated.positions) - 2
    checks = {(c["check"], c["column"]) for c in report["quarantine_by_check"]}
    assert checks == {
        ("greater_than(0)", "balance_mn"),
        ("isin(['GBP', 'USD', 'EUR'])", "currency"),
    }
    assert report["reconciliation"]["balances_with_quarantine"]  # the amounts are explained
    quarantine = pd.read_parquet(result.run_dir / "quarantine.parquet")
    assert sorted(quarantine["row_number"]) == [3, 4]
    assert "greater_than(0)" in _markdown(result)


def test_row_lost_after_sending_stops_and_keeps_previous_data(raw_dir, small_cfg, quality_cfg):
    first = _run(raw_dir, small_cfg, quality_cfg)
    _edit(raw_dir, FIRST_DAY, lambda frame: frame.iloc[1:])  # control totals not updated
    second = _run(raw_dir, small_cfg, quality_cfg)

    assert second.status == "stopped"
    assert _manifest(raw_dir)["run_id"] == first.run_id  # previous good data still published
    assert any("rows sent" in reason for reason in second.report["stop_reasons"])
    assert second.report["reconciliation"] is not None  # kept for the report
    assert second.report["file_checks"] is None  # never reached
    markdown = _markdown(second)
    assert "**Status: STOPPED**" in markdown and "Nothing was published" in markdown
    assert "Not run (the run stopped earlier)." in markdown


def test_day_not_sent_stops_on_the_missing_day_check(raw_dir, small_cfg, quality_cfg):
    (raw_dir / SECOND_DAY).unlink()
    _resend_control_totals(raw_dir)  # the source itself sent one day fewer
    result = _run(raw_dir, small_cfg, quality_cfg)
    assert result.status == "stopped"
    stops = {c["name"]: c["passed"] for c in result.report["file_checks"]["stops"]}
    assert stops["day_count_matches_config"] is False
    assert not (raw_dir.parent / "out" / "validated").exists()


def test_unreadable_file_stops_the_run(raw_dir, small_cfg, quality_cfg):
    (raw_dir / FIRST_DAY).write_bytes(b"not parquet")
    result = _run(raw_dir, small_cfg, quality_cfg)
    assert result.status == "stopped"
    assert any("cannot be read" in reason for reason in result.report["stop_reasons"])


def test_missing_control_totals_stop_the_run(raw_dir, small_cfg, quality_cfg):
    (raw_dir / CONTROL_FILE).unlink()
    result = _run(raw_dir, small_cfg, quality_cfg)
    assert result.status == "stopped"
    assert any("control totals" in reason for reason in result.report["stop_reasons"])


def test_outlier_is_reported_and_the_run_still_publishes(raw_dir, small_cfg, quality_cfg):
    def jump(frame):
        frame = frame.copy()
        frame.loc[frame["position_id"] == "BANK_UK_GBP_corporate_loans_001", "balance_mn"] *= 1.5
        return frame

    _edit(raw_dir, SECOND_DAY, jump)
    _resend_control_totals(raw_dir)
    result = _run(raw_dir, small_cfg, quality_cfg)
    assert result.status == "published"
    assert result.report["outliers"]["by_check"]["balance_jump"] >= 1
    assert "balance_jump" in _markdown(result)


def test_command_exit_codes(raw_dir, small_cfg, tmp_path):
    config = tmp_path / "small.yaml"
    config.write_text(f"base: {(ROOT / 'config' / 'generator.yaml').as_posix()}\nn_days: 5\n")
    args = ["--config", str(config), "--quality", str(QUALITY_PATH)]
    args += ["--raw", str(raw_dir), "--out", str(tmp_path / "out")]
    assert main(args) == 0
    (raw_dir / CONTROL_FILE).unlink()
    assert main(args) == 1


def test_bad_fx_rate_is_quarantined_and_the_missing_rate_stops_the_run(
    raw_dir, small_cfg, quality_cfg
):
    def negative_usd_rate(frame):
        frame = frame.copy()
        frame.loc[frame.index[frame["currency"] == "USD"][1], "to_reporting"] = -0.74
        return frame

    _edit(raw_dir, "fx.parquet", negative_usd_rate)
    _resend_control_totals(raw_dir)  # the source sent this rate
    result = _run(raw_dir, small_cfg, quality_cfg)

    checks = {(c["table"], c["check"]) for c in result.report["quarantine_by_check"]}
    assert ("fx", "greater_than(0)") in checks  # the bad row is quarantined ...
    assert result.status == "stopped"  # ... which leaves USD without a rate that day
    stops = {c["name"]: c["passed"] for c in result.report["file_checks"]["stops"]}
    assert stops["fx_covers_every_date"] is False


def test_empty_day_file_stops_the_run(raw_dir, small_cfg, quality_cfg):
    _edit(raw_dir, SECOND_DAY, lambda frame: frame.iloc[0:0])  # header only
    _resend_control_totals(raw_dir)  # the source itself sent an empty day
    result = _run(raw_dir, small_cfg, quality_cfg)
    assert result.status == "stopped"
    stops = {c["name"]: c["passed"] for c in result.report["file_checks"]["stops"]}
    assert stops["day_count_matches_config"] is False


def test_currency_without_a_curve_stops_the_run(raw_dir, small_cfg, quality_cfg):
    day = pd.Timestamp("2026-08-04")
    _edit(
        raw_dir,
        "rates.parquet",
        lambda frame: frame[~((frame["currency"] == "EUR") & (frame["date"] == day))],
    )
    _resend_control_totals(raw_dir)
    result = _run(raw_dir, small_cfg, quality_cfg)
    assert result.status == "stopped"  # before: published, then IRRBB crashed
    stops = {c["name"]: c["passed"] for c in result.report["file_checks"]["stops"]}
    assert stops == {**dict.fromkeys(stops, True), "curves_cover_every_currency": False}


def test_book_missing_on_a_day_warns_and_still_publishes(raw_dir, small_cfg, quality_cfg):
    _edit(raw_dir, SECOND_DAY, lambda frame: frame[frame["currency"] != "EUR"])
    _resend_control_totals(raw_dir)
    result = _run(raw_dir, small_cfg, quality_cfg)
    assert result.status == "published"
    warnings = {c["name"]: c["passed"] for c in result.report["file_checks"]["warnings"]}
    assert warnings["every_book_every_day"] is False
    assert "every_book_every_day" in _markdown(result)


def test_second_good_run_replaces_the_first_and_leaves_nothing_behind(
    raw_dir, small_cfg, quality_cfg
):
    """Every daily run after the first publishes over earlier data: the swap must be clean."""
    first = _run(raw_dir, small_cfg, quality_cfg)
    second = _run(raw_dir, small_cfg, quality_cfg)
    assert (first.status, second.status) == ("published", "published")
    assert _manifest(raw_dir)["run_id"] == second.run_id
    out = raw_dir.parent / "out"
    assert sorted(p.name for p in out.iterdir()) == ["quality_runs", "validated"]  # no staging
    assert len(list((out / "validated" / "positions").glob("*.parquet"))) == small_cfg.n_days


def test_rename_retries_while_a_file_is_briefly_locked(tmp_path, monkeypatch):
    source, target = tmp_path / "staging", tmp_path / "validated"
    source.mkdir()
    real_rename, calls = Path.rename, []

    def locked_twice(self, destination):
        calls.append(self)
        if len(calls) <= 2:
            raise PermissionError("locked by antivirus")
        return real_rename(self, destination)

    monkeypatch.setattr(Path, "rename", locked_twice)
    _rename(source, target, attempts=5, wait_seconds=0)
    assert target.exists() and not source.exists() and len(calls) == 3


def test_rename_gives_up_after_its_last_attempt(tmp_path, monkeypatch):
    def always_locked(self, destination):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "rename", always_locked)
    with pytest.raises(PermissionError):
        _rename(tmp_path / "a", tmp_path / "b", attempts=3, wait_seconds=0)


def test_markdown_row_limit_comes_from_config(raw_dir, small_cfg, quality_cfg):
    result = _run(raw_dir, small_cfg, quality_cfg)
    checks = result.report["file_checks"]
    total = len(checks["stops"]) + len(checks["warnings"])
    assert f"... and {total - 1} more" in render_markdown(result.report, max_rows=1)
    assert "more (see breaks_report.json)" not in render_markdown(result.report, max_rows=10_000)


def test_missing_fx_file_stops_the_run(raw_dir, small_cfg, quality_cfg):
    (raw_dir / "fx.parquet").unlink()
    result = _run(raw_dir, small_cfg, quality_cfg)
    assert result.status == "stopped"
    assert any("fx.parquet missing" in reason for reason in result.report["stop_reasons"])
