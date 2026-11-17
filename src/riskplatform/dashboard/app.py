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
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from riskplatform.config import load_storage_config
from riskplatform.dashboard import data
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
LIMIT_NAMES = {"lcr": "LCR", "nsfr": "NSFR", "eve_tier1": "EVE loss / Tier 1"}
TILE_NAMES = {**LIMIT_NAMES, "eve_tier1": "EVE / Tier 1"}  # short enough for a tile
# The standardised shock scenarios in the order the rules list them (SRP31.90), not alphabetical.
SCENARIOS = ["parallel_up", "parallel_down", "steepener", "flattener", "short_up", "short_down"]
GRID = "#e5e4e0"


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


def limit_chart(rows: pd.DataFrame, levels: dict[str, float]) -> alt.LayerChart:
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
                alt.Tooltip("movement:N", title="since the day before"),
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
            x=alt.X("share:Q", axis=_axis(".0%", "worst EVE loss / Tier 1")),
            color=alt.Color(
                "result:N",
                scale=alt.Scale(
                    domain=["within the test", "outlier"], range=[STATUS["ok"], STATUS["breach"]]
                ),
                title=None,
            ),
            tooltip=[
                alt.Tooltip("who:N"),
                alt.Tooltip("share:Q", format=".1%", title="EVE loss / Tier 1"),
                alt.Tooltip("eve_risk:Q", format=",.1f", title="EVE loss (GBP mn)"),
                alt.Tooltip("tier1:Q", format=",.1f", title="Tier 1 (GBP mn)"),
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


def limits_tab(dashboard: data.Dashboard, con, regime: str) -> None:
    levels = data.thresholds(dashboard)
    st.caption(
        f"Share of each internal limit used on the latest date (config/limits.yaml). Warning from "
        f"{levels['warning']:.0%}, breach from {levels['breach']:.0%} (the dashed lines). "
        "These are the bank's own limits, not the regulatory minimums."
    )
    table = data.limit_status(con, regime)
    columns = st.columns(len(LIMIT_NAMES))
    for column, (name, title) in zip(columns, LIMIT_NAMES.items(), strict=True):
        column.markdown(f"**{title}**")
        column.altair_chart(limit_chart(table[table["limit_name"] == name], levels))
    shown = table.assign(
        limit=table["limit_name"].map(LIMIT_NAMES), status=table["status"].map(STATUS_ICON)
    )
    st.dataframe(
        shown[
            ["limit", "who", "kind", "actual", "limit_value", "utilisation", "status", "movement"]
        ],
        hide_index=True,
        column_config={
            "actual": st.column_config.NumberColumn(format="percent"),
            "limit_value": st.column_config.NumberColumn("limit", format="percent"),
            "utilisation": st.column_config.NumberColumn("used", format="percent"),
        },
    )
    early_warning_table(con, regime)


def early_warning_table(con, regime: str) -> None:
    """The early-warning indicators (config/early_warning.yaml), alerts first."""
    table = data.early_warning(con, regime)
    st.markdown("**Early-warning indicators**")
    st.caption(
        "Alert when 4-week retail outflows exceed their threshold (about the system's 99th "
        "percentile), or when the run-off that would take the LCR to the 110% limit is below its "
        "threshold. The bank's own judgement (config/early_warning.yaml)."
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
    right.markdown("**Change in NII by scenario** (GBP mn, next 12 months)")
    right.altair_chart(scenario_chart(nii[nii["who"] == who], "delta_nii", "ΔNII (GBP mn)"))


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


def main() -> None:
    st.set_page_config(page_title="Risk monitor", page_icon="📊", layout="wide")
    storage = load_storage_config(Path("config/storage.yaml"))
    data_dir = os.environ.get(DATA_ENV, "data")
    try:
        dashboard = _open(storage.backend, data_dir)
    except QueryInputError as error:
        st.title("Liquidity and IRRBB monitor")
        st.error(f"No published run to show: {error}")
        st.stop()
    con = dashboard.con.cursor()  # one cursor per page view: sessions never share a cursor

    with st.sidebar:
        regime = st.radio(
            "Rulebook", list(REGIMES), format_func=REGIMES.get, key="regime", horizontal=True
        )
        st.caption(f"Run {dashboard.run_id} ({dashboard.source})")
        st.caption("All positions are synthetic.")

    st.title("Liquidity and IRRBB monitor")
    st.caption(f"{REGIMES[regime]} rules, as of {data.latest_date(con):%d %b %Y}")
    headline(con, regime)
    tabs = st.tabs(["Limits", "Liquidity", "IRRBB", "Since the day before", "Data quality"])
    with tabs[0]:
        limits_tab(dashboard, con, regime)
    with tabs[1]:
        liquidity_tab(con, regime)
    with tabs[2]:
        irrbb_tab(con, regime)
    with tabs[3]:
        changes_tab(con, regime)
    with tabs[4]:
        quality_tab(dashboard, con)


if __name__ == "__main__":  # how Streamlit runs this file (and AppTest in the tests)
    main()
