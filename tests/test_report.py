"""The daily PDF report: written by the pipeline, readable, the run's own numbers, reproducible."""

import dataclasses
import re
from pathlib import Path

import pandas as pd
import pytest
from pypdf import PdfReader

from riskplatform import report
from riskplatform.dashboard import data
from riskplatform.pipeline import load_inputs, run_pipeline
from riskplatform.query import connect
from riskplatform.report import REPORT_FILE, main, run_report, write_report

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One whole pipeline run (2-day bank): its report stage has written the PDF."""
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    data_dir = tmp_path_factory.mktemp("report")
    result = run_pipeline(dataclasses.replace(loaded, generator=small), data_dir)
    assert result.stages[-1].name == "report" and result.stages[-1].status == "ok"
    return data_dir, result.run_id


@pytest.fixture(scope="module")
def board(run) -> data.Dashboard:
    data_dir, run_id = run
    return data.Dashboard(connect(data_dir, run_id), run_id, data_dir, "local")


@pytest.fixture(scope="module")
def text(run) -> str:
    data_dir, run_id = run
    reader = PdfReader(data_dir / "reports" / run_id / REPORT_FILE)
    text = " ".join(page.extract_text() for page in reader.pages)
    return " ".join(text.split())  # pypdf puts each table cell on its own line: one space each


def test_the_pipeline_writes_a_short_readable_pdf(run):
    data_dir, run_id = run
    reader = PdfReader(data_dir / "reports" / run_id / REPORT_FILE)
    assert 1 <= len(reader.pages) <= 3  # a report to read, not a data dump
    assert reader.metadata.title.startswith("Liquidity and IRRBB daily report")


def test_it_names_the_run_the_dates_and_that_the_data_is_synthetic(board, text):
    previous, latest = data.compared_dates(board.con)
    assert f"daily report: {latest:%d %B %Y}" in text
    assert f"compared with {previous:%d %b %Y}" in text
    assert board.run_id in text
    assert "All positions are synthetic" in text


def test_the_headline_is_the_runs_limits_and_group_ratios(board, text):
    for regime, name in report.REGIMES.items():
        counts = data.status_counts(board.con, regime)
        assert re.search(rf"{name} {counts['breach']} {counts['warning']} ", text), name
        group = data.group_headline(board.con, regime).set_index("limit_name")
        assert report.pct(group.loc["lcr", "actual"]) in text


def test_every_limit_at_warning_or_breach_is_listed(board, text):
    flagged = 0
    for regime in report.REGIMES:
        table = data.limit_status(board.con, regime)
        for _, row in table[table["status"] != "ok"].iterrows():
            limit = report.LIMIT_NAMES[row["limit_name"]]
            assert f"{row['who']} {limit} {report.pct(row['actual'])}" in text
            flagged += 1
    assert flagged > 0  # precondition: the test bank has EVE limit alerts
    assert text.count("BREACH") + text.count("WARNING") == flagged


def test_irrbb_section_flags_the_outlier(board, text):
    risk = data.eve_against_tier1(board.con, "basel")
    outliers = risk[risk["outlier"]]
    assert len(outliers) >= 1
    assert text.count("OUTLIER") == sum(
        int(data.eve_against_tier1(board.con, r)["outlier"].sum()) for r in report.REGIMES
    )
    assert "UK ICAA 9.4A, SS31/15 2.7B" in text and "SRP31.82" in text


def test_drivers_and_data_quality_are_the_runs(board, text):
    for _, d in data.top_drivers(board.con, report.TOP_DRIVERS).iterrows():
        assert f"{d['category']} {d['side']} {report.mn(d['change_gbp'])}" in text
    rows = data.breaks_report(board)["rows"]
    assert f"{sum(rows['arrived'].values()):,} rows arrived" in text


def test_the_same_run_gives_the_same_bytes(board, tmp_path):
    first = write_report(board, tmp_path / "a").path.read_bytes()
    second = write_report(board, tmp_path / "b").path.read_bytes()
    assert first == second  # invariant mode: no timestamp or random id in the file


def test_a_one_day_run_says_there_is_nothing_to_compare(board, monkeypatch):
    monkeypatch.setattr(data, "compared_dates", lambda con: (None, pd.Timestamp("2026-08-03")))
    (paragraph,) = report.changes_section(board.con)
    assert "nothing to compare" in paragraph.text


def test_when_every_limit_is_ok_the_report_says_so(board, monkeypatch):
    real = data.limit_status
    monkeypatch.setattr(data, "limit_status", lambda con, r: real(con, r).assign(status="ok"))
    parts = report.limits_section(board.con, {"warning": 0.9, "breach": 1.0})
    assert "Every internal limit is ok" in parts[0].text


def test_run_report_and_the_command(run, tmp_path, capsys):
    data_dir, run_id = run
    assert run_report(data_dir, run_id).path == data_dir / "reports" / run_id / REPORT_FILE
    args = ["--source", "local", "--data", str(data_dir), "--out", str(tmp_path / "out")]
    assert main(args) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "out" / run_id / REPORT_FILE)
    empty = ["--source", "local", "--data", str(tmp_path / "none"), "--out", str(tmp_path / "x")]
    assert main(empty) == 1
    assert not (tmp_path / "x").exists()
