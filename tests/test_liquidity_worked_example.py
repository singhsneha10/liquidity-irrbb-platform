"""The hand-worked example in docs/liquidity_worked_example.md: the code must reproduce it.

The expected values were worked out by hand (and rechecked with plain arithmetic) before any
calculation code existed. While each calculation was being built, its tests were marked strict
xfail, so they had to fail until the code reproduced these answers exactly. The LCR (step 4.3)
and the NSFR (step 4.4) now both do, and the answers are enforced.
"""

from pathlib import Path

import pandas as pd
import pytest

from riskplatform.config import load_liquidity_rules

RULES_DIR = Path(__file__).resolve().parents[1] / "config" / "liquidity"

# (id, category, side, balance_mn, residual_days), as in the document.
PORTFOLIO = [
    ("A1", "central_bank_reserves", "asset", 100, 1),
    ("A2", "govt_bonds", "asset", 200, 400),
    ("A3", "covered_bonds", "asset", 100, 900),
    ("A4", "corporate_bonds", "asset", 150, 20),
    ("A5", "corporate_loans", "asset", 600, 10),
    ("A6", "corporate_loans", "asset", 400, 800),
    ("A7", "retail_mortgages", "asset", 1500, 5000),
    ("A8", "retail_mortgages", "asset", 100, 25),
    ("L1", "retail_stable_deposits", "liability", 1200, 1),
    ("L2", "retail_less_stable_deposits", "liability", 600, 1),
    ("L3", "wholesale_operational", "liability", 200, 10),
    ("L4", "wholesale_non_operational", "liability", 150, 20),
    ("L5", "wholesale_non_operational", "liability", 100, 90),
    ("L6", "financial_institution_deposits", "liability", 80, 5),
    ("L7", "financial_institution_deposits", "liability", 50, 250),
    ("L8", "term_debt", "liability", 100, 15),
    ("L9", "term_debt", "liability", 150, 700),
    ("U1", "undrawn_committed_lines", "off_balance_sheet", 200, 400),
]

# The "Answers the code must reproduce" table.
EXPECTED = {
    "basel": {
        "hqla_before_caps": 460.0,
        "hqla": 452.941176,
        "outflows": 430.0,
        "inflows": 350.0,
        "inflows_capped": 322.5,
        "net_outflows": 107.5,
        "lcr": 4.213406,
        "asf": 2600.0,
        "rsf": 1775.0,
        "nsfr": 1.464789,
    },
    "pra": {
        "hqla_before_caps": 468.0,
        "hqla": 462.352941,
        "outflows": 430.0,
        "inflows": 350.0,
        "inflows_capped": 322.5,
        "net_outflows": 107.5,
        "lcr": 4.300958,
        "asf": 2600.0,
        "rsf": 1757.0,
        "nsfr": 1.479795,
    },
}
LCR_FIELDS = ["hqla_before_caps", "hqla", "outflows", "inflows", "inflows_capped", "net_outflows"]


@pytest.fixture
def portfolio() -> pd.DataFrame:
    return pd.DataFrame(
        PORTFOLIO, columns=["id", "category", "side", "balance_mn", "residual_days"]
    ).astype({"balance_mn": float})


def test_portfolio_matches_the_document(portfolio):
    totals = portfolio.groupby("side")["balance_mn"].sum()
    assert (totals["asset"], totals["liability"]) == (3150, 2630)  # equity 520


@pytest.mark.parametrize("regime", ["basel", "pra"])
def test_lcr_matches_the_hand_calculation(portfolio, regime):
    from riskplatform.metrics.lcr import calculate_lcr

    result = calculate_lcr(portfolio, load_liquidity_rules(RULES_DIR / f"{regime}.yaml"))
    for field in LCR_FIELDS:
        assert getattr(result, field) == pytest.approx(EXPECTED[regime][field], abs=1e-6), field
    assert result.ratio == pytest.approx(EXPECTED[regime]["lcr"], abs=1e-6)


@pytest.mark.parametrize("regime", ["basel", "pra"])
def test_nsfr_matches_the_hand_calculation(portfolio, regime):
    from riskplatform.metrics.nsfr import calculate_nsfr

    result = calculate_nsfr(portfolio, load_liquidity_rules(RULES_DIR / f"{regime}.yaml"))
    assert result.asf == pytest.approx(EXPECTED[regime]["asf"], abs=1e-6)
    assert result.rsf == pytest.approx(EXPECTED[regime]["rsf"], abs=1e-6)
    assert result.ratio == pytest.approx(EXPECTED[regime]["nsfr"], abs=1e-6)
