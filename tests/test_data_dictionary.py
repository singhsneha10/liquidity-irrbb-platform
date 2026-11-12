"""Keep docs/data_dictionary.md in step with the data contract and the config."""

import re
from pathlib import Path

import pytest

from riskplatform.schemas import positions_schema, rates_schema

DICTIONARY_PATH = Path(__file__).resolve().parents[1] / "docs" / "data_dictionary.md"


def _block(text: str, heading: str) -> str:
    """Return the text under a markdown heading, up to the next heading of the same level."""
    marker = heading.split(" ", 1)[0] + " "  # "## " or "### "
    start = text.index(f"\n{heading}\n")
    end = text.find(f"\n{marker}", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _first_column_names(block: str) -> list[str]:
    """Names written as `name` in the first column of a markdown table's rows, in order.

    Only rows below the |---| separator count, so a header row is never mistaken for data.
    """
    rows = block.split("\n|---", 1)[1]
    return re.findall(r"^\| `([^`]+)` \|", rows, flags=re.MULTILINE)


def _documented(section: str, table: str) -> list[str]:
    text = DICTIONARY_PATH.read_text(encoding="utf-8")
    return _first_column_names(_block(_block(text, f"## {section}"), f"### {table}"))


@pytest.mark.parametrize(
    ("section", "make_schema"),
    [("1. Positions", positions_schema), ("2. Market rates", rates_schema)],
)
def test_dictionary_lists_exactly_the_schema_columns(cfg, section, make_schema):
    assert _documented(section, "Columns") == list(make_schema(cfg).columns)


def test_dictionary_lists_exactly_the_config_categories(cfg):
    assert _documented("1. Positions", "Product categories") == cfg.category_names
