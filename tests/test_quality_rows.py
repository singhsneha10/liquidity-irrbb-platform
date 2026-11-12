"""Row-level validation: bad rows go to quarantine with the reason; good rows pass unchanged."""

import json

import pandas as pd
import pytest

from riskplatform.ingestion.generate import generate
from riskplatform.quality.rows import FileLevelError, validate_rows
from riskplatform.schemas import positions_schema, rates_schema

RUN = {
    "run_id": "test-run",
    "run_at": pd.Timestamp("2026-11-12 09:00", tz="UTC"),
    "source_file": "positions/date=2026-08-03.parquet",
}


@pytest.fixture(scope="module")
def generated(cfg):
    return generate(cfg)


@pytest.fixture
def day(generated):
    """One clean day of positions, as one raw file holds it (a fresh copy for each test)."""
    positions = generated.positions
    first = positions[positions["date"] == positions["date"].min()]
    return first.reset_index(drop=True).astype({"balance_mn": object})  # object: may hold text


def _validate(cfg, frame, schema=None, table="positions"):
    return validate_rows(frame, schema or positions_schema(cfg), table=table, **RUN)


def _only_record(result):
    assert len(result.quarantine) == 1
    return result.quarantine.iloc[0]


def test_clean_day_passes_whole(cfg, day):
    result = _validate(cfg, day)
    assert result.quarantine.empty
    pd.testing.assert_frame_equal(result.valid, positions_schema(cfg).validate(day))


@pytest.mark.parametrize(
    ("column", "bad_value", "check"),
    [
        ("balance_mn", -5.0, "greater_than(0)"),
        ("currency", "JPY", "isin(['GBP', 'USD', 'EUR'])"),
        ("balance_mn", "abc", "coerce_dtype('float64')"),  # text in a number column
    ],
)
def test_bad_value_is_quarantined_with_its_column_and_value(cfg, day, column, bad_value, check):
    day.loc[7, column] = bad_value
    result = _validate(cfg, day)
    record = _only_record(result)
    assert (record["row_number"], record["check"], record["column"]) == (7, check, column)
    assert record["failure_value"] == str(bad_value)
    assert len(result.valid) == len(day) - 1


def test_empty_value_is_quarantined_with_no_value(cfg, day):
    day.loc[7, "entity"] = None
    record = _only_record(_validate(cfg, day))
    assert (record["check"], record["column"]) == ("not_nullable", "entity")
    assert pd.isna(record["failure_value"])


def test_multi_column_rule_is_recorded_once_for_the_row(cfg, day):
    floating = day.index[day["rate_type"] == "floating"][0]
    day.loc[floating, "reprice_days"] = day.loc[floating, "residual_days"] + 1
    record = _only_record(_validate(cfg, day))
    assert record["row_number"] == floating
    assert record["check"] == "reprice_days must not exceed residual_days"
    assert pd.isna(record["column"]) and pd.isna(record["failure_value"])


def test_every_copy_of_a_duplicate_is_quarantined(cfg, day):
    doubled = pd.concat([day, day.iloc[[3]]], ignore_index=True)
    result = _validate(cfg, doubled)
    assert result.quarantine["row_number"].tolist() == [3, len(day)]
    assert set(result.quarantine["check"]) == {"multiple_fields_uniqueness"}
    assert len(result.valid) == len(day) - 1


def test_row_breaking_two_rules_has_two_records(cfg, day):
    day.loc[7, "balance_mn"] = -5.0
    day.loc[7, "currency"] = "JPY"
    result = _validate(cfg, day)
    assert result.quarantine["row_number"].tolist() == [7, 7]
    assert set(result.quarantine["column"]) == {"balance_mn", "currency"}


def test_other_rows_pass_through_unchanged(cfg, day):
    day.loc[7, "balance_mn"] = -5.0
    result = _validate(cfg, day)
    expected = positions_schema(cfg).validate(day.drop(index=7)).reset_index(drop=True)
    pd.testing.assert_frame_equal(result.valid, expected)


def test_record_keeps_the_original_row(cfg, day):
    day.loc[7, "balance_mn"] = -5.0
    original = json.loads(_only_record(_validate(cfg, day))["record"])
    assert original["position_id"] == day.loc[7, "position_id"]
    assert original["balance_mn"] == -5.0
    assert set(original) == set(day.columns)


@pytest.mark.parametrize("change", ["drop", "add"])
def test_missing_or_extra_column_stops_the_run(cfg, day, change):
    broken = day.drop(columns="side") if change == "drop" else day.assign(trader="x")
    with pytest.raises(FileLevelError, match="side" if change == "drop" else "trader"):
        _validate(cfg, broken)


def test_rates_table_is_validated_the_same_way(cfg, generated):
    rates = generated.rates.reset_index(drop=True)
    rates.loc[2, "rate"] = 0.9  # 90%: outside the allowed range
    result = _validate(cfg, rates, schema=rates_schema(cfg), table="rates")
    record = _only_record(result)
    assert (record["table"], record["row_number"], record["column"]) == ("rates", 2, "rate")
    assert len(result.valid) == len(rates) - 1
