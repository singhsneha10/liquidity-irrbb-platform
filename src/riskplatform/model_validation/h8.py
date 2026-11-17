"""Real deposit data for the run-off backtest: Federal Reserve H.8 weekly deposits, from FRED.

Run with:  python -m riskplatform.model_validation.h8     (needs the internet once)

Saves data/external/h8_deposits.parquet (date, series, fred_id, deposits_bn) and a provenance
record next to it (when, from where, which series, first and last date, rows, SHA-256), like the
market curves (riskplatform.ingestion.market_rates). Series IDs and paths: config/validation.yaml.
FRED marks the data as copyrighted, so the files stay under data/ (ignored by Git) and the
backtest reads the saved file, never the internet: results can be repeated offline.

Every download is checked before it is saved; a check that fails refuses the whole download:
deposits must be positive, each date once per series, every date a Wednesday (H.8 weeks end on
Wednesday), dates in order and exactly 7 days apart (the backtest counts weeks by rows, so a
missing week would silently stretch a horizon). load_h8 refuses a file whose hash no longer matches its provenance.
Not checked here: breaks in the levels from nonbank structure activity (docs/validation_report.md,
limitation 7); the backtest checks any large outflow against the H.8 notes.
"""

import argparse
import hashlib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from riskplatform.config import H8Source, load_validation_config
from riskplatform.ingestion.market_rates import Fetch, http_fetcher, parse_fred_csv
from riskplatform.logs import setup_logging

logger = logging.getLogger(__name__)

COLUMNS = ["date", "series", "fred_id", "deposits_bn"]
WEDNESDAY = 2  # pandas: Monday = 0
TITLES = {  # FRED's series titles, for the citation
    "all_commercial_banks": "Deposits, All Commercial Banks",
    "small_banks": "Deposits, Small Domestically Chartered Commercial Banks",
}
CITATION = (
    "Board of Governors of the Federal Reserve System (US), {title} [{fred_id}], retrieved from "
    "FRED, Federal Reserve Bank of St. Louis; https://fred.stlouisfed.org/series/{fred_id}"
)


class H8DataError(Exception):
    """A download or a saved file failed a check; nothing is saved or used."""


def parse_h8(text: str, name: str, fred_id: str) -> pd.DataFrame:
    """One FRED CSV -> date, series, fred_id, deposits_bn (missing values dropped)."""
    try:
        frame = parse_fred_csv(text, fred_id)  # date, rate (the column name is the parser's)
    except (KeyError, ValueError) as error:  # e.g. an error page instead of the CSV
        raise H8DataError(f"{name}: the reply is not FRED's CSV for {fred_id}") from error
    return pd.DataFrame(
        {
            "date": frame["date"],
            "series": name,
            "fred_id": fred_id,
            "deposits_bn": frame["rate"].astype(float),
        }
    )[COLUMNS]


def check_h8(deposits: pd.DataFrame) -> None:
    """Raise H8DataError if the deposits fail any check (see the module notes)."""
    if deposits.empty:
        raise H8DataError("no deposits downloaded")
    problems = []
    for name, rows in deposits.groupby("series"):
        dates = rows["date"]
        if (rows["deposits_bn"] <= 0).any():
            problems.append(f"{name}: deposits must be positive")
        if dates.duplicated().any():
            problems.append(f"{name}: repeated dates {sorted(dates[dates.duplicated()].dt.date)}")
        not_wednesday = dates[dates.dt.day_of_week != WEDNESDAY]
        if len(not_wednesday):
            problems.append(f"{name}: dates not on a Wednesday {list(not_wednesday.dt.date[:5])}")
        if not dates.is_monotonic_increasing:
            problems.append(f"{name}: dates out of order")
        gaps = dates[dates.diff().dt.days.fillna(7) != 7]
        if len(gaps):
            problems.append(f"{name}: weeks missing before {list(gaps.dt.date[:5])}")
    if problems:
        raise H8DataError("; ".join(problems))


def fetch_h8(cfg: H8Source, fetch: Fetch) -> pd.DataFrame:
    """Download every configured series, check them, and return one table."""
    frames = []
    for name, fred_id in cfg.series.items():
        url = cfg.url.format(series=fred_id)
        logger.info("Fetching %s (%s) from %s", name, fred_id, url)
        frames.append(parse_h8(fetch(url).decode("utf-8"), name, fred_id))
    deposits = pd.concat(frames, ignore_index=True)
    check_h8(deposits)
    return deposits


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_h8(cfg: H8Source, deposits: pd.DataFrame, retrieved_at: datetime) -> dict:
    """Save the deposits and their provenance record; return the provenance."""
    check_h8(deposits)
    cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
    deposits.to_parquet(cfg.output_file, index=False)
    series = {}
    for name, rows in deposits.groupby("series", sort=False):
        fred_id = rows["fred_id"].iloc[0]
        series[name] = {
            "fred_id": fred_id,
            "url": cfg.url.format(series=fred_id),
            "first_date": rows["date"].min().date().isoformat(),
            "last_date": rows["date"].max().date().isoformat(),
            "rows": len(rows),
            "citation": CITATION.format(title=TITLES.get(name, name), fred_id=fred_id),
        }
    provenance = {
        "retrieved_at": retrieved_at.isoformat(timespec="seconds"),
        "source": "Federal Reserve H.8, via FRED (Federal Reserve Bank of St. Louis)",
        "units": "billions of US dollars, not seasonally adjusted, weekly ending Wednesday",
        "file": cfg.output_file.as_posix(),
        "sha256": _sha256(cfg.output_file),
        "rows": len(deposits),
        "series": series,
        "licence_note": "FRED marks this data as copyrighted: keep it local, do not commit it.",
    }
    cfg.provenance_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.provenance_file.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return provenance


def load_h8(cfg: H8Source) -> tuple[pd.DataFrame, dict]:
    """The saved deposits and their provenance; refused if missing, changed or failing a check."""
    if not cfg.output_file.exists() or not cfg.provenance_file.exists():
        raise H8DataError(
            f"{cfg.output_file.as_posix()} not found: fetch it first with "
            "python -m riskplatform.model_validation.h8"
        )
    provenance = json.loads(cfg.provenance_file.read_text(encoding="utf-8"))
    if _sha256(cfg.output_file) != provenance["sha256"]:
        raise H8DataError(f"{cfg.output_file.as_posix()} has changed since it was downloaded")
    deposits = pd.read_parquet(cfg.output_file)
    check_h8(deposits)
    return deposits, provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Fed H.8 weekly deposits from FRED.")
    parser.add_argument("--config", type=Path, default=Path("config/validation.yaml"))
    args = parser.parse_args(argv)

    setup_logging()
    cfg = load_validation_config(args.config).h8
    fetch = http_fetcher(cfg.user_agent, cfg.timeout_seconds)
    try:
        deposits = fetch_h8(cfg, fetch)
    except H8DataError as error:
        logger.error("Download refused, nothing saved: %s", error)
        return 1
    provenance = write_h8(cfg, deposits, retrieved_at=datetime.now(UTC))
    for name, info in provenance["series"].items():
        logger.info(
            "%s (%s): %d weeks, %s to %s",
            name, info["fred_id"], info["rows"], info["first_date"], info["last_date"],
        )  # fmt: skip
    logger.info("Wrote %s (sha256 %s)", cfg.output_file, provenance["sha256"][:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
