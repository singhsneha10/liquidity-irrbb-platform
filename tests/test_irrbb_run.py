"""IRRBB run: the worked example end to end, conversion, Basel vs UK at group level, consistency."""

import json
from pathlib import Path

import pandas as pd
import pytest
from test_irrbb_worked_example import EXPECTED_EVE

from riskplatform.config import GeneratorConfig, load_irrbb_rules, load_quality_config
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.irrbb.run import compute_irrbb, main
from riskplatform.quality.run import run_quality

ROOT = Path(__file__).resolve().parents[1]
RULES = {r: load_irrbb_rules(ROOT / "config" / "irrbb" / f"{r}.yaml") for r in ("basel", "pra")}
DAY = pd.Timestamp("2026-08-03")


def _rates(currencies=("GBP", "USD", "EUR"), level=0.01) -> pd.DataFrame:
    return pd.DataFrame(
        [(DAY, c, t, level) for c in currencies for t in (0.0833, 30.0)],
        columns=["date", "currency", "tenor_years", "rate"],
    )


def _fx(usd=0.5, eur=0.8) -> pd.DataFrame:
    return pd.DataFrame(
        {"date": DAY, "currency": ["GBP", "USD", "EUR"], "to_reporting": [1.0, usd, eur]}
    )


def _book(entity="BANK_UK", currency="GBP") -> pd.DataFrame:
    """The worked example: a 1,000 fixed loan (1,700 days) and 800 stable deposits."""
    return pd.DataFrame(
        [
            ("corporate_loans", "asset", 1000.0, 1700),
            ("retail_stable_deposits", "liability", 800.0, 1),
        ],
        columns=["category", "side", "balance_mn", "reprice_days"],
    ).assign(date=DAY, entity=entity, currency=currency)


def _risk(result, level, regime="basel"):
    rows = result.risk[(result.risk["level"] == level) & (result.risk["regime"] == regime)]
    assert len(rows) == 1
    return rows.iloc[0]


@pytest.mark.parametrize("regime", ["basel", "pra"])
def test_worked_example_end_to_end(cfg, regime):
    result = compute_irrbb(_book(), _rates(), _fx(), cfg, RULES)
    local = result.eve[
        (result.eve["level"] == "entity_currency") & (result.eve["regime"] == regime)
    ]
    assert dict(zip(local["scenario"], local["delta_eve"], strict=True)) == pytest.approx(
        EXPECTED_EVE[regime], abs=1e-6
    )
    for level in ("entity", "group"):
        row = _risk(result, level, regime)
        assert row["eve_risk"] == pytest.approx(61.639071, abs=1e-6)
        assert (row["worst_scenario"], row["tier1"], bool(row["outlier"])) == (
            "parallel_up",
            200.0,
            True,
        )
    assert _risk(result, "entity_currency", regime)["outlier"] is None  # monitoring only


def test_foreign_currency_book_is_converted_for_the_group(cfg):
    result = compute_irrbb(_book("BANK_US", "USD"), _rates(), _fx(usd=0.5), cfg, RULES)
    entity, group = _risk(result, "entity"), _risk(result, "group")
    assert entity["currency"] == "USD" and group["currency"] == "GBP"
    assert group["eve_risk"] == pytest.approx(0.5 * entity["eve_risk"])
    assert group["tier1"] == pytest.approx(0.5 * entity["tier1"])
    assert bool(group["outlier"]) == bool(entity["outlier"])  # a ratio test: unit-free


def test_basel_and_uk_combine_currencies_differently(cfg):
    loss_in_gbp = _book("BANK_UK", "GBP")  # loses when rates rise
    gain_in_eur = pd.DataFrame(  # long fixed funding gains value when rates rise
        [("term_debt", "liability", 500.0, 3000)],
        columns=["category", "side", "balance_mn", "reprice_days"],
    ).assign(date=DAY, entity="BANK_EU", currency="EUR")
    result = compute_irrbb(
        pd.concat([loss_in_gbp, gain_in_eur]), _rates(), _fx(eur=0.8), cfg, RULES
    )
    eve = result.eve
    local = eve[(eve["level"] == "entity_currency") & (eve["regime"] == "basel")]
    local = local.set_index(["currency", "scenario"])["delta_eve"]
    gbp, eur = local[("GBP", "parallel_up")], local[("EUR", "parallel_up")]
    assert gbp < 0 < eur

    def group_loss(regime):
        rows = eve[(eve["level"] == "group") & (eve["regime"] == regime)]
        return rows.set_index("scenario")["aggregated_loss"]["parallel_up"]

    assert group_loss("basel") == pytest.approx(-gbp)  # losses only
    assert group_loss("pra") == pytest.approx(-gbp - 0.5 * eur * 0.8)  # gains offset at 50%


@pytest.fixture(scope="module")
def small_cfg(cfg) -> GeneratorConfig:
    return GeneratorConfig.model_validate({**cfg.model_dump(), "n_days": 2})


def test_group_rebuilds_from_currency_results_on_the_generated_bank(small_cfg):
    raw = generate(small_cfg)
    result = compute_irrbb(raw.positions, raw.rates, raw.fx, small_cfg, {"basel": RULES["basel"]})
    rate = raw.fx.set_index(["date", "currency"])["to_reporting"]
    eve = result.eve
    local = eve[eve["level"] == "entity_currency"].copy()
    local["converted"] = (
        local["delta_eve"]
        * rate.loc[list(zip(local["date"], local["currency"], strict=True))].to_numpy()
    )
    per_currency = local.groupby(["date", "scenario", "currency"])["converted"].sum()
    rebuilt = (-per_currency.clip(upper=0)).groupby(["date", "scenario"]).sum()  # Basel: losses
    group = eve[eve["level"] == "group"].set_index(["date", "scenario"])["aggregated_loss"]
    pd.testing.assert_series_equal(group.sort_index(), rebuilt.sort_index(), check_names=False)


def test_command_end_to_end(small_cfg, tmp_path):
    raw_dir = tmp_path / "raw"
    write_outputs(generate(small_cfg), raw_dir)
    quality = run_quality(
        raw_dir, tmp_path, small_cfg, load_quality_config(ROOT / "config" / "quality.yaml")
    )
    assert quality.status == "published"
    config = tmp_path / "small.yaml"
    config.write_text(f"base: {(ROOT / 'config' / 'generator.yaml').as_posix()}\nn_days: 2\n")
    out = tmp_path / "irrbb"
    code = main(
        ["--config", str(config), "--validated", str(tmp_path / "validated"), "--out", str(out)]
    )

    folder = out / quality.run_id
    for name in ("eve", "risk", "nii", "gap"):
        assert (folder / f"{name}.parquet").exists()
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert summary["source_run_id"] == quality.run_id
    assert code == (1 if summary["outliers"] else 0)


def test_hedges_reduce_the_groups_eve_risk(small_cfg):
    raw = generate(small_cfg)
    rules = {"basel": RULES["basel"]}
    hedged = compute_irrbb(raw.positions, raw.rates, raw.fx, small_cfg, rules)
    unhedged_positions = raw.positions[raw.positions["category"] != "interest_rate_swaps"]
    unhedged = compute_irrbb(unhedged_positions, raw.rates, raw.fx, small_cfg, rules)

    def group_risk(result):
        risk = result.risk
        return risk[risk["level"] == "group"].set_index("date")["eve_risk"]

    assert (group_risk(hedged) < group_risk(unhedged)).all()
