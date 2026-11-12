"""Data contracts: the exact shape every validated table must have."""

import json
from typing import get_args

import pandas as pd
import pandera.pandas as pa

from riskplatform.config import GeneratorConfig, RateType, Side

SIDES = list(get_args(Side))
RATE_TYPES = list(get_args(RateType))
RAW_TABLES = ["positions", "rates", "fx"]


def positions_schema(cfg: GeneratorConfig) -> pa.DataFrameSchema:
    """Contract for the daily positions table (one row per position per date)."""
    side_by_category = cfg.side_by_category
    return pa.DataFrameSchema(
        columns={
            "date": pa.Column("datetime64[ns]"),
            "position_id": pa.Column(str),
            "entity": pa.Column(str, pa.Check.isin(cfg.entity_names)),
            "currency": pa.Column(str, pa.Check.isin(cfg.currencies)),
            "category": pa.Column(str, pa.Check.isin(cfg.category_names)),
            "side": pa.Column(str, pa.Check.isin(SIDES)),
            "balance_mn": pa.Column(float, pa.Check.gt(0)),
            "rate_type": pa.Column(str, pa.Check.isin(RATE_TYPES)),
            "interest_rate": pa.Column(float, pa.Check.in_range(-0.05, 0.25)),
            "residual_days": pa.Column(int, pa.Check.ge(1)),
            "reprice_days": pa.Column(int, pa.Check.ge(1)),
        },
        checks=[
            pa.Check(
                lambda df: df["reprice_days"] <= df["residual_days"],
                error="reprice_days must not exceed residual_days",
            ),
            pa.Check(
                lambda df: (
                    (df["rate_type"] != "fixed") | (df["reprice_days"] == df["residual_days"])
                ),
                error="fixed-rate positions must reprice at maturity",
            ),
            pa.Check(
                lambda df: df["side"] == df["category"].map(side_by_category),
                error="side does not match the category's side in config",
            ),
        ],
        unique=["date", "position_id"],
        strict=True,
        coerce=True,
    )


def rates_schema(cfg: GeneratorConfig) -> pa.DataFrameSchema:
    """Contract for the market rates table (one row per date, currency and tenor)."""
    return pa.DataFrameSchema(
        columns={
            "date": pa.Column("datetime64[ns]"),
            "currency": pa.Column(str, pa.Check.isin(cfg.currencies)),
            "tenor_years": pa.Column(float, pa.Check.gt(0)),
            "rate": pa.Column(float, pa.Check.in_range(-0.05, 0.25)),
        },
        unique=["date", "currency", "tenor_years"],
        strict=True,
        coerce=True,
    )


def fx_schema(cfg: GeneratorConfig) -> pa.DataFrameSchema:
    """Contract for FX rates (one row per date and currency, the reporting currency included).

    to_reporting is the reporting currency per one unit of the currency, so converting any
    amount is amount x to_reporting; the reporting currency's own row is exactly 1.
    """
    reporting = cfg.fx.reporting_currency
    return pa.DataFrameSchema(
        columns={
            "date": pa.Column("datetime64[ns]"),
            "currency": pa.Column(str, pa.Check.isin(cfg.currencies)),
            "to_reporting": pa.Column(float, pa.Check.gt(0)),
        },
        checks=[
            pa.Check(
                lambda df: (df["currency"] != reporting) | (df["to_reporting"] == 1.0),
                error=f"the reporting currency ({reporting}) must convert at exactly 1",
            ),
        ],
        unique=["date", "currency"],
        strict=True,
        coerce=True,
    )


MARKET_SOURCES = ["fred", "ecb", "boe"]


def market_curves_schema(currencies: list[str]) -> pa.DataFrameSchema:
    """Contract for fetched real yield curves (one row per date, currency and source tenor).

    Tenors are each source's own points; the generator interpolates them onto its tenor grid.
    """
    return pa.DataFrameSchema(
        columns={
            "date": pa.Column("datetime64[ns]"),
            "currency": pa.Column(str, pa.Check.isin(currencies)),
            "tenor_years": pa.Column(float, pa.Check.gt(0)),
            "rate": pa.Column(float, pa.Check.in_range(-0.05, 0.25)),
            "source": pa.Column(str, pa.Check.isin(MARKET_SOURCES)),
        },
        unique=["date", "currency", "tenor_years"],
        strict=True,
        coerce=True,
    )


def _is_json_object(text: str) -> bool:
    """True if the text is a JSON object, e.g. '{"balance_mn": -5.0}'."""
    try:
        return isinstance(json.loads(text), dict)
    except (TypeError, ValueError):
        return False


def quarantine_schema() -> pa.DataFrameSchema:
    """Contract for quarantined records (one row per failed record per failed check)."""
    return pa.DataFrameSchema(
        columns={
            "run_id": pa.Column(str),
            "run_at": pa.Column(pd.DatetimeTZDtype(unit="ns", tz="UTC")),
            "table": pa.Column(str, pa.Check.isin(RAW_TABLES)),
            "source_file": pa.Column(str),
            "row_number": pa.Column(int, pa.Check.ge(0)),
            "check": pa.Column(str),
            "column": pa.Column(str, nullable=True),
            "failure_value": pa.Column(str, nullable=True),
            "record": pa.Column(
                str, pa.Check(lambda s: s.map(_is_json_object), error="record must be JSON")
            ),
        },
        unique=["run_id", "source_file", "row_number", "check", "column"],
        strict=True,
        coerce=True,
    )
