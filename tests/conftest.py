from pathlib import Path

import pytest

from riskplatform.config import load_generator_config

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "generator.yaml"


@pytest.fixture(scope="session")
def cfg():
    return load_generator_config(CONFIG_PATH)
