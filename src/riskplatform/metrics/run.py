"""Liquidity metrics run: LCR, NSFR and maturity ladder from published validated data.

Run with:  python -m riskplatform.metrics.run                    # both regimes
           python -m riskplatform.metrics.run --regime pra
           python -m riskplatform.metrics.run --config config/generator_real.yaml

Reads data/validated/ only if it was published by a quality run (its _run.json), and records
which run the results came from. Results for every date, at three levels:

- entity_currency: one entity in one currency, in that currency. Basel and the PRA monitor the
  LCR by significant currency but set no minimum per currency, so `meets_minimum` is empty.
- entity: all of an entity's currencies, converted to its home currency.
- group: every entity, converted to the reporting currency (GBP).

Converting: amount x (reporting per unit of its currency) / (reporting per unit of the target).
Caps and the inflow cap are applied to the combined amounts, not per currency and then summed.

Meeting a minimum is tested on the amounts, HQLA >= minimum x net outflows (and ASF >= minimum x
RSF), not on the ratio. That is the same test whenever the ratio exists, and it also settles
0/0: a book with no net outflows and no HQLA owes nothing, so it meets the minimum (its ratio
stays NaN, "undefined", in the table).

Simplification: the group figure assumes liquidity and funding can move freely between
entities. In practice, surplus liquidity that cannot be transferred out of a subsidiary may not
count at group level, so the entity figures are the stricter view.

Outputs (data/metrics/<source run id>/): lcr.parquet, nsfr.parquet, ladder.parquet,
summary.json. Exit code 1 if any entity or group result is below a regulatory minimum.
"""

import argparse
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from riskplatform.config import (
    GeneratorConfig,
    LadderConfig,
    LiquidityRules,
    load_generator_config,
    load_ladder_config,
    load_liquidity_rules,
)
from riskplatform.logs import setup_logging
from riskplatform.metrics.ladder import build_ladder
from riskplatform.metrics.lcr import calculate_lcr
from riskplatform.metrics.nsfr import calculate_nsfr
from riskplatform.schemas import fx_schema, positions_schema

logger = logging.getLogger(__name__)

MANIFEST = "_run.json"
REGIMES = ("basel", "pra")
KEY_COLUMNS = ["date", "level", "entity", "currency"]


class MetricsInputError(Exception):
    """The validated data cannot be used (not published, or not matching the config)."""


@dataclass(frozen=True)
class MetricsResult:
    lcr: pd.DataFrame
    nsfr: pd.DataFrame
    ladder: pd.DataFrame


def read_validated(
    validated_dir: str | Path, cfg: GeneratorConfig
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Positions, FX and the manifest of the quality run that published them."""
    validated_dir = Path(validated_dir)
    manifest_path = validated_dir / MANIFEST
    if not manifest_path.exists():
        raise MetricsInputError(
            f"{manifest_path} not found: {validated_dir} was not published by a quality run "
            "(run: python -m riskplatform.quality.run)"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("rates_source") != cfg.rates.source:
        raise MetricsInputError(
            f"validated data has rates_source {manifest.get('rates_source')!r} but the config "
            f"uses {cfg.rates.source!r}; run the quality layer with the same config"
        )
    files = sorted((validated_dir / "positions").glob("date=*.parquet"))
    if not files:
        raise MetricsInputError(f"{validated_dir} has no positions files")
    positions = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    if positions.empty:
        raise MetricsInputError(f"{validated_dir}: every positions file is empty")
    positions = positions_schema(cfg).validate(positions)  # safety net: the contract still holds
    fx = fx_schema(cfg).validate(pd.read_parquet(validated_dir / "fx.parquet"))
    return positions, fx, manifest


def fx_converter(fx: pd.DataFrame):
    """A function (dates, currencies) -> reporting currency per unit; error if a rate is missing."""
    table = fx.pivot(index="date", columns="currency", values="to_reporting")

    def rate(dates: pd.Series, currencies: pd.Series) -> np.ndarray:
        rows = table.index.get_indexer(pd.DatetimeIndex(dates))
        cols = table.columns.get_indexer(pd.Index(currencies))
        if (rows < 0).any() or (cols < 0).any():
            raise ValueError("an FX rate is missing for a date or currency")
        values = table.to_numpy()[rows, cols]
        if np.isnan(values).any():
            raise ValueError("an FX rate is missing for a date or currency")
        return values

    return rate


def _converted(positions: pd.DataFrame, rate, target: pd.Series) -> pd.DataFrame:
    """positions with balance_mn converted to the target currency of each row."""
    factor = rate(positions["date"], positions["currency"]) / rate(positions["date"], target)
    return positions.assign(balance_mn=positions["balance_mn"].to_numpy() * factor)


def metric_groups(
    positions: pd.DataFrame, fx: pd.DataFrame, cfg: GeneratorConfig
) -> Iterator[tuple[dict, pd.DataFrame]]:
    """Every (key, positions) to measure: entity-currency, entity (home ccy), group (GBP)."""
    positions = positions.reset_index(drop=True)
    rate = fx_converter(fx)
    for (day, entity, currency), group in positions.groupby(["date", "entity", "currency"]):
        yield (
            {"date": day, "level": "entity_currency", "entity": entity, "currency": currency},
            group,
        )

    home = {e.name: e.home_currency for e in cfg.entities}
    to_home = _converted(positions, rate, positions["entity"].map(home))
    for (day, entity), group in to_home.groupby(["date", "entity"]):
        yield {"date": day, "level": "entity", "entity": entity, "currency": home[entity]}, group

    reporting = cfg.fx.reporting_currency
    to_reporting = _converted(positions, rate, pd.Series(reporting, index=positions.index))
    for day, group in to_reporting.groupby("date"):
        yield {"date": day, "level": "group", "entity": None, "currency": reporting}, group


def _meets(level: str, numerator: float, denominator: float, minimum: float) -> bool | None:
    """numerator >= minimum x denominator, for entity and group results (see module notes).

    Per-currency results are monitoring only (None).
    """
    return None if level == "entity_currency" else bool(numerator >= minimum * denominator)


def compute_metrics(
    positions: pd.DataFrame,
    fx: pd.DataFrame,
    cfg: GeneratorConfig,
    regimes: dict[str, LiquidityRules],
    ladder_cfg: LadderConfig,
) -> MetricsResult:
    """LCR and NSFR for each regime, and the ladder, for every date and level."""
    if positions.empty:
        raise ValueError("no positions to measure")
    lcr_rows, nsfr_rows, ladder_frames = [], [], []
    for key, group in metric_groups(positions, fx, cfg):
        for name, rules in regimes.items():
            lcr = calculate_lcr(group, rules)
            stock = lcr.stock
            lcr_minimum = rules.lcr.minimum.value
            lcr_rows.append(
                {
                    **key,
                    "regime": name,
                    "level1": stock.level1,
                    "level1_covered": stock.level1_covered_counted,
                    "level2a": stock.level2a_counted,
                    "level2b": stock.level2b_counted,
                    "hqla_before_caps": stock.before_caps,
                    "hqla": stock.total,
                    "outflows": lcr.outflows,
                    "inflows": lcr.inflows,
                    "inflows_capped": lcr.inflows_capped,
                    "net_outflows": lcr.net_outflows,
                    "lcr": lcr.ratio,
                    "minimum": lcr_minimum,
                    "meets_minimum": _meets(
                        key["level"], stock.total, lcr.net_outflows, lcr_minimum
                    ),
                }
            )
            nsfr = calculate_nsfr(group, rules)
            nsfr_minimum = rules.nsfr.minimum.value
            nsfr_rows.append(
                {
                    **key,
                    "regime": name,
                    "asf": nsfr.asf,
                    "rsf": nsfr.rsf,
                    "equity": nsfr.equity,
                    "nsfr": nsfr.ratio,
                    "minimum": nsfr_minimum,
                    "meets_minimum": _meets(key["level"], nsfr.asf, nsfr.rsf, nsfr_minimum),
                }
            )
        ladder = build_ladder(group, ladder_cfg)
        ladder_frames.append(ladder.table.assign(**key, contingent=ladder.contingent))
    ladder_table = pd.concat(ladder_frames, ignore_index=True)
    ordered = KEY_COLUMNS + [c for c in ladder_table.columns if c not in KEY_COLUMNS]
    return MetricsResult(
        lcr=pd.DataFrame(lcr_rows),
        nsfr=pd.DataFrame(nsfr_rows),
        ladder=ladder_table[ordered],
    )


def _breaches(result: MetricsResult) -> list[dict]:
    """Entity and group results below a regulatory minimum."""
    found = []
    for metric, table in (("lcr", result.lcr), ("nsfr", result.nsfr)):
        below = table[table["meets_minimum"].eq(False)]
        for row in below.itertuples():
            found.append(
                {
                    "metric": metric,
                    "regime": row.regime,
                    "date": row.date.date().isoformat(),
                    "level": row.level,
                    "entity": None if pd.isna(row.entity) else row.entity,  # JSON has no NaN
                    "value": float(getattr(row, metric)),
                    "minimum": float(row.minimum),
                }
            )
    return found


def headline(result: MetricsResult) -> list[str]:
    """One line per entity and for the group on the latest date: LCR and NSFR by regime."""
    keys = [*KEY_COLUMNS, "regime"]
    joined = result.lcr[[*keys, "lcr"]].merge(result.nsfr[[*keys, "nsfr"]], on=keys)
    latest = joined[
        (joined["date"] == joined["date"].max()) & (joined["level"] != "entity_currency")
    ]
    return [
        f"{row.date.date()} {row.regime:5} {('GROUP' if row.level == 'group' else row.entity):8} "
        f"{row.currency}: LCR {row.lcr:.1%}  NSFR {row.nsfr:.1%}"
        for row in latest.itertuples()
    ]


def write_metrics(out_dir: Path, result: MetricsResult, summary: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result.lcr.to_parquet(out_dir / "lcr.parquet", index=False)
    result.nsfr.to_parquet(out_dir / "nsfr.parquet", index=False)
    result.ladder.to_parquet(out_dir / "ladder.parquet", index=False)
    text = json.dumps(summary, indent=2, allow_nan=False)  # strict JSON: NaN fails loudly
    (out_dir / "summary.json").write_text(text, encoding="utf-8")


@dataclass(frozen=True)
class MetricsRun:
    result: MetricsResult
    summary: dict
    out_dir: Path  # where the results were written: <out>/<source run id>/


def load_liquidity_regimes(
    liquidity_dir: str | Path, names: tuple[str, ...] = REGIMES
) -> dict[str, LiquidityRules]:
    """The named rulebooks from liquidity_dir (<name>.yaml each)."""
    return {name: load_liquidity_rules(Path(liquidity_dir) / f"{name}.yaml") for name in names}


def run_metrics(
    validated_dir: str | Path,
    out_dir: str | Path,
    cfg: GeneratorConfig,
    regimes: dict[str, LiquidityRules],
    ladder_cfg: LadderConfig,
) -> MetricsRun:
    """Read published validated data, calculate every metric, and write the results."""
    positions, fx, manifest = read_validated(validated_dir, cfg)
    result = compute_metrics(positions, fx, cfg, regimes, ladder_cfg)
    summary = {
        "source_run_id": manifest["run_id"],
        "source_breaks_report": manifest.get("breaks_report"),
        "rates_source": manifest["rates_source"],
        "regimes": {name: rules.name for name, rules in regimes.items()},
        "dates": int(result.lcr["date"].nunique()),
        "rows": {"lcr": len(result.lcr), "nsfr": len(result.nsfr), "ladder": len(result.ladder)},
        "breaches": _breaches(result),
    }
    run_dir = Path(out_dir) / manifest["run_id"]
    write_metrics(run_dir, result, summary)
    return MetricsRun(result=result, summary=summary, out_dir=run_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calculate LCR, NSFR and the maturity ladder.")
    parser.add_argument("--config", type=Path, default=Path("config/generator.yaml"))
    parser.add_argument("--regime", choices=[*REGIMES, "both"], default="both")
    parser.add_argument("--liquidity", type=Path, default=Path("config/liquidity"))
    parser.add_argument("--validated", type=Path, default=Path("data/validated"))
    parser.add_argument("--out", type=Path, default=Path("data/metrics"))
    args = parser.parse_args(argv)

    setup_logging()
    cfg = load_generator_config(args.config)
    names = REGIMES if args.regime == "both" else (args.regime,)
    regimes = load_liquidity_regimes(args.liquidity, names)
    ladder_cfg = load_ladder_config(args.liquidity / "ladder.yaml")

    run = run_metrics(args.validated, args.out, cfg, regimes, ladder_cfg)
    for line in headline(run.result):
        logger.info(line)
    breaches = run.summary["breaches"]
    logger.info("Wrote metrics to %s (%d breaches)", run.out_dir, len(breaches))
    return 1 if breaches else 0


if __name__ == "__main__":
    sys.exit(main())
