"""The dashboard: its numbers equal the pipeline's files, and the page runs (Streamlit AppTest).

The data functions are checked against the run's own Parquet and JSON files, so the dashboard
can never show a number the pipeline did not produce. The page is run headless by Streamlit's
AppTest on the same run: no browser, no server.
"""

import dataclasses
import json
import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from riskplatform.config import load_storage_config
from riskplatform.dashboard import data
from riskplatform.dashboard.app import DATA_ENV, GROUP_COLOUR, SLOTS, entity_colours
from riskplatform.pipeline import load_inputs, run_pipeline

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
APP = ROOT / "src" / "riskplatform" / "dashboard" / "app.py"
LOCAL = load_storage_config(CONFIG / "storage.yaml", env={})


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory) -> Path:
    """One whole pipeline run (2-day bank), the run the dashboard shows."""
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    folder = tmp_path_factory.mktemp("dashboard")
    run_pipeline(dataclasses.replace(loaded, generator=small), folder)
    return folder


@pytest.fixture(scope="module")
def board(data_dir) -> data.Dashboard:
    return data.open_dashboard("local", data_dir, data_dir / "cache", LOCAL)


def _file(board: data.Dashboard, stage: str, name: str) -> pd.DataFrame:
    return pd.read_parquet(board.results_dir / stage / board.run_id / f"{name}.parquet")


def _latest(table: pd.DataFrame, regime: str) -> pd.DataFrame:
    rows = table[(table["regime"] == regime) & table["level"].isin(["entity", "group"])]
    return rows[rows["date"] == rows["date"].max()]


# --- The numbers --------------------------------------------------------------------------------


@pytest.mark.parametrize("regime", ["basel", "pra"])
def test_limit_status_is_the_runs_latest_limits_with_their_movement(board, regime):
    shown = data.limit_status(board.con, regime)
    expected = _latest(_file(board, "limits", "limits"), regime)
    assert len(shown) == len(expected) == 12  # 3 limits x (3 entities + the group)
    assert sorted(shown["utilisation"]) == pytest.approx(sorted(expected["utilisation"]))
    assert (shown["who"] == data.GROUP).sum() == 3
    assert shown["movement"].notna().all()  # every limit has a movement (or "unchanged")
    counts = data.status_counts(board.con, regime)
    assert counts == {s: int((expected["status"] == s).sum()) for s in ("ok", "warning", "breach")}


def test_ratio_trends_are_the_runs_lcr_and_nsfr(board):
    for metric in ("lcr", "nsfr"):
        trend = data.ratio_trend(board.con, metric, "basel")
        table = _file(board, "metrics", metric)
        expected = table[(table["regime"] == "basel") & table["level"].isin(["entity", "group"])]
        assert len(trend) == len(expected) == 2 * 4  # 2 days x (3 entities + the group)
        assert sorted(trend["value"]) == pytest.approx(sorted(expected[metric]))
        assert set(trend["minimum"]) == {1.0}  # the regulatory 100%
    assert data.limit_value(board.con, "lcr") == 1.10  # config/limits.yaml


def test_irrbb_views_are_the_runs_results(board):
    risk = data.eve_against_tier1(board.con, "basel")
    expected = _latest(_file(board, "irrbb", "risk"), "basel")
    assert sorted(risk["share"]) == pytest.approx(sorted(expected["eve_risk"] / expected["tier1"]))
    assert dict(zip(risk["who"], risk["outlier"], strict=True))["BANK_UK"]  # the known outlier
    assert list(risk["test"]) == pytest.approx([0.15] * len(risk))  # SRP31.82: 15% of Tier 1
    eve = data.eve_by_scenario(board.con, "basel")
    assert len(eve) == 4 * 6 and eve["eve_loss"].notna().all()  # the group has a loss too
    nii = data.nii_by_scenario(board.con, "pra")
    assert set(nii["scenario"]) == {"parallel_up", "parallel_down"}


def test_headline_and_changes_come_from_the_changes_stage(board):
    headline = data.group_headline(board.con, "basel").set_index("limit_name")
    assert list(headline.index) == ["eve_tier1", "lcr", "nsfr"]
    metric_changes = _file(board, "changes", "metric_changes")
    group = metric_changes[
        (metric_changes["level"] == "group") & (metric_changes["regime"] == "basel")
    ]
    for _, row in group.iterrows():
        assert headline.loc[row["limit_name"], "change"] == pytest.approx(row["change"])
    previous, latest = data.compared_dates(board.con)
    assert previous < latest == data.latest_date(board.con)
    drivers = data.top_drivers(board.con, 3)
    assert len(drivers) == 3
    assert list(drivers["change_gbp"].abs()) == sorted(drivers["change_gbp"].abs(), reverse=True)


def test_quality_views_match_the_breaks_report(board):
    report = data.breaks_report(board)
    assert report["run_id"] == board.run_id
    quarantined = data.quarantine_by_check(board.con)
    assert int(quarantined["rows"].sum()) == report["rows"]["quarantine_records"]
    checks = data.file_checks(report)
    stops, warnings = report["file_checks"]["stops"], report["file_checks"]["warnings"]
    assert len(checks) == len(stops) + len(warnings)
    assert checks["stops_the_run"].sum() == len(stops)
    assert data.thresholds(board) == {"warning": 0.9, "breach": 1.0}  # config/limits.yaml


def test_entities_keep_their_colour_and_the_group_is_neutral():
    scale = entity_colours(["GROUP", "BANK_US", "BANK_EU", "BANK_UK"]).to_dict()
    assert scale["domain"] == ["BANK_EU", "BANK_UK", "BANK_US", "GROUP"]
    assert scale["range"] == [*SLOTS[:3], GROUP_COLOUR]
    fewer = entity_colours(["BANK_EU", "BANK_UK", "GROUP"]).to_dict()  # a filtered view
    assert fewer["range"][:2] == SLOTS[:2]  # the survivors are not repainted


# --- The page -----------------------------------------------------------------------------------


@pytest.fixture
def page(data_dir, monkeypatch):
    monkeypatch.setenv(DATA_ENV, str(data_dir))
    monkeypatch.setenv("RISKPLATFORM_STORAGE_BACKEND", "local")
    return AppTest.from_file(str(APP), default_timeout=120).run()


def _charts(page: AppTest) -> list:
    return page.get("vega_lite_chart")


def test_the_page_shows_every_tab_and_chart_without_errors(page, board):
    assert not page.exception
    assert [tab.label for tab in page.tabs] == [
        "Limits", "Liquidity", "IRRBB", "Since the day before", "Data quality",
    ]  # fmt: skip
    assert len(_charts(page)) == 9  # 3 limit bars, 2 trends, the outlier test, 2 scenarios, drivers
    tiles = {metric.label: metric.value for metric in page.metric}
    counts = data.status_counts(board.con, "basel")
    assert tiles["Limits in breach"] == str(counts["breach"])
    assert tiles["Limits at warning"] == str(counts["warning"])
    lcr = data.group_headline(board.con, "basel").set_index("limit_name").loc["lcr", "actual"]
    assert tiles["Group LCR"] == f"{lcr:.1%}"
    assert board.run_id in " ".join(caption.value for caption in page.sidebar.caption)


def test_switching_rulebook_and_entity_reruns_cleanly(page):
    page.sidebar.radio(key="regime").set_value("pra").run()
    assert not page.exception
    assert page.caption[0].value.startswith("UK PRA rules")
    page.selectbox(key="irrbb_who").set_value("BANK_UK").run()
    assert not page.exception and len(_charts(page)) == 9


def test_without_a_published_run_the_page_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(DATA_ENV, str(tmp_path))
    monkeypatch.setenv("RISKPLATFORM_STORAGE_BACKEND", "local")
    page = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not page.exception
    assert "No published run to show" in page.error[0].value
    assert not page.tabs


def test_the_command_starts_streamlit_on_the_app(monkeypatch):
    from riskplatform.dashboard import __main__ as launcher

    seen = {}
    monkeypatch.setattr("streamlit.web.cli.main", lambda: seen.setdefault("argv", sys.argv) and 0)
    monkeypatch.setattr(sys, "argv", ["riskplatform-dashboard", "--server.port", "8502"])
    launcher.main()
    assert seen["argv"] == ["streamlit", "run", str(APP.resolve()), "--server.port", "8502"]
    assert json.dumps(seen)  # plain strings only


def test_early_warning_view_is_the_runs_indicators(board):
    rows = pd.read_parquet(board.results_dir / "early_warning" / board.run_id /
                           "early_warning.parquet")  # fmt: skip
    for regime in ("basel", "pra"):
        shown = data.early_warning(board.con, regime)
        expected = rows[rows["regime"].isna() | (rows["regime"] == regime)]
        assert len(shown) == len(expected)
        if (expected["status"] == "alert").any():
            assert shown["status"].iloc[0] == "alert"  # alerts listed first
    assert len(data.early_warning_alerts(board.con)) == int((rows["status"] == "alert").sum())
