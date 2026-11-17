"""Early-warning indicators: retail outflow (R4) and break-even run-off (R3)."""

import dataclasses
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from riskplatform.config import EarlyWarningConfig, load_early_warning_config, load_generator_config
from riskplatform.early_warning import (
    ALERT,
    NOT_ENOUGH_DAYS,
    OK,
    EarlyWarningInputError,
    break_even_run_offs,
    describe_indicator,
    retail_outflows,
    run_early_warning,
)
from riskplatform.ingestion.generate import generate
from riskplatform.metrics.run import load_liquidity_regimes
from riskplatform.model_validation.sensitivity import break_even, latest_groups
from riskplatform.pipeline import load_inputs, run_pipeline

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
EW = load_early_warning_config(CONFIG / "early_warning.yaml")
REGIMES = load_liquidity_regimes(CONFIG / "liquidity")
STABLE, LESS_STABLE = "retail_stable_deposits", "retail_less_stable_deposits"


def test_config_loads_and_refuses_bad_thresholds():
    assert (EW.retail_outflow.threshold, EW.break_even_run_off.threshold) == (0.03, 0.20)
    assert EW.retail_outflow.business_days == 20  # 28 calendar days, as the backtest
    raw = EW.model_dump()
    raw["retail_outflow"]["threshold"] = 1.5
    with pytest.raises(ValidationError):
        EarlyWarningConfig.model_validate(raw)


@pytest.fixture(scope="module")
def normal():
    cfg = load_generator_config(CONFIG / "generator.yaml")
    return cfg, generate(cfg)


@pytest.fixture(scope="module")
def stressed():
    """The planted run, generated up to its last day (2 October 2026): fully inside 20 days."""
    cfg = load_generator_config(CONFIG / "generator_stress.yaml", as_of=date(2026, 10, 2))
    return cfg, generate(cfg)


def _row(table, who, category):
    return table[(table["who"] == who) & (table["category"] == category)].iloc[0]


def test_no_outflow_alert_on_the_normal_bank(normal):
    _, raw = normal
    table = retail_outflows(raw.positions, raw.fx, EW)
    assert len(table) == 4 * 2  # 3 entities + the group, 2 categories
    assert (table["status"] == OK).all()


def test_the_planted_run_is_flagged_where_it_happened(stressed):
    _cfg, raw = stressed
    table = retail_outflows(raw.positions, raw.fx, EW)
    flagged = table[table["status"] == ALERT]
    assert {"BANK_UK"} <= set(flagged["who"])
    assert set(flagged["who"]) <= {"BANK_UK", "GROUP"}  # only where the run was
    uk_stable = _row(table, "BANK_UK", STABLE)["value"]
    assert uk_stable == pytest.approx(0.0939, abs=0.005)  # the planted 9.39% (validation T2)
    assert _row(table, "BANK_UK", LESS_STABLE)["value"] > uk_stable  # 20% planted there
    assert (table[table["who"].isin(["BANK_EU", "BANK_US"])]["status"] == OK).all()


def test_the_group_outflow_adds_the_entities(stressed):
    from riskplatform.model_validation.backtest import retail_deposits

    _, raw = stressed
    table = retail_outflows(raw.positions, raw.fx, EW)
    levels = retail_deposits(raw.positions, raw.fx)
    stable = levels.xs(STABLE, axis=1, level="category").sum(axis=1)
    expected = 1 - stable.iloc[-1] / stable.iloc[-1 - EW.retail_outflow.business_days]
    assert _row(table, "GROUP", STABLE)["value"] == pytest.approx(expected)


def test_too_few_days_is_not_an_alert(normal):
    cfg, _ = normal
    short = generate(cfg.model_validate({**cfg.model_dump(), "n_days": 5}))
    table = retail_outflows(short.positions, short.fx, EW)
    assert (table["status"] == NOT_ENOUGH_DAYS).all() and table["value"].isna().all()


def test_break_even_values_are_t3s_and_alert_below_the_threshold(normal):
    cfg, raw = normal
    table = break_even_run_offs(raw.positions, raw.fx, cfg, REGIMES, 1.10, EW)
    assert len(table) == 4 * 2  # 3 entities + the group, 2 rulebooks
    for key, group in latest_groups(raw.positions, raw.fx, cfg):
        for name, rules in REGIMES.items():
            row = table[(table["who"] == (key["entity"] or "GROUP")) & (table["regime"] == name)]
            value = break_even(group, rules, 1.10)
            assert row["value"].iloc[0] == pytest.approx(value)
            assert row["status"].iloc[0] == (ALERT if value < 0.20 else OK)
    relaxed = EW.model_copy(update={"break_even_run_off": EW.break_even_run_off.model_copy(
        update={"threshold": 0.10})})  # fmt: skip
    assert (break_even_run_offs(raw.positions, raw.fx, cfg, REGIMES, 1.10, relaxed)["status"]
            == OK).all()  # fmt: skip


def test_describe_uses_two_decimals_near_the_threshold():
    item = {"indicator": "break_even_run_off", "who": "BANK_UK", "regime": "basel",
            "value": 0.19988, "threshold": 0.20, "status": "alert"}  # fmt: skip
    assert describe_indicator(item) == (
        "ALERT BANK_UK (basel) break-even run-off 19.99% (alert below 20%)"
    )


@pytest.fixture(scope="module")
def pipeline_run(tmp_path_factory):
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    inputs = dataclasses.replace(loaded, generator=small)
    data_dir = tmp_path_factory.mktemp("early_warning")
    result = run_pipeline(inputs, data_dir)
    return inputs, data_dir, result


def test_the_pipeline_stage_writes_both_indicators(pipeline_run):
    _inputs, data_dir, result = pipeline_run
    stage = next(s for s in result.stages if s.name == "early_warning")
    out = data_dir / "early_warning" / result.run_id
    table = pd.read_parquet(out / "early_warning.parquet")
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))  # strict JSON
    assert set(table["indicator"]) == {"retail_outflow", "break_even_run_off"}
    assert summary["counts"]["alert"] == len(summary["alerts"])
    assert stage.status == ("findings" if summary["alerts"] else "ok")
    assert summary["counts"][NOT_ENOUGH_DAYS] == 8  # 2 days: no 4-week outflow yet


def test_refuses_another_runs_validated_data(pipeline_run):
    inputs, data_dir, _ = pipeline_run
    with pytest.raises(EarlyWarningInputError, match="not run other-run"):
        run_early_warning(data_dir, "other-run", inputs.generator, inputs.liquidity, 1.10,
                          inputs.early_warning)  # fmt: skip
