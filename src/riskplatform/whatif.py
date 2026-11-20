"""What-if scenarios and limit drivers on the latest day, with the pipeline's own code.

Two tools for the dashboard (no Streamlit here, so every number is tested without a browser):

1. what_if: stress the latest business day with three levers (config/whatif.yaml) and measure
   the LCR, NSFR, EVE and every internal limit again:
   - retail run-off add-on: points added to both retail LCR run-off rates (capped at 100%);
   - parallel shock add-on: basis points added to the parallel shock of every currency;
   - deposit outflow: a share of every retail deposit withdrawn today, before the stress starts,
     paid out of the same book's assets (same entity, same currency) in funding_order.
   Only a copy of the rulebook or of the positions changes. The measuring is the pipeline's own,
   unchanged: compute_metrics (LCR, NSFR), compute_irrbb (EVE, the outlier test) and
   measure_limits. With every lever at 0 the result equals the published limits (tested).

2. Drivers: what a limit's figure is made of, position by position, for "What drives this
   limit". Each driver table adds up exactly to the published figure (tested):
   - EVE: ΔEVE is linear in the repricing cash flows, so each position's ΔEVE is measured alone
     with repricing_flows and delta_eve (unchanged) and the parts add up to the book's ΔEVE.
     Across currencies a currency's ΔEVE counts in full if it is a loss and x gains_weight if a
     gain (Basel 0, UK 50%), so each position's contribution to the aggregated loss is its ΔEVE
     times its currency's weight, and the contributions add up to the aggregated loss.
   - LCR: HQLA after haircut, outflows (balance x run-off rate) and inflows (balance x inflow
     rate) per position, as calculate_lcr counts them. The level caps and the inflow cap apply to
     totals, so they are shown as their own rows, not split across positions.
   - NSFR: each position x its ASF or RSF factor (calculate_nsfr's own rows), plus equity.

Simplifications (labelled): the latest day only; the configs in config/ are taken to be the ones
the run used (true for the demo, which builds its run from them); a withdrawal is paid at book
value with no fire-sale loss, and a book that cannot pay from its own liquid assets stops the
what-if (UnfundedOutflowError) instead of borrowing from another currency or a central bank.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from riskplatform.config import (
    GeneratorConfig,
    IrrbbRules,
    LadderConfig,
    LimitsConfig,
    LiquidityRules,
    WhatIfConfig,
    load_generator_config,
    load_ladder_config,
    load_limits_config,
    load_whatif_config,
)
from riskplatform.irrbb.eve import delta_eve
from riskplatform.irrbb.repricing import repricing_pieces
from riskplatform.irrbb.run import compute_irrbb, load_irrbb_regimes
from riskplatform.limits import measure_limits
from riskplatform.metrics.lcr import calculate_lcr, hqla_stock
from riskplatform.metrics.nsfr import EQUITY, calculate_nsfr
from riskplatform.metrics.run import (
    compute_metrics,
    fx_converter,
    load_liquidity_regimes,
    metric_groups,
    read_validated,
)
from riskplatform.schemas import rates_schema

GROUP = "GROUP"
LEVEL_COLUMNS = ["who", "limit_name", "kind", "actual", "limit_value", "utilisation", "status"]
EPSILON = 1e-9  # mn: a remainder this small is rounding, not an unpaid withdrawal


class UnfundedOutflowError(ValueError):
    """A book cannot pay the withdrawal from its own liquid assets."""


@dataclass(frozen=True)
class Day:
    """The latest business day's inputs and the rules to measure them with."""

    run_id: str  # the quality run that published the positions
    positions: pd.DataFrame
    fx: pd.DataFrame
    rates: pd.DataFrame
    cfg: GeneratorConfig
    liquidity: dict[str, LiquidityRules]
    irrbb: dict[str, IrrbbRules]
    ladder: LadderConfig
    limits: LimitsConfig
    whatif: WhatIfConfig

    @property
    def date(self) -> pd.Timestamp:
        return pd.Timestamp(self.positions["date"].iloc[0])


@dataclass(frozen=True)
class Levers:
    retail_run_off_add_on: float = 0.0  # 0.10 = ten points on both retail run-off rates
    parallel_shock_add_on_bp: float = 0.0
    deposit_outflow: float = 0.0  # 0.05 = 5% of every retail deposit withdrawn today

    def __post_init__(self) -> None:
        for name in ("retail_run_off_add_on", "parallel_shock_add_on_bp", "deposit_outflow"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative: a lever only adds stress")
        if self.deposit_outflow > 1:
            raise ValueError("deposit_outflow is a share, at most 1")


def load_day(validated_dir: str | Path, config_dir: str | Path) -> Day:
    """The latest validated day and the rules in config_dir (as the pipeline loads them)."""
    config_dir = Path(config_dir)
    cfg = load_generator_config(config_dir / "generator.yaml")
    positions, fx, manifest = read_validated(validated_dir, cfg)
    rates = rates_schema(cfg).validate(pd.read_parquet(Path(validated_dir) / "rates.parquet"))
    latest = positions["date"].max()
    return Day(
        run_id=manifest["run_id"],
        positions=positions[positions["date"] == latest].reset_index(drop=True),
        fx=fx[fx["date"] == latest].reset_index(drop=True),
        rates=rates[rates["date"] == latest].reset_index(drop=True),
        cfg=cfg,
        liquidity=load_liquidity_regimes(config_dir / "liquidity"),
        irrbb=load_irrbb_regimes(config_dir / "irrbb"),
        ladder=load_ladder_config(config_dir / "liquidity" / "ladder.yaml"),
        limits=load_limits_config(config_dir / "limits.yaml"),
        whatif=load_whatif_config(config_dir / "whatif.yaml"),
    )


# --- The levers ---------------------------------------------------------------------------------


def with_run_off_add_on(
    rules: LiquidityRules, add_on: float, categories: list[str]
) -> LiquidityRules:
    """A copy of the rulebook with add_on added to each listed outflow rate, capped at 100%."""
    raw = rules.model_dump()
    for category in categories:
        outflow = raw["lcr"]["outflows"][category]
        outflow["rate"] = min(1.0, outflow["rate"] + add_on)
    return LiquidityRules.model_validate(raw)


def with_parallel_add_on(rules: IrrbbRules, add_on_bp: float) -> IrrbbRules:
    """A copy of the rulebook with add_on_bp added to every currency's parallel shock."""
    raw = rules.model_dump()
    for sizes in raw["shocks"]["sizes_bp"].values():
        sizes["parallel"] += add_on_bp
    return IrrbbRules.model_validate(raw)


def after_withdrawal(
    positions: pd.DataFrame, share: float, withdrawn: list[str], funding_order: list[str]
) -> pd.DataFrame:
    """positions after `share` of every listed deposit is withdrawn and paid in funding_order.

    Each book (entity, currency) pays its own withdrawal: the first category in funding_order
    is used up before the next, pro rata across its positions. Assets and liabilities fall by
    the same amount, so equity (the Tier 1 proxy) does not change.
    """
    balance = positions["balance_mn"].to_numpy(dtype=float).copy()
    category = positions["category"].to_numpy()
    leaving = np.isin(category, withdrawn)
    unpaid = []
    for (entity, currency), rows in positions.groupby(["entity", "currency"]).groups.items():
        rows = positions.index.get_indexer(rows)
        owed = share * balance[rows][leaving[rows]].sum()
        balance[rows[leaving[rows]]] *= 1.0 - share
        for name in funding_order:
            if owed <= EPSILON:
                break
            held = rows[category[rows] == name]
            available = balance[held].sum()
            if available <= 0:
                continue
            paid = min(owed, available)
            balance[held] *= 1.0 - paid / available
            owed -= paid
        if owed > EPSILON:
            unpaid.append(f"{entity} {currency} ({owed:,.1f} mn)")
    if unpaid:
        raise UnfundedOutflowError(
            "these books cannot pay the withdrawal from their own liquid assets: "
            + ", ".join(unpaid)
        )
    return positions.assign(balance_mn=balance)


def what_if(day: Day, regime: str, levers: Levers) -> pd.DataFrame:
    """Every internal limit of the latest day under one rulebook, after the levers."""
    settings = day.whatif
    liquidity = with_run_off_add_on(
        day.liquidity[regime], levers.retail_run_off_add_on, settings.retail_deposits
    )
    irrbb = with_parallel_add_on(day.irrbb[regime], levers.parallel_shock_add_on_bp)
    positions = day.positions
    if levers.deposit_outflow > 0:
        positions = after_withdrawal(
            positions, levers.deposit_outflow, settings.retail_deposits, settings.funding_order
        )
    metrics = compute_metrics(positions, day.fx, day.cfg, {regime: liquidity}, day.ladder)
    irrbb_result = compute_irrbb(positions, day.rates, day.fx, day.cfg, {regime: irrbb})
    limits = measure_limits(metrics.lcr, metrics.nsfr, irrbb_result.risk, day.limits)
    limits = limits.assign(who=limits["entity"].fillna(GROUP))
    order = {name: i for i, name in enumerate(day.limits.limits)}
    return (
        limits.assign(_who=limits["who"].eq(GROUP), _limit=limits["limit_name"].map(order))
        .sort_values(["_limit", "_who", "who"])[LEVEL_COLUMNS]
        .reset_index(drop=True)
    )


# --- Drivers ------------------------------------------------------------------------------------


def _members(day: Day, who: str) -> pd.DataFrame:
    return day.positions if who == GROUP else day.positions[day.positions["entity"] == who]


def target_currency(day: Day, who: str) -> str:
    """The currency who's figures are in: the entity's home currency, or the reporting one."""
    if who == GROUP:
        return day.cfg.fx.reporting_currency
    return {e.name: e.home_currency for e in day.cfg.entities}[who]


def _converted_group(day: Day, who: str) -> pd.DataFrame:
    """who's positions converted as the metrics run converts them (home currency, or GBP)."""
    level = "group" if who == GROUP else "entity"
    for key, group in metric_groups(day.positions, day.fx, day.cfg):
        if key["level"] == level and (who == GROUP or key["entity"] == who):
            return group
    raise ValueError(f"no positions for {who}")


def unit_values(rules: IrrbbRules, currency: str, tenors, base) -> np.ndarray:
    """ΔEVE of one unit of cash flow in each bucket (rows) under each scenario (columns), by
    delta_eve itself, so a position's ΔEVE is its repricing pieces times these values."""
    values = np.zeros((len(rules.buckets), len(rules.shocks.scenarios)))
    for i, bucket in enumerate(rules.buckets):
        unit = pd.DataFrame({"time_years": [bucket.midpoint_years], "amount": [1.0]})
        change = delta_eve(unit, tenors, base, rules, currency)
        values[i] = [change[scenario] for scenario in rules.shocks.scenarios]
    return values


def eve_drivers(day: Day, regime: str, who: str, scenario: str) -> pd.DataFrame:
    """Each position's contribution to who's aggregated EVE loss under one scenario.

    Columns: position_id, entity, currency, category, side, balance_mn (own currency), delta_eve
    (own currency, negative = a loss), contribution (target currency, positive = adds to the
    loss). The contributions add up to the aggregated loss of the IRRBB run.
    """
    rules = day.irrbb[regime]
    if scenario not in rules.shocks.scenarios:
        raise ValueError(f"{regime}: unknown scenario {scenario!r}")
    column = rules.shocks.scenarios.index(scenario)
    members = _members(day, who).reset_index(drop=True)
    change = np.zeros(len(members))
    for currency, book in members.groupby("currency"):
        curve = day.rates[day.rates["currency"] == currency].sort_values("tenor_years")
        values = unit_values(rules, currency, curve["tenor_years"].to_numpy(),
                             curve["rate"].to_numpy())  # fmt: skip
        pieces = repricing_pieces(book.reset_index(drop=True), rules)
        weighted = pieces["amount"].to_numpy() * values[pieces["bucket"].to_numpy(), column]
        change[book.index] = np.bincount(pieces["row"], weights=weighted, minlength=len(book))
    rate = fx_converter(day.fx)
    target = pd.Series(target_currency(day, who), index=members.index)
    converted = change * rate(members["date"], members["currency"]) / rate(members["date"], target)
    by_currency = pd.Series(converted).groupby(members["currency"].to_numpy()).sum()
    weight = {
        currency: 1.0 if total < 0 else rules.aggregation.gains_weight
        for currency, total in by_currency.items()
    }
    contribution = -converted * members["currency"].map(weight).to_numpy()
    columns = ["position_id", "entity", "currency", "category", "side", "balance_mn"]
    return members[columns].assign(delta_eve=change, contribution=contribution)


def lcr_drivers(day: Day, regime: str, who: str) -> pd.DataFrame:
    """Each position's part in who's LCR: HQLA after haircut, outflow or inflow.

    Columns: position_id, entity, currency, category, side, balance_mn (target currency),
    role (hqla, outflow, inflow), amount (the counted amount, target currency). Positions that
    do not count (e.g. a loan due after 30 days) are left out.
    """
    rules = day.liquidity[regime].lcr
    group = _converted_group(day, who)
    horizon = int(rules.horizon_days.value)
    category = group["category"].to_numpy()
    balance = group["balance_mn"].to_numpy(dtype=float)
    due = group["residual_days"].to_numpy() <= horizon
    role = np.full(len(group), "", dtype=object)
    amount = np.zeros(len(group))
    for name, treatment in rules.hqla.items():
        rows = category == name
        role[rows], amount[rows] = "hqla", balance[rows] * (1.0 - treatment.haircut)
    for kind, table in (("outflow", rules.outflows), ("inflow", rules.inflows)):
        for name, treatment in table.items():
            rows = category == name
            if treatment.within_horizon_only:
                rows &= due
            role[rows], amount[rows] = kind, balance[rows] * treatment.rate
    columns = ["position_id", "entity", "currency", "category", "side", "balance_mn"]
    counted = (role != "") & (amount != 0)
    return group[columns].assign(role=role, amount=amount)[counted].reset_index(drop=True)


def lcr_summary(day: Day, regime: str, who: str) -> pd.DataFrame:
    """who's LCR built up step by step, with the caps (target currency): item, amount."""
    rules = day.liquidity[regime]
    group = _converted_group(day, who)
    result = calculate_lcr(group, rules)
    stock = hqla_stock(group, rules)
    return pd.DataFrame(
        {
            "item": [
                "HQLA after haircuts, before caps",
                "less the Level 2 and covered-bond caps",
                "HQLA counted",
                "outflows",
                "inflows",
                "less the inflow cap",
                "net outflows",
            ],
            "amount": [
                stock.before_caps,
                stock.total - stock.before_caps,
                stock.total,
                result.outflows,
                result.inflows,
                result.inflows_capped - result.inflows,
                result.net_outflows,
            ],
        }
    )


def nsfr_drivers(day: Day, regime: str, who: str) -> pd.DataFrame:
    """Each position x its ASF or RSF factor, plus equity (ASF), for who's NSFR.

    Columns: position_id, entity, currency, category, side, balance_mn (target currency),
    role (asf, rsf), amount (weighted). Positions with a zero factor are left out.
    """
    group = _converted_group(day, who).reset_index(drop=True)
    result = calculate_nsfr(group, day.liquidity[regime])
    columns = ["position_id", "entity", "currency", "category", "side", "balance_mn"]
    table = group[columns].assign(
        role=np.where(group["side"] == "liability", "asf", "rsf"),
        amount=result.rows["weighted"].to_numpy(),
    )
    equity = {"position_id": EQUITY, "entity": None if who == GROUP else who,
              "currency": target_currency(day, who), "category": EQUITY, "side": "equity",
              "balance_mn": result.equity, "role": "asf", "amount": result.equity_asf}  # fmt: skip
    table = pd.concat([table, pd.DataFrame([equity])], ignore_index=True)
    return table[table["amount"] != 0].reset_index(drop=True)


def withdrawal_amount(day: Day, share: float) -> float:
    """The retail deposits (reporting currency, mn) a withdrawal of `share` takes from the group."""
    group = _converted_group(day, GROUP)
    return share * float(
        group.loc[group["category"].isin(day.whatif.retail_deposits), "balance_mn"].sum()
    )
