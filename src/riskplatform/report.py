"""The daily PDF report: one A4 document per run for people who read risk on paper or in email.

Run with:  python -m riskplatform.report            (or: riskplatform-report; the latest run)
           python -m riskplatform.report --source s3      (the run in latest.json)
The pipeline also writes it for every run that gets this far (its report stage), to
data/reports/<run_id>/daily_report.pdf, and publishes it to S3 with the rest of the run.

Sections: the headline for both rulebooks (limit breaches and warnings; the group's LCR, NSFR
and EVE loss / Tier 1 with the change since the day before), the limits at warning or breach,
liquidity (latest ratios and the LCR trend against the minimum and the internal limit), IRRBB
(EVE loss / Tier 1 against the outlier test, NII under the parallel shocks), what changed since
the day before, and data quality.

Every number comes from riskplatform.dashboard.data, the same tested queries the dashboard
uses, so the PDF, the dashboard, the email and the Tableau extracts always agree. Nothing is
calculated here.

Labelled choices: ReportLab draws the chart itself (no matplotlib); the standard PDF font
(Helvetica) only, so the text sticks to plain characters (no Greek delta); the PDF is written in
ReportLab's invariant mode, so the same run always gives the same bytes (no timestamp inside).
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from reportlab.graphics.charts.legends import LineLegend
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, Line, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from riskplatform.config import load_storage_config
from riskplatform.dashboard import data
from riskplatform.logs import setup_logging
from riskplatform.query import QueryInputError, Source, connect

logger = logging.getLogger(__name__)

REPORT_FILE = "daily_report.pdf"
REGIMES = {"basel": "Basel", "pra": "UK PRA"}
LIMIT_NAMES = {"lcr": "LCR", "nsfr": "NSFR", "eve_tier1": "EVE loss / Tier 1"}
TOP_DRIVERS = 5  # a page, not a database: the full list is in the extracts and the dashboard

# The same colours as the dashboard (the reference chart palette): entity slots, neutral group,
# reserved status colours (always with the status word).
INK, MUTED, RULE = "#0b0b0b", "#52514e", "#e5e4e0"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
STATUS = {"ok": "#0ca30c", "warning": "#fab219", "breach": "#d03b3b"}
STATUS_WORDS = {"warning": "warning", "breach": "breach", "outlier": "breach"}  # word -> colour

WIDTH = A4[0] - 30 * mm  # the text width inside 15 mm margins


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["BodyText"], fontSize=8.5, leading=11)
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontSize=16, leading=19,
                                alignment=0, spaceAfter=2),
        "subtitle": ParagraphStyle("subtitle", parent=body, textColor=colors.HexColor(MUTED)),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=11, leading=14,
                             spaceBefore=8, spaceAfter=3, keepWithNext=1),
        "body": body,
        "small": ParagraphStyle("small", parent=body, fontSize=7, leading=9,
                                textColor=colors.HexColor(MUTED)),
        "cell": ParagraphStyle("cell", parent=body, fontSize=7.5, leading=9),
        "num": ParagraphStyle("num", parent=body, fontSize=7.5, leading=9, alignment=TA_RIGHT),
    }  # fmt: skip


STYLES = _styles()


def pct(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value:.1%}"


def bp(value) -> str:
    """A change in a ratio as basis points (0.0014 -> '+14.0 bp')."""
    return "" if value is None or pd.isna(value) else f"{value * 10_000:+,.1f} bp"


def mn(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value:+,.1f}"


def _table(rows: list[list], widths: list[float], numeric_from: int = 1) -> Table:
    """A plain table: bold header, hairlines, numbers right-aligned from column numeric_from."""
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 7.5),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor(INK)),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor(MUTED)),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor(RULE)),
                ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _status_cells(table: Table, rows: list[list], column: int) -> None:
    """Colour the status cells (the word stays in the cell: colour is never alone)."""
    for index, row in enumerate(rows[1:], start=1):
        status = STATUS_WORDS.get(str(row[column]).lower())
        if status:
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (column, index), (column, index),
                         colors.HexColor(STATUS[status])),
                        ("FONT", (column, index), (column, index), "Helvetica-Bold", 7.5),
                        ("TEXTCOLOR", (column, index), (column, index),
                         colors.white if status == "breach" else colors.HexColor(INK)),
                    ]
                )
            )  # fmt: skip


# --- Sections ------------------------------------------------------------------------------------


def headline(con) -> list:
    rows = [["Rulebook", "Breaches", "Warnings", "Group LCR", "Group NSFR", "Group EVE / Tier 1"]]
    for regime, name in REGIMES.items():
        counts = data.status_counts(con, regime)
        group = data.group_headline(con, regime).set_index("limit_name")
        cells = [
            f"{pct(group.loc[limit, 'actual'])}  {bp(group.loc[limit, 'change'])}".strip()
            for limit in ("lcr", "nsfr", "eve_tier1")
        ]
        rows.append([name, counts["breach"], counts["warning"], *cells])
    widths = [22 * mm, 18 * mm, 18 * mm, 38 * mm, 38 * mm, WIDTH - 134 * mm]
    note = Paragraph(
        "Changes are since the business day before. Limits are the bank's own "
        "(config/limits.yaml), not the regulatory minimums.",
        STYLES["small"],
    )
    return [_table(rows, widths), Spacer(1, 2), note]


def limits_section(con, levels: dict[str, float]) -> list:
    rows = [
        ["Rulebook", "Who", "Limit", "Actual", "Limit", "Used", "Status", "Since the day before"]
    ]
    for regime, name in REGIMES.items():
        table = data.limit_status(con, regime)
        flagged = table[table["status"] != "ok"].sort_values("utilisation", ascending=False)
        for _, row in flagged.iterrows():
            rows.append([
                name, row["who"], LIMIT_NAMES[row["limit_name"]], pct(row["actual"]),
                f"{pct(row['limit_value'])} {row['kind']}", pct(row["utilisation"]),
                row["status"].upper(), row["movement"],
            ])  # fmt: skip
    intro = Paragraph(
        f"Warning from {levels['warning']:.0%} of a limit used, breach from "
        f"{levels['breach']:.0%}. A floor (LCR, NSFR) is used by limit / actual; a cap "
        "(EVE loss / Tier 1) by actual / limit.",
        STYLES["small"],
    )
    if len(rows) == 1:
        return [Paragraph("Every internal limit is ok in both rulebooks.", STYLES["body"]), intro]
    widths = [18 * mm, 18 * mm, 27 * mm, 18 * mm, 22 * mm, 16 * mm, 20 * mm]
    widths.append(WIDTH - sum(widths))
    table = _table(rows, widths, numeric_from=3)
    table.setStyle(TableStyle([("ALIGN", (6, 0), (-1, -1), "LEFT")]))
    _status_cells(table, rows, column=6)
    return [table, Spacer(1, 2), intro]


def lcr_chart(con, regime: str = "basel") -> Drawing:
    """The LCR of each entity and the group over the run, with the minimum and the limit."""
    trend = data.ratio_trend(con, "lcr", regime)
    minimum, limit = float(trend["minimum"].max()), data.limit_value(con, "lcr")
    names = sorted(n for n in trend["who"].unique() if n != data.GROUP) + [data.GROUP]
    palette = {n: SLOTS[i] for i, n in enumerate(names[:-1])} | {data.GROUP: MUTED}
    start = trend["date"].min()
    days = lambda d: (d - start).days
    series = [
        [(days(d), float(v)) for d, v in trend.loc[trend["who"] == n, ["date", "value"]].itertuples(
            index=False)]
        for n in names
    ]  # fmt: skip
    height = 62 * mm
    drawing = Drawing(WIDTH, height)
    plot = LinePlot()
    plot.x, plot.y, plot.width, plot.height = 12 * mm, 9 * mm, WIDTH - 45 * mm, height - 14 * mm
    plot.data = series
    low = min(minimum, min(v for s in series for _, v in s)) * 0.95
    high = max(v for s in series for _, v in s) * 1.03
    plot.yValueAxis.valueMin, plot.yValueAxis.valueMax = low, high
    plot.yValueAxis.labelTextFormat = lambda v: f"{v:.0%}"
    plot.yValueAxis.labels.fontSize = plot.xValueAxis.labels.fontSize = 6.5
    plot.yValueAxis.visibleGrid, plot.yValueAxis.gridStrokeColor = True, colors.HexColor(RULE)
    plot.yValueAxis.strokeColor = plot.xValueAxis.strokeColor = colors.HexColor(RULE)
    span = max(days(trend["date"].max()), 1)
    plot.xValueAxis.valueMin, plot.xValueAxis.valueMax = 0, span
    step = max(span // 5, 1)
    ticks = list(range(0, span + 1, step))
    if span - ticks[-1] >= step / 2:  # the last day too, unless its label would collide
        ticks.append(span)
    plot.xValueAxis.valueSteps = ticks
    plot.xValueAxis.labelTextFormat = lambda d: (start + pd.Timedelta(days=d)).strftime("%d %b")
    for index, name in enumerate(names):
        plot.lines[index].strokeColor = colors.HexColor(palette[name])
        plot.lines[index].strokeWidth = 1.6 if name == data.GROUP else 1.1
    drawing.add(plot)

    def level_line(value: float, text: str, dash: list[int] | None) -> None:
        y = plot.y + (value - low) / (high - low) * plot.height
        line = Line(plot.x, y, plot.x + plot.width, y, strokeColor=colors.HexColor(MUTED))
        line.strokeWidth, line.strokeDashArray = 0.8, dash
        drawing.add(line)
        drawing.add(String(plot.x + 2, y + 1.5, text, fontSize=6, fillColor=colors.HexColor(MUTED)))

    level_line(minimum, f"regulatory minimum {minimum:.0%}", None)
    level_line(limit, f"internal limit {limit:.0%}", [3, 2])
    legend = LineLegend()
    legend.x, legend.y = plot.x + plot.width + 5 * mm, plot.y + plot.height
    legend.fontSize, legend.dx, legend.dy, legend.deltay = 6.5, 8, 2, 9
    legend.columnMaximum = len(names)  # one column
    legend.colorNamePairs = [(colors.HexColor(palette[n]), n) for n in names]
    drawing.add(legend)
    return drawing


def liquidity_section(con) -> list:
    lcr = {r: data.ratio_trend(con, "lcr", r) for r in REGIMES}
    nsfr = {r: data.ratio_trend(con, "nsfr", r) for r in REGIMES}

    def latest(trend: pd.DataFrame) -> pd.Series:
        return trend[trend["date"] == trend["date"].max()].set_index("who")["value"]

    values = {(m, r): latest(t[r]) for m, t in (("LCR", lcr), ("NSFR", nsfr)) for r in REGIMES}
    rows = [["Who", "LCR Basel", "LCR UK PRA", "NSFR Basel", "NSFR UK PRA"]]
    for who in values[("LCR", "basel")].index:
        rows.append([who, *(pct(values[key].get(who)) for key in values)])
    widths = [30 * mm, *[28 * mm] * 4]
    caption = Paragraph(
        f"LCR, Basel rules, every business day of the run ({lcr['basel']['date'].min():%d %b} to "
        f"{lcr['basel']['date'].max():%d %b %Y}).",
        STYLES["small"],
    )
    return [_table(rows, widths), Spacer(1, 4), KeepTogether([lcr_chart(con), caption])]


def irrbb_section(con) -> list:
    rows = [["Rulebook", "Who", "EVE loss / Tier 1", "Test", "Worst scenario", "Result",
             "NII +200bp (GBP mn)", "NII -200bp"]]  # fmt: skip
    for regime, name in REGIMES.items():
        risk = data.eve_against_tier1(con, regime)
        nii = data.nii_by_scenario(con, regime).pivot(
            index="who", columns="scenario", values="delta_nii"
        )
        for _, row in risk.iterrows():
            rows.append([
                name, row["who"], pct(row["share"]), pct(row["test"]), row["worst_scenario"],
                "OUTLIER" if row["outlier"] else "within", mn(nii.loc[row["who"], "parallel_up"]),
                mn(nii.loc[row["who"], "parallel_down"]),
            ])  # fmt: skip
    widths = [18 * mm, 18 * mm, 24 * mm, 14 * mm, 26 * mm, 18 * mm, 30 * mm]
    widths.append(WIDTH - sum(widths))
    table = _table(rows, widths, numeric_from=2)
    table.setStyle(TableStyle([("ALIGN", (4, 0), (5, -1), "LEFT")]))
    _status_cells(table, rows, column=5)
    note = Paragraph(
        "Outlier test: the worst EVE loss over the six standardised shocks against 15% of "
        "Tier 1 (Basel SRP31.82-31.83; UK ICAA 9.4A). NII: the change in net interest income over the next "
        "12 months under the parallel shocks, constant balance sheet.",
        STYLES["small"],
    )
    return [table, Spacer(1, 2), note]


def changes_section(con) -> list:
    previous, latest = data.compared_dates(con)
    if previous is None:
        return [
            Paragraph("This run has one business day only: nothing to compare.", STYLES["body"])
        ]
    parts: list = [
        Paragraph(f"{latest:%d %b %Y} against {previous:%d %b %Y}.", STYLES["small"]),
        Spacer(1, 2),
    ]
    moved = [(REGIMES[r], m) for r in REGIMES for _, m in data.limit_movements(con, r).iterrows()]
    if moved:
        rows = [["Rulebook", "Movement", "Who", "Limit", "Before", "Now"]]
        for name, m in moved:
            rows.append([
                name, m["movement"], m["who"], LIMIT_NAMES[m["limit_name"]],
                f"{m['previous_status'] or 'not measured'} ({pct(m['previous_utilisation'])})",
                f"{m['latest_status']} ({pct(m['latest_utilisation'])})",
            ])  # fmt: skip
        widths = [20 * mm, 20 * mm, 20 * mm, 30 * mm, 40 * mm, 40 * mm]
        parts.append(_table(rows, widths, numeric_from=len(widths)))
    else:
        parts.append(Paragraph("No internal limit changed status.", STYLES["body"]))
    drivers = data.top_drivers(con, TOP_DRIVERS)
    rows = [["Entity", "Category", "Side", "Change (GBP mn)", "Volume", "FX"]]
    for _, d in drivers.iterrows():
        rows.append([d["entity"], d["category"], d["side"], mn(d["change_gbp"]),
                     mn(d["volume_gbp"]), mn(d["fx_gbp"])])  # fmt: skip
    widths = [20 * mm, 46 * mm, 18 * mm, 28 * mm, 22 * mm, 22 * mm]
    parts += [
        Spacer(1, 4),
        Paragraph(f"Largest {TOP_DRIVERS} balance moves (on balance sheet)", STYLES["body"]),
        _table(rows, widths, numeric_from=3),
        Spacer(1, 2),
        Paragraph(
            "Volume: the change in the position's own currency at the day before's FX rate. "
            "FX: today's balance times the FX move. They add up to the change.",
            STYLES["small"],
        ),
    ]
    return parts


def quality_section(con, dashboard: data.Dashboard) -> list:
    report = data.breaks_report(dashboard)
    rows = report["rows"]
    checks = data.file_checks(report)
    failed = checks[~checks["passed"]]
    text = (
        f"{sum(rows['arrived'].values()):,} rows arrived, {sum(rows['validated'].values()):,} "
        f"validated, {rows['quarantined']:,} quarantined. {int(checks['passed'].sum())} of "
        f"{len(checks)} file checks passed"
    )
    text += "." if failed.empty else ": failed " + ", ".join(failed["check"]) + "."
    parts: list = [Paragraph(text, STYLES["body"])]
    quarantined = data.quarantine_by_check(con)
    if not quarantined.empty:
        rows_ = [["Table", "Check", "Rows"]] + quarantined.astype(str).values.tolist()
        parts += [Spacer(1, 3), _table(rows_, [30 * mm, 70 * mm, 20 * mm], numeric_from=2)]
    return parts


# --- The document --------------------------------------------------------------------------------


def _page(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(colors.HexColor(MUTED))
    canvas.drawString(15 * mm, 9 * mm, f"riskplatform daily report, run {doc.run_id}. "
                      "All positions are synthetic.")  # fmt: skip
    canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _section(title: str, parts: list) -> KeepTogether:
    """A heading and its content on one page (moved to the next page whole if need be)."""
    return KeepTogether([Paragraph(title, STYLES["h2"]), *parts])


@dataclass(frozen=True)
class ReportRun:
    run_id: str
    path: Path
    pages: int


def write_report(dashboard: data.Dashboard, out: str | Path) -> ReportRun:
    """Write <out>/<run_id>/daily_report.pdf for the dashboard's run."""
    con, run_id = dashboard.con, dashboard.run_id
    path = Path(out) / run_id / REPORT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    previous, latest = data.compared_dates(con)
    since = "" if previous is None else f", compared with {previous:%d %b %Y}"
    story = [
        Paragraph(f"Liquidity and IRRBB daily report: {latest:%d %B %Y}", STYLES["title"]),
        Paragraph(f"Run {run_id}{since}. Basel and UK PRA rules side by side.", STYLES["subtitle"]),
        Spacer(1, 6),
        *headline(con),
        Paragraph("Limits at warning or breach", STYLES["h2"]),
        *limits_section(con, data.thresholds(dashboard)),
        Paragraph("Liquidity", STYLES["h2"]),
        *liquidity_section(con),
        _section("Interest rate risk in the banking book", irrbb_section(con)),
        _section("Since the day before", changes_section(con)),
        _section("Data quality", quality_section(con, dashboard)),
    ]
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=14 * mm,
        bottomMargin=15 * mm,
        title=f"Liquidity and IRRBB daily report {latest:%Y-%m-%d}",
        author="riskplatform",
        invariant=True,  # no timestamp or random id inside: the same run, the same bytes
    )
    doc.run_id = run_id
    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    logger.info("Wrote the daily report (%d pages) to %s", doc.page, path)
    return ReportRun(run_id=run_id, path=path, pages=doc.page)


def run_report(data_dir: str | Path, run_id: str) -> ReportRun:
    """The pipeline's report stage: this run's results under data_dir, to data_dir/reports/."""
    data_dir = Path(data_dir)
    dashboard = data.Dashboard(connect(data_dir, run_id), run_id, data_dir, "local")
    return write_report(dashboard, data_dir / "reports")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the daily PDF report of the latest run.")
    parser.add_argument("--source", choices=["local", "s3"], help="default: storage.yaml backend")
    parser.add_argument("--storage", type=Path, default=Path("config/storage.yaml"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--cache", type=Path, default=Path("data/cache"))
    parser.add_argument("--out", type=Path, default=Path("data/reports"))
    args = parser.parse_args(argv)

    setup_logging()
    storage = load_storage_config(args.storage)
    source: Source = args.source or storage.backend
    try:
        dashboard = data.open_dashboard(source, args.data, args.cache, storage)
    except QueryInputError as error:
        logger.error("%s", error)
        return 1
    print(write_report(dashboard, args.out).path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
