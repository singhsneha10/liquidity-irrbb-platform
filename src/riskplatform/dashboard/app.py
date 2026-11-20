"""The risk dashboard (Streamlit): the latest good run's limits, liquidity, IRRBB, what changed
since the day before, and data quality.

Run with:  python -m riskplatform.dashboard          (or: riskplatform-dashboard, make dashboard)
           RISKPLATFORM_STORAGE_BACKEND=s3 RISKPLATFORM_S3_BUCKET=<bucket> riskplatform-dashboard

It reads through riskplatform.query (data/ locally, latest.json in S3), so it shows exactly what
the query command shows and recalculates nothing (the numbers: riskplatform.dashboard.data).
RISKPLATFORM_DATA_DIR sets the data folder (default data/).

Colours follow the reference palette of the project's chart guidance: entities take the first
categorical slots in name order (so an entity keeps its colour), the group is a neutral dark
line, and limit status uses the reserved status colours, always with its word next to it.
Labelled simplification: one light theme (.streamlit/config.toml), so one set of colours.
"""

import os
import tempfile
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from riskplatform import whatif
from riskplatform.config import load_storage_config
from riskplatform.dashboard import data, executive
from riskplatform.dashboard.demo import DEMO_ENV, DemoBuildError, DemoRun, build_demo
from riskplatform.metrics.run import MetricsInputError
from riskplatform.query import QueryInputError

DATA_ENV = "RISKPLATFORM_DATA_DIR"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
GROUP_COLOUR = "#52514e"  # neutral: the group is the total, not another entity
STATUS = {"ok": "#0ca30c", "warning": "#fab219", "breach": "#d03b3b"}
STATUS_ICON = {"ok": "✅ ok", "warning": "⚠️ warning", "breach": "⛔ breach"}
INDICATOR_ICON = {"ok": "✅ ok", "alert": "⚠️ alert", "not_enough_days": "— too few days"}
INDICATOR_NAMES = {"retail_outflow": "Retail outflow, 4 weeks", "break_even_run_off":
                   "Break-even run-off (LCR to 110%)"}  # fmt: skip
REGIMES = {"basel": "Basel", "pra": "UK PRA"}
LIMIT_NAMES = {"lcr": "LCR", "nsfr": "NSFR", "eve_tier1": "EVE loss / Tier 1 (proxy)"}
TILE_NAMES = {**LIMIT_NAMES, "eve_tier1": "EVE / Tier 1 (proxy)"}  # short enough for a tile
# What each internal limit is, against the regulatory rules (config/limits.yaml, the why).
LIMIT_TYPE = {
    "lcr": "internal buffer above the regulatory minimum",
    "nsfr": "internal buffer above the regulatory minimum",
    "eve_tier1": "equal to the supervisory outlier test",
}
PROXY_NOTE = "Tier 1 (proxy): the synthetic bank has no capital data, so each entity's implied equity (assets minus liabilities) stands in for its Tier 1 capital (config/irrbb)."
# The standardised shock scenarios in the order the rules list them (SRP31.90), not alphabetical.
SCENARIOS = ["parallel_up", "parallel_down", "steepener", "flattener", "short_up", "short_down"]
GRID = "#e5e4e0"
NEWLINE = chr(10)  # joins the key messages into one Markdown list


def entity_colours(names) -> alt.Scale:
    """A fixed colour per entity (slots in name order), the group neutral."""
    entities = sorted(n for n in set(names) if n != data.GROUP)
    domain = [*entities, data.GROUP]
    return alt.Scale(domain=domain, range=[*SLOTS[: len(entities)], GROUP_COLOUR])


def status_scale() -> alt.Scale:
    return alt.Scale(domain=list(STATUS), range=list(STATUS.values()))


def _axis(fmt: str | None = None, title: str | None = None) -> alt.Axis:
    style = {"title": title, "gridColor": GRID, "domain": False, "tickColor": GRID}
    return alt.Axis(**style, format=fmt) if fmt else alt.Axis(**style)


def limit_chart(
    rows: pd.DataFrame, levels: dict[str, float], movement: str = "since the day before"
) -> alt.LayerChart:
    """Utilisation of one limit per entity and the group, with the warning and breach lines."""
    shown = rows.assign(used=rows["utilisation"].clip(upper=2.0))  # off the scale: drawn at 200%
    top = max(1.2, float(shown["used"].max()) * 1.1)
    bars = (
        alt.Chart(shown)
        .mark_bar(cornerRadiusEnd=4, height={"band": 0.6})
        .encode(
            y=alt.Y("who:N", title=None, sort=None),
            x=alt.X("used:Q", scale=alt.Scale(domain=[0, top]), axis=_axis(".0%", "used")),
            color=alt.Color("status:N", scale=status_scale(), legend=None),
            tooltip=[
                alt.Tooltip("who:N", title="who"),
                alt.Tooltip("utilisation:Q", format=".1%", title="used"),
                alt.Tooltip("actual:Q", format=".1%"),
                alt.Tooltip("limit_value:Q", format=".1%", title="limit"),
                alt.Tooltip("status:N"),
                alt.Tooltip("movement:N", title=movement),
            ],
        )
    )
    labels = bars.mark_text(align="left", dx=4, color="#0b0b0b").encode(
        text=alt.Text("utilisation:Q", format=".0%"), color=alt.value("#0b0b0b")
    )
    lines = pd.DataFrame(
        [{"at": levels["warning"], "line": "warning"}, {"at": levels["breach"], "line": "breach"}]
    )
    rules = (
        alt.Chart(lines)
        .mark_rule(strokeDash=[4, 3], strokeWidth=1.5, color="#52514e")
        .encode(x="at:Q", tooltip=[alt.Tooltip("line:N"), alt.Tooltip("at:Q", format=".0%")])
    )
    return (bars + labels + rules).properties(height=160)


def trend_chart(trend: pd.DataFrame, minimum: float, limit: float, label: str) -> alt.LayerChart:
    """A ratio over time per entity and the group, with the minimum and the internal limit."""
    colour = alt.Color("who:N", scale=entity_colours(trend["who"]), title=None)
    hover = alt.selection_point(fields=["date"], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(trend).encode(
        x=alt.X("date:T", axis=_axis(title=None)),
        y=alt.Y("value:Q", axis=_axis(".0%", label), scale=alt.Scale(zero=False)),
    )
    lines = base.mark_line(strokeWidth=2).encode(color=colour)
    points = (
        base.mark_point(size=60, filled=True, opacity=0)
        .encode(
            color=colour,
            tooltip=[
                alt.Tooltip("date:T"),
                alt.Tooltip("who:N"),
                alt.Tooltip("value:Q", format=".1%", title=label),
            ],
        )
        .add_params(hover)
    )
    shown = base.mark_point(size=60, filled=True, stroke="white", strokeWidth=2).encode(
        color=colour, opacity=alt.condition(hover, alt.value(1), alt.value(0))
    )
    crosshair = (
        alt.Chart(trend).mark_rule(color="#a3a29c").encode(x="date:T").transform_filter(hover)
    )
    floors = pd.DataFrame(
        [
            {"at": minimum, "line": f"regulatory minimum {minimum:.0%}", "dash": "solid"},
            {"at": limit, "line": f"internal limit {limit:.0%}", "dash": "dashed"},
        ]
    )
    rules = (
        alt.Chart(floors)
        .mark_rule(strokeWidth=1.5, color="#52514e")
        .encode(
            y="at:Q",
            strokeDash=alt.StrokeDash(
                "dash:N", scale=alt.Scale(domain=["solid", "dashed"], range=[[1, 0], [5, 4]]),
                legend=None,
            ),
            tooltip=[alt.Tooltip("line:N")],
        )
    )  # fmt: skip
    notes = rules.mark_text(align="left", baseline="bottom", dx=4, dy=-2, color="#52514e").encode(
        x=alt.value(0), text="line:N"
    )
    return (lines + rules + notes + crosshair + points + shown).properties(height=320)


def scenario_chart(rows: pd.DataFrame, value: str, title: str) -> alt.LayerChart:
    """One bar per scenario for the chosen entity or the group (one series: one colour)."""
    bars = (
        alt.Chart(rows)
        .mark_bar(color=SLOTS[0], cornerRadiusEnd=4, height={"band": 0.6})
        .encode(
            y=alt.Y(
                "scenario:N", title=None, sort=[s for s in SCENARIOS if s in set(rows["scenario"])]
            ),
            x=alt.X(f"{value}:Q", axis=_axis(",.0f", title)),
            tooltip=[alt.Tooltip("scenario:N"), alt.Tooltip(f"{value}:Q", format=",.1f")],
        )
    )
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color="#52514e").encode(x="x:Q")
    return (bars + zero).properties(height=240)  # side by side: the same height


def outlier_chart(rows: pd.DataFrame) -> alt.LayerChart:
    """The worst EVE loss as a share of Tier 1 against the 15% outlier test."""
    shown = rows.assign(result=rows["outlier"].map({True: "outlier", False: "within the test"}))
    bars = (
        alt.Chart(shown)
        .mark_bar(cornerRadiusEnd=4, height={"band": 0.6})
        .encode(
            y=alt.Y("who:N", title=None, sort=None),
            x=alt.X("share:Q", axis=_axis(".0%", "worst EVE loss / Tier 1 (proxy)")),
            color=alt.Color(
                "result:N",
                scale=alt.Scale(
                    domain=["within the test", "outlier"], range=[STATUS["ok"], STATUS["breach"]]
                ),
                title=None,
            ),
            tooltip=[
                alt.Tooltip("who:N"),
                alt.Tooltip("share:Q", format=".1%", title="EVE loss / Tier 1 (proxy)"),
                alt.Tooltip("eve_risk:Q", format=",.1f", title="EVE loss (GBP mn)"),
                alt.Tooltip("tier1:Q", format=",.1f", title="Tier 1 proxy (GBP mn)"),
                alt.Tooltip("worst_scenario:N"),
                alt.Tooltip("result:N"),
            ],
        )
    )
    test = float(rows["test"].iloc[0])
    rule = (
        alt.Chart(pd.DataFrame({"at": [test], "line": [f"outlier test {test:.0%}"]}))
        .mark_rule(strokeDash=[4, 3], strokeWidth=1.5, color="#52514e")
        .encode(x="at:Q", tooltip=["line:N"])
    )
    return (bars + rule).properties(height=180)


def driver_chart(drivers: pd.DataFrame) -> alt.Chart:
    """Each top driver's change split into volume and FX (two series, side by side)."""
    shown = drivers.assign(driver=drivers["entity"] + " " + drivers["category"])
    long = shown.melt(
        id_vars=["driver", "side", "change_gbp"],
        value_vars=["volume_gbp", "fx_gbp"],
        var_name="part",
        value_name="gbp_mn",
    )
    long["part"] = long["part"].map({"volume_gbp": "volume", "fx_gbp": "FX"})
    return (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y(
                "driver:N", title=None, sort=list(shown["driver"]), axis=alt.Axis(labelLimit=320)
            ),
            yOffset=alt.YOffset("part:N", sort=["volume", "FX"]),
            x=alt.X("gbp_mn:Q", axis=_axis(",.0f", "change since the day before (GBP mn)")),
            color=alt.Color(
                "part:N", scale=alt.Scale(domain=["volume", "FX"], range=SLOTS[:2]), title=None
            ),
            tooltip=[
                alt.Tooltip("driver:N"),
                alt.Tooltip("side:N"),
                alt.Tooltip("part:N"),
                alt.Tooltip("gbp_mn:Q", format="+,.1f", title="GBP mn"),
                alt.Tooltip("change_gbp:Q", format="+,.1f", title="total change"),
            ],
        )
        .properties(height=34 * len(shown) + 20)
    )


# --- The page -----------------------------------------------------------------------------------


@st.cache_resource(
    show_spinner="Building the synthetic bank with the full pipeline (about a minute, once)..."
)
def _demo() -> DemoRun:
    """The demo run (riskplatform.dashboard.demo), built once per server start and shared."""
    folder = tempfile.mkdtemp(prefix="riskplatform-demo-")
    return build_demo(Path.cwd(), folder)  # Streamlit runs from the repository root


@st.cache_resource(ttl=300, show_spinner="Opening the latest run...")
def _open(source: str, data_dir: str) -> data.Dashboard:
    """One connection per source and folder, reopened every 5 minutes to pick up a new run."""
    storage = load_storage_config(Path("config/storage.yaml"))
    return data.open_dashboard(source, data_dir, Path(data_dir) / "cache", storage)


def _percent(value) -> str:
    return "n/a" if pd.isna(value) else f"{value:.1%}"


def headline(con, regime: str) -> None:
    counts = data.status_counts(con, regime)
    group = data.group_headline(con, regime).set_index("limit_name")
    tiles = st.columns(5)
    tiles[0].metric("Limits in breach", counts["breach"], border=True)
    tiles[1].metric("Limits at warning", counts["warning"], border=True)
    for tile, name in zip(tiles[2:], ["lcr", "nsfr", "eve_tier1"], strict=True):
        row = group.loc[name]
        change = None if pd.isna(row["change"]) else f"{row['change'] * 10_000:+,.1f} bp"
        tile.metric(
            f"Group {TILE_NAMES[name]}",
            _percent(row["actual"]),
            delta=change,
            delta_color="inverse" if row["kind"] == "cap" else "normal",  # a cap: up is worse
            help=f"Internal limit {row['limit_value']:.0%} ({row['kind']}): "
            f"{STATUS_ICON[row['status']]}",
            border=True,
        )


BADGE = {
    "green": ("GREEN", "green", ":material/check_circle:"),
    "amber": ("AMBER", "orange", ":material/warning:"),
    "red": ("RED", "red", ":material/error:"),
}  # the word is always shown with the colour


def _badge(status: str) -> None:
    word, colour, icon = BADGE[status]
    st.badge(word, color=colour, icon=icon)


def headroom_chart(rows: pd.DataFrame, levels: dict[str, float]) -> alt.LayerChart:
    """Per limit, the tightest entity or group: the share of the limit used."""
    shown = rows.assign(
        item=rows["label"] + " (" + rows["who"] + ")", used=rows["utilisation"].clip(upper=2.0)
    )
    top = max(1.2, float(shown["used"].max()) * 1.1)
    bars = (
        alt.Chart(shown)
        .mark_bar(cornerRadiusEnd=4, height={"band": 0.6})
        .encode(
            y=alt.Y("item:N", title=None, sort=None, axis=alt.Axis(labelLimit=260)),
            x=alt.X(
                "used:Q", scale=alt.Scale(domain=[0, top]), axis=_axis(".0%", "of the limit used")
            ),
            color=alt.Color("status:N", scale=status_scale(), legend=None),
            tooltip=[
                alt.Tooltip("item:N", title="limit"),
                alt.Tooltip("utilisation:Q", format=".1%", title="used"),
                alt.Tooltip("actual:Q", format=".1%"),
                alt.Tooltip("limit_value:Q", format=".0%", title="limit"),
                alt.Tooltip("status:N"),
            ],
        )
    )
    labels = bars.mark_text(align="left", dx=4).encode(
        text=alt.Text("utilisation:Q", format=".0%"), color=alt.value("#0b0b0b")
    )
    lines = pd.DataFrame(
        [{"at": levels["warning"], "line": "warning"}, {"at": levels["breach"], "line": "breach"}]
    )
    rules = (
        alt.Chart(lines)
        .mark_rule(strokeDash=[4, 3], strokeWidth=1.5, color="#52514e")
        .encode(x="at:Q", tooltip=[alt.Tooltip("line:N"), alt.Tooltip("at:Q", format=".0%")])
    )
    return (bars + labels + rules).properties(height=150)


def small_trend(trend: pd.DataFrame, limit: float, title: str, kind: str) -> alt.LayerChart:
    """The group's ratio over the run, with its internal limit (one series: one colour)."""
    group = trend[trend["who"] == data.GROUP]
    line = (
        alt.Chart(group)
        .mark_line(strokeWidth=2, color=SLOTS[0])
        .encode(
            x=alt.X(
                "date:T",
                axis=alt.Axis(
                    format="%d %b",
                    title=None,
                    tickCount=5,
                    gridColor=GRID,
                    domain=False,
                    tickColor=GRID,
                ),
            ),
            y=alt.Y("value:Q", axis=_axis(".0%", None), scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T"), alt.Tooltip("value:Q", format=".1%", title=title)],
        )
    )
    word = "floor" if kind == "floor" else "cap"
    note = f"internal limit {limit:.0%} ({word})"
    rule = (
        alt.Chart(pd.DataFrame({"at": [limit], "line": [note]}))
        .mark_rule(strokeDash=[5, 4], strokeWidth=1.5, color="#52514e")
        .encode(y="at:Q", tooltip=["line:N"])
    )
    text = rule.mark_text(align="left", baseline="bottom", dx=4, dy=-2, color="#52514e").encode(
        x=alt.value(0), text="line:N"
    )
    return (line + rule + text).properties(height=130, title=alt.TitleParams(title, fontSize=12))


def executive_tab(dashboard: data.Dashboard, con, regime: str) -> None:
    """The one-page view for a CRO or treasurer (riskplatform.dashboard.executive)."""
    card_list = executive.cards(con, regime)
    status = executive.overall(card_list)
    left, right = st.columns([1, 5], vertical_alignment="center")
    with left:
        st.markdown("**Overall status**")
    with right:
        _badge(status)
    columns = st.columns(len(card_list))
    for column, card in zip(columns, card_list, strict=True):
        with column.container(border=True):
            st.markdown(f"**{card.area}**")
            _badge(card.status)
            st.caption(card.reason)
    st.markdown("**Key messages**")
    st.markdown(NEWLINE.join(f"- {line}" for line in executive.key_messages(con, regime)))
    left, right = st.columns([1, 1])
    with left:
        st.markdown("**Headroom to the internal limits** (tightest entity or the group)")
        st.altair_chart(headroom_chart(executive.headroom(con, regime), data.thresholds(dashboard)))
    with right:
        st.markdown("**Group trends** (every business day of the run)")
        lcr, nsfr = data.ratio_trend(con, "lcr", regime), data.ratio_trend(con, "nsfr", regime)
        st.altair_chart(small_trend(lcr, data.limit_value(con, "lcr"), "Group LCR", "floor"))
        st.altair_chart(small_trend(nsfr, data.limit_value(con, "nsfr"), "Group NSFR", "floor"))
        eve = data.eve_share_trend(con, regime)
        st.altair_chart(
            small_trend(
                eve, data.limit_value(con, "eve_tier1"), "Group EVE loss / Tier 1 (proxy)", "cap"
            )
        )
    rulebook_comparison_section(con)
    st.caption(
        "Status: red for a limit breach, a missed regulatory minimum or an IRRBB outlier; amber "
        "for a limit at warning or an early-warning indicator in alert. Every sentence is built "
        "from the figures in the other tabs. The model validation of the LCR run-off "
        "assumptions is in the project's validation report."
    )


def _points(value: float) -> str:
    """A difference in percentage points, e.g. +1.4 pts; 0.0 pts (not -0.0) when it rounds away."""
    rounded = round(value * 100, 1)
    return "0.0 pts" if rounded == 0 else f"{rounded:+.1f} pts"


def rulebook_comparison_section(con) -> None:
    """Both rulebooks side by side, whatever the sidebar shows (the differences are small)."""
    table = data.rulebook_comparison(con)
    st.markdown("**Basel vs UK PRA** (latest day; the sidebar switches the rest of the page)")
    shown = table.assign(
        basel=table["basel"].map(lambda v: f"{v:.1%}"),
        pra=table["pra"].map(lambda v: f"{v:.1%}"),
        difference=table["difference"].map(_points),
    ).rename(columns={"basel": "Basel", "pra": "UK PRA", "difference": "UK minus Basel"})
    st.dataframe(shown, hide_index=True)
    st.caption(
        "The two rulebooks share the run-off rates, the shock sizes and the limits, so results "
        "differ only where a treatment differs. The UK counts covered bonds as Level 1 HQLA "
        "(Basel: Level 2A) and gives government and covered bonds lower NSFR funding factors, "
        "so UK LCR and NSFR are a little higher. For IRRBB, the UK floors shocked rates (-100 bp "
        "rising to 0% at 20 years) and lets gains offset losses at 50% across currencies, so EVE "
        "losses can move either way. Details: the liquidity and IRRBB methodology documents."
    )


def outlier_phrase(names: list[str]) -> str:
    """'BANK_UK is also a supervisory outlier', or 'BANK_UK and GROUP are also supervisory
    outliers' (the grammar follows the count)."""
    if len(names) == 1:
        return f"{names[0]} is also a supervisory outlier"
    listed = ", ".join(names[:-1]) + f" and {names[-1]}"
    return f"{listed} are also supervisory outliers"


def limits_tab(dashboard: data.Dashboard, con, regime: str) -> None:
    levels = data.thresholds(dashboard)
    st.caption(
        f"Share of each internal limit used on the latest date (config/limits.yaml). Warning from "
        f"{levels['warning']:.0%}, breach from {levels['breach']:.0%} (the dashed lines). "
        "These are the bank's own limits, not the regulatory minimums. Used: for a floor (LCR, "
        "NSFR) the limit divided by the ratio, so a higher ratio uses less; for a cap (EVE "
        "loss) the loss divided by the limit."
    )
    table = data.limit_status(con, regime)
    columns = st.columns(len(LIMIT_NAMES))
    for column, (name, title) in zip(columns, LIMIT_NAMES.items(), strict=True):
        column.markdown(f"**{title}**")
        column.altair_chart(limit_chart(table[table["limit_name"] == name], levels))
    shown = table.assign(
        limit=table["limit_name"].map(LIMIT_NAMES),
        type=table["limit_name"].map(LIMIT_TYPE),
        status=table["status"].map(STATUS_ICON),
    )
    labels = [
        f"{LIMIT_NAMES[n]} · {w}" for n, w in zip(table["limit_name"], table["who"], strict=True)
    ]

    def clicked() -> None:  # a row clicked in the table picks the limit to explain
        rows = st.session_state["limits_table"].selection.rows
        if rows:
            st.session_state["driver_pick"] = labels[rows[0]]

    st.dataframe(
        shown[
            [
                "limit",
                "type",
                "who",
                "kind",
                "actual",
                "limit_value",
                "utilisation",
                "status",
                "movement",
            ]
        ],
        hide_index=True,
        column_config={
            "actual": st.column_config.NumberColumn(format="percent"),
            "limit_value": st.column_config.NumberColumn("limit", format="percent"),
            "utilisation": st.column_config.NumberColumn("used", format="percent"),
        },
        key="limits_table",
        on_select=clicked,
        selection_mode="single-row",
    )
    outliers = data.eve_against_tier1(con, regime)
    flagged = list(outliers.loc[outliers["outlier"], "who"])
    if flagged:
        st.caption(
            f"The EVE limit equals the supervisory outlier test (Basel SRP31.82; UK ICAA 9.4A), "
            f"so {outlier_phrase(flagged)}: under the UK rules the PRA must be notified. The LCR and NSFR limits sit "
            "above the 100% regulatory minimums, so breaching them is an internal matter."
        )
    st.caption(PROXY_NOTE)
    drivers_section(dashboard, con, regime, table, labels)
    early_warning_table(con, regime)


def early_warning_table(con, regime: str) -> None:
    """The early-warning indicators (config/early_warning.yaml), alerts first."""
    table = data.early_warning(con, regime)
    st.markdown("**Early-warning indicators**")
    st.caption(
        "Alert when 4-week retail outflows exceed their threshold (about the system's 99th "
        "percentile), or when the run-off that would take the LCR to the 110% limit is below its "
        "threshold. The bank's own judgement (config/early_warning.yaml). Retail outflow: the "
        "fall in retail deposits over the last 20 business days, all currencies at the first "
        "day's FX rates (so exchange-rate moves are not counted as withdrawals); a rise counts "
        "as 0%."
    )
    shown = table.assign(
        indicator=table["indicator"].map(INDICATOR_NAMES),
        status=table["status"].map(INDICATOR_ICON),
        category=table["category"].fillna(""),
    )
    st.dataframe(
        shown[["indicator", "who", "category", "value", "threshold", "status"]],
        hide_index=True,
        column_config={
            "value": st.column_config.NumberColumn(format="percent"),
            "threshold": st.column_config.NumberColumn(format="percent"),
        },
    )


def liquidity_tab(con, regime: str) -> None:
    st.caption(
        "Entity and group ratios over every business day of the run, against the regulatory "
        "minimum and the bank's internal limit."
    )
    for metric, title in (("lcr", "LCR"), ("nsfr", "NSFR")):
        trend = data.ratio_trend(con, metric, regime)
        minimum = float(trend["minimum"].max())
        st.markdown(f"**{title}**")
        st.altair_chart(trend_chart(trend, minimum, data.limit_value(con, metric), title))
    ladder_section(con)


def bucket_chart(
    rows: pd.DataFrame, value: str, cumulative: str, signs: tuple[str, str], unit: str
) -> alt.LayerChart:
    """Bars for each time bucket's net amount (coloured by sign, with a legend) and a line for
    the cumulative position. Both are in the same unit, so one axis (never a second one)."""
    order = list(rows["bucket"])
    shown = rows.assign(side=rows[value].ge(0).map({True: signs[0], False: signs[1]}))
    x = alt.X(
        "bucket:N",
        sort=order,
        title=None,
        axis=alt.Axis(labelAngle=-40, labelLimit=90, labelOverlap=False),
    )
    bars = (
        alt.Chart(shown)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            x=x,
            y=alt.Y(f"{value}:Q", axis=_axis(",.0f", unit)),
            color=alt.Color(
                "side:N", scale=alt.Scale(domain=list(signs), range=SLOTS[:2]), title=None
            ),
            tooltip=[alt.Tooltip("bucket:N"), alt.Tooltip(f"{value}:Q", format="+,.0f",
                                                          title="in the bucket"),
                     alt.Tooltip(f"{cumulative}:Q", format="+,.0f", title="cumulative")],
        )
    )  # fmt: skip
    line = (
        alt.Chart(shown)
        .mark_line(color="#52514e", strokeWidth=2, point=alt.OverlayMarkDef(size=40, filled=True, color="#52514e"))
        .encode(x=x, y=f"{cumulative}:Q", tooltip=[alt.Tooltip(f"{cumulative}:Q", format="+,.0f",
                                                                title="cumulative")])
    )  # fmt: skip
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#a3a29c").encode(y="y:Q")
    return (bars + zero + line).properties(height=300)


def ladder_section(con) -> None:
    """The contractual maturity ladder of one entity or the group."""
    st.markdown("**Maturity ladder** (contractual cash flows on the latest day)")
    whos = data.ladder_whos(con)  # the group comes last
    who = st.selectbox("Entity or group", whos, index=len(whos) - 1, key="ladder_who")
    rows = data.maturity_ladder(con, who)
    unit = f"{rows['currency'].iloc[0]} mn"
    st.altair_chart(bucket_chart(rows, "net", "cumulative_net", ("inflows exceed outflows",
                                                                 "outflows exceed inflows"),
                                 unit))  # fmt: skip
    st.caption(
        "Bars: cash in minus cash out in each bucket; line: the cumulative position. This is "
        "the contractual view: each position flows when it legally matures, so retail deposits, "
        "repayable on demand, sit in the overnight bucket. How much of them would really leave "
        "under stress is the LCR's job (its run-off rates), not the ladder's. Undrawn "
        f"commitments, a contingent outflow, are shown apart: {rows['contingent'].iloc[0]:,.0f} "
        f"{unit}. The final cumulative position equals the equity."
    )


def gap_section(con, regime: str) -> None:
    """The repricing gap of one entity's book in one currency."""
    books = data.gap_books(con, regime)
    labels = [f"{e} {c}" for e, c in books.itertuples(index=False)]
    choice = st.selectbox("Book (entity and currency)", labels, key="gap_book")
    entity, currency = choice.split(" ")
    rows = data.repricing_gap(con, regime, entity, currency)
    st.markdown(f"**Repricing gap** ({choice}, latest day, {currency} mn)")
    st.altair_chart(bucket_chart(rows, "gap", "cumulative_gap", ("more assets reprice",
                                                                 "more liabilities reprice"),
                                 f"{currency} mn"))  # fmt: skip
    st.caption(
        "Bars: assets minus liabilities that reprice in each of the 19 IRRBB time buckets "
        "(SRP31.96); line: the cumulative gap. A positive gap means more assets than "
        "liabilities reset their rate then, so income rises if rates rise. This is the "
        "behavioural view used for EVE: non-maturity deposits split into a core part, "
        "repricing at its behavioural maturity, and a non-core part repricing overnight "
        "(SRP31.111); swaps count as two legs; equity is excluded."
    )


def irrbb_tab(con, regime: str) -> None:
    risk = data.eve_against_tier1(con, regime)
    st.markdown("**Worst EVE loss against the outlier test** (Basel SRP31.82; UK ICAA 9.4A)")
    st.altair_chart(outlier_chart(risk))
    who = st.selectbox("Entity or group", list(risk["who"]), key="irrbb_who")
    left, right = st.columns(2)
    eve = data.eve_by_scenario(con, regime)
    nii = data.nii_by_scenario(con, regime)
    left.markdown("**EVE loss by scenario** (GBP mn, positive = a loss)")
    left.altair_chart(scenario_chart(eve[eve["who"] == who], "eve_loss", "EVE loss (GBP mn)"))
    left.caption(
        "Basel adds up currency losses only (SRP31.129(5)): a scenario in which every currency "
        "gains shows 0, not a gain."
        if regime == "basel"
        else "The UK counts gains at 50% against losses across currencies (SS31/15 2.9B(iv)), "
        "so a scenario can show a net gain (a negative loss)."
    )
    right.markdown("**Change in NII by scenario** (GBP mn, next 12 months)")
    right.altair_chart(scenario_chart(nii[nii["who"] == who], "delta_nii", "ΔNII (GBP mn)"))
    st.caption(PROXY_NOTE)
    gap_section(con, regime)


def changes_tab(con, regime: str) -> None:
    previous, latest = data.compared_dates(con)
    if previous is None:
        st.info("This run has only one business day: there is nothing to compare yet.")
        return
    st.caption(f"{latest:%d %b %Y} against the business day before, {previous:%d %b %Y}.")
    moved = data.limit_movements(con, regime)
    st.markdown("**Limits whose status moved**")
    if moved.empty:
        st.success("No internal limit changed status.")
    else:
        st.dataframe(
            moved.assign(limit_name=moved["limit_name"].map(LIMIT_NAMES)),
            hide_index=True,
            column_config={
                "previous_utilisation": st.column_config.NumberColumn("used before", format="percent"),
                "latest_utilisation": st.column_config.NumberColumn("used now", format="percent"),
            },
        )  # fmt: skip
    drivers = data.top_drivers(con, 10)
    st.markdown("**Largest balance moves** (on balance sheet, GBP mn): volume and FX")
    st.altair_chart(driver_chart(drivers))
    st.caption(
        "Volume = the change in the position's own currency at the day before's FX rate; "
        "FX = today's balance times the FX move. The two add up to the change in GBP."
    )


def quality_tab(dashboard: data.Dashboard, con) -> None:
    report = data.breaks_report(dashboard)
    rows = report["rows"]
    tiles = st.columns(3)
    tiles[0].metric("Rows arrived", f"{sum(rows['arrived'].values()):,}", border=True)
    tiles[1].metric("Rows validated", f"{sum(rows['validated'].values()):,}", border=True)
    tiles[2].metric("Rows quarantined", f"{rows['quarantined']:,}", border=True)
    quarantined = data.quarantine_by_check(con)
    st.markdown("**Quarantined rows by check**")
    if quarantined.empty:
        st.success("No rows were quarantined in this run.")
    else:
        st.dataframe(quarantined, hide_index=True)
    checks = data.file_checks(report)
    st.markdown("**File checks**")
    st.dataframe(
        checks.assign(passed=checks["passed"].map({True: "✅ passed", False: "⛔ failed"})),
        hide_index=True,
    )


# --- What-if and limit drivers (riskplatform.whatif) ---------------------------------------------

ROLES = {  # a driver's role -> (legend label, colour): what drains is orange, what holds is blue
    "loss": ("adds to the loss", SLOTS[1]),
    "offset": ("reduces the loss", SLOTS[0]),
    "hqla": ("HQLA after haircut", SLOTS[0]),
    "outflow": ("outflow", SLOTS[1]),
    "inflow": ("inflow", SLOTS[2]),
    "asf": ("available stable funding", SLOTS[0]),
    "rsf": ("required stable funding", SLOTS[1]),
}
LEVER_KEYS = ("wi_run_off", "wi_parallel", "wi_outflow")


@st.cache_resource(show_spinner="Loading the latest day's positions...")
def _day(validated_dir: str, run_id: str) -> whatif.Day:
    """The run's latest validated day and the rules, loaded once per run and shared."""
    return whatif.load_day(validated_dir, Path("config"))


def whatif_day(dashboard: data.Dashboard) -> whatif.Day | None:
    """The latest day to stress or explain, or None (with a note) if it cannot be loaded."""
    if dashboard.validated_dir is None:
        st.info("This run published no validated positions, so this section is not available.")
        return None
    try:
        day = _day(str(dashboard.validated_dir), dashboard.run_id)
    except (MetricsInputError, OSError, ValueError) as error:
        st.info(
            f"The latest day's positions could not be loaded, so this section is not available: {error}"
        )
        return None
    if day.run_id != dashboard.run_id:
        st.info(
            f"A newer run ({day.run_id}) has published positions since this page opened run {dashboard.run_id}; reload the page."
        )
        return None
    return day


@st.cache_data(max_entries=512, show_spinner=False)
def _what_if(_day: whatif.Day, run_id: str, regime: str, run_off: float, parallel: float,
             outflow: float) -> pd.DataFrame:  # fmt: skip
    """Cached per run and lever setting, so moving a slider back is instant."""
    return whatif.what_if(_day, regime, whatif.Levers(run_off, parallel, outflow))


@st.cache_data(max_entries=256, show_spinner=False)
def _drivers(_day: whatif.Day, run_id: str, kind: str, regime: str, who: str,
             scenario: str | None = None) -> pd.DataFrame:  # fmt: skip
    if kind == "eve":
        return whatif.eve_drivers(_day, regime, who, scenario)
    return {"lcr": whatif.lcr_drivers, "nsfr": whatif.nsfr_drivers}[kind](_day, regime, who)


def _reset_levers() -> None:
    for key in LEVER_KEYS:
        st.session_state[key] = 0


def _short(category: str) -> str:
    return category.removeprefix("retail_").removesuffix("_deposits").replace("_", " ")


def whatif_tab(dashboard: data.Dashboard, regime: str) -> None:
    day = whatif_day(dashboard)
    if day is None:
        return
    st.caption(
        f"Stress the latest day ({day.date:%d %b %Y}) by hand and watch the LCR, NSFR, EVE and "
        f"every internal limit move. Each figure is measured again with the pipeline's own code, "
        f"under the {REGIMES[regime]} rules chosen in the sidebar. Nothing is saved and the "
        "published figures do not change."
    )
    levers = day.whatif.levers
    outflows = day.liquidity[regime].lcr.outflows
    sizes = day.irrbb[regime].shocks.sizes_bp
    retail = day.whatif.retail_deposits
    reporting = day.cfg.fx.reporting_currency
    left, middle, right = st.columns(3)
    run_off = left.slider(
        "Retail run-off rates",
        0,
        round(levers.retail_run_off_add_on.max * 100),
        step=max(1, round(levers.retail_run_off_add_on.step * 100)),
        format="+%d pts",
        key="wi_run_off",
        help="Percentage points added to both retail run-off rates of the LCR (capped at 100%): "
        "how much of the deposits is assumed to leave over the 30-day stress.",
    )
    left.caption(", ".join(
        f"{_short(name)} {outflows[name].rate:.0%} → {min(1.0, outflows[name].rate + run_off / 100):.0%}"
        for name in retail
    ))  # fmt: skip
    parallel = middle.slider(
        "Parallel rate shock",
        0,
        int(levers.parallel_shock_add_on_bp.max),
        step=int(levers.parallel_shock_add_on_bp.step),
        format="+%d bp",
        key="wi_parallel",
        help="Basis points added to the parallel up and down shocks of every currency.",
    )
    middle.caption(", ".join(
        f"{currency} {size.parallel:.0f} → {size.parallel + parallel:.0f} bp"
        for currency, size in sizes.items()
    ))  # fmt: skip
    outflow = right.slider(
        "Deposit outflow today",
        0,
        round(levers.deposit_outflow.max * 100),
        step=max(1, round(levers.deposit_outflow.step * 100)),
        format="%d%%",
        key="wi_outflow",
        help="This share of every retail deposit is withdrawn today, before the 30-day stress "
        "starts. Each book pays from its own central bank reserves first, then government, "
        "covered and corporate bonds.",
    )
    amount = whatif.withdrawal_amount(day, outflow / 100)
    right.caption(f"{reporting} {amount:,.0f} mn leaves the group, paid from reserves first")
    st.button("Reset the levers", on_click=_reset_levers, icon=":material/restart_alt:")

    try:
        base = _what_if(day, dashboard.run_id, regime, 0.0, 0.0, 0.0)
        now = _what_if(day, dashboard.run_id, regime, run_off / 100, float(parallel), outflow / 100)
    except whatif.UnfundedOutflowError as error:
        st.warning(f"This withdrawal cannot be paid: {error}.")
        return
    compared = now.merge(
        base[["who", "limit_name", "actual", "status"]],
        on=["who", "limit_name"],
        suffixes=("", "_before"),
    )
    before, after = base["status"].value_counts(), now["status"].value_counts()
    tiles = st.columns(1 + len(LIMIT_NAMES))
    breaches = int(after.get("breach", 0))
    tiles[0].metric("What-if: limits in breach", breaches, delta=breaches - int(before.get("breach", 0)) or None,
                    delta_color="inverse", border=True)  # fmt: skip
    for tile, name in zip(tiles[1:], LIMIT_NAMES, strict=True):
        row = compared[(compared["limit_name"] == name) & (compared["who"] == data.GROUP)].iloc[0]
        change = row["actual"] - row["actual_before"]
        tile.metric(
            f"Group {TILE_NAMES[name]}",
            _percent(row["actual"]),
            delta=_points(change) if abs(change) >= 5e-5 else None,
            delta_color="inverse" if name == "eve_tier1" else "normal",
            border=True,
        )
    levels = day.limits.thresholds.model_dump()
    columns = st.columns(len(LIMIT_NAMES))
    for column, (name, title) in zip(columns, LIMIT_NAMES.items(), strict=True):
        rows = compared[compared["limit_name"] == name]
        rows = rows.assign(movement=rows["status_before"] + " → " + rows["status"])
        column.markdown(f"**{title}**")
        column.altair_chart(limit_chart(rows, levels, movement="published → what-if"))
    shown = compared.assign(
        limit=compared["limit_name"].map(LIMIT_NAMES),
        change=(compared["actual"] - compared["actual_before"]).map(_points),
        published_status=compared["status_before"].map(STATUS_ICON),
        whatif_status=compared["status"].map(STATUS_ICON),
    )
    st.dataframe(
        shown[
            [
                "limit",
                "who",
                "actual_before",
                "actual",
                "change",
                "published_status",
                "whatif_status",
            ]
        ],
        hide_index=True,
        column_config={
            "actual_before": st.column_config.NumberColumn("published", format="percent"),
            "actual": st.column_config.NumberColumn("what-if", format="percent"),
            "published_status": "published status",
            "whatif_status": "what-if status",
        },
        height=35 * (len(shown) + 1) + 3,  # every limit, no scrolling
    )
    st.caption(
        "How each lever works. Run-off: both retail run-off rates of the LCR rise by the points "
        "chosen. Parallel shock: the parallel up and down shocks grow in every currency; the EVE "
        "limit uses the worst of the six scenarios, so this moves it only where a parallel shock "
        "becomes the worst one. Deposit outflow: the deposits leave today and each book pays out "
        "of its own liquid assets at book value (no fire-sale loss), so assets and liabilities "
        "fall together and equity, the Tier 1 proxy, is unchanged; HQLA falls by the whole "
        "amount while the LCR's outflows fall only by the run-off share of it, so the LCR falls. "
        "The ranges are set in config/whatif.yaml. Latest day only."
    )


def position_chart(rows: pd.DataFrame, roles: list[str], title: str) -> alt.Chart:
    """The largest positions as bars, coloured by their role (legend), largest first."""
    shown = rows.assign(role_label=rows["role"].map(lambda role: ROLES[role][0]))
    domain, colours = zip(*(ROLES[role] for role in roles), strict=True)
    return (
        alt.Chart(shown)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y("position_id:N", title=None, sort=list(shown["position_id"]),
                    axis=alt.Axis(labelLimit=320, labelOverlap=False)),
            x=alt.X("value:Q", axis=_axis(",.0f", title)),
            color=alt.Color("role_label:N", scale=alt.Scale(domain=list(domain), range=list(colours)),
                            title=None),
            tooltip=[alt.Tooltip("position_id:N", title="position"), alt.Tooltip("category:N"),
                     alt.Tooltip("currency:N"), alt.Tooltip("role_label:N", title="role"),
                     alt.Tooltip("value:Q", format=",.1f", title=title)],
        )
        .properties(height=30 * len(shown) + 40)
    )  # fmt: skip


def _largest(drivers: pd.DataFrame, value: str, top: int) -> pd.DataFrame:
    order = drivers[value].abs().sort_values(ascending=False).index
    return drivers.loc[order].head(top).assign(value=lambda t: t[value])


def eve_drivers_view(day: whatif.Day, dashboard: data.Dashboard, con, regime: str, row) -> None:
    who = row["who"]
    risk = data.eve_against_tier1(con, regime).set_index("who").loc[who]
    rules = day.irrbb[regime]
    scenarios = list(rules.shocks.scenarios)
    worst = risk["worst_scenario"] if risk["worst_scenario"] in scenarios else scenarios[0]
    scenario = st.selectbox(
        "Scenario",
        scenarios,
        index=scenarios.index(worst),
        key=f"driver_scenario_{regime}_{who}",
        help="The limit uses the worst scenario, chosen here to start with.",
    )
    drivers = _drivers(day, dashboard.run_id, "eve", regime, who, scenario)
    currency = whatif.target_currency(day, who)
    loss = float(drivers["contribution"].sum())
    st.markdown(
        f"{who}, {scenario}: an EVE loss of **{loss:,.1f} {currency} mn**, "
        f"{loss / risk['tier1']:.1%} of Tier 1 (proxy) {risk['tier1']:,.0f} mn, against a limit "
        f"of {row['limit_value']:.0%}."
    )
    top = _largest(drivers, "contribution", day.whatif.drivers.top_positions)
    top = top.assign(role=np.where(top["contribution"] >= 0, "loss", "offset"))
    left, right = st.columns([3, 2])
    left.altair_chart(position_chart(top, ["loss", "offset"], f"{currency} mn"))
    by_category = drivers.groupby(["category", "currency"], as_index=False)["contribution"].sum()
    by_category = by_category.sort_values("contribution", ascending=False)
    by_category["share"] = by_category["contribution"] / loss if loss else np.nan
    right.dataframe(
        by_category,
        hide_index=True,
        column_config={
            "contribution": st.column_config.NumberColumn(f"{currency} mn", format="%.1f"),
            "share": st.column_config.NumberColumn("share", format="percent"),
        },
    )
    rule = ("losses only" if rules.aggregation.gains_weight == 0
            else f"gains at {rules.aggregation.gains_weight:.0%}")  # fmt: skip
    st.caption(
        f"The {day.whatif.drivers.top_positions} positions that move this loss most, and every "
        "category. Each position is measured alone with the IRRBB run's own repricing and ΔEVE "
        "code (ΔEVE is linear in the cash flows), so the parts add up exactly to the loss. "
        f"Across currencies a currency counts in full when it loses and is otherwise weighted "
        f"({REGIMES[regime]}: {rule}). Deposits count through their core and non-core parts, a "
        "swap through both legs."
    )


def _composition_view(drivers: pd.DataFrame, roles: list[str], currency: str, top: int) -> None:
    left, right = st.columns([3, 2])
    left.altair_chart(position_chart(_largest(drivers, "amount", top), roles, f"{currency} mn"))
    by_category = drivers.groupby(["role", "category"], as_index=False)["amount"].sum()
    by_category = by_category.assign(role=by_category["role"].map(lambda role: ROLES[role][0]))
    right.dataframe(
        by_category.sort_values(["role", "amount"], ascending=[True, False]),
        hide_index=True,
        column_config={"amount": st.column_config.NumberColumn(f"{currency} mn", format="%.1f")},
    )


def lcr_drivers_view(day: whatif.Day, dashboard: data.Dashboard, con, regime: str, row) -> None:
    who, currency = row["who"], whatif.target_currency(day, row["who"])
    summary = whatif.lcr_summary(day, regime, who).set_index("item")["amount"]
    st.markdown(
        f"{who}: LCR **{row['actual']:.1%}** = HQLA counted {summary['HQLA counted']:,.0f} ÷ net "
        f"outflows {summary['net outflows']:,.0f} {currency} mn, against a limit of "
        f"{row['limit_value']:.0%}."
    )
    drivers = _drivers(day, dashboard.run_id, "lcr", regime, who)
    _composition_view(drivers, ["hqla", "outflow", "inflow"], currency,
                      day.whatif.drivers.top_positions)  # fmt: skip
    steps = " → ".join(f"{item} {amount:,.0f}" for item, amount in summary.items())
    st.caption(
        f"The largest positions in the buffer and the 30-day stress, and every category. In "
        f"{currency} mn: {steps}. HQLA is shown after haircuts and before the caps, and inflows "
        "before the inflow cap, because the caps apply to totals, not to single positions."
    )


def nsfr_drivers_view(day: whatif.Day, dashboard: data.Dashboard, con, regime: str, row) -> None:
    who, currency = row["who"], whatif.target_currency(day, row["who"])
    drivers = _drivers(day, dashboard.run_id, "nsfr", regime, who)
    total = drivers.groupby("role")["amount"].sum()
    st.markdown(
        f"{who}: NSFR **{row['actual']:.1%}** = available stable funding {total['asf']:,.0f} ÷ "
        f"required stable funding {total['rsf']:,.0f} {currency} mn, against a limit of "
        f"{row['limit_value']:.0%}."
    )
    _composition_view(drivers, ["asf", "rsf"], currency, day.whatif.drivers.top_positions)
    st.caption(
        "The largest positions after their ASF or RSF factor, and every category. Equity is "
        "available stable funding at 100%; positions with a 0% factor are left out."
    )


def drivers_section(dashboard: data.Dashboard, con, regime: str, table: pd.DataFrame,
                    labels: list[str]) -> None:  # fmt: skip
    st.markdown("**What drives this limit** (click a row of the table above, or choose here)")
    day = whatif_day(dashboard)
    if day is None:
        return
    if st.session_state.get("driver_pick") not in labels:  # first view: the first breach
        breaches = [
            label for label, s in zip(labels, table["status"], strict=True) if s == "breach"
        ]
        st.session_state["driver_pick"] = (breaches or labels)[0]
    pick = st.selectbox("Limit", labels, key="driver_pick", label_visibility="collapsed")
    row = table.iloc[labels.index(pick)]
    view = {"eve_tier1": eve_drivers_view, "lcr": lcr_drivers_view, "nsfr": nsfr_drivers_view}
    view[row["limit_name"]](day, dashboard, con, regime, row)


def main() -> None:
    st.set_page_config(page_title="Risk monitor", page_icon="📊", layout="wide")
    storage = load_storage_config(Path("config/storage.yaml"))
    source, data_dir, demo = storage.backend, os.environ.get(DATA_ENV, "data"), None
    if os.environ.get(DEMO_ENV) == "1":
        try:
            demo = _demo()
        except DemoBuildError as error:
            st.title("Liquidity and IRRBB monitor")
            st.error(f"The demo could not be built: {error}")
            st.stop()
        source, data_dir = "local", str(demo.data_dir)
    try:
        dashboard = _open(source, data_dir)
    except QueryInputError as error:
        st.title("Liquidity and IRRBB monitor")
        st.error(f"No published run to show: {error}")
        st.stop()
    con = dashboard.con.cursor()  # one cursor per page view: sessions never share a cursor

    largest = data.rulebook_comparison(con)["difference"].abs().max()
    with st.sidebar:
        regime = st.radio(
            "Rulebook",
            list(REGIMES),
            format_func=REGIMES.get,
            key="regime",
            horizontal=True,
            help=(
                "Every figure on the page follows the rulebook chosen here. The two differ only "
                f"in a few treatments, so for this bank results move by at most {largest * 100:.1f} "
                "percentage points. The executive summary compares both side by side."
            ),
        )
        where = "demo, built on this server" if demo else dashboard.source
        st.caption(f"Run {dashboard.run_id} ({where})")
        st.caption("All positions are synthetic.")

    st.title("Liquidity and IRRBB monitor")
    if demo:
        st.info(
            f"Demo: a synthetic bank (3 entities, 3 currencies), run through the platform's full "
            f"pipeline on this server at {demo.built_at:%d %b %Y %H:%M} UTC. Every figure is the "
            "pipeline's own output; no real bank's data is shown. The series is fixed: "
            f"{data.business_days(con)} business days to {data.latest_date(con):%d %b %Y}, "
            "whatever today's date. Methodology, validation and source code: the project's "
            "README."
        )
    st.badge(f"Rulebook: {REGIMES[regime]}", color="blue", icon=":material/gavel:")
    st.caption(f"{REGIMES[regime]} rules, as of {data.latest_date(con):%d %b %Y}")
    headline(con, regime)
    names = ["Executive summary", "Limits", "Liquidity", "IRRBB", "What-if", "Since the day before"]
    tabs = st.tabs([*names, "Data quality"])
    with tabs[0]:
        executive_tab(dashboard, con, regime)
    with tabs[1]:
        limits_tab(dashboard, con, regime)
    with tabs[2]:
        liquidity_tab(con, regime)
    with tabs[3]:
        irrbb_tab(con, regime)
    with tabs[4]:
        whatif_tab(dashboard, regime)
    with tabs[5]:
        changes_tab(con, regime)
    with tabs[6]:
        quality_tab(dashboard, con)


if __name__ == "__main__":  # how Streamlit runs this file (and AppTest in the tests)
    main()
