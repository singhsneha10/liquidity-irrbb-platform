"""Data contracts: the exact shape every validated table must have."""

from typing import get_args

import pandera.pandas as pa

from riskplatform.config import GeneratorConfig, RateType, Side

SIDES = list(get_args(Side))
RATE_TYPES = list(get_args(RateType))


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
                lambda df: (df["rate_type"] != "fixed") | (df["reprice_days"] == df["residual_days"]),
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
