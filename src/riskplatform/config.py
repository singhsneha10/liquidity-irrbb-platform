"""Typed, validated configuration for the synthetic data generator."""

import itertools
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
    spread: float  # added to the reference curve rate (decimal; negative for most deposits)

    @model_validator(mode="after")
    def _check_tenor(self) -> "Category":
        _check_day_range(f"category {self.name} tenor_days", self.tenor_days)
        return self


class CurveParams(_Strict):
    """Starting shape of one currency's yield curve: short-end and long-end rates."""

    short_rate: float
    long_rate: float


RatesSource = Literal["synthetic", "real"]


class RealCurves(_Strict):
    """Where real curves are read from (a file saved by the market_rates fetch, never live)."""

    curves_file: Path
    max_fill_days: int = Field(ge=0)  # calendar days a missing curve may be filled from before


class RatesConfig(_Strict):
    source: RatesSource
    real: RealCurves | None = None
    tenors_years: list[float]
    curve_decay_years: float = Field(gt=0)
    daily_vol: float = Field(ge=0)
    curves: dict[str, CurveParams]

    @model_validator(mode="after")
    def _check_tenors(self) -> "RatesConfig":
        tenors = self.tenors_years
        increasing = all(later > earlier for earlier, later in itertools.pairwise(tenors))
        if not tenors or tenors[0] <= 0 or not increasing:
            raise ValueError("tenors_years must be non-empty, positive and strictly increasing")
        if self.source == "real" and self.real is None:
            raise ValueError("rates.source is 'real', so a rates.real section is required")
        return self


class GeneratorConfig(_Strict):
    seed: int
    start_date: date
    n_days: int = Field(gt=0)
    currencies: list[str]
    foreign_currency_weight: float = Field(gt=0, le=1)
    balance_log_sigma: float = Field(ge=0)
    equity_ratio: float = Field(ge=0, lt=1)
    entities: list[Entity]
    categories: list[Category]
    floating_reprice_days: tuple[int, int]
    spread_sd: float = Field(ge=0)
    interest_rate_floor: float
    daily_balance_drift: float
    daily_balance_vol: float = Field(ge=0)
    rates: RatesConfig

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
        if not {"asset", "liability"} <= {c.side for c in self.categories}:
            raise ValueError("categories must include at least one asset and one liability")
        if set(self.rates.curves) != set(self.currencies):
            raise ValueError(
                f"rates.curves must define exactly the currencies {self.currencies}, "
                f"got {sorted(self.rates.curves)}"
            )
        _check_day_range("floating_reprice_days", self.floating_reprice_days)
        return self


def _read_yaml(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _merge(base: dict, override: dict) -> dict:
    """Override's values replace base's; nested sections are merged key by key."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_generator_yaml(path: Path) -> dict:
    """Read a generator YAML file; a 'base:' key names a file (relative to this one) to extend."""
    raw = _read_yaml(path)
    base_name = raw.pop("base", None)
    if base_name is None:
        return raw
    return _merge(_read_generator_yaml(path.parent / base_name), raw)


def load_generator_config(path: str | Path) -> GeneratorConfig:
    """Read a YAML file (optionally extending a base file) and return a GeneratorConfig."""
    return GeneratorConfig.model_validate(_read_generator_yaml(Path(path)))


class SeriesSource(_Strict):
    """A source that publishes one series per tenor (FRED, ECB): series code -> tenor in years."""

    currency: str
    url: str  # template with {series} and {start} placeholders
    series: dict[str, float] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_template(self) -> "SeriesSource":
        if "{series}" not in self.url or "{start}" not in self.url:
            raise ValueError(f"url must contain {{series}} and {{start}}: {self.url}")
        return self


class BoeSource(_Strict):
    """The Bank of England yield curve archives (zip files of Excel workbooks)."""

    currency: str
    archive_urls: list[str] = Field(min_length=1)
    workbook_name_contains: str
    sheets: list[str] = Field(min_length=1)


class MarketRatesConfig(_Strict):
    start_date: date
    output_file: Path
    provenance_file: Path
    user_agent: str
    timeout_seconds: float = Field(gt=0)
    fred: SeriesSource
    ecb: SeriesSource
    boe: BoeSource

    @property
    def currencies(self) -> list[str]:
        return [self.fred.currency, self.ecb.currency, self.boe.currency]

    @model_validator(mode="after")
    def _check_currencies(self) -> "MarketRatesConfig":
        if len(set(self.currencies)) != len(self.currencies):
            raise ValueError(f"each source must cover a different currency: {self.currencies}")
        return self


def load_market_rates_config(path: str | Path) -> MarketRatesConfig:
    """Read a YAML file and return a validated MarketRatesConfig."""
    return MarketRatesConfig.model_validate(_read_yaml(path))
