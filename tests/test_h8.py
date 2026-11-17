"""Fed H.8 deposits for the run-off backtest: parsing, the checks, provenance and loading.

Offline: FRED's CSV format is reproduced in small samples; no test touches the internet.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from riskplatform.config import H8Source, load_validation_config
from riskplatform.model_validation import h8
from riskplatform.model_validation.h8 import (
    COLUMNS,
    H8DataError,
    check_h8,
    fetch_h8,
    load_h8,
    parse_h8,
    write_h8,
)

ROOT = Path(__file__).resolve().parents[1]


def _csv(fred_id: str, rows: list[tuple[str, str]]) -> str:
    """FRED's CSV layout: observation_date, <series id>; '.' for a missing value."""
    return "observation_date," + fred_id + "\n" + "".join(f"{d},{v}\n" for d, v in rows)


WEEKS = [("2023-03-01", "17600.5"), ("2023-03-08", "17550.0"), ("2023-03-15", "17400.25")]


@pytest.fixture
def cfg(tmp_path) -> H8Source:
    real = load_validation_config(ROOT / "config" / "validation.yaml").h8
    return real.model_copy(
        update={
            "output_file": tmp_path / "h8.parquet",
            "provenance_file": tmp_path / "h8_provenance.json",
        }
    )


def test_the_config_names_the_two_checked_fred_series():
    cfg = load_validation_config(ROOT / "config" / "validation.yaml").h8
    assert cfg.series == {
        "all_commercial_banks": "DPSACBW027NBOG",  # not seasonally adjusted (N), checked on FRED
        "small_banks": "DPSSCBW027NBOG",
    }
    assert cfg.output_file.parts[:2] == ("data", "external")  # under data/: ignored by Git


def test_config_refuses_a_url_without_the_series_and_a_repeated_id():
    base = {
        "series": {"a": "X"}, "output_file": "o", "provenance_file": "p",
        "user_agent": "u", "timeout_seconds": 1,
    }  # fmt: skip
    with pytest.raises(ValidationError, match="series"):
        H8Source.model_validate({**base, "url": "https://example.org/csv"})
    with pytest.raises(ValidationError, match="its own FRED series ID"):
        H8Source.model_validate({**base, "url": "u{series}", "series": {"a": "X", "b": "X"}})


def test_parse_reads_fred_csv_and_drops_missing_values():
    text = _csv("DPSSCBW027NBOG", [*WEEKS, ("2023-03-22", ".")])
    deposits = parse_h8(text, "small_banks", "DPSSCBW027NBOG")
    assert list(deposits.columns) == COLUMNS
    assert len(deposits) == 3  # the '.' week is dropped
    assert deposits["deposits_bn"].tolist() == [17600.5, 17550.0, 17400.25]
    assert set(deposits["series"]) == {"small_banks"}
    assert deposits["date"].dt.day_of_week.eq(2).all()


def _frame(rows: list[tuple[str, float]], name: str = "s") -> pd.DataFrame:
    return pd.DataFrame(
        {"date": pd.to_datetime([d for d, _ in rows]), "series": name, "fred_id": "X",
         "deposits_bn": [v for _, v in rows]}
    )[COLUMNS]  # fmt: skip


def test_good_weeks_pass_the_checks():
    check_h8(_frame([(d, float(v)) for d, v in WEEKS]))


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([("2023-03-01", 1.0), ("2023-03-08", 0.0)], "positive"),
        ([("2023-03-01", 1.0), ("2023-03-01", 2.0)], "repeated dates"),
        ([("2023-03-01", 1.0), ("2023-03-09", 2.0)], "not on a Wednesday"),
        ([("2023-03-08", 1.0), ("2023-03-01", 2.0)], "out of order"),
        ([("2023-03-01", 1.0), ("2023-03-15", 2.0)], "weeks missing before"),  # a week skipped
    ],
)
def test_a_bad_download_is_refused(rows, message):
    with pytest.raises(H8DataError, match=message):
        check_h8(_frame(rows))


def test_a_reply_that_is_not_freds_csv_is_refused():
    with pytest.raises(H8DataError, match="not FRED's CSV for DPSSCBW027NBOG"):
        parse_h8("<html>Service unavailable</html>", "small_banks", "DPSSCBW027NBOG")


def test_an_empty_download_is_refused():
    with pytest.raises(H8DataError, match="no deposits"):
        check_h8(_frame([]))


def test_fetch_asks_for_each_series_and_combines_them(cfg):
    asked = []

    def fake_fetch(url: str) -> bytes:
        asked.append(url)
        fred_id = url.split("id=")[1]
        return _csv(fred_id, WEEKS).encode("utf-8")

    deposits = fetch_h8(cfg, fake_fetch)
    assert asked == [cfg.url.format(series=i) for i in cfg.series.values()]
    assert deposits.groupby("series").size().to_dict() == {"all_commercial_banks": 3,
                                                           "small_banks": 3}  # fmt: skip


def test_fetch_refuses_bad_data_and_nothing_is_saved(cfg, monkeypatch, caplog):
    def thursday(url: str) -> bytes:
        return _csv(url.split("id=")[1], [("2023-03-01", "1"), ("2023-03-02", "2")]).encode()

    monkeypatch.setattr(h8, "load_validation_config", lambda path: type("C", (), {"h8": cfg}))
    monkeypatch.setattr(h8, "http_fetcher", lambda agent, timeout: thursday)
    assert h8.main([]) == 1
    assert not cfg.output_file.exists() and not cfg.provenance_file.exists()
    assert "nothing saved" in caplog.text


def test_write_records_provenance_and_load_returns_the_same_data(cfg):
    deposits = pd.concat(
        [_frame([(d, float(v)) for d, v in WEEKS], name) for name in cfg.series], ignore_index=True
    )
    when = datetime(2026, 11, 17, 4, 0, tzinfo=UTC)
    provenance = write_h8(cfg, deposits, retrieved_at=when)
    assert provenance["retrieved_at"] == "2026-11-17T04:00:00+00:00"
    assert provenance["rows"] == 6 and len(provenance["sha256"]) == 64
    small = provenance["series"]["small_banks"]
    assert (small["first_date"], small["last_date"], small["rows"]) == (
        "2023-03-01",
        "2023-03-15",
        3,
    )
    assert "Small Domestically Chartered" in small["citation"]
    assert "copyrighted" in provenance["licence_note"]
    loaded, saved = load_h8(cfg)
    pd.testing.assert_frame_equal(loaded, deposits)
    assert saved == json.loads(cfg.provenance_file.read_text(encoding="utf-8"))


def test_load_refuses_a_missing_or_changed_file(cfg):
    with pytest.raises(H8DataError, match="fetch it first"):
        load_h8(cfg)
    deposits = _frame([(d, float(v)) for d, v in WEEKS])
    write_h8(cfg, deposits, retrieved_at=datetime.now(UTC))
    changed = deposits.assign(deposits_bn=deposits["deposits_bn"] * 2)
    changed.to_parquet(cfg.output_file, index=False)  # edited after download
    with pytest.raises(H8DataError, match="has changed since it was downloaded"):
        load_h8(cfg)
