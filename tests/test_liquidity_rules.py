"""Regulatory rulebooks (Basel and UK PRA): complete, sourced, verified values, and validated."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from riskplatform.config import LiquidityRules, load_liquidity_rules

RULES_DIR = Path(__file__).resolve().parents[1] / "config" / "liquidity"
REGIMES = ["basel", "pra"]


@pytest.fixture(scope="module", params=REGIMES)
def rules(request) -> LiquidityRules:
    return load_liquidity_rules(RULES_DIR / f"{request.param}.yaml")


def test_file_name_matches_regime():
    for regime in REGIMES:
        assert load_liquidity_rules(RULES_DIR / f"{regime}.yaml").regime == regime


def test_every_category_has_exactly_one_lcr_treatment(cfg, rules):
    lcr = rules.lcr
    for category in cfg.category_names:
        places = [name for name, table in [("hqla", lcr.hqla), ("outflows", lcr.outflows),
                  ("inflows", lcr.inflows)] if category in table]  # fmt: skip
        assert len(places) == 1, f"{rules.regime} {category}: {places}"
    mapped = set(lcr.hqla) | set(lcr.outflows) | set(lcr.inflows)
    assert mapped <= set(cfg.category_names), mapped - set(cfg.category_names)


def test_every_category_has_an_nsfr_factor_on_the_right_side(cfg, rules):
    for category in cfg.categories:
        side = "asf" if category.side == "liability" else "rsf"
        table = getattr(rules.nsfr, side)
        assert category.name in table, f"{rules.regime} {category.name} missing from {side}"
    known = set(cfg.category_names) | {"equity"}
    assert set(rules.nsfr.asf) | set(rules.nsfr.rsf) <= known


def _all_refs(rules: LiquidityRules) -> list[str]:
    lcr, nsfr = rules.lcr, rules.nsfr
    params = [lcr.horizon_days, lcr.minimum, lcr.inflow_cap, lcr.level2_cap, lcr.level2b_cap]
    params += [lcr.level1_covered_bond_cap] if lcr.level1_covered_bond_cap else []
    treatments = [*lcr.hqla.values(), *lcr.outflows.values(), *lcr.inflows.values()]
    bands = [band for table in (nsfr.asf, nsfr.rsf) for bs in table.values() for band in bs]
    return [p.ref for p in params + treatments + bands + [nsfr.minimum]]


def test_every_value_cites_its_source(rules):
    refs = _all_refs(rules)
    assert refs and all(ref.strip() for ref in refs)


# Values verified against the Basel Framework and the PRA Rulebook on 2026-11-12. Pinned here
# so an accidental edit to a rulebook file fails loudly.
VERIFIED = {
    # (regime, path): expected
    ("basel", "lcr.inflow_cap"): 0.75,
    ("basel", "lcr.level2_cap"): 0.40,
    ("basel", "lcr.level2b_cap"): 0.15,
    ("basel", "lcr.hqla.covered_bonds"): ("2A", 0.15),
    ("basel", "lcr.hqla.corporate_bonds"): ("2B", 0.50),
    ("basel", "lcr.outflows.retail_stable_deposits"): 0.05,
    ("basel", "lcr.outflows.retail_less_stable_deposits"): 0.10,
    ("basel", "lcr.outflows.wholesale_operational"): 0.25,
    ("basel", "lcr.outflows.wholesale_non_operational"): 0.40,
    ("basel", "lcr.outflows.financial_institution_deposits"): 1.00,
    ("basel", "lcr.outflows.undrawn_committed_lines"): 0.10,
    ("basel", "lcr.inflows.corporate_loans"): 0.50,
    ("basel", "nsfr.rsf.govt_bonds"): [0.05],
    ("basel", "nsfr.rsf.covered_bonds"): [0.15],
    ("basel", "nsfr.rsf.retail_mortgages"): [0.50, 0.65],
    ("basel", "nsfr.rsf.corporate_loans"): [0.50, 0.85],
    ("basel", "nsfr.asf.retail_stable_deposits"): [0.95, 1.00],
    ("basel", "nsfr.asf.financial_institution_deposits"): [0.00, 0.50, 1.00],
    ("pra", "lcr.inflow_cap"): 0.75,
    ("pra", "lcr.level1_covered_bond_cap"): 0.70,
    ("pra", "lcr.hqla.covered_bonds"): ("1_covered", 0.07),
    ("pra", "lcr.hqla.corporate_bonds"): ("2B", 0.50),
    ("pra", "lcr.outflows.retail_stable_deposits"): 0.05,
    ("pra", "lcr.outflows.wholesale_non_operational"): 0.40,
    ("pra", "lcr.outflows.term_debt"): 1.00,
    ("pra", "lcr.outflows.undrawn_committed_lines"): 0.10,
    ("pra", "nsfr.rsf.govt_bonds"): [0.00],  # UK: 0% (Basel: 5%)
    ("pra", "nsfr.rsf.covered_bonds"): [0.07],
    ("pra", "nsfr.rsf.retail_mortgages"): [0.50, 0.65],
    ("pra", "nsfr.asf.term_debt"): [0.00, 0.50, 1.00],
}


def _value(rules: LiquidityRules, path: str):
    section, kind, *name = path.split(".")
    item = getattr(getattr(rules, section), kind)
    if not name:  # a Referenced parameter
        return item.value
    entry = item[name[0]]
    if isinstance(entry, list):  # NSFR bands
        return [band.factor for band in entry]
    if hasattr(entry, "level"):
        return (entry.level, entry.haircut)
    return entry.rate


@pytest.mark.parametrize(("key", "expected"), list(VERIFIED.items()), ids=lambda v: str(v))
def test_verified_values(key, expected):
    regime, path = key
    rules = load_liquidity_rules(RULES_DIR / f"{regime}.yaml")
    assert _value(rules, path) == expected


# --- Validation rejects broken rulebooks -------------------------------------------------------


def _broken(rules: LiquidityRules, change) -> dict:
    raw = rules.model_dump()
    change(raw)
    return raw


def _hqla_also_inflow(raw):
    raw["lcr"]["inflows"]["govt_bonds"] = {"rate": 1.0, "within_horizon_only": True, "ref": "x"}


def _covered_level_without_cap(raw):
    raw["lcr"]["hqla"]["covered_bonds"]["level"] = "1_covered"
    raw["lcr"]["level1_covered_bond_cap"] = None


def _bands_not_rising(raw):
    raw["nsfr"]["asf"]["term_debt"][1]["below_days"] = 100  # 183 then 100


def _no_open_last_band(raw):
    raw["nsfr"]["asf"]["term_debt"][-1]["below_days"] = 9999


def _no_equity(raw):
    del raw["nsfr"]["asf"]["equity"]


@pytest.mark.parametrize(
    "change",
    [
        _hqla_also_inflow,
        _covered_level_without_cap,
        _bands_not_rising,
        _no_open_last_band,
        _no_equity,
    ],
)
def test_broken_rulebooks_are_rejected(change):
    basel = load_liquidity_rules(RULES_DIR / "basel.yaml")
    with pytest.raises(ValidationError):
        LiquidityRules.model_validate(_broken(basel, change))
