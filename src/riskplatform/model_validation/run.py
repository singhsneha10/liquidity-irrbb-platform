"""The whole run-off validation in one command: tests T1-T4 of docs/validation_report.md.

Run with:  riskplatform-validate          (or: python -m riskplatform.model_validation.run)
Needs the H.8 deposits saved once:  python -m riskplatform.model_validation.h8

Steps:
1. Load the saved H.8 deposits (refused if the file changed since it was downloaded).
2. Build the default synthetic bank from scratch (config/generator.yaml): generate it and run the
   quality layer, so T3 uses validated data, as the report says. Built fresh, not read from
   data/validated/ (which holds whatever ran last), so the same configs give the same numbers.
3. Build the stressed bank (config/generator_stress.yaml) for T2.
4. Run T1 (backtest), T2 (detection check), T3 (sensitivity) and T4 (benchmark), and rate them;
   and T5, the LCR under documented single-bank runs (case study, report only, no rating).

Writes data/validation/<UTC time>/: results.json (every number, the ratings, the H.8 hash and
the quality run id), t1_spells.csv (every spell above the stable rate, for the H.8 notes check)
t3_sensitivity.csv and t5_single_bank_runs.csv. Exit codes, as for the pipeline: 0 every rating green, 2 any amber, red
or inconclusive overall (needs attention), 1 an error (for example no H.8 file yet).
Confirmed breaks in the H.8 levels (config/validation.yaml) are left out of T1 and T4; the
ratings before those exclusions are kept in results.json (`..._before_breaks`).
"""

import argparse
import json
import logging
import math
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from riskplatform.config import (
    GeneratorConfig,
    ValidationConfig,
    load_generator_config,
    load_limits_config,
    load_quality_config,
    load_validation_config,
)
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.logs import setup_logging
from riskplatform.metrics.run import load_liquidity_regimes, read_validated
from riskplatform.model_validation import allbanks
from riskplatform.model_validation.backtest import GREEN, overall, run_off_rates, t1, t2
from riskplatform.model_validation.fdic import FdicDataError, load_fdic
from riskplatform.model_validation.h8 import H8DataError, load_h8
from riskplatform.model_validation.sensitivity import latest_groups, t3, t4, t5
from riskplatform.quality.run import run_quality

logger = logging.getLogger(__name__)


def _plain(value):
    """JSON-safe: timestamps as ISO dates, NaN and infinities as None."""
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _records(table: pd.DataFrame) -> list[dict]:
    return [{k: _plain(v) for k, v in row.items()} for row in table.to_dict(orient="records")]


def validated_bank(cfg: GeneratorConfig, quality_path: Path, work_dir: Path):
    """Generate the bank, run the quality layer on it, and read back what it published."""
    raw_dir = work_dir / "raw"
    write_outputs(generate(cfg), raw_dir)
    run = run_quality(raw_dir, work_dir, cfg, load_quality_config(quality_path))
    if run.status != "published":
        raise RuntimeError(f"the quality run {run.run_id} stopped: {run.report['stop_reasons']}")
    positions, fx, manifest = read_validated(work_dir / "validated", cfg)
    return positions, fx, manifest["run_id"]


def run_validation(
    vcfg: ValidationConfig,
    liquidity_dir: Path,
    limits_path: Path,
    quality_path: Path,
    root: Path = Path("."),
) -> dict:
    """Every test, its numbers and its rating, as one JSON-safe dict (plus two tables)."""
    rates = run_off_rates(liquidity_dir)
    regimes = load_liquidity_regimes(liquidity_dir)
    limit = load_limits_config(limits_path).limits["lcr"].value
    settings = vcfg.backtest

    deposits, provenance = load_h8(vcfg.h8)
    t1_results = t1(deposits, settings, rates)

    normal_cfg = load_generator_config(root / settings.normal_config)
    stress_cfg = load_generator_config(root / settings.stress_config)
    with tempfile.TemporaryDirectory(prefix="riskplatform-validation-") as work:
        positions, fx, quality_run_id = validated_bank(normal_cfg, quality_path, Path(work))
    stressed = generate(stress_cfg)
    t2_result = t2((positions, fx), (stressed.positions, stressed.fx), stress_cfg, settings, rates)

    stress_rate = max(result["main"]["worst"] for result in t1_results.values())
    groups = latest_groups(positions, fx, normal_cfg)
    t3_result = t3(groups, regimes, stress_rate, limit, vcfg.sensitivity.rate_grid)
    t5_table = t5(groups, regimes, vcfg.single_bank_runs.runs, limit)
    t4_result = t4(
        deposits, settings.h8_weeks, vcfg.sensitivity.benchmark_percentile,
        settings.confirmed_breaks,
    )  # fmt: skip

    t6_result, t6_largest = run_t6(vcfg, rates)
    spells = [
        result.pop("spells_above_stable").assign(series=name) for name, result in t1_results.items()
    ]
    t3_table = t3_result.pop("table")
    ratings = [result["rating"] for result in t1_results.values()]
    return {
        "results": {
            "rates": {"stable": rates.stable, "less_stable": rates.less_stable},
            "h8": {"sha256": provenance["sha256"], "retrieved_at": provenance["retrieved_at"]},
            "synthetic": {
                "quality_run_id": quality_run_id,
                "latest_date": positions["date"].max().date().isoformat(),
            },
            "t1": t1_results,
            "t2": t2_result,
            "t3": {**t3_result, "lowest_stressed_lcr": float(t3_table["stressed_lcr"].min())},
            "t4": {"percentile": vcfg.sensitivity.benchmark_percentile, "values": t4_result},
            "t5": {
                "source": vcfg.single_bank_runs.source,
                "runs": [
                    {
                        "bank": run.bank,
                        "period": run.period,
                        "outflow": run.outflow,
                        "at_least": run.at_least,
                        "lowest_lcr": float(rows["lcr"].min()),
                        "below_limit": int(rows["below_limit"].sum()),
                        "below_minimum": int(rows["below_minimum"].sum()),
                        "results": len(rows),
                    }
                    for run in vcfg.single_bank_runs.runs
                    for rows in [
                        t5_table[
                            (t5_table["bank"] == run.bank) & (t5_table["period"] == run.period)
                        ]
                    ]
                ],
            },
            "t6": t6_result,
            "ratings": {
                **{f"t1_{name}": r["rating"] for name, r in t1_results.items()},
                **{
                    f"t1_{name}_before_breaks": r["raw"]["rating"] for name, r in t1_results.items()
                },
                "t2": t2_result["rating"],
                "t3": t3_result["rating"],
                "overall": overall(ratings, t2_result["rating"], t3_result["rating"]),
            },
        },
        "t1_spells": pd.concat(spells, ignore_index=True),
        "t3_table": t3_table,
        "t5_table": t5_table,
        "t6_largest": t6_largest,
    }


def run_t6(vcfg: ValidationConfig, rates) -> tuple[dict, pd.DataFrame | None]:
    """T6 (report only): every FDIC bank's quarterly outflows. Skipped, and said so, if the FDIC
    data has not been downloaded (it is not in Git)."""
    try:
        deposits, events, provenance = load_fdic(vcfg.fdic)
    except FdicDataError as error:
        logger.warning("T6 not run: %s", error)
        return {"run": False, "reason": str(error)}, None
    settings = vcfg.all_banks
    levels = (rates.stable, rates.less_stable)
    planned = allbanks.quarterly_outflows(
        deposits, events, settings.min_previous_thousands, settings.excluded_by_hand
    )
    stricter = allbanks.quarterly_outflows(
        deposits, events, settings.min_previous_thousands, settings.excluded_by_hand,
        quarters_before=1,
    )  # fmt: skip
    largest = allbanks.largest(
        allbanks.quarterly_outflows(deposits, events, settings.min_previous_thousands),
        settings.top_n,
        settings.top_min_previous_thousands,
    )  # fmt: skip  (the list that was hand-checked: before the hand-check exclusions)
    verdicts = {(h.cert, h.quarter.isoformat()): h for h in settings.hand_check}
    largest["name"] = [verdicts.get((c, q.date().isoformat())) for c, q in
                       zip(largest["cert"], largest["quarter"], strict=True)]  # fmt: skip
    largest["verdict"] = [h.verdict if h else "not checked" for h in largest["name"]]
    largest["name"] = [h.name if h else "" for h in largest["name"]]
    return {
        "run": True,
        "fdic": {"deposits_sha256": provenance["deposits"]["sha256"],
                 "events_sha256": provenance["events"]["sha256"],
                 "last_quarter": provenance["deposits"]["last_quarter"]},
        "planned": allbanks.summarise(planned, levels, settings.stress_years),
        "supplementary_quarter_before": allbanks.summarise(stricter, levels, settings.stress_years),
        "hand_check": {v: sum(h.verdict == v for h in settings.hand_check)
                       for v in ("run", "missed_structure_event", "unresolved")},
    }, largest  # fmt: skip


def write_results(output: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(output["results"], indent=2, allow_nan=False, default=_plain)
    (out_dir / "results.json").write_text(text, encoding="utf-8")
    output["t1_spells"].to_csv(out_dir / "t1_spells.csv", index=False, date_format="%Y-%m-%d")
    output["t3_table"].to_csv(out_dir / "t3_sensitivity.csv", index=False)
    output["t5_table"].to_csv(out_dir / "t5_single_bank_runs.csv", index=False)
    if output["t6_largest"] is not None:
        output["t6_largest"].to_csv(out_dir / "t6_largest.csv", index=False, date_format="%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the LCR retail run-off rates (T1-T4).")
    parser.add_argument("--config", type=Path, default=Path("config/validation.yaml"))
    parser.add_argument("--liquidity", type=Path, default=Path("config/liquidity"))
    parser.add_argument("--limits", type=Path, default=Path("config/limits.yaml"))
    parser.add_argument("--quality", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--out", type=Path, default=Path("data/validation"))
    args = parser.parse_args(argv)

    setup_logging()
    vcfg = load_validation_config(args.config)
    try:
        output = run_validation(vcfg, args.liquidity, args.limits, args.quality)
    except H8DataError as error:
        logger.error("%s", error)
        return 1
    out_dir = args.out / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    write_results(output, out_dir)
    ratings = output["results"]["ratings"]
    for test, rating in ratings.items():
        logger.info("%s: %s", test, rating.upper())
    logger.info("Results in %s", out_dir)
    print(out_dir)
    return 0 if ratings["overall"] == GREEN else 2


if __name__ == "__main__":
    sys.exit(main())
