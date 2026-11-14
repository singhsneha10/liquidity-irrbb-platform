"""Internal limits: utilisation by hand, status boundaries, agreement with the regulatory tests."""

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from riskplatform.config import (
    GeneratorConfig,
    LimitsConfig,
    load_ladder_config,
    load_limits_config,
)
from riskplatform.ingestion.generate import generate
from riskplatform.irrbb.run import compute_irrbb, load_irrbb_regimes
from riskplatform.limits import (
    LIMIT_COLUMNS,
    eve_share_of_tier1,
    latest_alerts,
    measure_limits,
    status,
    utilisation,
)
from riskplatform.metrics.run import compute_metrics, load_liquidity_regimes

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
LIMITS = load_limits_config(CONFIG / "limits.yaml")
DAY, NEXT = pd.Timestamp("2026-08-03"), pd.Timestamp("2026-08-04")
INF, NAN = math.inf, math.nan


# --- Utilisation and status ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (2.20, 0.5),  # LCR 220% against 110%: half the limit used
        (1.10, 1.0),  # exactly at the limit
        (0.55, 2.0),
        (NAN, 0.0),  # 0/0: nothing owed and nothing held
        (INF, 0.0),  # nothing owed
        (0.0, INF),  # owed something, held nothing
        (-0.5, INF),
    ],
)
def test_floor_utilisation_is_limit_over_actual(actual, expected):
    assert utilisation("floor", np.array([actual]), 1.10)[0] == pytest.approx(expected)


def test_cap_utilisation_is_actual_over_limit():
    assert utilisation("cap", np.array([0.075, 0.15, 0.30]), 0.15) == pytest.approx([0.5, 1, 2])


@pytest.mark.parametrize(
    ("used", "expected"),
    [(0.0, "ok"), (0.899, "ok"), (0.90, "warning"), (0.999, "warning"), (1.0, "breach"),
     (INF, "breach")],
)  # fmt: skip
def test_status_thresholds_are_inclusive(used, expected):
    assert status(np.array([used]), LIMITS.thresholds)[0] == expected


def test_eve_share_handles_missing_capital():
    risk = pd.DataFrame({"eve_risk": [30.0, 0.0, 5.0], "tier1": [200.0, 0.0, -10.0]})
    assert eve_share_of_tier1(risk) == pytest.approx([0.15, 0.0, INF])


# --- Measuring every limit -----------------------------------------------------------------------


def _key(level, entity, regime="basel", day=DAY):
    currency = {"BANK_UK": "GBP", None: "GBP"}.get(entity, "EUR")
    return {"date": day, "level": level, "entity": entity, "currency": currency, "regime": regime}


def _tables(lcr_value=1.25, nsfr_value=1.20, eve_risk=40.0, tier1=200.0, day=DAY):
    """One entity-currency, one entity and the group, with the same values for each."""
    keys = [_key("entity_currency", "BANK_UK", day=day), _key("entity", "BANK_UK", day=day),
            _key("group", None, day=day)]  # fmt: skip
    lcr = pd.DataFrame([{**k, "lcr": lcr_value} for k in keys])
    nsfr = pd.DataFrame([{**k, "nsfr": nsfr_value} for k in keys])
    risk = pd.DataFrame([{**k, "eve_risk": eve_risk, "tier1": tier1} for k in keys])
    return lcr, nsfr, risk


def test_every_limit_for_entities_and_the_group_only():
    table = measure_limits(*_tables(), LIMITS)
    assert list(table.columns) == LIMIT_COLUMNS
    assert set(table["level"]) == {"entity", "group"}  # per-currency: monitoring only
    assert len(table) == 2 * len(LIMITS.limits)


def test_values_worked_by_hand():
    table = measure_limits(*_tables(), LIMITS).set_index(["level", "limit"])
    entity = table.loc["entity"]
    # LCR 125% vs a 110% floor: 1.10 / 1.25 = 88% used, ok (below the 90% warning)
    assert entity.loc["lcr", "utilisation"] == pytest.approx(0.88)
    assert entity.loc["lcr", "status"] == "ok"
    # NSFR 120% vs a 105% floor: 1.05 / 1.20 = 87.5%, ok
    assert entity.loc["nsfr", "utilisation"] == pytest.approx(0.875)
    # EVE loss 40 of Tier 1 200 = 20% vs a 15% cap: 0.20 / 0.15 = 133%, breach
    assert entity.loc["eve_tier1", "actual"] == pytest.approx(0.20)
    assert entity.loc["eve_tier1", "utilisation"] == pytest.approx(4 / 3)
    assert entity.loc["eve_tier1", "status"] == "breach"


def test_a_stricter_limit_in_config_changes_the_result():
    stricter = LimitsConfig.model_validate(
        {**LIMITS.model_dump(), "limits": {**LIMITS.model_dump()["limits"],
         "lcr": {**LIMITS.limits["lcr"].model_dump(), "value": 1.15}}}
    )  # fmt: skip

    def lcr_statuses(limits):
        table = measure_limits(*_tables(), limits)
        return table.loc[table["limit"] == "lcr", "status"].unique().tolist()

    assert lcr_statuses(LIMITS) == ["ok"]  # 1.10 / 1.25 = 88%
    assert lcr_statuses(stricter) == ["warning"]  # 1.15 / 1.25 = 92%


def test_latest_alerts_are_the_latest_dates_breaches_first():
    old = measure_limits(*_tables(lcr_value=1.0, day=DAY), LIMITS)  # an old LCR breach
    new = measure_limits(*_tables(lcr_value=1.21, eve_risk=29.0, day=NEXT), LIMITS)
    alerts = latest_alerts(pd.concat([old, new], ignore_index=True))
    assert set(alerts["date"]) == {NEXT}
    # 1.10 / 1.21 = 90.9% warning; 29 / 200 = 14.5% of 15% = 96.7% warning; nothing breached
    assert set(alerts["status"]) == {"warning"}
    assert alerts["utilisation"].is_monotonic_decreasing


# --- Agreement with the regulatory tests on the generated bank -----------------------------------


@pytest.fixture(scope="module")
def generated_limits(cfg):
    small = GeneratorConfig.model_validate({**cfg.model_dump(), "n_days": 3})
    raw = generate(small)
    ladder = load_ladder_config(CONFIG / "liquidity" / "ladder.yaml")
    metrics = compute_metrics(
        raw.positions, raw.fx, small, load_liquidity_regimes(CONFIG / "liquidity"), ladder
    )
    irrbb = compute_irrbb(
        raw.positions, raw.rates, raw.fx, small, load_irrbb_regimes(CONFIG / "irrbb")
    )
    return metrics, irrbb, measure_limits(metrics.lcr, metrics.nsfr, irrbb.risk, LIMITS)


def test_eve_breach_agrees_with_the_irrbb_outlier_test(generated_limits):
    _, irrbb, table = generated_limits
    eve = table[table["limit"] == "eve_tier1"].merge(
        irrbb.risk[["date", "level", "entity", "regime", "outlier"]],
        on=["date", "level", "entity", "regime"],
    )
    not_exactly_at_the_limit = ~np.isclose(eve["actual"], LIMITS.limits["eve_tier1"].value)
    agree = eve["status"].eq("breach") == eve["outlier"].astype(bool)
    assert agree[not_exactly_at_the_limit].all()
    assert len(eve) == len(irrbb.risk[irrbb.risk["level"].isin(LIMITS.levels)])


@pytest.mark.parametrize("ratio", [0.0, 0.5, 0.95, 0.9999])
def test_a_regulatory_shortfall_is_always_a_limit_breach(ratio):
    """The internal floors are above the 100% minimum, so a ratio below the minimum breaches
    them too. (The generated bank has no shortfall, so the values are set here.)"""
    assert all(limit.value >= 1.0 for limit in LIMITS.limits.values() if limit.kind == "floor")
    table = measure_limits(*_tables(lcr_value=ratio, nsfr_value=ratio), LIMITS)
    floors = table[table["kind"] == "floor"]
    assert len(floors) == 4 and floors["status"].eq("breach").all()


# --- Config --------------------------------------------------------------------------------------


def _broken(**changes) -> dict:
    raw = LIMITS.model_dump()
    for path, value in changes.items():
        section, key = path.split("__")
        raw[section][key] = value
    return raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_broken(thresholds__warning=1.2), "must not be above breach"),
        (_broken(limits__lcr={"kind": "floor", "value": 1.1, "source": "lcr_typo", "why": "x"}),
         "source"),
        (_broken(limits__lcr={"kind": "floor", "value": 1.1, "source": "lcr", "why": ""}), "why"),
        ({**LIMITS.model_dump(), "levels": ["entity_currency"]}, "levels"),
    ],
)  # fmt: skip
def test_config_refuses_a_bad_limits_file(raw, message):
    with pytest.raises(ValidationError, match=message):
        LimitsConfig.model_validate(raw)
