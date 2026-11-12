"""Outlier warnings: values that pass every rule but are unusual enough for an analyst to review.

Outliers are never quarantined; they are listed in the breaks report. Thresholds come from
config/quality.yaml. All checks run on validated rows.

Simplification: positions are aggregated lines, not individual counterparties, so the
concentration check is a rough signal, not a large-exposures or funding-concentration measure.
"""

import numpy as np
import pandas as pd

from riskplatform.config import OutlierThresholds

WARNING_COLUMNS = ["check", "date", "subject", "value", "threshold"]


def _warnings(check: str, rows: pd.DataFrame, subject, value, threshold: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "check": check,
            "date": rows["date"].to_numpy(),
            "subject": np.asarray(subject),
            "value": np.asarray(value, dtype=float),
            "threshold": threshold,
        },
        columns=WARNING_COLUMNS,
    )


def balance_jumps(positions: pd.DataFrame, limit: float) -> pd.DataFrame:
    """Positions whose balance moved more than limit (a fraction) since their previous day."""
    ordered = positions.sort_values(["position_id", "date"])
    previous = ordered.groupby("position_id")["balance_mn"].shift()
    change = ordered["balance_mn"] / previous - 1.0
    hit = change.abs() > limit
    return _warnings(
        "balance_jump", ordered[hit], ordered.loc[hit, "position_id"], change[hit], limit
    )


def rate_gaps(positions: pd.DataFrame, rates: pd.DataFrame, limit: float) -> pd.DataFrame:
    """Positions whose rate is further than limit from their curve at their remaining maturity."""
    curves = {
        key: curve for key, curve in rates.sort_values("tenor_years").groupby(["date", "currency"])
    }
    found = []
    for key, group in positions.groupby(["date", "currency"]):
        curve = curves.get(key)
        if curve is None:  # a day without curves already stops the run (file-level check)
            continue
        maturity_years = group["residual_days"] / 365.0
        reference = np.interp(maturity_years, curve["tenor_years"], curve["rate"])
        gap = group["interest_rate"] - reference
        hit = gap.abs() > limit
        found.append(
            _warnings(
                "rate_gap_to_curve", group[hit], group.loc[hit, "position_id"], gap[hit], limit
            )
        )
    return pd.concat(found, ignore_index=True) if found else pd.DataFrame(columns=WARNING_COLUMNS)


def concentrations(positions: pd.DataFrame, limit: float, exclude: list[str]) -> pd.DataFrame:
    """Positions larger than limit of their date/entity/currency/side total (on balance sheet)."""
    on_sheet = positions[
        (positions["side"] != "off_balance_sheet") & ~positions["category"].isin(exclude)
    ]
    groups = ["date", "entity", "currency", "side"]
    share = on_sheet["balance_mn"] / on_sheet.groupby(groups)["balance_mn"].transform("sum")
    hit = share > limit
    return _warnings(
        "concentration_share", on_sheet[hit], on_sheet.loc[hit, "position_id"], share[hit], limit
    )


def curve_moves(rates: pd.DataFrame, limit: float) -> pd.DataFrame:
    """Curve points that moved more than limit (decimal) since the previous business day."""
    ordered = rates.sort_values(["currency", "tenor_years", "date"])
    move = ordered.groupby(["currency", "tenor_years"])["rate"].diff()
    hit = move.abs() > limit
    rows = ordered[hit]
    # e.g. "GBP 10Y"; built row by row so that zero rows also work
    subject = [f"{c} {t:g}Y" for c, t in zip(rows["currency"], rows["tenor_years"], strict=True)]
    return _warnings("curve_move", rows, subject, move[hit], limit)


def find_outliers(
    positions: pd.DataFrame, rates: pd.DataFrame, thresholds: OutlierThresholds
) -> pd.DataFrame:
    """Every outlier warning, sorted by check, date and subject (empty if none)."""
    found = [
        balance_jumps(positions, thresholds.balance_jump),
        rate_gaps(positions, rates, thresholds.rate_gap_to_curve),
        concentrations(
            positions,
            thresholds.concentration_share,
            thresholds.concentration_exclude_categories,
        ),
        curve_moves(rates, thresholds.curve_move),
    ]
    found = [frame for frame in found if not frame.empty]
    if not found:
        return pd.DataFrame(columns=WARNING_COLUMNS)
    combined = pd.concat(found, ignore_index=True)
    return combined.sort_values(["check", "date", "subject"], ignore_index=True)
