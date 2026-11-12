"""NSFR calculation: maturity band boundaries, equity, breakdown, edge cases."""

import math
from pathlib import Path

import pandas as pd
import pytest

from riskplatform.config import load_liquidity_rules
from riskplatform.metrics.nsfr import calculate_nsfr

RULES_DIR = Path(__file__).resolve().parents[1] / "config" / "liquidity"
BASEL = load_liquidity_rules(RULES_DIR / "basel.yaml")
PRA = load_liquidity_rules(RULES_DIR / "pra.yaml")


def _positions(*rows) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["category", "side", "balance_mn", "residual_days"])
    return frame.astype({"balance_mn": float})


def _weighted(result, category) -> float:
    rows = result.by_category[result.by_category["category"] == category]
    return float(rows["weighted"].sum())


@pytest.mark.parametrize(
    ("days", "factor"),
    [(182, 0.00), (183, 0.50), (364, 0.50), (365, 1.00)],
)
def test_asf_band_boundaries(days, factor):
    result = calculate_nsfr(
        _positions(("financial_institution_deposits", "liability", 100, days)), BASEL
    )
    assert _weighted(result, "financial_institution_deposits") == pytest.approx(100 * factor)


@pytest.mark.parametrize(("days", "factor"), [(364, 0.50), (365, 0.65)])
def test_rsf_band_boundaries(days, factor):
    result = calculate_nsfr(_positions(("retail_mortgages", "asset", 100, days)), BASEL)
    assert result.rsf == pytest.approx(100 * factor)


@pytest.mark.parametrize(("rules", "factor"), [(BASEL, 0.05), (PRA, 0.00)])
@pytest.mark.parametrize("days", [1, 5000])
def test_single_band_category_ignores_maturity(rules, factor, days):
    result = calculate_nsfr(_positions(("govt_bonds", "asset", 100, days)), rules)
    assert result.rsf == pytest.approx(100 * factor)


def test_equity_is_counted_once_from_on_balance_sheet_items():
    result = calculate_nsfr(
        _positions(
            ("govt_bonds", "asset", 100, 400),
            ("financial_institution_deposits", "liability", 60, 10),  # ASF 0%
            ("undrawn_committed_lines", "off_balance_sheet", 200, 400),  # RSF 5%, not equity
        ),
        BASEL,
    )
    assert result.equity == pytest.approx(40)
    assert result.asf == pytest.approx(40)  # only the equity
    assert result.rsf == pytest.approx(100 * 0.05 + 200 * 0.05)


def test_negative_equity_is_reported_but_gives_no_stable_funding():
    result = calculate_nsfr(
        _positions(
            ("govt_bonds", "asset", 50, 400),
            ("retail_stable_deposits", "liability", 80, 1),
        ),
        BASEL,
    )
    assert result.equity == pytest.approx(-30)
    assert result.asf == pytest.approx(80 * 0.95)  # the deficit adds nothing


def test_breakdown_adds_up_to_asf_and_rsf():
    result = calculate_nsfr(
        _positions(
            ("corporate_loans", "asset", 300, 800),
            ("covered_bonds", "asset", 100, 900),
            ("term_debt", "liability", 200, 700),
            ("wholesale_operational", "liability", 100, 10),
        ),
        PRA,
    )
    table = result.by_category
    on_asf = table["side"].isin(["liability", "equity"])
    assert table.loc[on_asf, "weighted"].sum() == pytest.approx(result.asf)
    assert table.loc[~on_asf, "weighted"].sum() == pytest.approx(result.rsf)


def test_ratio_edge_cases():
    only_funding = calculate_nsfr(_positions(("term_debt", "liability", 100, 700)), BASEL)
    assert only_funding.ratio == math.inf
    assert math.isnan(calculate_nsfr(_positions(), BASEL).ratio)


def test_unmapped_category_is_an_error():
    with pytest.raises(ValueError, match="no NSFR factor"):
        calculate_nsfr(_positions(("crypto", "asset", 100, 1)), BASEL)


def test_missing_column_is_an_error():
    with pytest.raises(ValueError, match="residual_days"):
        calculate_nsfr(_positions().drop(columns="residual_days"), BASEL)
