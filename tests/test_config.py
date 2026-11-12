from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from riskplatform.config import (
    GeneratorConfig,
    IrrbbRules,
    LadderConfig,
    MarketRatesConfig,
    _read_generator_yaml,
    load_generator_config,
    load_market_rates_config,
    load_quality_config,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
REAL_CONFIG_PATH = CONFIG_DIR / "generator_real.yaml"


def test_loads_with_expected_shape(cfg):
    assert cfg.n_days == 60
    assert len(cfg.entities) == 3
    assert set(cfg.currencies) == {"GBP", "USD", "EUR"}


def test_rejects_unknown_home_currency(cfg):
    raw = cfg.model_dump()
    raw["entities"][0]["home_currency"] = "JPY"
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_rejects_unknown_keys(cfg):
    raw = cfg.model_dump()
    raw["seeed"] = 1
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_rejects_duplicate_category_names(cfg):
    raw = cfg.model_dump()
    raw["categories"].append(raw["categories"][0])
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_rejects_reversed_tenor_range(cfg):
    raw = cfg.model_dump()
    raw["categories"][0]["tenor_days"] = (10, 5)
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_rejects_config_without_liabilities(cfg):
    raw = cfg.model_dump()
    raw["categories"] = [c for c in raw["categories"] if c["side"] != "liability"]
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_rejects_curve_missing_for_a_currency(cfg):
    raw = cfg.model_dump()
    del raw["rates"]["curves"]["EUR"]
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


@pytest.mark.parametrize("tenors", [[], [0.0, 1.0], [1.0, 1.0], [2.0, 1.0]])
def test_rejects_bad_tenor_lists(cfg, tenors):
    raw = cfg.model_dump()
    raw["rates"]["tenors_years"] = tenors
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_real_config_differs_from_base_only_in_rates_source(cfg):
    real = load_generator_config(REAL_CONFIG_PATH)
    assert cfg.rates.source == "synthetic"
    assert real.rates.source == "real"
    assert real.rates.real is not None
    assert real.model_dump(exclude={"rates"}) == cfg.model_dump(exclude={"rates"})
    assert real.rates.model_dump(exclude={"source", "real"}) == cfg.rates.model_dump(
        exclude={"source", "real"}
    )


def test_rejects_real_source_without_real_section(cfg):
    raw = cfg.model_dump()
    raw["rates"]["source"] = "real"
    raw["rates"]["real"] = None
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_rejects_rates_without_source(cfg):
    raw = cfg.model_dump()
    del raw["rates"]["source"]
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def _write_yaml(path: Path, content: dict) -> Path:
    path.write_text(yaml.safe_dump(content), encoding="utf-8")
    return path


def test_base_file_is_merged_key_by_key(tmp_path):
    _write_yaml(tmp_path / "a.yaml", {"x": 1, "rates": {"source": "synthetic", "tenors": [1, 2]}})
    _write_yaml(tmp_path / "b.yaml", {"base": "a.yaml", "rates": {"source": "real"}})
    child = _write_yaml(tmp_path / "c.yaml", {"base": "b.yaml", "rates": {"tenors": [5]}})
    merged = _read_generator_yaml(child)
    assert merged == {"x": 1, "rates": {"source": "real", "tenors": [5]}}  # list replaced


def test_rejects_rate_bounds_the_wrong_way_round(cfg):
    raw = cfg.model_dump()
    raw["rate_bounds"] = {"low": 0.25, "high": -0.05}
    with pytest.raises(ValidationError, match="low must be below high"):
        GeneratorConfig.model_validate(raw)


def test_rejects_interest_rate_floor_outside_the_rate_bounds(cfg):
    raw = cfg.model_dump()
    raw["interest_rate_floor"] = -0.10  # below the -5% bound: floored rates would be quarantined
    with pytest.raises(ValidationError, match="outside rate_bounds"):
        GeneratorConfig.model_validate(raw)


def test_fetch_and_generator_use_the_same_rate_bounds(cfg):
    """Saved curves are checked on download and again when read: the two bounds must agree."""
    market = load_market_rates_config(CONFIG_DIR / "market_rates.yaml")
    assert market.rate_bounds == cfg.rate_bounds


def test_quality_config_loads_every_setting():
    quality = load_quality_config(CONFIG_DIR / "quality.yaml")
    assert isinstance(quality.reconciliation.relative_tolerance, float)  # 1e-9 in YAML is text
    assert quality.report.max_markdown_rows > 0


# --- Every config rule refuses a bad file -----------------------------------------------------


def _set_fx_rate_to_zero(raw):
    raw["fx"]["start_to_reporting"]["USD"] = 0.0


def _reporting_currency_not_held(raw):
    raw["fx"]["reporting_currency"] = "JPY"


def _fx_start_rate_missing(raw):
    del raw["fx"]["start_to_reporting"]["EUR"]


def _last_ladder_bucket_closed(raw):
    raw["buckets"][-1]["up_to_days"] = 99_999


def _ladder_edges_out_of_order(raw):
    raw["buckets"][1]["up_to_days"], raw["buckets"][2]["up_to_days"] = 30, 7


def _ladder_labels_repeated(raw):
    raw["buckets"][1]["label"] = raw["buckets"][0]["label"]


def _nmd_type_unknown(raw):
    first = next(iter(raw["nmd"]["categories"]))
    raw["nmd"]["categories"][first]["type"] = "very_stable"


def _last_repricing_bucket_closed(raw):
    raw["buckets"][-1]["up_to_years"] = 50.0


def _fx_url_without_placeholders(raw):
    raw["boe_fx"]["url"] = "https://example.invalid/fx.csv"


@pytest.mark.parametrize(
    ("model", "path", "break_it", "message"),
    [
        (GeneratorConfig, "generator.yaml", _set_fx_rate_to_zero, "must be above 0"),
        (GeneratorConfig, "generator.yaml", _reporting_currency_not_held, "is not in"),
        (GeneratorConfig, "generator.yaml", _fx_start_rate_missing, "must give exactly"),
        (LadderConfig, "liquidity/ladder.yaml", _last_ladder_bucket_closed, "open-ended"),
        (LadderConfig, "liquidity/ladder.yaml", _ladder_edges_out_of_order, "rise strictly"),
        (LadderConfig, "liquidity/ladder.yaml", _ladder_labels_repeated, "unique"),
        (IrrbbRules, "irrbb/basel.yaml", _nmd_type_unknown, "unknown type"),
        (IrrbbRules, "irrbb/basel.yaml", _last_repricing_bucket_closed, "open-ended"),
        (MarketRatesConfig, "market_rates.yaml", _fx_url_without_placeholders, "must contain"),
    ],
)
def test_config_rule_refuses_a_bad_file(model, path, break_it, message):
    raw = _read_generator_yaml(CONFIG_DIR / path)  # plain YAML (and 'base:') as a dict
    model.model_validate(raw)  # the shipped file is valid ...
    break_it(raw)
    with pytest.raises(ValidationError, match=message):  # ... and the broken one is refused
        model.model_validate(raw)
