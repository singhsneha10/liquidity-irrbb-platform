"""FDIC data for T6: complete downloads by splitting (never offset paging), checks, provenance.

Offline: a fake FDIC API serves an in-memory dataset with the same filters and a tiny page
limit, so the splitting is exercised hard. No test touches the internet.
"""

import json
import re
import urllib.parse
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from riskplatform.config import load_validation_config
from riskplatform.model_validation import fdic
from riskplatform.model_validation.fdic import (
    FdicDataError,
    check_fdic,
    fetch_deposits,
    fetch_events,
    load_fdic,
    quarter_ends,
    write_fdic,
)

ROOT = Path(__file__).resolve().parents[1]
REAL = load_validation_config(ROOT / "config" / "validation.yaml").fdic


def _cfg(tmp_path, page_limit=3):
    return REAL.model_copy(
        update={
            "first_quarter": date(2023, 3, 31),
            "events_from": date(2022, 10, 1),
            "page_limit": page_limit,
            "deposits_file": tmp_path / "deposits.parquet",
            "events_file": tmp_path / "events.parquet",
            "provenance_file": tmp_path / "provenance.json",
        }
    )


QUARTERS = ["20230331", "20230630"]
BANKS = [5, 17, 99, 628, 24735, 57053, 59017]  # 7 banks: more than a page of 3
FINANCIALS = [{"CERT": c, "REPDTE": q, "DEP": 1000 * c} for q in QUARTERS for c in BANKS]
EVENTS = [
    {"ID": f"e{i}", "CERT": c, "ACQ_CERT": 628, "OUT_CERT": c, "EFFDATE": f"2023-0{m}-1{i % 9}T00:00:00",
     "CHANGECODE": code}
    for i, (c, m, code) in enumerate(
        [(5, 1, 223), (17, 2, 223), (99, 3, 223), (59017, 5, 211), (57053, 3, 211), (5, 4, 712)]
    )
]  # fmt: skip


class FakeFdic:
    """Answers /financials and /history like the API: at most `limit` rows, the true total."""

    def __init__(self, short: bool = False):
        self.urls, self.short = [], short

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        assert "offset" not in query, "never page by offset"
        filters, limit = query["filters"], int(query["limit"])
        if parsed.path.endswith("financials"):
            quarter = re.search(r"REPDTE:(\d{8})", filters).group(1)
            rows = [r for r in FINANCIALS if r["REPDTE"] == quarter]
            span = re.search(r"CERT:\[(\d+) TO (\d+)\]", filters)
            if span:
                rows = [r for r in rows if int(span[1]) <= r["CERT"] <= int(span[2])]
        else:
            code = int(re.search(r"CHANGECODE:(\d+)", filters).group(1))
            start, end = re.search(r'EFFDATE:\["([\d-]+)" TO "([\d-]+)"\]', filters).groups()
            rows = [
                r for r in EVENTS if r["CHANGECODE"] == code and start <= r["EFFDATE"][:10] <= end
            ]
        page = rows[: min(limit, len(rows) - 1)] if self.short and rows else rows[:limit]
        return json.dumps(
            {"meta": {"total": len(rows)}, "data": [{"data": r} for r in page]}
        ).encode()


def test_quarter_ends():
    assert quarter_ends(date(2023, 3, 31), date(2023, 12, 31)) == [
        date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]  # fmt: skip


def test_deposits_arrive_complete_and_once_through_splitting(tmp_path):
    fake = FakeFdic()
    deposits = fetch_deposits(_cfg(tmp_path), fake, date(2023, 6, 30))
    assert len(deposits) == len(FINANCIALS) and not deposits.duplicated(["cert", "quarter"]).any()
    assert sorted(deposits["cert"].unique()) == BANKS
    assert deposits.loc[deposits["cert"] == 628, "deposits_thousands"].tolist() == [628000] * 2
    assert any("CERT" in url for url in fake.urls)  # 7 banks > a page of 3: it had to split
    assert all("+" not in url for url in fake.urls)  # spaces sent as %20


def test_events_arrive_complete_by_code_and_date_splitting(tmp_path):
    events = fetch_events(_cfg(tmp_path, page_limit=1), FakeFdic(), date(2023, 6, 30))
    assert sorted(events["event_id"]) == sorted(e["ID"] for e in EVENTS)
    assert set(events["code"]) == {211, 223, 712}
    assert events["effective"].dt.hour.eq(0).all()


def test_a_short_page_is_refused(tmp_path):
    with pytest.raises(FdicDataError, match="rows of"):
        fetch_deposits(_cfg(tmp_path, page_limit=10), FakeFdic(short=True), date(2023, 3, 31))


def _tables():
    deposits = pd.DataFrame({"cert": [1, 2], "quarter": pd.to_datetime(["2023-03-31"] * 2),
                             "deposits_thousands": [10.0, 20.0]})  # fmt: skip
    events = pd.DataFrame({"event_id": ["a"], "cert": pd.array([1], "Int64"),
                           "acq_cert": pd.array([2], "Int64"), "out_cert": pd.array([1], "Int64"),
                           "effective": pd.to_datetime(["2023-02-01"]), "code": [223]})  # fmt: skip
    return deposits, events


@pytest.mark.parametrize(
    ("break_it", "message"),
    [
        (lambda d, e: d.assign(cert=[1, 1]), "appears twice in a quarter"),
        (lambda d, e: d.assign(quarter=pd.to_datetime(["2023-03-30"] * 2)), "not a quarter end"),
        (lambda d, e: d.assign(deposits_thousands=[-1.0, 2.0]), "negative deposits"),
    ],
)
def test_bad_deposits_are_refused(break_it, message):
    deposits, events = _tables()
    with pytest.raises(FdicDataError, match=message):
        check_fdic(break_it(deposits, events), events, REAL)


def test_bad_events_are_refused():
    deposits, events = _tables()
    with pytest.raises(FdicDataError, match="appears twice"):
        check_fdic(deposits, pd.concat([events, events]), REAL)
    with pytest.raises(FdicDataError, match=r"unexpected change codes \[711\]"):
        check_fdic(deposits, events.assign(code=[711]), REAL)  # a branch opening: not planned


def test_write_load_and_refuse_a_changed_file(tmp_path):
    cfg = _cfg(tmp_path)
    deposits, events = _tables()
    provenance = write_fdic(cfg, deposits, events, datetime(2026, 11, 17, tzinfo=UTC))
    assert provenance["deposits"]["rows"] == 2 and provenance["events"]["by_code"] == {"223": 1}
    loaded, loaded_events, saved = load_fdic(cfg)
    pd.testing.assert_frame_equal(loaded, deposits)
    pd.testing.assert_frame_equal(loaded_events, events)
    assert saved["deposits"]["sha256"] == provenance["deposits"]["sha256"]
    deposits.assign(deposits_thousands=[99.0, 20.0]).to_parquet(cfg.deposits_file, index=False)
    with pytest.raises(FdicDataError, match="has changed since it was downloaded"):
        load_fdic(cfg)


def test_missing_files_say_how_to_fetch(tmp_path):
    with pytest.raises(FdicDataError, match="fetch it first"):
        load_fdic(_cfg(tmp_path))


def test_the_command_saves_nothing_when_a_download_is_refused(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path, page_limit=10)
    monkeypatch.setattr(fdic, "load_validation_config", lambda p: type("C", (), {"fdic": cfg}))
    monkeypatch.setattr(fdic, "http_fetcher", lambda agent, timeout: FakeFdic(short=True))
    monkeypatch.setattr(fdic, "latest_quarter", lambda c, f: date(2023, 6, 30))
    assert fdic.main([]) == 1
    assert not cfg.deposits_file.exists() and not cfg.provenance_file.exists()
    assert "nothing saved" in caplog.text
