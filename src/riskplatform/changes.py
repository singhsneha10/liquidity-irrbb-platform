"""What changed since yesterday: the latest business day against the one before.

"Yesterday" is the previous business day in the same run's series. Each daily run generates the
series again (identically) and adds its own day (riskplatform.pipeline --as-of), so this equals
the previous run's latest day without reading the previous run.

Three views, all for the levels the limits apply to (entity and group) and both regimes:

- limit_changes: each internal limit's status and utilisation on both days, and its movement:
      new       ok -> warning or breach        worse   warning -> breach
      resolved  warning or breach -> ok        better  breach -> warning
      unchanged the same status
  A limit with no result on the previous day counts as previously ok.
- metric_changes: the measured value behind each limit (LCR, NSFR, EVE loss / Tier 1) on both
  days and the change, from the same limits table, so nothing is calculated twice.
- balance_drivers: on-balance-sheet balances per entity, category and side in GBP, and their
  change split exactly into two parts that add up to it:
      volume = (balance today - balance yesterday, in the position's currency) x FX yesterday
      fx     = balance today x (FX today - FX yesterday)
  Swap notionals are off balance sheet and are not balances, so they are left out.

run_changes writes data/changes/<run_id>/: limit_changes.parquet, metric_changes.parquet,
balance_drivers.parquet and summary.json (both dates, movement counts, every movement except
"unchanged", and the top drivers; config/reporting.yaml sets how many). A series of one day has
nothing to compare: the tables are empty and previous_date is null.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from riskplatform.config import ReportingConfig

ID = ["level", "entity", "currency", "regime", "limit_name"]
RANK = {"ok": 0, "warning": 1, "breach": 2}
MOVEMENTS = ["new", "worse", "resolved", "better", "unchanged"]  # also the report order
LIMIT_CHANGE_COLUMNS = [
    *ID,
    "previous_date",
    "latest_date",
    "previous_status",
    "latest_status",
    "previous_utilisation",
    "latest_utilisation",
    "utilisation_change",
    "movement",
]
METRIC_CHANGE_COLUMNS = [*ID, "previous_date", "latest_date", "previous", "latest", "change"]
DRIVER_KEYS = ["entity", "category", "side"]
DRIVER_COLUMNS = [
    *DRIVER_KEYS,
    "previous_gbp",
    "latest_gbp",
    "change_gbp",
    "volume_gbp",
    "fx_gbp",
]


def compare_dates(dates: pd.Series) -> tuple[pd.Timestamp | None, pd.Timestamp]:
    """(the business day before the latest, the latest); (None, latest) for one day."""
    unique = sorted(pd.to_datetime(dates).unique())
    if not unique:
        raise ValueError("no dates to compare")
    latest = pd.Timestamp(unique[-1])
    previous = pd.Timestamp(unique[-2]) if len(unique) > 1 else None
    return previous, latest


def movement(previous_status: pd.Series, latest_status: pd.Series) -> np.ndarray:
    """new / worse / resolved / better / unchanged for each pair of statuses."""
    before = previous_status.map(RANK).fillna(0).to_numpy()  # no result yesterday: ok
    after = latest_status.map(RANK).to_numpy()
    return np.select(
        [
            (before == 0) & (after > 0),
            (before > 0) & (after == 0),
            after > before,
            after < before,
        ],
        ["new", "resolved", "worse", "better"],
        "unchanged",
    )


def _both_days(limits: pd.DataFrame, values: list[str]) -> pd.DataFrame:
    """Latest-day rows joined to the previous day's values (suffix _previous) on the limit id."""
    previous, latest = compare_dates(limits["date"])
    if previous is None:
        return pd.DataFrame(columns=[*ID, *values, *[f"{v}_previous" for v in values]])
    today = limits.loc[limits["date"] == latest, [*ID, *values]]
    before = limits.loc[limits["date"] == previous, [*ID, *values]]
    joined = today.merge(before, on=ID, how="left", suffixes=("", "_previous"))
    return joined.assign(previous_date=previous, latest_date=latest)


def limit_changes(limits: pd.DataFrame) -> pd.DataFrame:
    """Each limit's status and utilisation on both days, with its movement (module notes)."""
    joined = _both_days(limits, ["status", "utilisation"])
    if joined.empty:
        return pd.DataFrame(columns=LIMIT_CHANGE_COLUMNS)
    table = joined.rename(
        columns={
            "status": "latest_status",
            "status_previous": "previous_status",
            "utilisation": "latest_utilisation",
            "utilisation_previous": "previous_utilisation",
        }
    )
    table["utilisation_change"] = table["latest_utilisation"] - table["previous_utilisation"]
    table["movement"] = movement(table["previous_status"], table["latest_status"])
    order = table["movement"].map({name: i for i, name in enumerate(MOVEMENTS)})
    table = table.assign(_order=order).sort_values(
        ["_order", "latest_utilisation"], ascending=[True, False], na_position="last"
    )
    return table[LIMIT_CHANGE_COLUMNS].reset_index(drop=True)


def metric_changes(limits: pd.DataFrame) -> pd.DataFrame:
    """The measured value behind each limit on both days, and the change."""
    joined = _both_days(limits, ["actual"])
    if joined.empty:
        return pd.DataFrame(columns=METRIC_CHANGE_COLUMNS)
    table = joined.rename(columns={"actual": "latest", "actual_previous": "previous"})
    table["change"] = table["latest"] - table["previous"]
    return table[METRIC_CHANGE_COLUMNS].sort_values(ID, ignore_index=True)


def balance_drivers(positions: pd.DataFrame, fx: pd.DataFrame) -> pd.DataFrame:
    """Balance change per entity, category and side in GBP, split into volume and FX."""
    previous, latest = compare_dates(positions["date"])
    if previous is None:
        return pd.DataFrame(columns=DRIVER_COLUMNS)
    on_sheet = positions[
        positions["side"].isin(["asset", "liability"]) & positions["date"].isin([previous, latest])
    ]
    local = (
        on_sheet.groupby([*DRIVER_KEYS, "currency", "date"])["balance_mn"]
        .sum()
        .unstack("date", fill_value=0.0)
        .reindex(columns=[previous, latest], fill_value=0.0)
    )
    rate = fx.set_index(["date", "currency"])["to_reporting"]
    currencies = local.index.get_level_values("currency")
    fx_before = rate.loc[[(previous, c) for c in currencies]].to_numpy()
    fx_after = rate.loc[[(latest, c) for c in currencies]].to_numpy()
    before, after = local[previous].to_numpy(), local[latest].to_numpy()
    parts = pd.DataFrame(
        {
            "previous_gbp": before * fx_before,
            "latest_gbp": after * fx_after,
            "volume_gbp": (after - before) * fx_before,
            "fx_gbp": after * (fx_after - fx_before),
        },
        index=local.index,
    )
    table = parts.groupby(level=DRIVER_KEYS).sum().reset_index()
    table["change_gbp"] = table["volume_gbp"] + table["fx_gbp"]
    table = table.assign(_size=table["change_gbp"].abs()).sort_values("_size", ascending=False)
    return table[DRIVER_COLUMNS].reset_index(drop=True)


def _plain(value):
    """A JSON-safe value: NaN and infinities become None, timestamps become ISO dates."""
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, float | np.floating):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA:
        return None
    return value


def _records(table: pd.DataFrame) -> list[dict]:
    return [
        {key: _plain(None if pd.isna(value) else value) for key, value in row.items()}
        for row in table.to_dict(orient="records")
    ]


class ChangesInputError(Exception):
    """The run's limits or validated data are missing, or validated/ is another run's."""


@dataclass(frozen=True)
class ChangesRun:
    limit_changes: pd.DataFrame
    metric_changes: pd.DataFrame
    balance_drivers: pd.DataFrame
    summary: dict
    out_dir: Path  # data/changes/<run_id>/


def _validated_positions(validated_dir: Path) -> pd.DataFrame:
    """The last two days' validated positions (all a comparison needs)."""
    files = sorted((validated_dir / "positions").glob("date=*.parquet"))[-2:]
    if not files:
        raise ChangesInputError(f"{validated_dir} has no validated positions")
    return pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)


def run_changes(data_dir: str | Path, run_id: str, cfg: ReportingConfig) -> ChangesRun:
    """Compare the run's latest day with the one before and write data/changes/<run_id>/."""
    data_dir = Path(data_dir)
    validated = data_dir / "validated"
    limits_file = data_dir / "limits" / run_id / "limits.parquet"
    if not limits_file.exists():
        raise ChangesInputError(f"run {run_id} has no limits: {limits_file.as_posix()}")
    manifest = validated / "_run.json"
    published = (
        json.loads(manifest.read_text(encoding="utf-8")).get("run_id")
        if manifest.exists()
        else None
    )
    if published != run_id:
        raise ChangesInputError(f"{validated} holds run {published}, not run {run_id}")

    limits = pd.read_parquet(limits_file)
    tables = {
        "limit_changes": limit_changes(limits),
        "metric_changes": metric_changes(limits),
        "balance_drivers": balance_drivers(
            _validated_positions(validated), pd.read_parquet(validated / "fx.parquet")
        ),
    }
    previous, latest = compare_dates(limits["date"])
    moves = tables["limit_changes"]
    summary = {
        "source_run_id": run_id,
        "latest_date": latest.date().isoformat(),
        "previous_date": None if previous is None else previous.date().isoformat(),
        "movements": {name: int((moves["movement"] == name).sum()) for name in MOVEMENTS},
        "limit_movements": _records(moves[moves["movement"] != "unchanged"]),
        "top_drivers": _records(tables["balance_drivers"].head(cfg.changes.top_drivers)),
    }
    out_dir = data_dir / "changes" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, table in tables.items():
        table.to_parquet(out_dir / f"{name}.parquet", index=False)
    text = json.dumps(summary, indent=2, allow_nan=False)  # strict JSON: NaN fails loudly
    (out_dir / "summary.json").write_text(text, encoding="utf-8")
    return ChangesRun(**tables, summary=summary, out_dir=out_dir)


def describe_movement(item: dict) -> str:
    """One line, e.g. 'WORSE BREACH BANK_US (basel) eve_tier1: 101% used (was 98%, warning)'."""

    def used(value):
        return "inf" if value is None else f"{value:.0%}"

    who = item["entity"] or "GROUP"
    return (
        f"{item['movement'].upper()} {item['latest_status'].upper()} {who} ({item['regime']}) "
        f"{item['limit_name']}: {used(item['latest_utilisation'])} used "
        f"(was {used(item['previous_utilisation'])}, {item['previous_status'] or 'not measured'})"
    )
