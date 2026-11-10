import pytest
from pydantic import ValidationError

from riskplatform.config import GeneratorConfig


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
