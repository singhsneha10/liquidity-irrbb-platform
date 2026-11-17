"""Early-warning indicators: two daily signals that come before a limit is reached.

From the model validation (docs/validation_report.md, recommendations R3 and R4):
- retail_outflow (R4): the fall in each entity's, and the group's, retail deposits (stable and
  less stable separately) over the last N business days, all currencies at the first day's FX
  rates (as in the run-off backtest, so FX moves are not withdrawals). Alert above a threshold
  set near the system's 99th percentile (2.92%, validation test T4).
- break_even_run_off (R3): the single retail run-off rate (both categories, each at least its
  rulebook rate) at which each entity's and the group's LCR would fall to the internal limit
  (config/limits.yaml), under each rulebook. Alert below a threshold.

Thresholds and the reasons for them: config/early_warning.yaml (judgement, like the limits).
Both use code that is already tested: the backtest's outflow measure and T3's break-even search,
so nothing here calculates a ratio or an outflow of its own.

run_early_warning writes data/early_warning/<run_id>/: early_warning.parquet (one row per
indicator, level, entity, category or rulebook) and strict-JSON summary.json (as_of, counts and
every alert). The pipeline stage reports findings (exit 2) if any indicator is in alert.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from riskplatform.config import EarlyWarningConfig, GeneratorConfig, LiquidityRules
from riskplatform.metrics.run import read_validated
from riskplatform.model_validation.backtest import retail_deposits
from riskplatform.model_validation.sensitivity import break_even, latest_groups

GROUP = "GROUP"
OK, ALERT, NOT_ENOUGH_DAYS = "ok", "alert", "not_enough_days"
COLUMNS = [
    "date", "indicator", "level", "entity", "who", "category", "regime", "value", "threshold",
    "status",
]  # fmt: skip


def retail_outflows(
    positions: pd.DataFrame, fx: pd.DataFrame, cfg: EarlyWarningConfig
) -> pd.DataFrame:
    """The retail outflow indicator on the latest date, per entity, the group and category."""
    setting = cfg.retail_outflow
    table = retail_deposits(positions, fx)  # columns (entity, category), GBP at fixed FX
    group = table.T.groupby(level="category").sum().T
    group.columns = pd.MultiIndex.from_product([[GROUP], group.columns])
    levels = pd.concat([table, group], axis=1)
    latest = levels.index.max()
    rows = []
    for (who, category), series in levels.items():
        if len(series) <= setting.business_days:
            value, status = None, NOT_ENOUGH_DAYS
        else:
            before = series.iloc[-1 - setting.business_days]
            value = max(0.0, 1.0 - series.iloc[-1] / before)  # a rise counts as no outflow
            status = ALERT if value > setting.threshold else OK
        rows.append(
            {
                "date": latest,
                "indicator": "retail_outflow",
                "level": "group" if who == GROUP else "entity",
                "entity": None if who == GROUP else who,
                "who": who,
                "category": category,
                "regime": None,
                "value": value,
                "threshold": setting.threshold,
                "status": status,
            }
        )
    return pd.DataFrame(rows, columns=COLUMNS)


def break_even_run_offs(
    positions: pd.DataFrame,
    fx: pd.DataFrame,
    generator: GeneratorConfig,
    regimes: dict[str, LiquidityRules],
    limit: float,
    cfg: EarlyWarningConfig,
) -> pd.DataFrame:
    """The break-even run-off indicator on the latest date, per entity, the group and rulebook.

    A break-even of None (even a 100% run-off keeps the LCR at the limit) is ok."""
    threshold = cfg.break_even_run_off.threshold
    rows = []
    for key, group in latest_groups(positions, fx, generator):
        for name, rules in regimes.items():
            value = break_even(group, rules, limit)
            rows.append(
                {
                    "date": key["date"],
                    "indicator": "break_even_run_off",
                    "level": key["level"],
                    "entity": key["entity"],
                    "who": key["entity"] or GROUP,
                    "category": None,
                    "regime": name,
                    "value": value,
                    "threshold": threshold,
                    "status": ALERT if value is not None and value < threshold else OK,
                }
            )
    return pd.DataFrame(rows, columns=COLUMNS)


def _plain(value):
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class EarlyWarningInputError(Exception):
    """data/validated/ is missing or holds another run's data."""


@dataclass(frozen=True)
class EarlyWarningRun:
    table: pd.DataFrame
    summary: dict
    out_dir: Path  # data/early_warning/<run_id>/

    @property
    def alerts(self) -> list[dict]:
        return self.summary["alerts"]


def run_early_warning(
    data_dir: str | Path,
    run_id: str,
    generator: GeneratorConfig,
    regimes: dict[str, LiquidityRules],
    limit: float,
    cfg: EarlyWarningConfig,
) -> EarlyWarningRun:
    """Measure both indicators on the run's validated data and write data/early_warning/<run_id>/."""
    data_dir = Path(data_dir)
    positions, fx, manifest = read_validated(data_dir / "validated", generator)
    if manifest.get("run_id") != run_id:
        raise EarlyWarningInputError(
            f"data/validated holds run {manifest.get('run_id')}, not run {run_id}"
        )
    table = pd.concat(
        [
            retail_outflows(positions, fx, cfg),
            break_even_run_offs(positions, fx, generator, regimes, limit, cfg),
        ],
        ignore_index=True,
    )
    alerts = table[table["status"] == ALERT]
    summary = {
        "source_run_id": run_id,
        "as_of": table["date"].max().date().isoformat(),
        "limit": limit,
        "thresholds": {
            "retail_outflow": cfg.retail_outflow.threshold,
            "retail_outflow_business_days": cfg.retail_outflow.business_days,
            "break_even_run_off": cfg.break_even_run_off.threshold,
        },
        "counts": {s: int((table["status"] == s).sum()) for s in (OK, ALERT, NOT_ENOUGH_DAYS)},
        "alerts": [
            {k: _plain(v) for k, v in row.items()}
            for row in alerts.astype(object).where(alerts.notna(), None).to_dict("records")
        ],
    }
    out_dir = data_dir / "early_warning" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out_dir / "early_warning.parquet", index=False)
    text = json.dumps(summary, indent=2, allow_nan=False)
    (out_dir / "summary.json").write_text(text, encoding="utf-8")
    return EarlyWarningRun(table=table, summary=summary, out_dir=out_dir)


def describe_indicator(item: dict) -> str:
    """One line, e.g. 'ALERT BANK_EU (basel) break-even run-off 19.18% (alert below 20%)'."""
    who = item["who"]
    if item["indicator"] == "retail_outflow":
        what = f"{item['category']} outflow over 4 weeks {item['value']:.2%}"
        rule = f"alert above {item['threshold']:.0%}"
    else:  # two decimals: 19.99% must not read as 20.0% against a 20% threshold
        what = f"({item['regime']}) break-even run-off {item['value']:.2%}"
        rule = f"alert below {item['threshold']:.0%}"
    return f"{item['status'].upper()} {who} {what} ({rule})"
