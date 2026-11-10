import pandas as pd
import pytest
from pandera.errors import SchemaError, SchemaErrors

from riskplatform.schemas import positions_schema, rates_schema

# pandera raises SchemaError for bad values and SchemaErrors for structural problems.
VALIDATION_ERRORS = (SchemaError, SchemaErrors)


def make_positions(**overrides) -> pd.DataFrame:
    row = {
        "date": pd.Timestamp("2026-08-03"),
        "position_id": "P000001",
        "entity": "BANK_UK",
        "currency": "GBP",
        "category": "govt_bonds",
        "side": "asset",
        "balance_mn": 100.0,
        "rate_type": "fixed",
        "interest_rate": 0.03,
        "residual_days": 365,
        "reprice_days": 365,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def make_rates(**overrides) -> pd.DataFrame:
    row = {
        "date": pd.Timestamp("2026-08-03"),
        "currency": "GBP",
        "tenor_years": 1.0,
        "rate": 0.04,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_valid_positions_pass(cfg):
    positions_schema(cfg).validate(make_positions())


def test_floating_position_can_reprice_before_maturity(cfg):
    df = make_positions(rate_type="floating", reprice_days=30)
    positions_schema(cfg).validate(df)


@pytest.mark.parametrize(
    "overrides",
    [
        {"balance_mn": -1.0},
        {"currency": "JPY"},
        {"reprice_days": 400},
        {"reprice_days": 30},
        {"side": "liability"},
    ],
)
def test_invalid_positions_fail(cfg, overrides):
    with pytest.raises(VALIDATION_ERRORS):
        positions_schema(cfg).validate(make_positions(**overrides))


def test_duplicate_position_rows_fail(cfg):
    df = pd.concat([make_positions()] * 2, ignore_index=True)
    with pytest.raises(VALIDATION_ERRORS):
        positions_schema(cfg).validate(df)


def test_unexpected_column_fails(cfg):
    with pytest.raises(VALIDATION_ERRORS):
        positions_schema(cfg).validate(make_positions().assign(extra=1))


def test_valid_rates_pass(cfg):
    rates_schema(cfg).validate(make_rates())


def test_out_of_range_rate_fails(cfg):
    with pytest.raises(VALIDATION_ERRORS):
        rates_schema(cfg).validate(make_rates(rate=0.9))
