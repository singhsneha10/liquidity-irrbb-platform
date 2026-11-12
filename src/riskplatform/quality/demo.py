"""Error-injection demo: plant known errors in a copy of the data, run the quality layer, and
show that every one is caught the way it should be.

Run with:  python -m riskplatform.quality.demo

Everything happens under --out (default data/demo/); data/raw and data/validated are never
touched. Exit code 0 only if every planted error was caught as expected and the transit-loss
scenario stopped the run, so the demo doubles as an end-to-end test.

Scenario 1 (the source sends bad data): seven known errors are planted in one day's file and
the source's control totals include them. Bad values must be quarantined; the outlier must be
warned about, not quarantined; the run must still publish.
Scenario 2 (data lost after sending): one row disappears but the control totals still count
it. The run must stop and publish nothing.
"""

import argparse
import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from riskplatform.config import (
    GeneratorConfig,
    QualityConfig,
    load_generator_config,
    load_quality_config,
)
from riskplatform.controls import write_control_totals
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.quality.report import table_of
from riskplatform.quality.run import RunResult, run_quality

PREFIX = "BANK_UK_GBP_corporate_loans_"  # floating-rate loans: every error has its own position
INJECTED_DAY = 2  # errors go into the third business day's file
MARKER = ".riskplatform-demo"  # marks a folder this demo created, so only such folders are cleared


@dataclass(frozen=True)
class Injection:
    name: str
    position_id: str
    expected: str  # "quarantined" or "warned"
    check: str  # the check expected to catch it
    plant: Callable[[pd.DataFrame, pd.Series], pd.DataFrame]  # (day frame, row mask) -> frame


def _set(column, value):
    def plant(frame, row):
        frame.loc[row, column] = value
        return frame

    return plant


def _duplicate(frame, row):
    return pd.concat([frame, frame[row]], ignore_index=True)


def _reprice_after_maturity(frame, row):
    frame.loc[row, "reprice_days"] = frame.loc[row, "residual_days"] + 1
    return frame


def _jump(frame, row):
    frame.loc[row, "balance_mn"] = frame.loc[row, "balance_mn"] * 1.5
    return frame


INJECTIONS = [
    Injection("negative balance", PREFIX + "001", "quarantined", "greater_than(0)", _set("balance_mn", -5.0)),
    Injection("unknown currency", PREFIX + "002", "quarantined", "isin(['GBP', 'USD', 'EUR'])", _set("currency", "JPY")),
    Injection("empty entity", PREFIX + "003", "quarantined", "not_nullable", _set("entity", None)),
    Injection("text in a number column", PREFIX + "004", "quarantined", "coerce_dtype('float64')", _set("balance_mn", "abc")),
    Injection("duplicate row", PREFIX + "005", "quarantined", "multiple_fields_uniqueness", _duplicate),
    Injection("reset after maturity", PREFIX + "006", "quarantined", "reprice_days must not exceed residual_days", _reprice_after_maturity),
    Injection("balance up 50% overnight", PREFIX + "007", "warned", "balance_jump", _jump),
]  # fmt: skip


@dataclass(frozen=True)
class DemoResult:
    scorecard: pd.DataFrame  # one row per injection: expected, result, caught_by
    bad_data_run: RunResult  # scenario 1
    lost_data_run: RunResult  # scenario 2

    @property
    def passed(self) -> bool:
        return bool(self.scorecard["caught"].all()) and self.lost_data_run.status == "stopped"


def _resend_control_totals(raw_dir: Path) -> None:
    """The source recalculates its control totals from the files as it is sending them."""
    files = {}
    for path in raw_dir.rglob("*.parquet"):
        name = path.relative_to(raw_dir).as_posix()
        files[name] = (table_of(name), pd.read_parquet(path))
    write_control_totals(raw_dir, files)


def _day_file(raw_dir: Path) -> Path:
    return sorted((raw_dir / "positions").glob("date=*.parquet"))[INJECTED_DAY]


def _plant_errors(raw_dir: Path) -> None:
    path = _day_file(raw_dir)
    frame = pd.read_parquet(path)
    # Parquet cannot mix numbers and text in one column, so this file stores balances as text;
    # every other balance is still a valid number written as text and converts normally.
    frame["balance_mn"] = frame["balance_mn"].astype(object)
    for injection in INJECTIONS:
        row = frame["position_id"] == injection.position_id
        frame = injection.plant(frame, row)
    frame["balance_mn"] = frame["balance_mn"].astype(str)
    frame.to_parquet(path, index=False)


def _caught_by(injection: Injection, run: RunResult, day: str) -> list[str]:
    """The checks that flagged this injection's position in this run."""
    if injection.expected == "warned":
        items = (run.report["outliers"] or {}).get("items", [])
        return [
            item["check"]
            for item in items
            if item["subject"] == injection.position_id and item["date"].startswith(day)
        ]
    quarantine = pd.read_parquet(run.run_dir / "quarantine.parquet")
    ids = quarantine["record"].map(lambda record: json.loads(record).get("position_id"))
    return sorted(set(quarantine.loc[ids == injection.position_id, "check"]))


def _fresh_demo_folder(out_dir: Path) -> None:
    """Empty out_dir for a new demo, refusing to delete anything that is not a demo folder.

    Only a folder this demo created (it holds MARKER) or an empty folder is cleared, so a
    mistaken --out such as data/ can never wipe real data.
    """
    if out_dir.exists() and any(out_dir.iterdir()):
        if not (out_dir / MARKER).exists():
            raise ValueError(f"refusing to clear {out_dir}: it is not a demo folder")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MARKER).write_text("created by riskplatform.quality.demo\n", encoding="utf-8")


def run_demo(cfg: GeneratorConfig, quality_cfg: QualityConfig, out_dir: str | Path) -> DemoResult:
    """Run both scenarios under out_dir (cleared first) and score every planted error."""
    out_dir = Path(out_dir)
    _fresh_demo_folder(out_dir)
    positions, rates = generate(cfg)

    bad_raw = out_dir / "bad_data" / "raw"
    write_outputs(positions, rates, bad_raw)
    day = _day_file(bad_raw).stem.removeprefix("date=")
    _plant_errors(bad_raw)
    _resend_control_totals(bad_raw)
    bad_run = run_quality(bad_raw, out_dir / "bad_data", cfg, quality_cfg)

    lost_raw = out_dir / "lost_data" / "raw"
    write_outputs(positions, rates, lost_raw)
    path = _day_file(lost_raw)
    pd.read_parquet(path).iloc[1:].to_parquet(path, index=False)  # control totals not resent
    lost_run = run_quality(lost_raw, out_dir / "lost_data", cfg, quality_cfg)

    rows = []
    for injection in INJECTIONS:
        caught_by = _caught_by(injection, bad_run, day)
        rows.append(
            {
                "planted": injection.name,
                "position_id": injection.position_id,
                "expected": f"{injection.expected} by {injection.check}",
                "caught": injection.check in caught_by,
                "caught_by": ", ".join(caught_by) or "-",
            }
        )
    return DemoResult(pd.DataFrame(rows), bad_run, lost_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plant known errors and show they are caught.")
    parser.add_argument("--config", type=Path, default=Path("config/generator.yaml"))
    parser.add_argument("--quality", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--out", type=Path, default=Path("data/demo"))
    parser.add_argument("--days", type=int, default=10, help="business days to generate")
    args = parser.parse_args(argv)

    base = load_generator_config(args.config)
    cfg = GeneratorConfig.model_validate({**base.model_dump(), "n_days": args.days})
    result = run_demo(cfg, load_quality_config(args.quality), args.out)

    print("Scenario 1: the source sends bad data")
    for row in result.scorecard.itertuples():
        mark = "CAUGHT" if row.caught else "MISSED"
        print(f"  {mark}  {row.planted:<26} expected {row.expected}  (flagged by: {row.caught_by})")
    bad = result.bad_data_run
    print(f"  Run {bad.status.upper()}; report: {bad.run_dir / 'breaks_report.md'}")
    lost = result.lost_data_run
    print("Scenario 2: data lost after sending")
    print(f"  Run {lost.status.upper()} (expected STOPPED)")
    for reason in lost.report["stop_reasons"]:
        print(f"    {reason}")
    print("DEMO PASSED" if result.passed else "DEMO FAILED")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
