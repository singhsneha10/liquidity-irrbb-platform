"""The error-injection demo catches every planted error, can report a miss, and is safe to run."""

from pathlib import Path

import pytest

from riskplatform.config import GeneratorConfig, load_quality_config
from riskplatform.quality.demo import INJECTIONS, main, run_demo

ROOT = Path(__file__).resolve().parents[1]
QUALITY_PATH = ROOT / "config" / "quality.yaml"


@pytest.fixture(scope="module")
def small_cfg(cfg) -> GeneratorConfig:
    return GeneratorConfig.model_validate({**cfg.model_dump(), "n_days": 5})


@pytest.fixture(scope="module")
def quality_cfg():
    return load_quality_config(QUALITY_PATH)


def test_every_planted_error_is_caught_as_expected(small_cfg, quality_cfg, tmp_path):
    result = run_demo(small_cfg, quality_cfg, tmp_path / "demo")
    card = result.scorecard
    assert card["planted"].tolist() == [i.name for i in INJECTIONS]
    assert card["caught"].all(), card[~card["caught"]].to_string()
    assert result.bad_data_run.status == "published"  # bad rows are quarantined, not fatal
    assert result.lost_data_run.status == "stopped"  # lost data is fatal
    assert result.passed


def test_scorecard_reports_a_miss_when_a_check_cannot_fire(small_cfg, quality_cfg, tmp_path):
    relaxed = quality_cfg.model_copy(
        update={"outliers": quality_cfg.outliers.model_copy(update={"balance_jump": 1.0})}
    )
    result = run_demo(small_cfg, relaxed, tmp_path / "demo")
    missed = result.scorecard.loc[~result.scorecard["caught"], "planted"].tolist()
    assert missed == ["balance up 50% overnight"]
    assert not result.passed


def test_refuses_to_clear_a_folder_it_did_not_create(small_cfg, quality_cfg, tmp_path):
    precious = tmp_path / "data"
    precious.mkdir()
    (precious / "keep.txt").write_text("real data", encoding="utf-8")
    with pytest.raises(ValueError, match="not a demo folder"):
        run_demo(small_cfg, quality_cfg, precious)
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "real data"


def test_reruns_reuse_its_own_folder(small_cfg, quality_cfg, tmp_path):
    out = tmp_path / "demo"
    run_demo(small_cfg, quality_cfg, out)
    assert run_demo(small_cfg, quality_cfg, out).passed


def test_command_passes_and_says_so(tmp_path, capsys):
    args = ["--quality", str(QUALITY_PATH), "--out", str(tmp_path / "demo"), "--days", "5"]
    args += ["--config", str(ROOT / "config" / "generator.yaml")]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert out.count("CAUGHT") == len(INJECTIONS)
    assert "DEMO PASSED" in out
