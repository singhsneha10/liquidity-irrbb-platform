# Interest Rate Risk in the Banking Book (IRRBB) Methodology (Phase 5)

> **All positions are synthetic.** The method follows the Basel standardised framework (SRP31)
> and the UK PRA rules (ICAA Part, chapter 9; SS31/15). Results describe a made-up bank, and
> every simplification is listed below.

The platform measures how the bank's **economic value of equity (EVE)** and **net interest
income (NII)** change under the prescribed interest rate shocks, under two rulebooks, and applies
the supervisory **outlier test**.

## Commands

```
python -m riskplatform.quality.run          # publishes data/validated/ (Phase 3)
python -m riskplatform.irrbb.run            # delta EVE, outlier test, delta NII, gap (both regimes)
python -m riskplatform.irrbb.run --regime pra
python -m riskplatform.irrbb.run --config config/generator_real.yaml   # real-rates mode
```

The command reads `data/validated/` only if a quality run published it. Results go to
`data/irrbb/<source run id>/`:

| File | Contents |
|---|---|
| `eve.parquet` | Per date, level, entity, currency, regime and scenario: delta EVE (per currency) and the aggregated loss |
| `risk.parquet` | Per date, level, entity and regime: EVE risk (worst loss), worst scenario, Tier 1, threshold, outlier |
| `nii.parquet` | Delta NII per NII scenario |
| `gap.parquet` | Repricing gap per bucket: assets, liabilities, gap, cumulative gap |
| `summary.json` | Source quality run, regimes, and every outlier result |

The command exits with 1 if any entity or the group is an outlier (a notification trigger).

## Rulebooks

| Regime | File | Source |
|---|---|---|
| Basel | `config/irrbb/basel.yaml` | Basel Framework SRP31 (in force 1 Jan 2026), SRP98 |
| UK PRA | `config/irrbb/pra.yaml` | PRA Rulebook ICAA Part chapter 9 (rule 9.11 table dated 01/07/2026); SS31/15 |

Every parameter sits in config with its reference and was checked against the official texts on
2026-11-12. Values are pinned in `tests/test_irrbb_rules.py`.

## Method

### 1. Repricing cash flows (`irrbb/repricing.py`)

Each position's balance is placed in one of the **19 time buckets** (SRP31.96 Table 3) at the
point where it next reprices, and sits at the bucket midpoint:

- fixed-rate positions at maturity, floating-rate positions at their next reset;
- **non-maturity deposits** are split into a **core** part, repricing at its core maturity, and
  a **non-core** part, repricing overnight (SRP31.111);
- amounts are signed (+ assets, - liabilities). Equity and off-balance-sheet commitments are
  excluded.

### 2. Shocks (`irrbb/shocks.py`)

The six scenarios are parallel up and down, steepener, flattener, and short up and down
(SRP31.90; ICAA 9.7). At tenor *t*, with decay *x* = 4 years:

- short shape = short x e^(-t/x); long shape = long x (1 - e^(-t/x))
- steepener = -0.65 x short + 0.9 x long; flattener = +0.8 x short - 0.6 x long

| Currency | Parallel | Short | Long |
|---|---:|---:|---:|
| GBP | 275 bp | 425 bp | 250 bp |
| EUR | 225 bp | 350 bp | 200 bp |
| USD | 200 bp | 300 bp | 225 bp |

These sizes are the same in SRP31.90 Table 2 and ICAA 9.11 Table 1 for these currencies. The
formulas reproduce Basel's own worked example (SRP31.92: +25.4 bp and -1.6 bp).

### 3. Delta EVE (`irrbb/eve.py`)

delta EVE = sum of cash flow x (e^(-shocked rate x t) - e^(-base rate x t)). The base rate at *t*
comes from that day's curve for the currency (straight-line interpolation, flat beyond the ends).
A negative value is a loss.

### 4. Aggregation and the outlier test

For each scenario, the currencies are combined, then the worst scenario is the **EVE risk**:

- **Basel:** losses only (SRP31.129(5)).
- **UK:** losses, with gains offsetting at 50% (SS31/15 2.9B(iv)).

The bank is an **outlier** if the EVE risk is above **15% of Tier 1** (Basel SRP31.82-31.83;
UK ICAA 9.4A, which requires notifying the PRA).

### 5. Delta NII (`irrbb/nii.py`)

Over 12 months with a constant balance sheet, a cash flow repricing at *t* < 1 year earns or pays
the rate change for the rest of the year:

delta NII = sum of cash flow x (shocked rate - base rate) x (1 - *t*)

The scenarios are parallel up and down. Neither rulebook names the NII scenarios, so this is an
assumption.

## Levels of results

| Level | Combined | Currency | Outlier test? |
|---|---|---|---|
| `entity_currency` | One entity, one currency | That currency | No (monitoring) |
| `entity` | The entity's currencies, by the regime's rule | Home currency | Yes, against the entity's equity |
| `group` | All entities | GBP | Yes, against the group's equity |

Delta EVE and delta NII are additive in the cash flows. So each currency's group figure is the
sum of the entities' figures in that currency, converted with FX and then combined.
`tests/test_irrbb_run.py` checks this on the generated bank.

## Main differences between Basel and the UK PRA

| Item | Basel | UK PRA |
|---|---|---|
| Floor on shocked rates | National discretion (at most 0%); **none applied** | **-100 bp at the short end, rising to 0% at 20 years** (SS31/15 2.9B(iii)); observed rates below it are kept |
| Combining currencies | Losses only | Gains offset at **50%** |
| Outlier test | 15% of Tier 1 | 15% of CET1 + AT1; breaching it means **notifying the PRA** |
| Non-maturity deposit caps | 90%/5y, 70%/4.5y, 50%/4y (SRP31.112) | The same (ICAA 9.36), **plus** a 5-year average per currency (SS31/15 2.9B(vi)) |

## Assumptions and simplifications

1. **Principal only.** Cash flows are principal at the repricing date. Coupons and commercial
   margins are excluded, which SRP31.100 allows.
2. **Continuous compounding,** DF = e^(-rate x t). The standardised formulas are images on
   bis.org, so their exact form was not checked in the text.
3. **Bucket midpoints:** each bucket's cash flows sit at its midpoint.
4. **Non-maturity deposits:** stable retail is taken as transactional (70% core, 4 years) and
   less stable as non-transactional (50% core, 3 years). These are judgement values inside the
   caps, with no deposit history. Each core part is one cash flow at its core maturity. Phase 9
   (model validation) challenges these.
5. **Off-balance-sheet commitments are excluded** (undrawn lines have no repricing cash flow).
6. **Tier 1 = implied equity** (assets minus liabilities) at each level.
7. **NII:** parallel up and down, 12 months, constant balance sheet. Neither text names the two
   NII scenarios.
8. **NII across currencies** is added after conversion; neither rulebook sets a rule.
9. **The UK floor rises linearly** (5 bp a year), as read from SS31/15. It is a supervisory
   expectation, not a rule.
10. **Group figures** assume value can be combined freely across entities.
11. **No behavioural options:** no prepayment of loans or mortgages and no early redemption of
    term deposits.
12. **No hedging:** the synthetic bank holds no interest rate swaps (see the results below).
13. **Scope of the rule review:** UK rule versions after 1 January 2027 were not reviewed, and the
    SS31/15 text read was not labelled with its version.

## How the results were verified

| Check | Where |
|---|---|
| Every parameter checked against SRP31 and the PRA texts, with references in config; values pinned | `config/irrbb/*.yaml`, `tests/test_irrbb_rules.py` |
| Shock formulas reproduce Basel's own example (SRP31.92) | `tests/test_irrbb_worked_example.py` |
| A book worked by hand before any code (rechecked with plain arithmetic); the code reproduces every delta EVE, the worst loss, the outlier result and delta NII, for both regimes | `docs/irrbb_worked_example.md`, `tests/test_irrbb_worked_example.py` |
| The same example through the whole command, at every level | `tests/test_irrbb_run.py` |
| UK floor shape and when it binds; mirror symmetry of up and down shocks | `tests/test_irrbb_shocks.py` |
| Bucket edges; the deposit split adds back to the balance; the gap ends at assets minus liabilities on the generated bank | `tests/test_irrbb_repricing.py` |
| Basel vs UK aggregation worked by hand; a matched book has no EVE risk | `tests/test_irrbb_eve.py`, `tests/test_irrbb_run.py` |
| Group rebuilt from per-currency results on the generated bank | `tests/test_irrbb_run.py` |

## Latest results (synthetic data, illustrative)

On the generated bank's last date (2026-10-23, synthetic rates), the worst scenario is the
**steepener**:

| | EVE risk | % of Tier 1 | Outlier |
|---|---:|---:|---|
| BANK_UK (GBP) | 489.0 | 30.5% | yes |
| BANK_EU (EUR) | 455.2 | 22.2% | yes |
| BANK_US (USD) | 184.3 | 10.7% | no |
| Group (GBP) | 1,014.2 | 22.0% | yes |

**Why the synthetic bank is an outlier:** it holds long fixed-rate mortgages (up to about 25
years) and long bonds, funded by short deposits, **with no interest rate hedging**. The
steepener is the worst case for that book: long rates rise, cutting the value of long fixed
assets, while short rates fall, raising the value of short liabilities. Real banks hedge much of
this with interest rate swaps and typically report delta EVE of roughly 5-15% of Tier 1. The
calculation is the one verified above. The result reflects the synthetic book, and adding
hedging to the generator is a planned realism step.

At these rate levels (2-4%), the Basel and UK results coincide. The UK floor binds only in the
down scenarios, which are not the worst case, and gains do not offset because every currency
loses under the steepener.
