"""Liquidity Coverage Ratio (LCR) for one group of positions (e.g. one date, entity, currency).

LCR = stock of HQLA / net outflows over a 30-day stress horizon, where
net outflows = outflows - min(inflows, inflow_cap x outflows). Must be at least 100%.

Pure functions: no files are read. Every factor comes from the rulebook
(config/liquidity/basel.yaml or pra.yaml); see docs/liquidity_worked_example.md for the method
worked by hand.

Caps are applied layer by layer, keeping the highest-quality assets first:
1. (UK only) Level 1 covered bonds: at least (1 - cap) of the buffer must be other Level 1,
   so covered bonds can count up to cap / (1 - cap) x Level 1.
2. Level 2 in total: at most level2_cap of the buffer, so up to cap / (1 - cap) x (Level 1 +
   covered bonds); in the UK, also within the covered-bond limit above.
3. Level 2B: at most level2b_cap of the buffer, so up to cap / (1 - cap) x (all higher layers).
This gives the largest buffer that meets every cap. For Basel it equals the formula in
LCR30.35-30.39 (proven in tests/test_lcr.py). For the UK it implements the composition
requirements of LCR Article 17; the Annex I formula itself was not transcribed.

Simplifications: book value is used as market value; there is no short-term secured funding to
unwind, so "adjusted" and unadjusted amounts are equal; operational requirements for HQLA
(unencumbered, under treasury control) are assumed to be met.
"""

import math
from dataclasses import dataclass

import pandas as pd

from riskplatform.config import LiquidityRules

REQUIRED_COLUMNS = ["category", "side", "balance_mn", "residual_days"]


@dataclass(frozen=True)
class HqlaStock:
    """HQLA by level after haircuts: as held (before caps) and as counted (after caps)."""

    level1: float  # Level 1 other than covered bonds; never capped
    level1_covered: float
    level2a: float
    level2b: float
    level1_covered_counted: float
    level2a_counted: float
    level2b_counted: float

    @property
    def before_caps(self) -> float:
        return self.level1 + self.level1_covered + self.level2a + self.level2b

    @property
    def total(self) -> float:
        counted = self.level1_covered_counted + self.level2a_counted + self.level2b_counted
        return self.level1 + counted


@dataclass(frozen=True)
class LcrResult:
    stock: HqlaStock
    outflows: float
    inflows: float  # before the cap
    inflows_capped: float
    net_outflows: float
    ratio: float  # inf if net outflows are 0 with HQLA; nan if there is nothing at all

    @property
    def hqla_before_caps(self) -> float:
        return self.stock.before_caps

    @property
    def hqla(self) -> float:
        return self.stock.total


def _share_limit(cap: float, base: float) -> float:
    """The most a layer can add if it may be at most `cap` of (base + layer)."""
    return cap / (1.0 - cap) * base


def apply_caps(
    level1: float,
    level1_covered: float,
    level2a: float,
    level2b: float,
    *,
    level2_cap: float,
    level2b_cap: float,
    level1_covered_cap: float | None,
) -> tuple[float, float, float]:
    """Counted (covered bonds, Level 2A, Level 2B) after the caps; Level 1 is never capped."""
    covered = level1_covered
    level2_limit = _share_limit(level2_cap, level1 + covered)
    if level1_covered_cap is not None:
        covered = min(level1_covered, _share_limit(level1_covered_cap, level1))
        level2_limit = min(
            _share_limit(level2_cap, level1 + covered),
            _share_limit(level1_covered_cap, level1) - covered,
        )
    elif level1_covered:
        raise ValueError("Level 1 covered bonds need a level1_covered_bond_cap")
    counted_2a = min(level2a, level2_limit)
    counted_2b = min(
        level2b,
        level2_limit - counted_2a,
        _share_limit(level2b_cap, level1 + covered + counted_2a),
    )
    return covered, counted_2a, counted_2b


def _check(positions: pd.DataFrame, rules: LiquidityRules) -> None:
    missing = [column for column in REQUIRED_COLUMNS if column not in positions]
    if missing:
        raise ValueError(f"positions are missing columns {missing}")
    lcr = rules.lcr
    treated = set(lcr.hqla) | set(lcr.outflows) | set(lcr.inflows)
    unknown = sorted(set(positions["category"]) - treated)
    if unknown:
        raise ValueError(f"{rules.regime}: no LCR treatment for categories {unknown}")


def hqla_stock(positions: pd.DataFrame, rules: LiquidityRules) -> HqlaStock:
    """HQLA after haircuts, by level, before and after the caps."""
    lcr = rules.lcr
    held = positions[positions["category"].isin(lcr.hqla)]
    level = held["category"].map({name: t.level for name, t in lcr.hqla.items()})
    # astype(float): with no rows, map() would return a text column and the maths would fail
    haircut = held["category"].map({name: t.haircut for name, t in lcr.hqla.items()}).astype(float)
    by_level = (held["balance_mn"] * (1.0 - haircut)).groupby(level).sum()
    amounts = {key: float(by_level.get(key, 0.0)) for key in ("1", "1_covered", "2A", "2B")}
    covered_cap = lcr.level1_covered_bond_cap
    counted = apply_caps(
        amounts["1"],
        amounts["1_covered"],
        amounts["2A"],
        amounts["2B"],
        level2_cap=lcr.level2_cap.value,
        level2b_cap=lcr.level2b_cap.value,
        level1_covered_cap=covered_cap.value if covered_cap else None,
    )
    return HqlaStock(amounts["1"], amounts["1_covered"], amounts["2A"], amounts["2B"], *counted)


def _flow_total(positions: pd.DataFrame, table: dict, horizon_days: int) -> float:
    """Sum of balance x rate; where within_horizon_only, only amounts due within the horizon."""
    rows = positions[positions["category"].isin(table)]
    rate = rows["category"].map({name: t.rate for name, t in table.items()}).astype(float)
    only_due = rows["category"].map({name: t.within_horizon_only for name, t in table.items()})
    counted = ~only_due.astype(bool) | (rows["residual_days"] <= horizon_days)
    return float((rows["balance_mn"] * rate)[counted].sum())


def calculate_lcr(positions: pd.DataFrame, rules: LiquidityRules) -> LcrResult:
    """The LCR and every component, for one group of positions."""
    _check(positions, rules)
    lcr = rules.lcr
    horizon = int(lcr.horizon_days.value)
    stock = hqla_stock(positions, rules)
    outflows = _flow_total(positions, lcr.outflows, horizon)
    inflows = _flow_total(positions, lcr.inflows, horizon)
    inflows_capped = min(inflows, lcr.inflow_cap.value * outflows)
    net = outflows - inflows_capped
    if net > 0:
        ratio = stock.total / net
    else:
        ratio = math.inf if stock.total > 0 else math.nan
    return LcrResult(stock, outflows, inflows, inflows_capped, net, ratio)
