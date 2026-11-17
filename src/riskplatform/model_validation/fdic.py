"""Bank-level deposit data for test T6: every FDIC-insured institution's quarterly deposits, and
the structure events that move deposits without any depositor withdrawing them.

Run with:  python -m riskplatform.model_validation.fdic     (needs the internet once; minutes)

Source: the FDIC BankFind API (free, no key), settings in config/validation.yaml (fdic):
- /financials: total deposits (DEP, thousands of dollars) by certificate (CERT) and quarter end
  (REPDTE), from config's first quarter to the latest the FDIC has published;
- /history: structure events with the deposit-moving change codes fixed in the plan
  (docs/validation_report.md, section 6.9), from config's events_from.

Every request is ONE page of at most page_limit rows, with no paging by offset: the API cannot
sort events by a unique key, and offset paging over a sort with ties can skip or repeat rows. A
chunk with more rows than the limit is split in two (deposits by certificate range, events by
date range) until each fits. Every chunk must return exactly the total the FDIC reports for it.

Checks before saving (a failed check refuses the whole download): one row per bank and quarter;
every date a quarter end; no negative deposits; one row per event; only the configured codes.
Saved to data/external/ with a provenance record and SHA-256 hashes; load_fdic refuses files
that changed after download. Nothing here filters or measures: that is T6 itself.
"""

import argparse
import hashlib
import json
import logging
import sys
import urllib.parse
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from riskplatform.config import FdicSource, load_validation_config
from riskplatform.ingestion.market_rates import Fetch, http_fetcher
from riskplatform.logs import setup_logging

logger = logging.getLogger(__name__)

DEPOSIT_COLUMNS = ["cert", "quarter", "deposits_thousands"]
EVENT_COLUMNS = ["event_id", "cert", "acq_cert", "out_cert", "effective", "code"]
MAX_CERT = 1_000_000  # above every FDIC certificate number


class FdicDataError(Exception):
    """A download or a saved file failed a check; nothing is saved or used."""


def _request(cfg: FdicSource, fetch: Fetch, endpoint: str, filters: str, fields: str) -> tuple:
    """One page: (rows, total the FDIC reports for these filters)."""
    query = urllib.parse.urlencode(  # spaces as %20 (as tested), not "+"
        {"filters": filters, "fields": fields, "limit": cfg.page_limit},
        quote_via=urllib.parse.quote,
    )
    url = f"{cfg.api_url}/{endpoint}?{query}"
    body = json.loads(fetch(url).decode("utf-8"))
    if "data" not in body or "meta" not in body:
        raise FdicDataError(f"unexpected reply for {endpoint} {filters}: {str(body)[:300]}")
    return [row["data"] for row in body["data"]], int(body["meta"]["total"])


def _complete(cfg: FdicSource, fetch: Fetch, endpoint: str, filters: str, fields: str,
              split) -> list[dict]:  # fmt: skip
    """Every row for the filters: one page, or split in two (by `split`) until each fits."""
    rows, total = _request(cfg, fetch, endpoint, filters, fields)
    if total <= cfg.page_limit:
        if len(rows) != total:
            raise FdicDataError(f"{endpoint} {filters}: got {len(rows)} rows of {total}")
        return rows
    parts = split(filters)
    if parts is None:
        raise FdicDataError(f"{endpoint} {filters}: {total} rows and cannot be split further")
    return [r for part in parts for r in _complete(cfg, fetch, endpoint, part, fields, split)]


def quarter_ends(first: date, last: date) -> list[date]:
    """Every quarter end from first to last, both included."""
    return [d.date() for d in pd.date_range(first, last, freq="QE")]


def latest_quarter(cfg: FdicSource, fetch: Fetch) -> date:
    """The latest quarter end the FDIC has published financials for."""
    query = urllib.parse.urlencode(
        {"fields": "REPDTE", "limit": 1, "sort_by": "REPDTE", "sort_order": "DESC"}
    )
    body = json.loads(fetch(f"{cfg.api_url}/financials?{query}").decode("utf-8"))
    return datetime.strptime(body["data"][0]["data"]["REPDTE"], "%Y%m%d").date()


def _split_by_cert(base: str):
    """Split a quarter's filter into two certificate ranges (the whole range the first time)."""

    def split(filters: str):
        if "CERT:[" in filters:
            low, high = (int(x) for x in filters.split("CERT:[")[1].rstrip("]").split(" TO "))
        else:
            low, high = 0, MAX_CERT
        if high - low < 1:
            return None
        middle = (low + high) // 2
        return [f"{base} AND CERT:[{low} TO {middle}]", f"{base} AND CERT:[{middle + 1} TO {high}]"]

    return split


def fetch_deposits(cfg: FdicSource, fetch: Fetch, last: date) -> pd.DataFrame:
    """Total deposits of every institution in every quarter from cfg.first_quarter to last."""
    frames = []
    for quarter in quarter_ends(cfg.first_quarter, last):
        base = f"REPDTE:{quarter:%Y%m%d}"
        rows = _complete(cfg, fetch, "financials", base, "CERT,REPDTE,DEP", _split_by_cert(base))
        logger.info("FDIC deposits %s: %d institutions", quarter, len(rows))
        frames.append(pd.DataFrame(rows))
    raw = pd.concat(frames, ignore_index=True)
    return pd.DataFrame(
        {
            "cert": raw["CERT"].astype("int64"),
            "quarter": pd.to_datetime(raw["REPDTE"], format="%Y%m%d"),
            "deposits_thousands": pd.to_numeric(raw["DEP"], errors="coerce"),
        }
    )[DEPOSIT_COLUMNS]


def _split_dates(code: int):
    def split(filters: str):
        start, end = (
            date.fromisoformat(x.strip('"'))
            for x in filters.split("EFFDATE:[")[1].rstrip("]").split(" TO ")
        )
        if (end - start).days < 1:
            return None
        middle = start + (end - start) // 2
        later = date.fromordinal(middle.toordinal() + 1)
        return [_event_filter(code, start, middle), _event_filter(code, later, end)]

    return split


def _event_filter(code: int, start: date, end: date) -> str:
    return f'CHANGECODE:{code} AND EFFDATE:["{start.isoformat()}" TO "{end.isoformat()}"]'


def fetch_events(cfg: FdicSource, fetch: Fetch, last: date) -> pd.DataFrame:
    """Every structure event with a deposit-moving code, from cfg.events_from to last."""
    fields = "CERT,ACQ_CERT,OUT_CERT,EFFDATE,CHANGECODE"
    frames = []
    for code in cfg.deposit_moving_codes:
        rows = _complete(cfg, fetch, "history", _event_filter(code, cfg.events_from, last),
                         fields, _split_dates(code))  # fmt: skip
        logger.info("FDIC events code %d: %d", code, len(rows))
        if rows:
            frames.append(pd.DataFrame(rows))
    raw = pd.concat(frames, ignore_index=True)
    certs = {name: pd.to_numeric(raw.get(name), errors="coerce").astype("Int64")
             for name in ("CERT", "ACQ_CERT", "OUT_CERT")}  # fmt: skip
    return pd.DataFrame(
        {
            "event_id": raw["ID"].astype(str),
            "cert": certs["CERT"],
            "acq_cert": certs["ACQ_CERT"],
            "out_cert": certs["OUT_CERT"],
            "effective": pd.to_datetime(raw["EFFDATE"]).dt.normalize(),
            "code": raw["CHANGECODE"].astype("int64"),
        }
    )[EVENT_COLUMNS]


def check_fdic(deposits: pd.DataFrame, events: pd.DataFrame, cfg: FdicSource) -> None:
    """Raise FdicDataError if either table fails a check (see the module notes)."""
    problems = []
    if deposits.empty:
        problems.append("no deposits")
    if deposits.duplicated(["cert", "quarter"]).any():
        problems.append("a bank appears twice in a quarter")
    if not deposits["quarter"].dt.is_quarter_end.all():
        problems.append("a date that is not a quarter end")
    if (deposits["deposits_thousands"] < 0).any():
        problems.append("negative deposits")
    if events["event_id"].duplicated().any():
        problems.append("an event appears twice")
    unknown = set(events["code"]) - set(cfg.deposit_moving_codes)
    if unknown:
        problems.append(f"unexpected change codes {sorted(unknown)}")
    if problems:
        raise FdicDataError("; ".join(problems))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_fdic(cfg: FdicSource, deposits: pd.DataFrame, events: pd.DataFrame,
               retrieved_at: datetime) -> dict:  # fmt: skip
    """Save both tables and their provenance; return the provenance."""
    check_fdic(deposits, events, cfg)
    for table, path in ((deposits, cfg.deposits_file), (events, cfg.events_file)):
        path.parent.mkdir(parents=True, exist_ok=True)
        table.to_parquet(path, index=False)
    provenance = {
        "retrieved_at": retrieved_at.isoformat(timespec="seconds"),
        "source": "FDIC BankFind Suite API (Call Report financials; structure history)",
        "api_url": cfg.api_url,
        "deposits": {
            "file": cfg.deposits_file.as_posix(),
            "sha256": _sha256(cfg.deposits_file),
            "rows": len(deposits),
            "institutions": int(deposits["cert"].nunique()),
            "first_quarter": deposits["quarter"].min().date().isoformat(),
            "last_quarter": deposits["quarter"].max().date().isoformat(),
            "units": "DEP: total deposits, thousands of US dollars",
        },
        "events": {
            "file": cfg.events_file.as_posix(),
            "sha256": _sha256(cfg.events_file),
            "rows": len(events),
            "by_code": {
                str(k): int(v) for k, v in events["code"].value_counts().sort_index().items()
            },
            "from": cfg.events_from.isoformat(),
        },
    }
    cfg.provenance_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.provenance_file.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return provenance


def load_fdic(cfg: FdicSource) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """The saved deposits, events and provenance; refused if missing or changed."""
    needed = (cfg.deposits_file, cfg.events_file, cfg.provenance_file)
    if not all(path.exists() for path in needed):
        raise FdicDataError(
            "FDIC data not found: fetch it first with python -m riskplatform.model_validation.fdic"
        )
    provenance = json.loads(cfg.provenance_file.read_text(encoding="utf-8"))
    for name, path in (("deposits", cfg.deposits_file), ("events", cfg.events_file)):
        if _sha256(path) != provenance[name]["sha256"]:
            raise FdicDataError(f"{path.as_posix()} has changed since it was downloaded")
    deposits, events = pd.read_parquet(cfg.deposits_file), pd.read_parquet(cfg.events_file)
    check_fdic(deposits, events, cfg)
    return deposits, events, provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch FDIC quarterly deposits and events.")
    parser.add_argument("--config", type=Path, default=Path("config/validation.yaml"))
    args = parser.parse_args(argv)

    setup_logging()
    cfg = load_validation_config(args.config).fdic
    fetch = http_fetcher(cfg.user_agent, cfg.timeout_seconds)
    try:
        last = latest_quarter(cfg, fetch)
        deposits = fetch_deposits(cfg, fetch, last)
        events = fetch_events(cfg, fetch, last)
        provenance = write_fdic(cfg, deposits, events, retrieved_at=datetime.now(UTC))
    except FdicDataError as error:
        logger.error("Download refused, nothing saved: %s", error)
        return 1
    d, e = provenance["deposits"], provenance["events"]
    logger.info(
        "Wrote %d bank-quarters (%d institutions, %s to %s) and %d events",
        d["rows"], d["institutions"], d["first_quarter"], d["last_quarter"], e["rows"],
    )  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
