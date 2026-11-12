"""The one-command pipeline: stage order, stopping, exit codes, and outputs end to end."""

import dataclasses
import json
from pathlib import Path

import pytest

from riskplatform import pipeline
from riskplatform.controls import CONTROL_FILE
from riskplatform.pipeline import PipelineResult, StageResult, load_inputs, main, run_pipeline
from riskplatform.validation.check_raw import CheckResult

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
ALL_STAGES = ["generate", "raw_checks", "quality", "metrics", "irrbb"]


@pytest.fixture(scope="module")
def inputs():
    """The real configs, with the bank shortened to 2 days so whole runs stay fast."""
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    return dataclasses.replace(loaded, generator=small)


def _stage(name, status):
    return StageResult(name=name, status=status, detail="", seconds=0.0)


@pytest.mark.parametrize(
    ("statuses", "expected", "code"),
    [
        (["ok", "ok"], "ok", 0),
        (["ok", "findings"], "findings", 2),
        (["ok", "stopped"], "stopped", 1),
        (["findings", "stopped"], "stopped", 1),  # a stop outranks findings
    ],
)
def test_status_and_exit_code(statuses, expected, code):
    result = PipelineResult([_stage(f"s{i}", s) for i, s in enumerate(statuses)])
    assert (result.status, result.exit_code) == (expected, code)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_runs_every_stage_and_links_the_outputs(inputs, tmp_path):
    result = run_pipeline(inputs, tmp_path)
    assert [stage.name for stage in result.stages] == ALL_STAGES
    assert result.status in ("ok", "findings")

    run_id = _json(tmp_path / "validated" / "_run.json")["run_id"]
    metrics = _json(tmp_path / "metrics" / run_id / "summary.json")
    irrbb = _json(tmp_path / "irrbb" / run_id / "summary.json")
    assert metrics["source_run_id"] == irrbb["source_run_id"] == run_id
    # The pipeline's verdict is exactly what the stages found.
    found = bool(metrics["breaches"] or irrbb["outliers"])
    assert result.status == ("findings" if found else "ok")


def test_failed_raw_check_stops_before_quality(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "run_checks", lambda raw_dir, cfg: [CheckResult("broken", 3)])
    result = run_pipeline(inputs, tmp_path)
    assert [stage.name for stage in result.stages] == ["generate", "raw_checks"]
    assert result.exit_code == 1 and "broken" in result.stages[-1].detail
    assert not (tmp_path / "quality_runs").exists()


def test_stopped_quality_run_publishes_nothing_and_skips_the_metrics(inputs, tmp_path, monkeypatch):
    def write_without_control_totals(raw, out_dir):
        pipeline_write(raw, out_dir)
        (Path(out_dir) / CONTROL_FILE).unlink()  # the source forgot to send its totals

    pipeline_write = pipeline.write_outputs
    monkeypatch.setattr(pipeline, "write_outputs", write_without_control_totals)
    result = run_pipeline(inputs, tmp_path)

    assert [stage.name for stage in result.stages] == ["generate", "raw_checks", "quality"]
    assert result.exit_code == 1 and "control totals" in result.stages[-1].detail
    assert not (tmp_path / "validated").exists()
    assert not (tmp_path / "metrics").exists() and not (tmp_path / "irrbb").exists()


def test_bad_config_fails_before_any_data_is_written(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(f"base: {(CONFIG / 'generator.yaml').as_posix()}\nn_days: 0\n")
    with pytest.raises(ValueError, match="n_days"):
        main(["--config", str(bad), "--data", str(tmp_path / "data")])
    assert not (tmp_path / "data").exists()  # not even a log file


def test_error_is_logged_with_its_traceback_then_raised(tmp_path, monkeypatch):
    """An unexpected error still leaves an audit trail, and exits non-zero (Python: 1)."""

    def broken(inputs, data_dir):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pipeline, "run_pipeline", broken)
    args = ["--config", str(CONFIG / "generator.yaml"), "--quality", str(CONFIG / "quality.yaml")]
    args += ["--liquidity", str(CONFIG / "liquidity"), "--irrbb", str(CONFIG / "irrbb")]
    with pytest.raises(RuntimeError, match="disk full"):
        main([*args, "--data", str(tmp_path / "data")])

    (log,) = (tmp_path / "data" / "logs").glob("pipeline-*.log")
    text = log.read_text(encoding="utf-8")
    assert "Pipeline STOPPED by an error (exit code 1)" in text
    assert "Traceback" in text and "RuntimeError: disk full" in text


def test_command_exit_code_matches_the_result(tmp_path):
    config = tmp_path / "small.yaml"
    config.write_text(f"base: {(CONFIG / 'generator.yaml').as_posix()}\nn_days: 2\n")
    args = ["--config", str(config), "--quality", str(CONFIG / "quality.yaml")]
    args += ["--liquidity", str(CONFIG / "liquidity"), "--irrbb", str(CONFIG / "irrbb")]
    code = main([*args, "--data", str(tmp_path / "data")])

    run_id = _json(tmp_path / "data" / "validated" / "_run.json")["run_id"]
    irrbb = _json(tmp_path / "data" / "irrbb" / run_id / "summary.json")
    metrics = _json(tmp_path / "data" / "metrics" / run_id / "summary.json")
    assert code == (2 if metrics["breaches"] or irrbb["outliers"] else 0)

    (log,) = (tmp_path / "data" / "logs").glob("pipeline-*.log")  # one log file per run
    text = log.read_text(encoding="utf-8")
    for stage in ALL_STAGES:
        assert f"Stage {stage}: " in text
    assert f"(exit code {code})" in text
