"""What-if and limit drivers (riskplatform.whatif): with no stress the what-if equals the
published run, and each driver table adds up to the published figure."""

import dataclasses
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from riskplatform.config import WhatIfConfig, load_whatif_config
from riskplatform.pipeline import load_inputs, run_pipeline
from riskplatform.whatif import (
    GROUP,
    Levers,
    UnfundedOutflowError,
    after_withdrawal,
    eve_drivers,
    lcr_drivers,
    lcr_summary,
    load_day,
    nsfr_drivers,
    what_if,
    with_parallel_add_on,
    with_run_off_add_on,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
WHATIF = load_whatif_config(CONFIG / "whatif.yaml")
WHO = ["BANK_EU", "BANK_UK", "BANK_US", GROUP]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One whole pipeline run (2-day bank): its folder and run id."""
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    folder = tmp_path_factory.mktemp("whatif")
    run_pipeline(dataclasses.replace(loaded, generator=small), folder)
    run_id = json.loads((folder / "validated" / "_run.json").read_text())["run_id"]
    return folder, run_id


@pytest.fixture(scope="module")
def day(run):
    return load_day(run[0] / "validated", CONFIG)


def _published(run, stage: str, name: str) -> pd.DataFrame:
    """The run's own table on its latest day, entity and group rows, with `who`."""
    folder, run_id = run
    table = pd.read_parquet(folder / stage / run_id / f"{name}.parquet")
    table = table[table["date"] == table["date"].max()]
    table = table[table["level"].isin(["entity", "group"])]
    return table.assign(who=table["entity"].fillna(GROUP))


def _row(table: pd.DataFrame, regime: str, who: str) -> pd.Series:
    rows = table[(table["regime"] == regime) & (table["who"] == who)]
    assert len(rows) == 1
    return rows.iloc[0]


# --- What-if ------------------------------------------------------------------------------------


@pytest.mark.parametrize("regime", ["basel", "pra"])
def test_with_no_stress_the_what_if_is_the_published_run(run, day, regime):
    shown = what_if(day, regime, Levers())
    published = _published(run, "limits", "limits")
    published = published[published["regime"] == regime]
    merged = shown.merge(published, on=["who", "limit_name"], suffixes=("", "_run"))
    assert len(shown) == len(published) == len(merged) == 12
    assert merged["actual"].to_numpy() == pytest.approx(merged["actual_run"].to_numpy())
    assert (merged["status"] == merged["status_run"]).all()


def test_run_off_add_on_lowers_only_the_lcr(day):
    base = what_if(day, "basel", Levers())
    stressed = what_if(day, "basel", Levers(retail_run_off_add_on=0.20))
    lcr = base["limit_name"] == "lcr"
    assert (stressed.loc[lcr, "actual"] < base.loc[lcr, "actual"]).all()
    assert stressed.loc[~lcr, "actual"].to_numpy() == pytest.approx(base.loc[~lcr, "actual"])


def test_run_off_add_on_is_added_to_both_retail_rates_and_capped_at_100_percent(day):
    rules = day.liquidity["basel"]
    outflows = with_run_off_add_on(rules, 0.20, WHATIF.retail_deposits).lcr.outflows
    assert outflows["retail_stable_deposits"].rate == pytest.approx(0.25)  # 5% + 20 points
    assert outflows["retail_less_stable_deposits"].rate == pytest.approx(0.30)  # 10% + 20
    assert outflows["wholesale_operational"] == rules.lcr.outflows["wholesale_operational"]
    capped = with_run_off_add_on(rules, 0.99, WHATIF.retail_deposits).lcr.outflows
    assert capped["retail_less_stable_deposits"].rate == 1.0
    assert rules.lcr.outflows["retail_stable_deposits"].rate == 0.05  # the original is unchanged


def test_parallel_add_on_moves_only_the_parallel_shock(day):
    rules = day.irrbb["basel"]
    stressed = with_parallel_add_on(rules, 100)
    for currency, sizes in rules.shocks.sizes_bp.items():
        new = stressed.shocks.sizes_bp[currency]
        assert new.parallel == sizes.parallel + 100
        assert (new.short, new.long) == (sizes.short, sizes.long)
    base = what_if(day, "basel", Levers())
    shocked = what_if(day, "basel", Levers(parallel_shock_add_on_bp=300))
    eve = base["limit_name"] == "eve_tier1"
    assert (shocked.loc[eve, "actual"] >= base.loc[eve, "actual"] - 1e-12).all()
    assert (shocked.loc[eve, "actual"] > base.loc[eve, "actual"]).any()  # it bites somewhere
    assert shocked.loc[~eve, "actual"].to_numpy() == pytest.approx(base.loc[~eve, "actual"])


def test_a_withdrawal_is_paid_from_the_same_book_cash_first(day):
    positions = day.positions
    after = after_withdrawal(positions, 0.05, WHATIF.retail_deposits, WHATIF.funding_order)
    for _, book in positions.groupby(["entity", "currency"]):
        new = after.loc[book.index, "balance_mn"]
        old = book["balance_mn"]
        deposits = book["category"].isin(WHATIF.retail_deposits)
        withdrawn = 0.05 * old[deposits].sum()
        assert new[deposits].to_numpy() == pytest.approx(0.95 * old[deposits].to_numpy())
        assets = book["side"] == "asset"
        assert old[assets].sum() - new[assets].sum() == pytest.approx(withdrawn)  # equity kept
        reserves = book["category"] == "central_bank_reserves"
        if old[reserves].sum() >= withdrawn:  # the cash was enough, so no bond was sold
            untouched = ~reserves & ~deposits
            assert new[untouched].to_numpy() == pytest.approx(old[untouched].to_numpy())


def test_a_withdrawal_a_book_cannot_pay_stops_the_what_if(day):
    with pytest.raises(UnfundedOutflowError, match="cannot pay"):
        after_withdrawal(day.positions, 0.9, WHATIF.retail_deposits, WHATIF.funding_order)


def test_the_largest_withdrawal_offered_can_be_paid_and_lowers_the_lcr(day):
    base = what_if(day, "basel", Levers())
    largest = WHATIF.levers.deposit_outflow.max
    stressed = what_if(day, "basel", Levers(deposit_outflow=largest))
    lcr = base["limit_name"] == "lcr"
    assert (stressed.loc[lcr, "actual"] < base.loc[lcr, "actual"]).all()


def test_a_lever_cannot_soften_the_stress():
    with pytest.raises(ValueError, match="cannot be negative"):
        Levers(retail_run_off_add_on=-0.01)
    with pytest.raises(ValueError, match="at most 1"):
        Levers(deposit_outflow=1.5)


# --- Drivers ------------------------------------------------------------------------------------


@pytest.mark.parametrize("regime", ["basel", "pra"])
@pytest.mark.parametrize("who", WHO)
def test_eve_contributions_add_up_to_the_published_loss_in_every_scenario(run, day, regime, who):
    eve = _published(run, "irrbb", "eve")
    for scenario in day.irrbb[regime].shocks.scenarios:
        drivers = eve_drivers(day, regime, who, scenario)
        rows = eve[(eve["regime"] == regime) & (eve["who"] == who) & (eve["scenario"] == scenario)]
        assert drivers["contribution"].sum() == pytest.approx(rows["aggregated_loss"].iloc[0])
        assert drivers["position_id"].is_unique


def test_a_home_currency_position_contributes_its_own_loss(day):
    drivers = eve_drivers(day, "basel", "BANK_UK", "steepener")
    assert np.isfinite(drivers["delta_eve"]).all()
    gbp = drivers[drivers["currency"] == "GBP"]  # BANK_UK's home currency, a loss here
    assert gbp["contribution"].to_numpy() == pytest.approx(-gbp["delta_eve"].to_numpy())


@pytest.mark.parametrize("regime", ["basel", "pra"])
@pytest.mark.parametrize("who", WHO)
def test_lcr_drivers_add_up_to_the_published_components(run, day, regime, who):
    published = _row(_published(run, "metrics", "lcr"), regime, who)
    total = lcr_drivers(day, regime, who).groupby("role")["amount"].sum()
    assert total["hqla"] == pytest.approx(published["hqla_before_caps"])
    assert total["outflow"] == pytest.approx(published["outflows"])
    assert total.get("inflow", 0.0) == pytest.approx(published["inflows"])
    summary = lcr_summary(day, regime, who).set_index("item")["amount"]
    assert summary["HQLA counted"] == pytest.approx(published["hqla"])
    assert summary["HQLA counted"] / summary["net outflows"] == pytest.approx(published["lcr"])


@pytest.mark.parametrize("regime", ["basel", "pra"])
@pytest.mark.parametrize("who", WHO)
def test_nsfr_drivers_add_up_to_the_published_funding(run, day, regime, who):
    published = _row(_published(run, "metrics", "nsfr"), regime, who)
    total = nsfr_drivers(day, regime, who).groupby("role")["amount"].sum()
    assert total["asf"] == pytest.approx(published["asf"])
    assert total["rsf"] == pytest.approx(published["rsf"])


def test_an_unknown_scenario_is_an_error(day):
    with pytest.raises(ValueError, match="unknown scenario"):
        eve_drivers(day, "basel", "BANK_UK", "sideways")


# --- Config -------------------------------------------------------------------------------------


def test_whatif_config_rejects_contradictions():
    raw = WHATIF.model_dump()
    with pytest.raises(ValueError, match="both withdrawn and used to pay"):
        WhatIfConfig.model_validate({**raw, "funding_order": ["retail_stable_deposits"]})
    with pytest.raises(ValueError, match="larger than max"):
        levers = {**raw["levers"], "deposit_outflow": {"max": 0.1, "step": 0.2}}
        WhatIfConfig.model_validate({**raw, "levers": levers})
    with pytest.raises(ValueError, match="at most 1"):
        levers = {**raw["levers"], "deposit_outflow": {"max": 2, "step": 0.1}}
        WhatIfConfig.model_validate({**raw, "levers": levers})
