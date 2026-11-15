"""Internal limits: how much of each limit is used, and whether that is ok, a warning or a breach.

Pure functions on the metrics and IRRBB tables; nothing is read from disk. The limits and the
thresholds come from config/limits.yaml (risk appetite, not regulatory parameters).

Utilisation is the fraction of a limit used:

    floor limit (stay at or above, e.g. LCR >= 110%):          limit / actual
    cap limit   (stay at or below, e.g. EVE loss <= 15% T1):   actual / limit

Status: breach when utilisation >= thresholds.breach (the limit is reached or crossed), warning
when >= thresholds.warning, else ok. Edge cases of a floor metric follow the regulatory
minimum's rule (riskplatform.metrics.run): a ratio of 0/0 (NaN: nothing owed and nothing held)
or an infinite ratio (nothing owed) uses 0% of the limit; a ratio of 0 with something owed uses
an infinite share, so it is a breach. For EVE, a loss with no positive Tier 1 is a breach too.

Limits apply to the levels in config (entity and group); per-currency results are monitoring
only. Each regime (Basel, UK PRA) is measured against the same internal limits.

The limits run (run_limits, or the command below) reads one run's results from the standard
layout and writes data/limits/<run_id>/: limits.parquet (every date) and summary.json (the
latest date's status counts and its alerts, the warnings and breaches).

Run with:  python -m riskplatform.limits     (the run that published data/validated/)
Exit code 1 if any limit is at warning or breach on the latest date (as the metrics and IRRBB
commands exit 1 on findings); the pipeline reports the same as findings (exit 2).
"""

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from riskplatform.config import LimitKind, LimitsConfig, LimitThresholds, load_limits_config
from riskplatform.logs import setup_logging

logger = logging.getLogger(__name__)

KEYS = ["date", "level", "entity", "currency", "regime"]
LIMIT_COLUMNS = [*KEYS, "limit_name", "kind", "actual", "limit_value", "utilisation", "status"]
STATUSES = ("ok", "warning", "breach")


def utilisation(kind: LimitKind, actual: np.ndarray, limit: float) -> np.ndarray:
    """The fraction of the limit used by each actual value (see the module notes)."""
    actual = np.asarray(actual, dtype=float)
    if kind == "cap":
        return actual / limit
    used = np.zeros_like(actual)  # NaN (0/0) and infinite ratios: nothing owed, 0% used
    finite = np.isfinite(actual)
    positive = finite & (actual > 0)
    used[positive] = limit / actual[positive]
    used[finite & (actual <= 0)] = np.inf  # owed something and held nothing: a breach
    return used


def status(used: np.ndarray, thresholds: LimitThresholds) -> np.ndarray:
    """ok, warning or breach for each utilisation (thresholds inclusive)."""
    used = np.asarray(used, dtype=float)
    return np.select(
        [used >= thresholds.breach, used >= thresholds.warning], ["breach", "warning"], "ok"
    )


def eve_share_of_tier1(risk: pd.DataFrame) -> np.ndarray:
    """Worst aggregated EVE loss as a share of Tier 1; a loss with no positive Tier 1 is inf."""
    loss = risk["eve_risk"].to_numpy(dtype=float)
    tier1 = risk["tier1"].to_numpy(dtype=float)
    share = np.zeros_like(loss)
    has_capital = tier1 > 0
    share[has_capital] = loss[has_capital] / tier1[has_capital]
    share[~has_capital & (loss > 0)] = np.inf
    return share


def measure_limits(
    lcr: pd.DataFrame, nsfr: pd.DataFrame, risk: pd.DataFrame, cfg: LimitsConfig
) -> pd.DataFrame:
    """Utilisation and status of every limit, for every date, level, entity and regime."""
    sources = {
        "lcr": (lcr, lambda table: table["lcr"].to_numpy(dtype=float)),
        "nsfr": (nsfr, lambda table: table["nsfr"].to_numpy(dtype=float)),
        "eve_tier1": (risk, eve_share_of_tier1),
    }
    frames = []
    for name, limit in cfg.limits.items():
        table, actual_of = sources[limit.source]
        rows = table[table["level"].isin(cfg.levels)]
        actual = actual_of(rows)
        used = utilisation(limit.kind, actual, limit.value)
        frames.append(
            rows[KEYS].assign(
                limit_name=name,
                kind=limit.kind,
                actual=actual,
                limit_value=limit.value,
                utilisation=used,
                status=status(used, cfg.thresholds),
            )
        )
    measured = pd.concat(frames, ignore_index=True)[LIMIT_COLUMNS]
    return measured.sort_values([*KEYS, "limit_name"], ignore_index=True, na_position="last")


def latest_alerts(table: pd.DataFrame) -> pd.DataFrame:
    """Warnings and breaches on the latest date, breaches first, then by utilisation."""
    latest = table[(table["date"] == table["date"].max()) & (table["status"] != "ok")]
    order = latest["status"].map({"breach": 0, "warning": 1})
    return (
        latest.assign(_order=order)
        .sort_values(["_order", "utilisation"], ascending=[True, False])
        .drop(columns="_order")
        .reset_index(drop=True)
    )


class LimitsInputError(Exception):
    """The run's metrics or IRRBB results are missing."""


@dataclass(frozen=True)
class LimitsRun:
    table: pd.DataFrame  # every limit on every date (LIMIT_COLUMNS)
    summary: dict
    out_dir: Path  # data/limits/<run_id>/

    @property
    def alerts(self) -> list[dict]:
        return self.summary["alerts"]


def _number(value: float) -> float | None:
    """A float for strict JSON: inf (e.g. a ratio of 0 against a floor) becomes None."""
    return float(value) if math.isfinite(value) else None


def _alert_records(alerts: pd.DataFrame) -> list[dict]:
    return [
        {
            "date": row.date.date().isoformat(),
            "level": row.level,
            "entity": None if pd.isna(row.entity) else row.entity,  # the group has no entity
            "currency": row.currency,
            "regime": row.regime,
            "limit": row.limit_name,
            "kind": row.kind,
            "actual": _number(row.actual),
            "limit_value": float(row.limit_value),
            "utilisation": _number(row.utilisation),
            "status": row.status,
        }
        for row in alerts.itertuples()
    ]


def run_limits(data_dir: str | Path, run_id: str, cfg: LimitsConfig) -> LimitsRun:
    """Measure every limit for one run's results and write data/limits/<run_id>/."""
    data_dir = Path(data_dir)
    inputs = {
        "lcr": data_dir / "metrics" / run_id / "lcr.parquet",
        "nsfr": data_dir / "metrics" / run_id / "nsfr.parquet",
        "risk": data_dir / "irrbb" / run_id / "risk.parquet",
    }
    missing = [path.as_posix() for path in inputs.values() if not path.exists()]
    if missing:
        raise LimitsInputError(f"run {run_id} has no results to measure: {', '.join(missing)}")
    tables = {name: pd.read_parquet(path) for name, path in inputs.items()}
    table = measure_limits(tables["lcr"], tables["nsfr"], tables["risk"], cfg)

    latest_date = table["date"].max()
    latest = table[table["date"] == latest_date]
    summary = {
        "source_run_id": run_id,
        "as_of": latest_date.date().isoformat(),
        "thresholds": cfg.thresholds.model_dump(),
        "limits": {name: limit.model_dump() for name, limit in cfg.limits.items()},
        "latest_status": {name: int((latest["status"] == name).sum()) for name in STATUSES},
        "alerts": _alert_records(latest_alerts(table)),
    }
    out_dir = data_dir / "limits" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out_dir / "limits.parquet", index=False)
    text = json.dumps(summary, indent=2, allow_nan=False)  # strict JSON: NaN fails loudly
    (out_dir / "summary.json").write_text(text, encoding="utf-8")
    return LimitsRun(table=table, summary=summary, out_dir=out_dir)


def describe_alert(alert: dict) -> str:
    """One line for a log or an email, e.g. 'BREACH BANK_UK (basel) eve_tier1: 138% used'."""
    who = alert["entity"] or "GROUP"
    used = "inf" if alert["utilisation"] is None else f"{alert['utilisation']:.0%}"
    actual = "n/a" if alert["actual"] is None else f"{alert['actual']:.1%}"
    return (
        f"{alert['status'].upper()} {who} ({alert['regime']}) {alert['limit']}: {used} used "
        f"(actual {actual}, limit {alert['limit_value']:.1%} {alert['kind']}) on {alert['date']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the internal limits for the latest run.")
    parser.add_argument("--limits", type=Path, default=Path("config/limits.yaml"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args(argv)

    setup_logging()
    manifest = args.data / "validated" / "_run.json"
    if not manifest.exists():
        logger.error("%s not found: no run has published validated data yet", manifest)
        return 1
    run_id = json.loads(manifest.read_text(encoding="utf-8"))["run_id"]
    try:
        run = run_limits(args.data, run_id, load_limits_config(args.limits))
    except LimitsInputError as error:
        logger.error("%s", error)
        return 1
    for alert in run.alerts:
        logger.warning("%s", describe_alert(alert))
    counts = run.summary["latest_status"]
    logger.info("Limits as of %s: %s; wrote %s", run.summary["as_of"], counts, run.out_dir)
    return 1 if run.alerts else 0


if __name__ == "__main__":
    sys.exit(main())
