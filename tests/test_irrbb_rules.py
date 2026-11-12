"""IRRBB rulebooks (Basel SRP31 and UK PRA): complete, sourced, verified values, validated."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from riskplatform.config import IrrbbRules, load_irrbb_rules

RULES_DIR = Path(__file__).resolve().parents[1] / "config" / "irrbb"
BASEL = load_irrbb_rules(RULES_DIR / "basel.yaml")
PRA = load_irrbb_rules(RULES_DIR / "pra.yaml")

# SRP31.96 Table 3 midpoints (and ICAA 9 standardised framework), in years.
OFFICIAL_MIDPOINTS = [
    0.0028, 0.0417, 0.1667, 0.375, 0.625, 0.875, 1.25, 1.75, 2.5, 3.5,
    4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 12.5, 17.5, 25.0,
]  # fmt: skip
# Verified 2026-11-12: SRP31.90 Table 2 (in force 1 Jan 2026) = ICAA 9.11 Table 1 (01/07/2026).
VERIFIED_SHOCKS = {
    "GBP": (275, 425, 250),
    "EUR": (225, 350, 200),
    "USD": (200, 300, 225),
}


@pytest.fixture(params=[BASEL, PRA], ids=["basel", "pra"])
def rules(request) -> IrrbbRules:
    return request.param


def test_file_name_matches_regime():
    assert (BASEL.regime, PRA.regime) == ("basel", "pra")


def test_every_currency_has_shock_sizes(cfg, rules):
    assert set(cfg.currencies) <= set(rules.shocks.sizes_bp)


def test_nmd_categories_are_non_maturity_liabilities(cfg, rules):
    by_name = {c.name: c for c in cfg.categories}
    for name in rules.nmd.categories:
        assert name in by_name, name
        assert by_name[name].side == "liability"
        assert tuple(by_name[name].tenor_days) == (1, 1)  # repayable on demand


def test_verified_shock_sizes_and_shapes(rules):
    for currency, (parallel, short, long) in VERIFIED_SHOCKS.items():
        sizes = rules.shocks.sizes_bp[currency]
        assert (sizes.parallel, sizes.short, sizes.long) == (parallel, short, long), currency
    assert rules.shocks.decay_years.value == 4
    assert (rules.shocks.steepener.short, rules.shocks.steepener.long) == (-0.65, 0.90)
    assert (rules.shocks.flattener.short, rules.shocks.flattener.long) == (0.80, -0.60)
    assert len(rules.shocks.scenarios) == 6


def test_verified_buckets_and_caps(rules):
    assert [b.midpoint_years for b in rules.buckets] == OFFICIAL_MIDPOINTS
    caps = {name: (cap.core_max, cap.years_max) for name, cap in rules.nmd.caps.items()}
    assert caps == {
        "retail_transactional": (0.90, 5.0),
        "retail_non_transactional": (0.70, 4.5),
        "wholesale": (0.50, 4.0),
    }
    assert rules.outlier_threshold.value == 0.15


def test_where_basel_and_the_uk_differ():
    assert not BASEL.shocks.floor.applied
    assert (PRA.shocks.floor.start_bp, PRA.shocks.floor.zero_at_years) == (-100, 20)
    assert (BASEL.aggregation.gains_weight, PRA.aggregation.gains_weight) == (0.0, 0.5)
    assert (BASEL.nmd.max_average_years, PRA.nmd.max_average_years) == (None, 5.0)


def test_every_value_cites_its_source(rules):
    refs = [
        rules.shocks.scenarios_ref,
        rules.shocks.sizes_ref,
        rules.shocks.decay_years.ref,
        rules.shocks.steepener.ref,
        rules.shocks.flattener.ref,
        rules.shocks.floor.ref,
        rules.aggregation.ref,
        rules.outlier_threshold.ref,
        rules.buckets_ref,
        rules.nmd.caps_ref,
        rules.nii.ref,
    ]
    assert all(ref.strip() for ref in refs)


def _broken(change) -> dict:
    raw = PRA.model_dump()
    change(raw)
    return raw


def _midpoint_outside(raw):
    raw["buckets"][3]["midpoint_years"] = 0.9  # 3M-6M bucket


def _core_above_cap(raw):
    raw["nmd"]["categories"]["retail_stable_deposits"]["core_share"] = 0.95


def _years_above_uk_average(raw):
    raw["nmd"]["max_average_years"] = 3.5  # stable deposits use 4.0


def _floor_without_start(raw):
    raw["shocks"]["floor"]["start_bp"] = None


def _unknown_nii_scenario(raw):
    raw["shocks"]["scenarios"] = ["steepener"]


def _edges_not_rising(raw):
    raw["buckets"][5]["up_to_years"] = 0.6


@pytest.mark.parametrize(
    "change",
    [
        _midpoint_outside,
        _core_above_cap,
        _years_above_uk_average,
        _floor_without_start,
        _unknown_nii_scenario,
        _edges_not_rising,
    ],
)
def test_broken_rulebooks_are_rejected(change):
    with pytest.raises(ValidationError):
        IrrbbRules.model_validate(_broken(change))


def test_swap_treatment(rules):
    swap = rules.derivatives["interest_rate_swaps"]
    assert (swap.direction, swap.floating_reset_days) == ("pay_fixed", 90)
    assert swap.ref.strip()
