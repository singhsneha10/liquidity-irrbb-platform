"""Control totals: what a source system sends alongside its files, so the receiver can prove
nothing was lost or changed on the way.

The sender (the generator) and the receiver (the quality layer) both use file_totals, so they
always calculate totals the same way. Totals describe the files as sent: if the source sends a
bad value, its totals include it and row-level validation quarantines the row; if data is lost
or changed after sending, the totals disagree and reconciliation stops the run.
"""

import json
from pathlib import Path

import pandas as pd

CONTROL_FILE = "control_totals.json"
BALANCE_KEYS = ["entity", "currency", "side"]


def _key(value) -> str | None:
    return None if pd.isna(value) else str(value)


def file_totals(table: str, frame: pd.DataFrame) -> dict:
    """One file's totals: its row count and, for positions, balance_mn by entity/currency/side.

    A balance that is not a number counts as 0 and empty keys are kept, so the totals can always
    be calculated, whatever the file holds.
    """
    totals: dict = {"table": table, "rows": len(frame)}
    if table == "positions":
        amount = pd.to_numeric(frame["balance_mn"], errors="coerce").fillna(0.0)
        groups = [frame[key] for key in BALANCE_KEYS]
        sums = amount.groupby(groups, dropna=False).sum()
        totals["balance_mn"] = [
            {**dict(zip(BALANCE_KEYS, map(_key, keys), strict=True)), "balance_mn": float(total)}
            for keys, total in sums.items()
        ]
    return totals


def write_control_totals(out_dir: str | Path, files: dict[str, tuple[str, pd.DataFrame]]) -> Path:
    """Write control_totals.json for files given as {relative path: (table, frame)}."""
    path = Path(out_dir) / CONTROL_FILE
    control = {"files": {name: file_totals(table, frame) for name, (table, frame) in files.items()}}
    path.write_text(json.dumps(control, indent=2), encoding="utf-8")
    return path


def read_control_totals(raw_dir: str | Path) -> dict[str, dict]:
    """Read control_totals.json: {relative path: totals}. A missing file is an error."""
    path = Path(raw_dir) / CONTROL_FILE
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: the source sent no control totals")
    return json.loads(path.read_text(encoding="utf-8"))["files"]
