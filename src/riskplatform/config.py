"""Typed, validated configuration for the synthetic data generator."""

import itertools
import math
import os
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


class RateBounds(_Strict):
    """Sanity bounds on any interest rate or yield (decimals): a value outside is an error.

    Not a market view: they exist to catch values no real rate could take, such as 4.5 typed
    for 4.5% (0.045). See the comments in config/generator.yaml for the chosen values.
    """

    low: float
    high: float

    @model_validator(mode="after")
    def _check(self) -> "RateBounds":
        if not self.low < self.high:
            raise ValueError(f"rate_bounds: low must be below high, got {self.low}, {self.high}")
        return self


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
    # Optional: this category's own weight in non-home currencies (else foreign_currency_weight)
    foreign_currency_weight: float | None = Field(default=None, gt=0, le=1)

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
    fx_file: Path  # saved Bank of England FX rates (GBP per unit)
    max_fill_days: int = Field(ge=0)  # calendar days a missing curve or rate may be filled from


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


class FxConfig(_Strict):
    """Synthetic FX: reporting currency per one unit of each other currency, as a random walk."""

    reporting_currency: str
    start_to_reporting: dict[str, float]
    daily_vol: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_rates(self) -> "FxConfig":
        if any(rate <= 0 for rate in self.start_to_reporting.values()):
            raise ValueError("fx.start_to_reporting rates must be above 0")
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
    rate_bounds: RateBounds  # data contract: every rate in positions and curves lies within
    rates: RatesConfig
    fx: FxConfig

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
        bounds = self.rate_bounds
        if not bounds.low <= self.interest_rate_floor < bounds.high:
            raise ValueError(
                f"interest_rate_floor {self.interest_rate_floor} is outside rate_bounds "
                f"[{bounds.low}, {bounds.high}]: every floored rate would break the contract"
            )
        reporting = self.fx.reporting_currency
        if reporting not in self.currencies:
            raise ValueError(f"fx.reporting_currency {reporting} is not in {self.currencies}")
        others = set(self.currencies) - {reporting}
        if set(self.fx.start_to_reporting) != others:
            raise ValueError(
                f"fx.start_to_reporting must give exactly {sorted(others)}, "
                f"got {sorted(self.fx.start_to_reporting)}"
            )
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


class OutlierThresholds(_Strict):
    """Review triggers for outlier warnings (see config/quality.yaml); not regulatory limits."""

    balance_jump: float = Field(gt=0)
    rate_gap_to_curve: float = Field(gt=0)
    concentration_share: float = Field(gt=0, le=1)
    concentration_exclude_categories: list[str]
    curve_move: float = Field(gt=0)


class ReconciliationSettings(_Strict):
    """How far arrived totals may differ from the source's control totals before the run stops."""

    relative_tolerance: float = Field(gt=0, lt=1)  # x max(1, |sent|)


class ReportSettings(_Strict):
    max_markdown_rows: int = Field(gt=0)  # longer lists are cut in the Markdown, never the JSON


class QualityConfig(_Strict):
    outliers: OutlierThresholds
    reconciliation: ReconciliationSettings
    report: ReportSettings


def load_quality_config(path: str | Path) -> QualityConfig:
    """Read a YAML file and return a validated QualityConfig."""
    return QualityConfig.model_validate(_read_yaml(path))


HqlaLevel = Literal["1", "1_covered", "2A", "2B"]  # 1_covered: UK Level 1 covered bonds
Regime = Literal["basel", "pra"]


class Referenced(_Strict):
    """A regulatory parameter and the paragraph or Article it comes from."""

    value: float
    ref: str


class HqlaTreatment(_Strict):
    level: HqlaLevel
    haircut: float = Field(ge=0, lt=1)
    ref: str
    assumption: str | None = None


class FlowTreatment(_Strict):
    """An LCR outflow or inflow rate; within_horizon_only counts only amounts due in 30 days."""

    rate: float = Field(ge=0, le=1)
    within_horizon_only: bool
    ref: str
    assumption: str | None = None


class FactorBand(_Strict):
    """An NSFR factor for residual maturities below below_days (None: everything longer)."""

    below_days: int | None = Field(default=None, gt=0)
    factor: float = Field(ge=0, le=1)
    ref: str
    assumption: str | None = None


def _check_bands(label: str, bands: list[FactorBand]) -> None:
    """Every maturity must fall in exactly one band: rising limits, only the last open-ended."""
    limits = [band.below_days for band in bands]
    if not bands or limits[-1] is not None or None in limits[:-1]:
        raise ValueError(f"{label}: only the last band may (and must) have no below_days")
    if any(later <= earlier for earlier, later in itertools.pairwise(limits[:-1])):
        raise ValueError(f"{label}: below_days must rise strictly")


class LcrRules(_Strict):
    horizon_days: Referenced
    minimum: Referenced
    inflow_cap: Referenced
    level2_cap: Referenced
    level2b_cap: Referenced
    level1_covered_bond_cap: Referenced | None
    hqla: dict[str, HqlaTreatment]
    outflows: dict[str, FlowTreatment]
    inflows: dict[str, FlowTreatment]

    @model_validator(mode="after")
    def _check(self) -> "LcrRules":
        both = set(self.hqla) & set(self.inflows)
        if both:  # maturing HQLA are not inflows (LCR40.88; UK LCR Art 32(6))
            raise ValueError(f"categories cannot be both HQLA and inflows: {sorted(both)}")
        covered = any(t.level == "1_covered" for t in self.hqla.values())
        if covered != (self.level1_covered_bond_cap is not None):
            raise ValueError("Level 1 covered bonds and level1_covered_bond_cap go together")
        return self


class NsfrRules(_Strict):
    minimum: Referenced
    asf: dict[str, list[FactorBand]]
    rsf: dict[str, list[FactorBand]]

    @model_validator(mode="after")
    def _check(self) -> "NsfrRules":
        if "equity" not in self.asf:
            raise ValueError("nsfr.asf must include 'equity' (capital counts as stable funding)")
        for side, table in (("asf", self.asf), ("rsf", self.rsf)):
            for category, bands in table.items():
                _check_bands(f"nsfr.{side}.{category}", bands)
        return self


class LiquidityRules(_Strict):
    """One regulatory rulebook's LCR and NSFR parameters (config/liquidity/<regime>.yaml)."""

    regime: Regime
    name: str
    lcr: LcrRules
    nsfr: NsfrRules


def load_liquidity_rules(path: str | Path) -> LiquidityRules:
    """Read a YAML file and return validated LiquidityRules."""
    return LiquidityRules.model_validate(_read_yaml(path))


class LadderBucket(_Strict):
    label: str
    up_to_days: int | None = Field(default=None, gt=0)  # last day included; None: open-ended


class LadderConfig(_Strict):
    """Maturity ladder buckets (config/liquidity/ladder.yaml)."""

    buckets: list[LadderBucket] = Field(min_length=1)
    # Off-balance-sheet categories that are contingent liquidity needs (e.g. undrawn lines);
    # other off-balance-sheet items (e.g. swap notionals) are not cash flows and are left out.
    contingent_categories: list[str]

    @model_validator(mode="after")
    def _check_buckets(self) -> "LadderConfig":
        limits = [bucket.up_to_days for bucket in self.buckets]
        if limits[-1] is not None or None in limits[:-1]:
            raise ValueError("only the last ladder bucket may (and must) be open-ended")
        if any(later <= earlier for earlier, later in itertools.pairwise(limits[:-1])):
            raise ValueError("ladder up_to_days must rise strictly")
        labels = [bucket.label for bucket in self.buckets]
        if len(set(labels)) != len(labels):
            raise ValueError("ladder bucket labels must be unique")
        return self


def load_ladder_config(path: str | Path) -> LadderConfig:
    """Read a YAML file and return a validated LadderConfig."""
    return LadderConfig.model_validate(_read_yaml(path))


ShockScenario = Literal[
    "parallel_up", "parallel_down", "steepener", "flattener", "short_up", "short_down"
]


class ShockSizes(_Strict):
    """Absolute shock sizes for one currency, in basis points."""

    parallel: float = Field(gt=0)
    short: float = Field(gt=0)
    long: float = Field(gt=0)


class Twist(_Strict):
    """Weights on the short and long shocks for the steepener or flattener."""

    short: float
    long: float
    ref: str


class PostShockFloor(_Strict):
    """Optional floor on shocked rates: start_bp at tenor 0, rising linearly to 0 at zero_at_years."""

    applied: bool
    start_bp: float | None = Field(default=None, le=0)
    zero_at_years: float | None = Field(default=None, gt=0)
    ref: str
    assumption: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "PostShockFloor":
        given = self.start_bp is not None and self.zero_at_years is not None
        if self.applied != given or (self.start_bp is None) != (self.zero_at_years is None):
            raise ValueError(
                "an applied floor needs start_bp and zero_at_years; a floor not applied has neither"
            )
        return self


class CurveShocks(_Strict):
    scenarios: list[ShockScenario] = Field(min_length=1)
    scenarios_ref: str
    sizes_ref: str
    sizes_bp: dict[str, ShockSizes]
    decay_years: Referenced
    steepener: Twist
    flattener: Twist
    floor: PostShockFloor


class GainsWeight(_Strict):
    gains_weight: float = Field(ge=0, le=1)
    ref: str


class OutlierThreshold(_Strict):
    value: float = Field(gt=0, lt=1)
    ref: str
    assumption: str | None = None


class RepricingBucket(_Strict):
    label: str
    up_to_years: float | None = Field(default=None, gt=0)  # upper edge; None: open-ended
    midpoint_years: float = Field(gt=0)


class NmdCap(_Strict):
    core_max: float = Field(gt=0, le=1)
    years_max: float = Field(gt=0)


class NmdTreatment(_Strict):
    type: str
    core_share: float = Field(ge=0, le=1)
    core_years: float = Field(gt=0)
    assumption: str | None = None


class NmdRules(_Strict):
    caps_ref: str
    caps: dict[str, NmdCap]
    max_average_years: float | None = Field(default=None, gt=0)
    categories: dict[str, NmdTreatment]

    @model_validator(mode="after")
    def _check(self) -> "NmdRules":
        for name, treatment in self.categories.items():
            cap = self.caps.get(treatment.type)
            if cap is None:
                raise ValueError(f"nmd {name}: unknown type {treatment.type!r}")
            if treatment.core_share > cap.core_max or treatment.core_years > cap.years_max:
                raise ValueError(f"nmd {name}: core share or years exceed the {treatment.type} cap")
            limit = self.max_average_years
            if limit is not None and treatment.core_years > limit:
                raise ValueError(f"nmd {name}: core_years above max_average_years {limit}")
        return self


class NiiRules(_Strict):
    scenarios: list[ShockScenario] = Field(min_length=1)
    horizon_months: int = Field(gt=0)
    ref: str
    assumption: str | None = None


class SwapTreatment(_Strict):
    """How an interest rate swap reprices: a fixed leg at maturity, a floating leg at reset."""

    direction: Literal["pay_fixed", "receive_fixed"]
    floating_reset_days: int = Field(gt=0)
    ref: str
    assumption: str | None = None


class IrrbbRules(_Strict):
    """One rulebook's IRRBB parameters (config/irrbb/<regime>.yaml)."""

    regime: Regime
    name: str
    shocks: CurveShocks
    aggregation: GainsWeight
    outlier_threshold: OutlierThreshold
    buckets_ref: str
    buckets: list[RepricingBucket] = Field(min_length=2)
    nmd: NmdRules
    nii: NiiRules
    derivatives: dict[str, SwapTreatment]  # category -> swap treatment

    @model_validator(mode="after")
    def _check(self) -> "IrrbbRules":
        edges = [bucket.up_to_years for bucket in self.buckets]
        if edges[-1] is not None or None in edges[:-1]:
            raise ValueError("only the last repricing bucket may (and must) be open-ended")
        if any(later <= earlier for earlier, later in itertools.pairwise(edges[:-1])):
            raise ValueError("repricing bucket edges must rise strictly")
        lower = 0.0
        for bucket in self.buckets:
            upper = bucket.up_to_years if bucket.up_to_years is not None else math.inf
            if not lower <= bucket.midpoint_years <= upper:
                raise ValueError(f"bucket {bucket.label}: midpoint outside the bucket")
            lower = upper
        unknown = set(self.nii.scenarios) - set(self.shocks.scenarios)
        if unknown:
            raise ValueError(f"nii scenarios not among the shock scenarios: {sorted(unknown)}")
        return self


def load_irrbb_rules(path: str | Path) -> IrrbbRules:
    """Read a YAML file and return validated IrrbbRules."""
    return IrrbbRules.model_validate(_read_yaml(path))


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


class BoeFxSource(_Strict):
    """Bank of England Database FX series, each quoted as units of currency per reporting unit."""

    reporting_currency: str
    url: str  # template with {series} and {start} placeholders
    series: dict[str, str] = Field(min_length=1)  # currency -> series code
    output_file: Path
    provenance_file: Path

    @model_validator(mode="after")
    def _check(self) -> "BoeFxSource":
        if "{series}" not in self.url or "{start}" not in self.url:
            raise ValueError(f"url must contain {{series}} and {{start}}: {self.url}")
        if self.reporting_currency in self.series:
            raise ValueError("the reporting currency needs no FX series (its rate is 1)")
        return self


class MarketRatesConfig(_Strict):
    start_date: date
    output_file: Path
    provenance_file: Path
    user_agent: str
    timeout_seconds: float = Field(gt=0)
    rate_bounds: RateBounds  # fetched curves are checked against these (keep = generator.yaml)
    fred: SeriesSource
    ecb: SeriesSource
    boe: BoeSource
    boe_fx: BoeFxSource

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


StorageBackend = Literal["local", "s3"]
STORAGE_ENV = {"backend": "RISKPLATFORM_STORAGE_BACKEND", "bucket": "RISKPLATFORM_S3_BUCKET"}


class S3Settings(_Strict):
    bucket: str | None = None  # usually set by RISKPLATFORM_S3_BUCKET
    prefix: str = Field(min_length=1)
    region: str = Field(min_length=1)


class StorageConfig(_Strict):
    """Where results are published (config/storage.yaml; environment variables override it)."""

    backend: StorageBackend
    s3: S3Settings

    @model_validator(mode="after")
    def _check(self) -> "StorageConfig":
        if self.backend == "s3" and not self.s3.bucket:
            raise ValueError(f"backend is s3 but no bucket is set (set {STORAGE_ENV['bucket']})")
        return self


def load_storage_config(path: str | Path, env: dict[str, str] | None = None) -> StorageConfig:
    """Read the storage YAML, apply the environment-variable overrides, then validate."""
    env = dict(os.environ) if env is None else env
    raw = _read_yaml(path)
    if env.get(STORAGE_ENV["backend"]):
        raw["backend"] = env[STORAGE_ENV["backend"]]
    if env.get(STORAGE_ENV["bucket"]):
        raw["s3"]["bucket"] = env[STORAGE_ENV["bucket"]]
    return StorageConfig.model_validate(raw)
