from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from riskplatform.config import GeneratorConfig, _read_generator_yaml, load_generator_config

REAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "generator_real.yaml"


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
