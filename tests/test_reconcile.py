"""Reconciliation to control totals: transit losses stop the run, quarantine is explained."""

import pandas as pd
import pytest

from riskplatform.controls import file_totals, read_control_totals, write_control_totals
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.quality.reconcile import ReconciliationFailed, reconcile
from riskplatform.quality.report import table_of

FIRST_DAY = "positions/date=2026-08-03.parquet"


@pytest.fixture(scope="module")
def generated(cfg):
    return generate(cfg)


@pytest.fixture
def raw_dir(generated, tmp_path):
    write_outputs(generated, tmp_path)
    return tmp_path


def _read_raw(raw_dir) -> dict[str, pd.DataFrame]:
    """Every raw data file, keyed by its path relative to the raw folder."""
    files = {
        path.relative_to(raw_dir).as_posix(): pd.read_parquet(path)
        for path in sorted(raw_dir.rglob("*.parquet"))
    }
    assert FIRST_DAY in files and "rates.parquet" in files
    return files


def test_control_totals_cover_every_file_written(raw_dir):
    control = read_control_totals(raw_dir)
    arrived = _read_raw(raw_dir)
    assert set(control) == set(arrived)
    for name, frame in arrived.items():
        assert control[name] == file_totals(control[name]["table"], frame)


def test_clean_files_reconcile_with_nothing_quarantined(raw_dir):
    arrived = _read_raw(raw_dir)
    result = reconcile(read_control_totals(raw_dir), arrived, validated=arrived)
    files = result.files
    assert (files["source_rows"] == files["arrived_rows"]).all()
    assert (files["quarantined_rows"] == 0).all()
    assert (result.balances["source_mn"] == result.balances["arrived_mn"]).all()
    assert (result.balances["quarantined_mn"] == 0).all()


def _row_lost(arrived):
    arrived[FIRST_DAY] = arrived[FIRST_DAY].iloc[1:]


def _file_lost(arrived):
    del arrived[FIRST_DAY]


def _unexpected_file(arrived):
    arrived["positions/date=2030-01-01.parquet"] = arrived[FIRST_DAY]


def _value_changed(arrived):
    arrived[FIRST_DAY].loc[0, "balance_mn"] += 10.0


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (_row_lost, "900 rows sent, 899 arrived"),
        (_file_lost, "sent but did not arrive"),
        (_unexpected_file, "arrived but was not sent"),
        (_value_changed, "balance_mn for BANK_UK/GBP/asset"),
    ],
)
def test_data_lost_or_changed_after_sending_stops_the_run(raw_dir, damage, message):
    arrived = _read_raw(raw_dir)
    damage(arrived)
    with pytest.raises(ReconciliationFailed, match=message) as stopped:
        reconcile(read_control_totals(raw_dir), arrived, validated=arrived)
    assert not stopped.value.reconciliation.files.empty  # the tables still reach the report


def test_quarantined_row_is_explained_in_rows_and_balance(raw_dir):
    arrived = _read_raw(raw_dir)
    validated = dict(arrived)
    dropped = arrived[FIRST_DAY].iloc[0]
    validated[FIRST_DAY] = arrived[FIRST_DAY].iloc[1:]  # as if row 0 was quarantined

    result = reconcile(read_control_totals(raw_dir), arrived, validated)
    day = result.files.set_index("file").loc[FIRST_DAY]
    assert (day["arrived_rows"], day["validated_rows"], day["quarantined_rows"]) == (900, 899, 1)
    balances = result.balances[result.balances["quarantined_mn"] != 0]
    assert len(balances) == 1
    row = balances.iloc[0]
    assert (row["entity"], row["currency"], row["side"]) == (
        dropped["entity"],
        dropped["currency"],
        dropped["side"],
    )
    assert row["quarantined_mn"] == pytest.approx(dropped["balance_mn"])


def test_bad_value_sent_by_the_source_reconciles(raw_dir):
    # The source itself sends text in a number column, and its control totals include it.
    arrived = _read_raw(raw_dir)
    arrived[FIRST_DAY] = arrived[FIRST_DAY].astype({"balance_mn": object})
    arrived[FIRST_DAY].loc[0, "balance_mn"] = "abc"
    sent = {name: (table_of(name), frame) for name, frame in arrived.items()}
    write_control_totals(raw_dir, sent)
    reconcile(read_control_totals(raw_dir), arrived, validated=arrived)  # no failure


def test_missing_control_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="control totals"):
        read_control_totals(tmp_path)
