"""IRRBB run: delta EVE, the outlier test, delta NII and the repricing gap, Basel and UK PRA.

Run with:  python -m riskplatform.irrbb.run                       # both regimes
           python -m riskplatform.irrbb.run --regime pra
           python -m riskplatform.irrbb.run --config config/generator_real.yaml

Reads data/validated/ only if a quality run published it (as the metrics run does). For every
date:

- entity_currency: one entity in one currency, in that currency. Repricing cash flows are
  discounted off that day's curve for that currency. Monitoring only: no outlier flag.
- entity: the entity's currencies converted to its home currency, then combined by the regime's
  rule (Basel: losses only; UK: gains offset at 50%). Outlier test against the entity's implied
  equity (as Tier 1).
- group: all entities, in GBP. Delta EVE and delta NII are additive in the cash flows, so each
  currency's group figure is the sum of the entities' figures in that currency. They are then
  converted and combined, and tested against the group's implied equity.

Delta NII is added up after conversion; neither rulebook sets a cross-currency rule for NII.

Outputs (data/irrbb/<source run id>/): eve.parquet, risk.parquet, nii.parquet, gap.parquet,
summary.json. Exit code 1 if any entity or the group is an outlier.
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from riskplatform.config import (
    GeneratorConfig,
    IrrbbRules,
    load_generator_config,
    load_irrbb_rules,
)
from riskplatform.irrbb.eve import aggregated_losses, delta_eve, eve_risk, is_outlier
from riskplatform.irrbb.nii import delta_nii
from riskplatform.irrbb.repricing import repricing_flows, repricing_gap
from riskplatform.logs import setup_logging
from riskplatform.metrics.run import read_validated
from riskplatform.schemas import rates_schema

logger = logging.getLogger(__name__)

REGIMES = ("basel", "pra")


@dataclass(frozen=True)
class IrrbbResult:
    eve: pd.DataFrame  # delta EVE (entity_currency) and aggregated loss, per scenario
    risk: pd.DataFrame  # worst loss, scenario, Tier 1, threshold, outlier
    nii: pd.DataFrame  # delta NII per NII scenario
    gap: pd.DataFrame  # repricing gap per bucket (entity_currency)


def _equity(group: pd.DataFrame) -> float:
    balance = group["balance_mn"]
    return float(
        balance[group["side"] == "asset"].sum() - balance[group["side"] == "liability"].sum()
    )


def _risk_row(key: dict, regime: str, deltas: dict, rules: IrrbbRules, tier1: float, test: bool):
    loss, scenario = eve_risk(deltas, rules)
    threshold = rules.outlier_threshold.value * tier1
    return {
        **key,
        "regime": regime,
        "eve_risk": loss,
        "worst_scenario": scenario,
        "tier1": tier1,
        "threshold": threshold,
        "outlier": is_outlier(loss, tier1, rules) if test else None,
    }


def compute_irrbb(
    positions: pd.DataFrame,
    rates: pd.DataFrame,
    fx: pd.DataFrame,
    cfg: GeneratorConfig,
    regimes: dict[str, IrrbbRules],
) -> IrrbbResult:
    """IRRBB results for every date and level, under each regime."""
    if positions.empty:
        raise ValueError("no positions to measure")
    curves = {
        key: (curve["tenor_years"].to_numpy(), curve["rate"].to_numpy())
        for key, curve in rates.sort_values("tenor_years").groupby(["date", "currency"])
    }
    to_reporting = fx.set_index(["date", "currency"])["to_reporting"].to_dict()
    home = {e.name: e.home_currency for e in cfg.entities}
    reporting = cfg.fx.reporting_currency

    eve_rows, risk_rows, nii_rows, gap_frames = [], [], [], []
    # Local results, kept for the entity and group levels: (regime, date, entity, currency) -> ...
    local_eve, local_nii, local_equity = {}, {}, {}
    for (day, entity, currency), group in positions.groupby(["date", "entity", "currency"]):
        key = {"date": day, "level": "entity_currency", "entity": entity, "currency": currency}
        if (day, currency) not in curves:  # the quality run stops on this; a second guard
            raise ValueError(f"no {currency} curve on {day.date()}: positions cannot be priced")
        tenors, base = curves[(day, currency)]
        local_equity[(day, entity, currency)] = _equity(group)
        for name, rules in regimes.items():
            flows = repricing_flows(group, rules)
            deltas = delta_eve(flows, tenors, base, rules, currency)
            nii = delta_nii(flows, tenors, base, rules, currency)
            local_eve[(name, day, entity, currency)] = deltas
            local_nii[(name, day, entity, currency)] = nii
            losses = aggregated_losses({currency: deltas}, rules)
            for scenario, change in deltas.items():
                eve_rows.append({**key, "regime": name, "scenario": scenario,
                                 "delta_eve": change, "aggregated_loss": losses[scenario]})  # fmt: skip
            for scenario, change in nii.items():
                nii_rows.append({**key, "regime": name, "scenario": scenario, "delta_nii": change})
            risk_rows.append(_risk_row(key, name, {currency: deltas}, rules, 0.0, test=False))
            gap_frames.append(repricing_gap(group, rules).assign(**key, regime=name))

    def convert(amount: float, day, currency: str, target: str) -> float:
        return amount * to_reporting[(day, currency)] / to_reporting[(day, target)]

    def combine(day, members: list[tuple[str, str]], target: str, key: dict) -> None:
        """Convert the members' (entity, currency) results to target, combine, test, record."""
        tier1 = sum(convert(local_equity[(day, e, c)], day, c, target) for e, c in members)
        for name, rules in regimes.items():
            by_currency: dict[str, dict[str, float]] = {}
            nii_total: dict[str, float] = {}
            for entity, currency in members:
                deltas = local_eve[(name, day, entity, currency)]
                summed = by_currency.setdefault(currency, dict.fromkeys(deltas, 0.0))
                for scenario, change in deltas.items():
                    summed[scenario] += convert(change, day, currency, target)
                for scenario, change in local_nii[(name, day, entity, currency)].items():
                    nii_total[scenario] = nii_total.get(scenario, 0.0) + convert(
                        change, day, currency, target
                    )
            losses = aggregated_losses(by_currency, rules)
            for scenario, loss in losses.items():
                eve_rows.append({**key, "regime": name, "scenario": scenario,
                                 "delta_eve": None, "aggregated_loss": loss})  # fmt: skip
            for scenario, change in nii_total.items():
                nii_rows.append({**key, "regime": name, "scenario": scenario, "delta_nii": change})
            risk_rows.append(_risk_row(key, name, by_currency, rules, tier1, test=True))

    pairs = sorted({(day, e, c) for (_, day, e, c) in local_eve})
    for day in sorted({day for day, _, _ in pairs}):
        members_of_day = [(e, c) for d, e, c in pairs if d == day]
        for entity in sorted({e for e, _ in members_of_day}):
            members = [(e, c) for e, c in members_of_day if e == entity]
            key = {"date": day, "level": "entity", "entity": entity, "currency": home[entity]}
            combine(day, members, home[entity], key)
        key = {"date": day, "level": "group", "entity": None, "currency": reporting}
        combine(day, members_of_day, reporting, key)

    return IrrbbResult(
        eve=pd.DataFrame(eve_rows),
        risk=pd.DataFrame(risk_rows),
        nii=pd.DataFrame(nii_rows),
        gap=pd.concat(gap_frames, ignore_index=True),
    )


def outliers(result: IrrbbResult) -> list[dict]:
    """Entity and group results whose EVE loss exceeds the outlier threshold."""
    flagged = result.risk[result.risk["outlier"].eq(True)]
    return [
        {
            "regime": row.regime,
            "date": row.date.date().isoformat(),
            "level": row.level,
            "entity": None if pd.isna(row.entity) else row.entity,  # JSON has no NaN
            "eve_risk": float(row.eve_risk),
            "worst_scenario": row.worst_scenario,
            "threshold": float(row.threshold),
        }
        for row in flagged.itertuples()
    ]


def headline(result: IrrbbResult) -> list[str]:
    """One line per entity and for the group on the latest date."""
    risk = result.risk
    latest = risk[(risk["date"] == risk["date"].max()) & (risk["level"] != "entity_currency")]
    return [
        f"{row.date.date()} {row.regime:5} {('GROUP' if row.level == 'group' else row.entity):8} "
        f"{row.currency}: EVE loss {row.eve_risk:,.1f} ({row.worst_scenario}) = "
        f"{(row.eve_risk / row.tier1 if row.tier1 else float('nan')):.1%} of Tier 1"
        f"{'  OUTLIER' if row.outlier else ''}"
        for row in latest.itertuples()
    ]


@dataclass(frozen=True)
class IrrbbRun:
    result: IrrbbResult
    summary: dict
    out_dir: Path  # where the results were written: <out>/<source run id>/


def load_irrbb_regimes(
    irrbb_dir: str | Path, names: tuple[str, ...] = REGIMES
) -> dict[str, IrrbbRules]:
    """The named rulebooks from irrbb_dir (<name>.yaml each)."""
    return {name: load_irrbb_rules(Path(irrbb_dir) / f"{name}.yaml") for name in names}


def run_irrbb(
    validated_dir: str | Path,
    out_dir: str | Path,
    cfg: GeneratorConfig,
    regimes: dict[str, IrrbbRules],
) -> IrrbbRun:
    """Read published validated data, calculate IRRBB under each regime, and write the results."""
    validated_dir = Path(validated_dir)
    positions, fx, manifest = read_validated(validated_dir, cfg)
    rates = rates_schema(cfg).validate(pd.read_parquet(validated_dir / "rates.parquet"))
    result = compute_irrbb(positions, rates, fx, cfg, regimes)

    run_dir = Path(out_dir) / manifest["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in ("eve", "risk", "nii", "gap"):
        getattr(result, name).to_parquet(run_dir / f"{name}.parquet", index=False)
    summary = {
        "source_run_id": manifest["run_id"],
        "rates_source": manifest["rates_source"],
        "regimes": {name: rules.name for name, rules in regimes.items()},
        "dates": int(result.risk["date"].nunique()),
        "outliers": outliers(result),
    }
    text = json.dumps(summary, indent=2, allow_nan=False)  # strict JSON: NaN fails loudly
    (run_dir / "summary.json").write_text(text, encoding="utf-8")
    return IrrbbRun(result=result, summary=summary, out_dir=run_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calculate IRRBB: delta EVE, delta NII, gap.")
    parser.add_argument("--config", type=Path, default=Path("config/generator.yaml"))
    parser.add_argument("--regime", choices=[*REGIMES, "both"], default="both")
    parser.add_argument("--irrbb", type=Path, default=Path("config/irrbb"))
    parser.add_argument("--validated", type=Path, default=Path("data/validated"))
    parser.add_argument("--out", type=Path, default=Path("data/irrbb"))
    args = parser.parse_args(argv)

    setup_logging()
    cfg = load_generator_config(args.config)
    names = REGIMES if args.regime == "both" else (args.regime,)
    run = run_irrbb(args.validated, args.out, cfg, load_irrbb_regimes(args.irrbb, names))
    for line in headline(run.result):
        logger.info(line)
    flagged = run.summary["outliers"]
    logger.info("Wrote IRRBB results to %s (%d outlier results)", run.out_dir, len(flagged))
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
