"""Reconciliation: prove the files that arrived are exactly what the source sent, and explain the
gap between what arrived and what passed row-level validation (the quarantine).

Stop the run (ReconciliationFailed) when a file is missing or unexpected, or when its row count
or balance totals differ from the control totals: data was lost or changed after sending.
The arrived-to-validated gap is not a failure; it is reported in rows and in balance_mn.
"""

from dataclasses import dataclass

import pandas as pd

from riskplatform.controls import BALANCE_KEYS, file_totals
from riskplatform.quality.rows import FileLevelError

# Relative tolerance on balance totals. Both sides sum the same way, so any real difference
# is far larger than this; it only absorbs floating-point rounding.
TOLERANCE = 1e-9

FILE_COLUMNS = [
    "file",
    "table",
    "source_rows",
    "arrived_rows",
    "validated_rows",
    "quarantined_rows",
]
BALANCE_COLUMNS = [
    "file",
    *BALANCE_KEYS,
    "source_mn",
    "arrived_mn",
    "validated_mn",
    "quarantined_mn",
]


@dataclass(frozen=True)
class Reconciliation:
    files: pd.DataFrame  # one row per file: rows sent, arrived, validated, quarantined
    balances: pd.DataFrame  # one row per file/entity/currency/side: the same, in balance_mn


class ReconciliationFailed(FileLevelError):
    """The files that arrived differ from what the source sent. Carries the reconciliation."""

    def __init__(self, problems: list[str], reconciliation: Reconciliation):
        self.reconciliation = reconciliation
        super().__init__("positions and rates", "control totals", problems)


def _balances(totals: dict, column: str) -> pd.DataFrame:
    rows = totals.get("balance_mn", [])
    frame = pd.DataFrame(rows, columns=[*BALANCE_KEYS, "balance_mn"])
    return frame.rename(columns={"balance_mn": column})


def _differs(arrived: float, sent: float) -> bool:
    return abs(arrived - sent) > TOLERANCE * max(1.0, abs(sent))


def reconcile(
    control: dict[str, dict],
    arrived: dict[str, pd.DataFrame],
    validated: dict[str, pd.DataFrame],
) -> Reconciliation:
    """Compare sent (control totals), arrived (raw tables) and validated tables, file by file.

    All three are keyed by the file's path relative to the raw folder.
    """
    problems = [f"{name}: sent but did not arrive" for name in sorted(set(control) - set(arrived))]
    problems += [
        f"{name}: arrived but was not sent" for name in sorted(set(arrived) - set(control))
    ]

    file_rows, balance_frames = [], []
    for name in sorted(set(control) & set(arrived)):
        sent = control[name]
        table = sent["table"]
        got = file_totals(table, arrived[name])
        passed = file_totals(table, validated[name])
        if got["rows"] != sent["rows"]:
            problems.append(f"{name}: {sent['rows']} rows sent, {got['rows']} arrived")
        file_rows.append(
            {
                "file": name,
                "table": table,
                "source_rows": sent["rows"],
                "arrived_rows": got["rows"],
                "validated_rows": passed["rows"],
                "quarantined_rows": got["rows"] - passed["rows"],
            }
        )
        if table != "positions":
            continue
        balances = (
            _balances(sent, "source_mn")
            .merge(_balances(got, "arrived_mn"), on=BALANCE_KEYS, how="outer")
            .merge(_balances(passed, "validated_mn"), on=BALANCE_KEYS, how="outer")
        )
        amounts = ["source_mn", "arrived_mn", "validated_mn"]
        balances[amounts] = balances[amounts].fillna(0.0)
        balances["quarantined_mn"] = balances["arrived_mn"] - balances["validated_mn"]
        for row in balances.itertuples():
            if _differs(row.arrived_mn, row.source_mn):
                problems.append(
                    f"{name}: balance_mn for {row.entity}/{row.currency}/{row.side} "
                    f"sent {row.source_mn:,.2f}, arrived {row.arrived_mn:,.2f}"
                )
        balance_frames.append(balances.assign(file=name))

    reconciliation = Reconciliation(
        files=pd.DataFrame(file_rows, columns=FILE_COLUMNS),
        balances=(
            pd.concat(balance_frames, ignore_index=True)[BALANCE_COLUMNS]
            if balance_frames
            else pd.DataFrame(columns=BALANCE_COLUMNS)
        ),
    )
    if problems:
        raise ReconciliationFailed(problems, reconciliation)
    return reconciliation
