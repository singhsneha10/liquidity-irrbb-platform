"""Market-rate fetching, tested offline with small samples in each source's real format."""

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from riskplatform.config import MarketRatesConfig, load_market_rates_config
from riskplatform.ingestion.market_rates import (
    _workbook_is_needed,
    fetch_curves,
    parse_boe_workbook,
    parse_ecb_csv,
    parse_fred_csv,
    write_outputs,
)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "market_rates.yaml"
SHEETS = ["3. spot, short end", "4. spot curve"]


@pytest.fixture(scope="module")
def market_cfg() -> MarketRatesConfig:
    return load_market_rates_config(CONFIG_PATH)


# --- Samples in each source's format ---------------------------------------------------------


def fred_csv(series: str) -> str:
    return f"observation_date,{series}\n2026-07-20,4.10\n2026-07-21,.\n2026-07-22,4.20\n"


def ecb_csv(series: str) -> str:
    return (
        "KEY,FREQ,DATA_TYPE_FM,TIME_PERIOD,OBS_VALUE\n"
        f"YC.B.U2.EUR.4F.G_N_A.SV_C_YM.{series},B,{series},2026-07-20,2.5\n"
        f"YC.B.U2.EUR.4F.G_N_A.SV_C_YM.{series},B,{series},2026-07-21,2.6\n"
    )


def _boe_sheet(title: str, tenors: list[float], rows: dict[datetime, list]) -> pd.DataFrame:
    """A sheet laid out like the Bank of England's: title, 'years:' row, blank, data rows."""
    lines = [
        [None, title] + [None] * (len(tenors) - 1),
        ["Maturity"] + [None] * len(tenors),
        ["years:", *tenors],
        [None] * (len(tenors) + 1),
    ]
    lines += [[day, *values] for day, values in rows.items()]
    return pd.DataFrame(lines)


def boe_workbook(day_rates: dict[datetime, float]) -> bytes:
    """Short-end sheet (1M and 6M) and long sheet (6M and 10Y); 6M differs to show which wins."""
    short = {day: [None, rate - 0.5] for day, rate in day_rates.items()}  # 1M blank
    long = {day: [rate, rate + 1.0] for day, rate in day_rates.items()}
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame([["Bank of England UK yield curve data"]]).to_excel(
            writer, sheet_name="info", header=False, index=False
        )
        _boe_sheet("short end", [1 / 12, 6 / 12], short).to_excel(
            writer, sheet_name=SHEETS[0], header=False, index=False
        )
        _boe_sheet("spot curve", [0.5, 10.0], long).to_excel(
            writer, sheet_name=SHEETS[1], header=False, index=False
        )
    return buffer.getvalue()


def zip_of(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def fake_fetch(market_cfg: MarketRatesConfig):
    """A Fetch that answers each configured URL with a sample, and fails on any other URL."""
    history = zip_of(
        {
            # Too old to be needed: unreadable on purpose, so reading it would fail the test.
            "GLC Nominal daily data_1979 to 1984.xlsx": b"not a workbook",
            "GLC Nominal daily data_2025 to present.xlsx": boe_workbook(
                {datetime(2026, 7, 20): 4.0, datetime(2026, 7, 21): 4.1}
            ),
            "GLC Real daily data_2025 to present.xlsx": b"other kind of curve",
        }
    )
    latest = zip_of(
        {"GLC Nominal daily data current month.xlsx": boe_workbook({datetime(2026, 7, 21): 4.3})}
    )
    start = market_cfg.start_date.isoformat()
    responses = {market_cfg.boe.archive_urls[0]: history, market_cfg.boe.archive_urls[1]: latest}
    for code in market_cfg.fred.series:
        responses[market_cfg.fred.url.format(series=code, start=start)] = fred_csv(code).encode()
    for code in market_cfg.ecb.series:
        responses[market_cfg.ecb.url.format(series=code, start=start)] = ecb_csv(code).encode()
    return responses.__getitem__  # KeyError for any URL the code was not expected to request


# --- Parsers -----------------------------------------------------------------------------------


def test_fred_parser_drops_missing_values():
    points = parse_fred_csv(fred_csv("DGS10"), "DGS10")
    assert points["rate"].tolist() == [4.10, 4.20]  # '.' is FRED's marker for no value
    assert points["date"].dt.day.tolist() == [20, 22]


def test_ecb_parser_reads_values_and_treats_empty_reply_as_no_data():
    assert parse_ecb_csv(ecb_csv("SR_1Y"), "SR_1Y")["rate"].tolist() == [2.5, 2.6]
    assert parse_ecb_csv("", "SR_1Y").empty


def test_boe_parser_combines_sheets_and_later_sheet_wins():
    curve = parse_boe_workbook(boe_workbook({datetime(2026, 7, 20): 4.0}), SHEETS)
    by_tenor = dict(zip(curve["tenor_years"], curve["rate"], strict=True))
    assert by_tenor == {0.5: 4.0, 10.0: 5.0}  # 1M was blank; 6M from the long sheet (4.0, not 3.5)


def test_boe_parser_rejects_unexpected_layout():
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet in SHEETS:
            pd.DataFrame([["no years row here"]]).to_excel(
                writer, sheet_name=sheet, header=False, index=False
            )
    with pytest.raises(ValueError, match="years:"):
        parse_boe_workbook(buffer.getvalue(), SHEETS)


@pytest.mark.parametrize(
    ("name", "needed"),
    [
        ("GLC Nominal daily data_2025 to present.xlsx", True),
        ("GLC Nominal daily data_2016 to 2024.xlsx", False),  # ends before 2026
        ("GLC Nominal daily data current month.xlsx", True),
        ("GLC Real daily data_2025 to present.xlsx", False),  # not the nominal curve
        ("GLC Nominal daily data_2025 to present.csv", False),  # not a workbook
    ],
)
def test_only_needed_boe_workbooks_are_read(name, needed):
    assert _workbook_is_needed(name, "GLC Nominal daily data", date(2026, 7, 20)) is needed


# --- Whole fetch -------------------------------------------------------------------------------


def test_fetch_curves_covers_every_currency_in_decimals(market_cfg):
    curves = fetch_curves(market_cfg, fake_fetch(market_cfg))
    assert set(curves["currency"]) == {"USD", "EUR", "GBP"}
    assert curves["rate"].between(0.0, 0.1).all()  # percent became decimal
    usd_10y = curves[(curves["currency"] == "USD") & (curves["tenor_years"] == 10.0)]
    assert usd_10y["rate"].tolist() == pytest.approx([0.041, 0.042])
    gbp = curves[curves["currency"] == "GBP"].set_index(["date", "tenor_years"])["rate"]
    assert gbp[(pd.Timestamp("2026-07-21"), 0.5)] == pytest.approx(0.043)  # current month wins


def test_series_with_no_data_is_an_error(market_cfg):
    fetch = fake_fetch(market_cfg)
    empty_url = market_cfg.ecb.url.format(series="SR_10Y", start=market_cfg.start_date.isoformat())

    def fetch_with_gap(url):
        return b"" if url == empty_url else fetch(url)

    with pytest.raises(ValueError, match="SR_10Y"):
        fetch_curves(market_cfg, fetch_with_gap)


def test_write_outputs_records_provenance(market_cfg, tmp_path):
    cfg = market_cfg.model_copy(
        update={
            "output_file": tmp_path / "curves.parquet",
            "provenance_file": tmp_path / "provenance.json",
        }
    )
    curves = fetch_curves(cfg, fake_fetch(cfg))
    retrieved_at = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    write_outputs(cfg, curves, retrieved_at)

    provenance = json.loads(cfg.provenance_file.read_text(encoding="utf-8"))
    assert provenance["rows"] == len(curves)
    assert provenance["retrieved_at"] == retrieved_at.isoformat()
    assert provenance["sha256"] == hashlib.sha256(cfg.output_file.read_bytes()).hexdigest()
    assert provenance["currencies"]["GBP"]["source"] == "boe"
    pd.testing.assert_frame_equal(pd.read_parquet(cfg.output_file), curves, check_dtype=False)


# --- Config ------------------------------------------------------------------------------------


def test_config_rejects_url_without_placeholders(market_cfg):
    raw = market_cfg.model_dump()
    raw["fred"]["url"] = "https://example.org/fixed.csv"
    with pytest.raises(ValidationError):
        MarketRatesConfig.model_validate(raw)


def test_config_rejects_two_sources_for_one_currency(market_cfg):
    raw = market_cfg.model_dump()
    raw["ecb"]["currency"] = "USD"
    with pytest.raises(ValidationError):
        MarketRatesConfig.model_validate(raw)
