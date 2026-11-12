"""LCR calculation: caps proven against the Basel formula, caps always hold, edge cases."""

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from riskplatform.config import load_liquidity_rules
from riskplatform.metrics.lcr import apply_caps, calculate_lcr

RULES_DIR = Path(__file__).resolve().parents[1] / "config" / "liquidity"
BASEL = load_liquidity_rules(RULES_DIR / "basel.yaml")
PRA = load_liquidity_rules(RULES_DIR / "pra.yaml")
BASEL_CAPS = {"level2_cap": 0.40, "level2b_cap": 0.15, "level1_covered_cap": None}
UK_CAPS = {"level2_cap": 0.40, "level2b_cap": 0.15, "level1_covered_cap": 0.70}


def basel_formula(level1: float, level2a: float, level2b: float) -> float:
    """HQLA by the Basel cap adjustments (LCR30.35-30.39), as worked in the example document."""
    adj15 = max(level2b - 15 / 85 * (level1 + level2a), level2b - 15 / 60 * level1, 0.0)
    adj40 = max(level2a + level2b - adj15 - 2 / 3 * level1, 0.0)
    return level1 + level2a + level2b - adj15 - adj40


def test_caps_equal_the_basel_formula_on_random_portfolios():
    rng = np.random.default_rng(0)
    for level1, level2a, level2b in rng.uniform(0, 1000, size=(5000, 3)) * rng.integers(
        0, 2, size=(5000, 3)
    ):  # some layers zero, to cover every branch
        _, counted_2a, counted_2b = apply_caps(level1, 0.0, level2a, level2b, **BASEL_CAPS)
        total = level1 + counted_2a + counted_2b
        assert total == pytest.approx(basel_formula(level1, level2a, level2b), abs=1e-9)


def test_caps_always_hold_on_random_uk_portfolios():
    rng = np.random.default_rng(1)
    for level1, covered, level2a, level2b in rng.uniform(1, 1000, size=(5000, 4)):
        c, a, b = apply_caps(level1, covered, level2a, level2b, **UK_CAPS)
        total = level1 + c + a + b
        assert 0 <= c <= covered and 0 <= a <= level2a and 0 <= b <= level2b
        assert b <= 0.15 * total + 1e-9
        assert a + b <= 0.40 * total + 1e-9
        assert level1 >= 0.30 * total - 1e-9


@pytest.mark.parametrize(
    ("held", "caps", "counted", "total"),
    [
        # Basel 40% cap: Level 2A cut to 2/3 of Level 1.
        ((100, 0, 200, 0), BASEL_CAPS, (0, 200 / 3, 0), 100 + 200 / 3),
        # Level 2B alone: cut to 15/85 of Level 1 (also inside the 40% cap).
        ((100, 0, 0, 100), BASEL_CAPS, (0, 0, 1500 / 85), 100 + 1500 / 85),
        # UK 70% cap: covered bonds cut to 70/30 of other Level 1.
        ((100, 400, 0, 0), UK_CAPS, (700 / 3, 0, 0), 100 + 700 / 3),
    ],
)
def test_hand_cases_where_other_caps_bite(held, caps, counted, total):
    result = apply_caps(*held, **caps)
    assert result == pytest.approx(counted)
    assert held[0] + sum(result) == pytest.approx(total)


def test_covered_bonds_without_a_cap_are_an_error():
    with pytest.raises(ValueError, match="covered"):
        apply_caps(100, 50, 0, 0, **BASEL_CAPS)


def _positions(*rows) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["category", "side", "balance_mn", "residual_days"])
    return frame.astype({"balance_mn": float})


def test_thirty_day_boundary():
    due_day_30 = calculate_lcr(_positions(("term_debt", "liability", 100, 30)), BASEL)
    due_day_31 = calculate_lcr(_positions(("term_debt", "liability", 100, 31)), BASEL)
    assert (due_day_30.outflows, due_day_31.outflows) == (100.0, 0.0)


def test_inflow_cap_only_applies_when_it_bites():
    result = calculate_lcr(
        _positions(
            ("govt_bonds", "asset", 100, 400),
            ("financial_institution_deposits", "liability", 100, 5),  # outflow 100
            ("corporate_loans", "asset", 40, 10),  # inflow 20, below the 75 cap
        ),
        PRA,
    )
    assert (result.inflows, result.inflows_capped, result.net_outflows) == (20.0, 20.0, 80.0)
    assert result.ratio == pytest.approx(100 / 80)


def test_ratio_when_nothing_flows_out():
    only_hqla = calculate_lcr(_positions(("govt_bonds", "asset", 100, 400)), BASEL)
    assert only_hqla.ratio == math.inf
    nothing = calculate_lcr(_positions(), BASEL)
    assert math.isnan(nothing.ratio)


def test_unmapped_category_is_an_error():
    with pytest.raises(ValueError, match="no LCR treatment"):
        calculate_lcr(_positions(("crypto", "asset", 100, 1)), BASEL)


def test_missing_column_is_an_error():
    with pytest.raises(ValueError, match="residual_days"):
        calculate_lcr(_positions().drop(columns="residual_days"), BASEL)
