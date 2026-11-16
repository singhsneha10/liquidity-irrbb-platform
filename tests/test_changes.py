"""What changed since yesterday: limit movements, metric changes and the volume / FX split."""

import dataclasses
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from riskplatform.changes import (
    DRIVER_COLUMNS,
    LIMIT_CHANGE_COLUMNS,
    METRIC_CHANGE_COLUMNS,
    MOVEMENTS,
    ChangesInputError,
    balance_drivers,
    describe_movement,
    limit_changes,
    metric_changes,
    movement,
    run_changes,
)
from riskplatform.config import ReportingConfig, load_reporting_config
from riskplatform.notify import ALERT_TOPIC_ENV, compose_limit_email, notify_run
from riskplatform.pipeline import load_inputs, run_pipeline

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
DAY1, DAY2 = pd.Timestamp("2026-08-03"), pd.Timestamp("2026-08-04")
REPORTING = ReportingConfig.model_validate({"changes": {"top_drivers": 3}})


# --- Movement labels ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("ok", "warning", "new"),
        ("ok", "breach", "new"),
        (None, "warning", "new"),  # first measured today: counts as previously ok
        ("warning", "breach", "worse"),
        ("warning", "ok", "resolved"),
        ("breach", "ok", "resolved"),
        ("breach", "warning", "better"),
        ("ok", "ok", "unchanged"),
        ("warning", "warning", "unchanged"),
        ("breach", "breach", "unchanged"),
        (None, "ok", "unchanged"),
    ],
)
def test_movement_labels(before, after, expected):
    assert movement(pd.Series([before]), pd.Series([after]))[0] == expected


# --- Limit and metric changes ---------------------------------------------------------------------


def _limit_row(date, entity, status, utilisation, actual, limit="eve_tier1", regime="basel"):
    return {
        "date": date,
        "level": "group" if entity is None else "entity",
        "entity": entity,  # None for the group, as in the limits table
        "currency": "GBP",
        "regime": regime,
        "limit_name": limit,
        "kind": "cap",
        "actual": actual,
        "limit_value": 0.15,
        "utilisation": utilisation,
        "status": status,
    }


@pytest.fixture
def limits() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _limit_row(DAY1, "BANK_UK", "warning", 0.95, 0.1425),
            _limit_row(DAY2, "BANK_UK", "breach", 1.05, 0.1575),  # worse
            _limit_row(DAY1, "BANK_US", "ok", 0.50, 0.075),
            _limit_row(DAY2, "BANK_US", "ok", 0.55, 0.0825),  # unchanged
            _limit_row(DAY1, None, "breach", 1.10, 0.165),
            _limit_row(DAY2, None, "ok", 0.80, 0.12),  # the group: resolved
            _limit_row(DAY2, "BANK_EU", "warning", 0.91, 0.1365),  # first measured today: new
        ]
    )


def test_limit_changes_label_each_limit_and_put_movements_first(limits):
    table = limit_changes(limits)
    assert list(table.columns) == LIMIT_CHANGE_COLUMNS
    by_entity = table.set_index(table["entity"].fillna("GROUP"))
    assert by_entity.loc["BANK_UK", "movement"] == "worse"
    assert by_entity.loc["BANK_US", "movement"] == "unchanged"
    assert by_entity.loc["GROUP", "movement"] == "resolved"  # the group (no entity) still joins
    assert by_entity.loc["GROUP", "previous_status"] == "breach"
    assert by_entity.loc["BANK_EU", "movement"] == "new"
    assert pd.isna(by_entity.loc["BANK_EU", "previous_status"])
    assert by_entity.loc["BANK_UK", "utilisation_change"] == pytest.approx(0.10)
    order = [MOVEMENTS.index(m) for m in table["movement"]]
    assert order == sorted(order)  # new, worse, resolved, better, unchanged
    assert (table["previous_date"] == DAY1).all() and (table["latest_date"] == DAY2).all()


def test_metric_changes_compare_the_measured_values(limits):
    table = metric_changes(limits)
    assert list(table.columns) == METRIC_CHANGE_COLUMNS
    uk = table[table["entity"] == "BANK_UK"].iloc[0]
    assert (uk["previous"], uk["latest"]) == (0.1425, 0.1575)
    assert uk["change"] == pytest.approx(0.015)


def test_only_one_day_gives_empty_tables_with_the_columns(limits):
    one_day = limits[limits["date"] == DAY2]
    assert limit_changes(one_day).empty
    assert list(limit_changes(one_day).columns) == LIMIT_CHANGE_COLUMNS
    assert list(metric_changes(one_day).columns) == METRIC_CHANGE_COLUMNS


def test_describe_movement_names_the_group_and_a_limit_not_measured_before():
    item = {
        "movement": "new",
        "latest_status": "warning",
        "entity": None,
        "regime": "pra",
        "limit_name": "lcr",
        "latest_utilisation": 0.93,
        "previous_utilisation": None,
        "previous_status": None,
    }
    assert describe_movement(item) == (
        "NEW WARNING GROUP (pra) lcr: 93% used (was inf, not measured)"
    )


# --- Balance drivers: volume and FX ---------------------------------------------------------------


def _position(date, currency, balance, side="asset", category="corporate_loans"):
    return {
        "date": date,
        "entity": "BANK_EU",
        "currency": currency,
        "category": category,
        "side": side,
        "balance_mn": balance,
    }


def _fx(eur1: float, eur2: float) -> pd.DataFrame:
    rows = [(DAY1, "GBP", 1.0), (DAY2, "GBP", 1.0), (DAY1, "EUR", eur1), (DAY2, "EUR", eur2)]
    return pd.DataFrame(rows, columns=["date", "currency", "to_reporting"])


def test_the_change_splits_exactly_into_volume_and_fx():
    """EUR 100mn at 0.80 becomes EUR 110mn at 0.90: 80 -> 99 GBP mn, a change of 19.
    Volume: 10 more EUR at yesterday's 0.80 = 8. FX: today's 110 EUR x 0.10 more = 11."""
    positions = pd.DataFrame([_position(DAY1, "EUR", 100.0), _position(DAY2, "EUR", 110.0)])
    (row,) = balance_drivers(positions, _fx(0.80, 0.90)).to_dict(orient="records")
    assert row["previous_gbp"] == pytest.approx(80.0)
    assert row["latest_gbp"] == pytest.approx(99.0)
    assert row["volume_gbp"] == pytest.approx(8.0)
    assert row["fx_gbp"] == pytest.approx(11.0)
    assert row["change_gbp"] == pytest.approx(19.0)


def test_currencies_add_up_new_positions_count_from_zero_and_swaps_are_left_out():
    positions = pd.DataFrame(
        [
            _position(DAY1, "EUR", 100.0),
            _position(DAY2, "EUR", 100.0),  # no volume change, only FX
            _position(DAY2, "GBP", 5.0),  # new today: volume from zero
            _position(DAY1, "GBP", 500.0, side="off_balance_sheet", category="irs_pay_fixed"),
            _position(DAY2, "GBP", 900.0, side="off_balance_sheet", category="irs_pay_fixed"),
        ]
    )
    table = balance_drivers(positions, _fx(0.80, 0.85))
    assert list(table.columns) == DRIVER_COLUMNS
    assert set(table["side"]) == {"asset"}  # swap notionals are not balances
    (row,) = table.to_dict(orient="records")
    assert row["volume_gbp"] == pytest.approx(5.0)
    assert row["fx_gbp"] == pytest.approx(5.0)  # 100 EUR x 0.05
    assert row["latest_gbp"] - row["previous_gbp"] == pytest.approx(row["change_gbp"])


def test_drivers_are_ranked_by_the_size_of_their_change():
    positions = pd.DataFrame(
        [
            _position(DAY1, "GBP", 100.0, category="govt_bonds"),
            _position(DAY2, "GBP", 101.0, category="govt_bonds"),  # +1
            _position(DAY1, "GBP", 50.0, side="liability", category="retail_stable_deposits"),
            _position(DAY2, "GBP", 30.0, side="liability", category="retail_stable_deposits"),
        ]
    )
    table = balance_drivers(positions, _fx(0.8, 0.8))
    assert list(table["change_gbp"]) == [-20.0, 1.0]


# --- run_changes on a whole pipeline run ----------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline_run(tmp_path_factory):
    """The real configs with a 2-day bank, run once for the tests below."""
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    data_dir = tmp_path_factory.mktemp("changes")
    result = run_pipeline(dataclasses.replace(loaded, generator=small), data_dir)
    assert result.stages[-1].name == "changes" and result.stages[-1].status == "ok"
    return data_dir, result.run_id


def test_the_pipeline_writes_the_changes_of_its_run(pipeline_run):
    data_dir, run_id = pipeline_run
    out = data_dir / "changes" / run_id
    names = {"limit_changes", "metric_changes", "balance_drivers"}
    assert {p.stem for p in out.glob("*.parquet")} == names
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))  # strict JSON
    assert summary["source_run_id"] == run_id
    assert summary["previous_date"] < summary["latest_date"]
    limits = pd.read_parquet(data_dir / "limits" / run_id / "limits.parquet")
    per_day = len(limits[limits["date"] == limits["date"].max()])
    assert sum(summary["movements"].values()) == per_day  # every limit, once
    moved = pd.read_parquet(out / "limit_changes.parquet")
    assert len(summary["limit_movements"]) == int((moved["movement"] != "unchanged").sum())


def test_on_real_data_the_split_adds_up_and_matches_the_validated_balances(pipeline_run):
    data_dir, run_id = pipeline_run
    drivers = pd.read_parquet(data_dir / "changes" / run_id / "balance_drivers.parquet")
    total = drivers["latest_gbp"] - drivers["previous_gbp"]
    assert np.allclose(drivers["volume_gbp"] + drivers["fx_gbp"], drivers["change_gbp"])
    assert np.allclose(total, drivers["change_gbp"])
    # The latest GBP balances are the validated positions converted at that day's FX.
    validated = data_dir / "validated"
    latest_file = max((validated / "positions").glob("date=*.parquet"))
    positions = pd.read_parquet(latest_file)
    fx = pd.read_parquet(validated / "fx.parquet")
    on_sheet = positions[positions["side"] != "off_balance_sheet"].merge(
        fx, on=["date", "currency"]
    )
    expected = (on_sheet["balance_mn"] * on_sheet["to_reporting"]).sum()
    assert drivers["latest_gbp"].sum() == pytest.approx(expected)


def test_the_summary_lists_the_top_drivers_set_in_config(pipeline_run):
    data_dir, run_id = pipeline_run
    run = run_changes(data_dir, run_id, REPORTING)  # top_drivers: 3
    assert len(run.summary["top_drivers"]) == 3
    top = run.balance_drivers.head(3)
    assert [d["category"] for d in run.summary["top_drivers"]] == list(top["category"])
    assert all(
        math.isclose(d["change_gbp"], v)
        for d, v in zip(run.summary["top_drivers"], top["change_gbp"])
    )


def test_the_metric_changes_are_the_limits_actuals(pipeline_run):
    data_dir, run_id = pipeline_run
    metrics = pd.read_parquet(data_dir / "changes" / run_id / "metric_changes.parquet")
    limits = pd.read_parquet(data_dir / "limits" / run_id / "limits.parquet")
    latest = limits[limits["date"] == limits["date"].max()]
    assert sorted(metrics["latest"].dropna()) == pytest.approx(sorted(latest["actual"].dropna()))


def test_refuses_a_run_whose_validated_data_has_been_replaced(pipeline_run):
    data_dir, run_id = pipeline_run
    with pytest.raises(ChangesInputError, match="has no limits"):
        run_changes(data_dir, "no-such-run", REPORTING)
    other = data_dir / "limits" / "older-run"
    other.mkdir(exist_ok=True)
    (data_dir / "limits" / run_id / "limits.parquet").replace(other / "limits.parquet")
    try:
        with pytest.raises(ChangesInputError, match=f"holds run {run_id}, not run older-run"):
            run_changes(data_dir, "older-run", REPORTING)
    finally:
        (other / "limits.parquet").replace(data_dir / "limits" / run_id / "limits.parquet")


def test_a_one_day_series_has_nothing_to_compare(tmp_path, limits):
    (tmp_path / "limits" / "r1").mkdir(parents=True)
    limits[limits["date"] == DAY2].to_parquet(tmp_path / "limits" / "r1" / "limits.parquet")
    validated = tmp_path / "validated"
    (validated / "positions").mkdir(parents=True)
    (validated / "_run.json").write_text(json.dumps({"run_id": "r1"}), encoding="utf-8")
    pd.DataFrame([_position(DAY2, "EUR", 1.0)]).to_parquet(
        validated / "positions" / "date=2026-08-04.parquet"
    )
    _fx(0.8, 0.8).to_parquet(validated / "fx.parquet")
    run = run_changes(tmp_path, "r1", REPORTING)
    assert run.summary["previous_date"] is None and run.summary["latest_date"] == "2026-08-04"
    assert run.summary["limit_movements"] == [] and run.summary["top_drivers"] == []
    assert run.limit_changes.empty and run.balance_drivers.empty
    assert (run.out_dir / "summary.json").exists()


# --- Config and the email -------------------------------------------------------------------------


def test_reporting_config_loads_and_rejects_bad_values():
    assert load_reporting_config(CONFIG / "reporting.yaml").changes.top_drivers == 10
    with pytest.raises(ValidationError):
        ReportingConfig.model_validate({"changes": {"top_drivers": 0}})
    with pytest.raises(ValidationError):
        ReportingConfig.model_validate({"changes": {"top_drivers": 3, "colour": "red"}})


def _limits_summary() -> dict:
    alert = {
        "date": "2026-08-04", "level": "entity", "entity": "BANK_UK", "currency": "GBP",
        "regime": "basel", "limit": "eve_tier1", "kind": "cap", "actual": 0.1575,
        "limit_value": 0.15, "utilisation": 1.05, "status": "breach",
    }  # fmt: skip
    return {
        "source_run_id": "r1",
        "as_of": "2026-08-04",
        "thresholds": {"warning": 0.9, "breach": 1.0},
        "alerts": [alert],
    }


CHANGES = {
    "previous_date": "2026-08-03",
    "limit_movements": [
        {
            "movement": "worse",
            "latest_status": "breach",
            "entity": "BANK_UK",
            "regime": "basel",
            "limit_name": "eve_tier1",
            "latest_utilisation": 1.05,
            "previous_utilisation": 0.95,
            "previous_status": "warning",
        }
    ],
    "top_drivers": [
        {
            "entity": "BANK_EU",
            "category": "corporate_loans",
            "side": "asset",
            "change_gbp": 19.0,
            "volume_gbp": 8.0,
            "fx_gbp": 11.0,
        }
    ],
}


def test_the_email_says_what_changed_since_the_day_before():
    _, body = compose_limit_email(_limits_summary(), CHANGES)
    assert "Since 2026-08-03:" in body
    assert "- WORSE BREACH BANK_UK (basel) eve_tier1: 105% used (was 95%, warning)" in body
    assert "- BANK_EU corporate_loans (asset): +19.0 = volume +8.0, FX +11.0" in body
    assert body.index("Since") > body.index("- BREACH BANK_UK")  # after today's alerts
    unchanged = {**CHANGES, "limit_movements": []}
    assert (
        "No internal limit changed status." in compose_limit_email(_limits_summary(), unchanged)[1]
    )
    assert "Since" not in compose_limit_email(_limits_summary())[1]  # no changes summary: as before


def test_notify_run_adds_the_runs_changes(tmp_path, monkeypatch):
    for stage, summary in (("limits", _limits_summary()), ("changes", CHANGES)):
        (tmp_path / stage / "r1").mkdir(parents=True)
        (tmp_path / stage / "r1" / "summary.json").write_text(json.dumps(summary), "utf-8")
    sent = {}

    class Client:
        def publish(self, **kwargs):
            sent.update(kwargs)

    monkeypatch.setattr("riskplatform.notify.boto3.client", lambda *a, **k: Client())
    arn = "arn:aws:sns:eu-west-2:123456789012:alerts"
    assert notify_run(tmp_path, "r1", env={ALERT_TOPIC_ENV: arn}) is True
    assert "Since 2026-08-03:" in sent["Message"]
