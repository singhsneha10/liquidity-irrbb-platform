"""Metrics run: the worked example at every level, FX conversion, consistency, inputs, end to end."""

import json
from pathlib import Path

import pandas as pd
import pytest
from test_liquidity_worked_example import EXPECTED, PORTFOLIO

from riskplatform.config import (
    GeneratorConfig,
    load_ladder_config,
    load_liquidity_rules,
    load_quality_config,
)
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.metrics.run import (
    MetricsInputError,
    _breaches,
    compute_metrics,
    main,
    read_validated,
)
from riskplatform.quality.run import run_quality

ROOT = Path(__file__).resolve().parents[1]
LIQUIDITY = ROOT / "config" / "liquidity"
BASEL = {"basel": load_liquidity_rules(LIQUIDITY / "basel.yaml")}
BOTH = {**BASEL, "pra": load_liquidity_rules(LIQUIDITY / "pra.yaml")}
LADDER = load_ladder_config(LIQUIDITY / "ladder.yaml")
DAY = pd.Timestamp("2026-08-03")


def _fx(usd=0.5, eur=0.8) -> pd.DataFrame:
    return pd.DataFrame(
        {"date": DAY, "currency": ["GBP", "USD", "EUR"], "to_reporting": [1.0, usd, eur]}
    )


def _worked_example(entity="BANK_UK", currency="GBP") -> pd.DataFrame:
    frame = pd.DataFrame(
        PORTFOLIO, columns=["id", "category", "side", "balance_mn", "residual_days"]
    )
    return frame.assign(date=DAY, entity=entity, currency=currency).astype({"balance_mn": float})


def _row(table, level, entity=None, regime="basel"):
    match = (table["level"] == level) & (table["regime"] == regime)
    match &= table["entity"].isna() if entity is None else table["entity"] == entity
    rows = table[match]
    assert len(rows) == 1, rows
    return rows.iloc[0]


def test_worked_example_gives_the_hand_answer_at_every_level(cfg):
    result = compute_metrics(_worked_example(), _fx(), cfg, BASEL, LADDER)
    for level, entity in [("entity_currency", "BANK_UK"), ("entity", "BANK_UK"), ("group", None)]:
        assert _row(result.lcr, level, entity)["lcr"] == pytest.approx(
            EXPECTED["basel"]["lcr"], abs=1e-6
        )
        assert _row(result.nsfr, level, entity)["nsfr"] == pytest.approx(
            EXPECTED["basel"]["nsfr"], abs=1e-6
        )


def test_foreign_currency_is_converted_for_the_group_but_not_locally(cfg):
    result = compute_metrics(_worked_example("BANK_US", "USD"), _fx(usd=0.5), cfg, BASEL, LADDER)
    local = _row(result.lcr, "entity_currency", "BANK_US")
    group = _row(result.lcr, "group")
    assert local["currency"] == "USD" and group["currency"] == "GBP"
    assert local["hqla"] == pytest.approx(EXPECTED["basel"]["hqla"], abs=1e-6)
    for field in ["hqla", "outflows", "inflows_capped", "net_outflows"]:
        assert group[field] == pytest.approx(0.5 * local[field])
    assert group["lcr"] == pytest.approx(local["lcr"])  # a ratio does not depend on the unit


def test_mixed_currency_entity_adds_up_in_its_home_currency(cfg):
    positions = pd.DataFrame(
        [
            ("central_bank_reserves", "asset", 100.0, 1, "GBP"),
            ("govt_bonds", "asset", 200.0, 400, "USD"),
        ],
        columns=["category", "side", "balance_mn", "residual_days", "currency"],
    ).assign(date=DAY, entity="BANK_UK")
    result = compute_metrics(positions, _fx(usd=0.5), cfg, BASEL, LADDER)
    assert _row(result.lcr, "entity", "BANK_UK")["level1"] == pytest.approx(100 + 200 * 0.5)


@pytest.fixture(scope="module")
def small_cfg(cfg) -> GeneratorConfig:
    return GeneratorConfig.model_validate({**cfg.model_dump(), "n_days": 3})


@pytest.fixture(scope="module")
def generated_metrics(small_cfg):
    raw = generate(small_cfg)
    return raw, compute_metrics(raw.positions, raw.fx, small_cfg, BOTH, LADDER)


def test_group_equals_converted_sum_of_currency_results(generated_metrics):
    raw, result = generated_metrics
    rate = raw.fx.set_index(["date", "currency"])["to_reporting"]
    for table, field in [(result.lcr, "outflows"), (result.nsfr, "rsf")]:
        parts = table[(table["level"] == "entity_currency") & (table["regime"] == "basel")]
        converted = (
            parts[field]
            * rate.loc[list(zip(parts["date"], parts["currency"], strict=True))].to_numpy()
        )
        expected = converted.groupby(parts["date"]).sum()
        group = table[(table["level"] == "group") & (table["regime"] == "basel")].set_index("date")[
            field
        ]
        pd.testing.assert_series_equal(group, expected, check_names=False)


def test_group_ladder_ends_at_group_equity(generated_metrics):
    _, result = generated_metrics
    ladder = result.ladder[result.ladder["level"] == "group"]
    final = ladder.groupby("date")["cumulative_net"].last()
    equity = result.nsfr[(result.nsfr["level"] == "group") & (result.nsfr["regime"] == "basel")]
    pd.testing.assert_series_equal(final, equity.set_index("date")["equity"], check_names=False)


def test_minimum_applies_to_entities_and_group_only(generated_metrics):
    _, result = generated_metrics
    for table in (result.lcr, result.nsfr):
        per_currency = table["level"] == "entity_currency"
        assert table.loc[per_currency, "meets_minimum"].isna().all()
        assert table.loc[~per_currency, "meets_minimum"].isin([True, False]).all()
    assert _breaches(result) == []


def test_breach_is_listed(cfg):
    only_deposits = pd.DataFrame(
        [("retail_stable_deposits", "liability", 100.0, 1, "GBP")],
        columns=["category", "side", "balance_mn", "residual_days", "currency"],
    ).assign(date=DAY, entity="BANK_UK")
    breaches = _breaches(compute_metrics(only_deposits, _fx(), cfg, BASEL, LADDER))
    lcr = [b for b in breaches if b["metric"] == "lcr"]
    assert {(b["level"], b["entity"]) for b in lcr} == {("entity", "BANK_UK"), ("group", None)}
    assert all(b["value"] == 0.0 and b["minimum"] == 1.0 for b in lcr)


def _publish(small_cfg, tmp_path) -> Path:
    raw = tmp_path / "raw"
    write_outputs(generate(small_cfg), raw)
    result = run_quality(
        raw, tmp_path, small_cfg, load_quality_config(ROOT / "config" / "quality.yaml")
    )
    assert result.status == "published"
    return tmp_path / "validated"


def test_unpublished_or_mismatched_data_is_refused(small_cfg, tmp_path):
    with pytest.raises(MetricsInputError, match="not published"):
        read_validated(tmp_path, small_cfg)
    validated = _publish(small_cfg, tmp_path)
    real = small_cfg.model_dump()
    real["rates"]["source"] = "real"
    real["rates"]["real"] = {"curves_file": "x", "fx_file": "x", "max_fill_days": 4}
    with pytest.raises(MetricsInputError, match="rates_source"):
        read_validated(validated, GeneratorConfig.model_validate(real))


def test_command_end_to_end(small_cfg, tmp_path):
    validated = _publish(small_cfg, tmp_path)
    config = tmp_path / "small.yaml"
    config.write_text(f"base: {(ROOT / 'config' / 'generator.yaml').as_posix()}\nn_days: 3\n")
    out = tmp_path / "metrics"
    args = ["--config", str(config), "--validated", str(validated), "--out", str(out)]
    assert main(args) == 0

    run_id = json.loads((validated / "_run.json").read_text(encoding="utf-8"))["run_id"]
    folder = out / run_id
    groups_per_day = 3 * 3 + 3 + 1  # entity-currency, entity, group
    assert len(pd.read_parquet(folder / "lcr.parquet")) == 3 * groups_per_day * 2  # 2 regimes
    assert len(pd.read_parquet(folder / "ladder.parquet")) == 3 * groups_per_day * len(
        LADDER.buckets
    )
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert summary["source_run_id"] == run_id and summary["breaches"] == []
