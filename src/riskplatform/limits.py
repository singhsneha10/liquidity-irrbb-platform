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
"""

import numpy as np
import pandas as pd

from riskplatform.config import LimitKind, LimitsConfig, LimitThresholds

KEYS = ["date", "level", "entity", "currency", "regime"]
LIMIT_COLUMNS = [*KEYS, "limit", "kind", "actual", "limit_value", "utilisation", "status"]
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
                limit=name,
                kind=limit.kind,
                actual=actual,
                limit_value=limit.value,
                utilisation=used,
                status=status(used, cfg.thresholds),
            )
        )
    measured = pd.concat(frames, ignore_index=True)[LIMIT_COLUMNS]
    return measured.sort_values([*KEYS, "limit"], ignore_index=True, na_position="last")


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
