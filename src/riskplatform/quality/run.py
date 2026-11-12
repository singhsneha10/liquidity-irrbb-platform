"""One quality run: raw files -> validated data, quarantine and a breaks report.

Run with:  python -m riskplatform.quality.run
           python -m riskplatform.quality.run --config config/generator_real.yaml

Stages (see docs/data_quality.md): read files and control totals -> row validation per file ->
reconciliation -> file-level checks -> outliers -> publish. Any FileLevelError stops the run:
data/validated/ is left exactly as it was, and the run's own folder records why.

Outputs (under --out, default data/):
  validated/                    latest good data, with _run.json naming the run that made it
  quality_runs/<run_id>/        every run: breaks_report.md, breaks_report.json, quarantine.parquet
"""

import argparse
import json
import logging
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from riskplatform.config import (
    GeneratorConfig,
    QualityConfig,
    load_generator_config,
    load_quality_config,
)
from riskplatform.controls import CONTROL_FILE, read_control_totals
from riskplatform.quality.files import FileChecksFailed, check_files
from riskplatform.quality.outliers import find_outliers
from riskplatform.quality.reconcile import ReconciliationFailed, reconcile
from riskplatform.quality.report import build_report, table_of, write_report
from riskplatform.quality.rows import QUARANTINE_COLUMNS, FileLevelError, validate_rows
from riskplatform.schemas import positions_schema, quarantine_schema, rates_schema

logger = logging.getLogger(__name__)

MANIFEST = "_run.json"
ALL_FILES = "positions and rates"


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str  # "published" or "stopped"
    run_dir: Path
    report: dict


def _new_run_id(run_at: pd.Timestamp) -> str:
    """Sortable by time, and unique even for two runs in the same second."""
    return f"{run_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _read_raw(raw_dir: Path) -> dict[str, pd.DataFrame]:
    """Every raw data file, keyed by its path relative to raw_dir. Unreadable files stop the run."""
    paths = sorted((raw_dir / "positions").glob("date=*.parquet"))
    paths += [raw_dir / "rates.parquet"] if (raw_dir / "rates.parquet").exists() else []
    arrived = {}
    for path in paths:
        name = path.relative_to(raw_dir).as_posix()
        try:
            arrived[name] = pd.read_parquet(path)
        except (OSError, ValueError) as error:
            raise FileLevelError(table_of(name), name, [f"cannot be read ({error})"]) from error
    return arrived


def _rename(source: Path, target: Path, attempts: int = 40, wait_seconds: float = 0.25) -> None:
    """Rename a folder, retrying for a few seconds if refused.

    On Windows, antivirus or file indexing can briefly lock files that were just written, so a
    rename straight after writing is sometimes refused although it succeeds a moment later.
    """
    for attempt in range(attempts):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait_seconds)


def _publish(out_dir: Path, run_id: str, validated: dict, manifest: dict) -> None:
    """Replace out_dir/validated with this run's data, never leaving it half-written."""
    target = out_dir / "validated"
    staging = out_dir / f"validated.staging-{run_id}"
    for name, frame in validated.items():
        path = staging / name
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
    (staging / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    previous = out_dir / f"validated.previous-{run_id}"
    if target.exists():
        _rename(target, previous)
    _rename(staging, target)
    if previous.exists():
        shutil.rmtree(previous)


def run_quality(
    raw_dir: str | Path,
    out_dir: str | Path,
    cfg: GeneratorConfig,
    quality_cfg: QualityConfig,
    run_at: pd.Timestamp | None = None,
) -> RunResult:
    """Run every quality stage on raw_dir and write the outputs under out_dir."""
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    run_at = run_at if run_at is not None else pd.Timestamp.now(tz="UTC")
    run_id = _new_run_id(run_at)
    schemas = {"positions": positions_schema(cfg), "rates": rates_schema(cfg)}

    arrived: dict[str, pd.DataFrame] = {}
    validated: dict[str, pd.DataFrame] = {}
    quarantines: list[pd.DataFrame] = []
    reconciliation = file_checks = outliers = None
    stop_reasons: list[str] = []
    try:
        arrived = _read_raw(raw_dir)
        position_files = [name for name in arrived if table_of(name) == "positions"]
        if not position_files or "rates.parquet" not in arrived:
            raise FileLevelError(
                ALL_FILES, raw_dir.as_posix(), ["positions or rates files missing"]
            )
        try:
            control = read_control_totals(raw_dir)
        except FileNotFoundError as error:
            raise FileLevelError(ALL_FILES, CONTROL_FILE, [str(error)]) from error

        for name, frame in arrived.items():
            table = table_of(name)
            result = validate_rows(
                frame, schemas[table], table=table, source_file=name, run_id=run_id, run_at=run_at
            )
            validated[name] = result.valid
            quarantines.append(result.quarantine)

        reconciliation = reconcile(control, arrived, validated)
        positions = pd.concat(
            [validated[name].assign(filename=name) for name in position_files], ignore_index=True
        )
        rates = validated["rates.parquet"]
        file_checks = check_files(positions, rates, cfg)
        outliers = find_outliers(positions.drop(columns="filename"), rates, quality_cfg.outliers)
    except FileLevelError as stop:
        stop_reasons = [f"{stop.source_file}: {problem}" for problem in stop.problems]
        if isinstance(stop, ReconciliationFailed):
            reconciliation = stop.reconciliation
        if isinstance(stop, FileChecksFailed):
            file_checks = stop.report

    status = "stopped" if stop_reasons else "published"
    found = [q for q in quarantines if not q.empty]
    quarantine = (
        pd.concat(found, ignore_index=True)
        if found
        else quarantine_schema().validate(pd.DataFrame(columns=QUARANTINE_COLUMNS))
    )
    run_dir = out_dir / "quality_runs" / run_id
    run_dir.mkdir(parents=True)
    quarantine.to_parquet(run_dir / "quarantine.parquet", index=False)
    report = build_report(
        run_id=run_id,
        run_at=run_at,
        status=status,
        stop_reasons=stop_reasons,
        raw_dir=raw_dir,
        arrived=arrived,
        validated=validated,
        quarantine=quarantine,
        reconciliation=reconciliation,
        file_checks=file_checks,
        outliers=outliers,
    )
    if status == "published":
        manifest = {
            "run_id": run_id,
            "run_at": run_at.isoformat(),
            "raw_dir": raw_dir.as_posix(),
            "rates_source": cfg.rates.source,
            "breaks_report": (run_dir / "breaks_report.md").as_posix(),
        }
        _publish(out_dir, run_id, validated, manifest)
    write_report(run_dir, report)
    return RunResult(run_id=run_id, status=status, run_dir=run_dir, report=report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate raw files and publish clean data.")
    parser.add_argument("--config", type=Path, default=Path("config/generator.yaml"))
    parser.add_argument("--quality", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--out", type=Path, default=Path("data"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_quality(
        args.raw, args.out, load_generator_config(args.config), load_quality_config(args.quality)
    )
    rows = result.report["rows"]
    logger.info(
        "Run %s %s: %d rows quarantined; report at %s",
        result.run_id,
        result.status.upper(),
        rows["quarantined"],
        result.run_dir / "breaks_report.md",
    )
    for reason in result.report["stop_reasons"]:
        logger.error("Stopped: %s", reason)
    return 0 if result.status == "published" else 1


if __name__ == "__main__":
    sys.exit(main())
