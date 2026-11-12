"""Fetch real government yield curves from public sources (FRED, ECB, Bank of England).

This is the only part of the platform that uses the network. It saves the curves to a local
file with a provenance record; the generator then reads that file, so generation, tests and
CI stay offline and repeatable.

Run with:  python -m riskplatform.ingestion.market_rates
"""

import argparse
import hashlib
import io
import json
import logging
import re
import urllib.request
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from riskplatform.config import (
    BoeSource,
    MarketRatesConfig,
    SeriesSource,
    load_market_rates_config,
)
from riskplatform.schemas import market_curves_schema

logger = logging.getLogger(__name__)

Fetch = Callable[[str], bytes]  # downloads a URL and returns the response body

TENOR_DECIMALS = 4  # tenors are rounded so that 6/12 and 0.5 count as the same curve point
COLUMNS = ["date", "currency", "tenor_years", "rate", "source"]


def http_fetcher(user_agent: str, timeout: float) -> Fetch:
    """A Fetch that downloads with a plain HTTP GET."""

    def fetch(url: str) -> bytes:
        # FRED never answers a request without an Accept header, so always send one.
        headers = {"User-Agent": user_agent, "Accept": "*/*"}
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    return fetch


# --- Parsers: downloaded text or bytes -> tidy tables (rates still in percent) -------------


def parse_fred_csv(text: str, series: str) -> pd.DataFrame:
    """FRED CSV (observation_date, <series>) -> date, rate. Missing values ('.') are dropped."""
    frame = pd.read_csv(io.StringIO(text))
    rate = pd.to_numeric(frame[series], errors="coerce")
    return pd.DataFrame({"date": pd.to_datetime(frame["observation_date"]), "rate": rate}).dropna()


def parse_ecb_csv(text: str, series: str) -> pd.DataFrame:
    """ECB 'csvdata' (TIME_PERIOD, OBS_VALUE, ...) -> date, rate. An empty reply means no data."""
    if not text.strip():
        return pd.DataFrame({"date": pd.Series(dtype="datetime64[ns]"), "rate": []})
    frame = pd.read_csv(io.StringIO(text))
    rate = pd.to_numeric(frame["OBS_VALUE"], errors="coerce")
    return pd.DataFrame({"date": pd.to_datetime(frame["TIME_PERIOD"]), "rate": rate}).dropna()


def _parse_boe_sheet(raw: pd.DataFrame, sheet: str) -> pd.DataFrame:
    """One BoE curve sheet -> date, tenor_years, rate.

    Layout: a row whose first cell is 'years:' holds the tenors; each data row has a date in the
    first column and one rate per tenor. Blank cells (no rate at that tenor) are dropped.
    """
    first = raw.iloc[:, 0]
    years_rows = raw.index[first.astype(str).str.strip() == "years:"]
    if len(years_rows) != 1:
        raise ValueError(f"sheet {sheet!r}: expected exactly one 'years:' row")
    tenors = pd.to_numeric(raw.loc[years_rows[0]].iloc[1:], errors="coerce").dropna()
    is_date = first.map(lambda value: isinstance(value, datetime))
    body = raw.loc[is_date, tenors.index]
    body.columns = tenors.round(TENOR_DECIMALS).to_numpy()
    body.insert(0, "date", pd.to_datetime(first[is_date]))
    long = body.melt(id_vars="date", var_name="tenor_years", value_name="rate")
    long["rate"] = pd.to_numeric(long["rate"], errors="coerce")
    return long.dropna(subset=["rate"])


def parse_boe_workbook(content: bytes, sheets: list[str]) -> pd.DataFrame:
    """A BoE workbook -> date, tenor_years, rate, combining sheets (later sheets win)."""
    raw = pd.read_excel(io.BytesIO(content), sheet_name=sheets, header=None)
    frames = [_parse_boe_sheet(raw[sheet], sheet) for sheet in sheets]
    combined = pd.concat(frames, ignore_index=True)
    return combined.drop_duplicates(["date", "tenor_years"], keep="last")


_YEAR_RANGE = re.compile(r"(\d{4}) to (\d{4}|present)")


def _workbook_is_needed(name: str, contains: str, start: date) -> bool:
    """True for workbooks of the wanted kind that may hold dates on or after start."""
    if contains not in name or not name.endswith(".xlsx"):
        return False
    years = _YEAR_RANGE.search(name)
    if years is None:  # e.g. "... current month.xlsx"
        return True
    last = years.group(2)
    return last == "present" or int(last) >= start.year


# --- Sources: download and parse, still in percent ------------------------------------------


def _fetch_series_source(
    source: SeriesSource, name: str, start: date, fetch: Fetch, parse
) -> pd.DataFrame:
    frames = []
    for code, tenor in source.series.items():
        text = fetch(source.url.format(series=code, start=start.isoformat())).decode("utf-8")
        points = parse(text, code)
        if points.empty:
            raise ValueError(f"{name} series {code}: no data on or after {start}")
        frames.append(points.assign(tenor_years=round(tenor, TENOR_DECIMALS)))
    return pd.concat(frames, ignore_index=True).assign(currency=source.currency, source=name)


def _fetch_boe(source: BoeSource, start: date, fetch: Fetch) -> pd.DataFrame:
    frames = []
    for url in source.archive_urls:  # later archives (the current month) win on overlaps
        with zipfile.ZipFile(io.BytesIO(fetch(url))) as archive:
            for name in archive.namelist():
                if _workbook_is_needed(name, source.workbook_name_contains, start):
                    frames.append(parse_boe_workbook(archive.read(name), source.sheets))
    if not frames:
        raise ValueError(f"boe: no workbook containing {source.workbook_name_contains!r}")
    curve = pd.concat(frames, ignore_index=True).drop_duplicates(
        ["date", "tenor_years"], keep="last"
    )
    return curve.assign(currency=source.currency, source="boe")


def fetch_curves(cfg: MarketRatesConfig, fetch: Fetch) -> pd.DataFrame:
    """Fetch every source and return one validated table, with rates as decimals."""
    start = cfg.start_date
    curves = pd.concat(
        [
            _fetch_series_source(cfg.fred, "fred", start, fetch, parse_fred_csv),
            _fetch_series_source(cfg.ecb, "ecb", start, fetch, parse_ecb_csv),
            _fetch_boe(cfg.boe, start, fetch),
        ],
        ignore_index=True,
    )
    curves = curves[curves["date"] >= pd.Timestamp(start)]
    curves = curves.assign(rate=curves["rate"] / 100.0)  # percent -> decimal
    curves = curves.sort_values(["currency", "date", "tenor_years"], ignore_index=True)
    return market_curves_schema(cfg.currencies).validate(curves[COLUMNS])


# --- Outputs ---------------------------------------------------------------------------------


def write_outputs(cfg: MarketRatesConfig, curves: pd.DataFrame, retrieved_at: datetime) -> dict:
    """Write the curves file and its provenance record; return the provenance."""
    cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
    curves.to_parquet(cfg.output_file, index=False)
    by_currency = {
        currency: {
            "source": group["source"].iloc[0],
            "first_date": group["date"].min().date().isoformat(),
            "last_date": group["date"].max().date().isoformat(),
            "dates": int(group["date"].nunique()),
            "tenors": sorted(group["tenor_years"].unique().tolist()),
        }
        for currency, group in curves.groupby("currency")
    }
    provenance = {
        "retrieved_at": retrieved_at.isoformat(),
        "start_date": cfg.start_date.isoformat(),
        "rows": len(curves),
        "sha256": hashlib.sha256(Path(cfg.output_file).read_bytes()).hexdigest(),
        "sources": {
            "fred": cfg.fred.url,
            "ecb": cfg.ecb.url,
            "boe": cfg.boe.archive_urls,
        },
        "currencies": by_currency,
    }
    cfg.provenance_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.provenance_file.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return provenance


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetch real government yield curves.")
    parser.add_argument("--config", type=Path, default=Path("config/market_rates.yaml"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_market_rates_config(args.config)
    curves = fetch_curves(cfg, http_fetcher(cfg.user_agent, cfg.timeout_seconds))
    provenance = write_outputs(cfg, curves, retrieved_at=datetime.now(UTC))
    for currency, info in provenance["currencies"].items():
        logger.info(
            "%s (%s): %d dates %s..%s, %d tenors",
            currency,
            info["source"],
            info["dates"],
            info["first_date"],
            info["last_date"],
            len(info["tenors"]),
        )
    logger.info("Wrote %d rows to %s", provenance["rows"], cfg.output_file)


if __name__ == "__main__":
    main()
