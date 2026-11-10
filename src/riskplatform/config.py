"""Typed, validated configuration for the synthetic data generator."""

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Side = Literal["asset", "liability", "off_balance_sheet"]
RateType = Literal["fixed", "floating"]


class _Strict(BaseModel):
    """Base class: values cannot change after loading, and unknown keys are errors."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _check_day_range(label: str, days: tuple[int, int]) -> None:
    low, high = days
    if not 1 <= low <= high:
        raise ValueError(f"{label}: expected 1 <= low <= high, got {days}")


class Entity(_Strict):
    name: str
    home_currency: str


class Category(_Strict):
    name: str
    side: Side
    n_positions: int = Field(gt=0)
    mean_balance_mn: float = Field(gt=0)
    rate_type: RateType
    tenor_days: tuple[int, int]

    @model_validator(mode="after")
    def _check_tenor(self) -> "Category":
        _check_day_range(f"category {self.name} tenor_days", self.tenor_days)
        return self


class GeneratorConfig(_Strict):
    seed: int
    start_date: date
    n_days: int = Field(gt=0)
    currencies: list[str]
    foreign_currency_weight: float = Field(gt=0, le=1)
    entities: list[Entity]
    categories: list[Category]
    floating_reprice_days: tuple[int, int]
    interest_rate_mean: float
    interest_rate_sd: float = Field(ge=0)
    daily_balance_drift: float
    daily_balance_vol: float = Field(ge=0)

    @property
    def entity_names(self) -> list[str]:
        return [e.name for e in self.entities]

    @property
    def category_names(self) -> list[str]:
        return [c.name for c in self.categories]

    @property
    def side_by_category(self) -> dict[str, str]:
        return {c.name: c.side for c in self.categories}

    @model_validator(mode="after")
    def _check_consistency(self) -> "GeneratorConfig":
        for entity in self.entities:
            if entity.home_currency not in self.currencies:
                raise ValueError(
                    f"entity {entity.name}: home_currency {entity.home_currency} "
                    f"is not in currencies {self.currencies}"
                )
        for label, names in (("entity", self.entity_names), ("category", self.category_names)):
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate {label} names in config")
        _check_day_range("floating_reprice_days", self.floating_reprice_days)
        return self


def load_generator_config(path: str | Path) -> GeneratorConfig:
    """Read a YAML file and return a validated GeneratorConfig."""
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return GeneratorConfig.model_validate(raw)
