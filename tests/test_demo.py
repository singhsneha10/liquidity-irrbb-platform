"""The public demo (Phase 10): built with the real pipeline, and the page runs as Cloud runs it."""

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from riskplatform.dashboard import demo
from riskplatform.dashboard.demo import DEMO_ENV, DemoBuildError, build_demo
from riskplatform.pipeline import PipelineResult, StageResult

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build_demo(ROOT, tmp_path_factory.mktemp("demo"))


def test_the_demo_is_a_full_pipeline_run_of_the_default_bank(built):
    assert built.status == "findings"  # the synthetic bank's EVE breach, as documented
    run = built.data_dir
    for stage in ("metrics", "irrbb", "limits", "changes", "early_warning", "reports"):
        assert (run / stage / built.run_id).is_dir(), stage
    positions = sorted((run / "validated" / "positions").glob("date=*.parquet"))
    assert len(positions) == 60  # the configs' fixed default, not "up to today"
    assert positions[-1].name == "date=2026-10-23.parquet"


def test_a_stopped_demo_run_says_why(tmp_path, monkeypatch):
    stopped = PipelineResult([StageResult("raw_checks", "stopped", "failed: x", 0.0)])
    monkeypatch.setattr(demo, "run_pipeline", lambda inputs, data_dir: stopped)
    with pytest.raises(DemoBuildError, match="raw_checks: failed: x"):
        build_demo(ROOT, tmp_path)


@pytest.fixture
def clean_environment():
    """streamlit_app.py sets two variables in the real process environment: put them back, so
    the tests that run after this one see their own data, not the demo."""
    names = (DEMO_ENV, "RISKPLATFORM_STORAGE_BACKEND")
    saved = {name: os.environ.pop(name, None) for name in names}
    yield
    for name, value in saved.items():
        os.environ.pop(name, None)
        if value is not None:
            os.environ[name] = value


def test_the_cloud_entry_point_shows_the_demo(monkeypatch, clean_environment):
    """streamlit_app.py, as Community Cloud runs it: demo on, no data/, no S3."""
    assert DEMO_ENV not in os.environ  # the entry point must switch it on itself
    monkeypatch.setenv("RISKPLATFORM_DATA_DIR", str(ROOT / "no-such-folder"))
    page = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=300).run()
    assert not page.exception
    assert "Demo: a synthetic bank" in page.info[0].value
    assert "demo, built on this server" in " ".join(c.value for c in page.sidebar.caption)
    assert page.tabs[0].label == "Executive summary"
    assert page.metric[0].label == "Limits in breach"


def test_cloud_installs_this_project_from_requirements():
    """Community Cloud reads requirements.txt before pyproject.toml (which it would treat as
    Poetry); the file must install this project with the dashboard extra, and nothing else."""
    lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == [".[dashboard]"]
    assert not (ROOT / "uv.lock").exists()  # Cloud would read it first
