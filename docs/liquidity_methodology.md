# Liquidity Metrics Methodology (Phase 4)

> **All positions are synthetic.** The methods follow the Basel III standards and the UK PRA
> rules. The results describe a made-up bank, and every simplification is listed below.

This document explains how the platform calculates the **Liquidity Coverage Ratio (LCR)**, the
**Net Stable Funding Ratio (NSFR)** and the **cash-flow maturity ladder**, under two rulebooks.
It also lists every assumption and how each result was checked.

## Commands

```
python -m riskplatform.quality.run         # publishes data/validated/ (Phase 3)
python -m riskplatform.metrics.run         # LCR, NSFR and ladder, Basel and UK PRA
python -m riskplatform.metrics.run --regime pra
python -m riskplatform.metrics.run --config config/generator_real.yaml   # real-rates mode
```

The metrics run reads `data/validated/` **only** if a quality run published it (its `_run.json`)
and the rates source matches the config. Results go to `data/metrics/<source run id>/`:

| File | Contents |
|---|---|
| `lcr.parquet` | One row per date, level, entity, currency and regime: HQLA by level after caps, HQLA before caps, outflows, inflows before and after the cap, net outflows, LCR, minimum, `meets_minimum` |
| `nsfr.parquet` | The same keys: ASF, RSF, implied equity, NSFR, minimum, `meets_minimum` |
| `ladder.parquet` | The same keys plus bucket: inflows, outflows, net, cumulative net, contingent |
| `summary.json` | Source quality run, regimes, row counts, and every **breach** (entity or group below a minimum) |

The command exits with 1 if there is any breach, so a scheduler or alert can react.

## Rulebooks

| Regime | File | Source |
|---|---|---|
| Basel III | `config/liquidity/basel.yaml` | BCBS Basel Framework, chapters LCR20/30/40 and NSF20/30 |
| UK PRA | `config/liquidity/pra.yaml` | PRA Rulebook: Liquidity Coverage Ratio (CRR) Part, and Liquidity (CRR) Part, Articles 428a-428ah |

**Every factor sits in config with its paragraph or Article reference.** None is in the code.
The factors were checked against the official texts on 2026-11-12. Thirty key values are pinned
in `tests/test_liquidity_rules.py`, so an accidental edit fails a test.

## Levels of results

| Level | What is added together | Currency | Minimum applies? |
|---|---|---|---|
| `entity_currency` | One entity, one currency | That currency | No. Basel and the PRA monitor the LCR by significant currency but set no minimum per currency |
| `entity` | All of an entity's currencies | The entity's home currency | Yes |
| `group` | All entities | GBP (reporting currency) | Yes |

**Converting:** amount x (GBP per unit of its currency) / (GBP per unit of the target currency).
The rates come from the FX table, which is synthetic, or Bank of England rates in real mode.
Caps and the inflow cap are applied to the **combined** amounts, not per currency and then
summed.

## LCR

LCR = stock of HQLA / net cash outflows over 30 calendar days. It must be at least 100% (Basel
LCR20.5; UK LCR Art 4).

### Stock of HQLA

1. **Haircuts.** Each HQLA position counts as balance x (1 - haircut), grouped by level.

   | Category | Basel | UK PRA |
   |---|---|---|
   | Central bank reserves | Level 1, 0% | Level 1, 0% |
   | Government bonds | Level 1, 0% | Level 1, 0% |
   | Covered bonds | **Level 2A, 15%** | **Level 1 covered bond, 7%** |
   | Corporate bonds | Level 2B, 50% | Level 2B, 50% |

2. **Caps,** applied layer by layer, keeping the highest-quality assets first:
   - UK only: Level 1 covered bonds at most 70% of the buffer. At least 30% must be other
     Level 1 (Art 17(1)(b)).
   - Level 2 at most 40% of HQLA (Basel LCR30.31; UK Art 17(1)(a)).
   - Level 2B at most 15% of HQLA (Basel LCR30.33; UK Art 17(1)(c)).

   Each layer can add at most cap / (1 - cap) x (the layers above it). This gives the largest
   stock that meets every cap. For Basel it equals the adjustment formula of LCR30.35-30.39,
   which `tests/test_lcr.py` checks on 5,000 random portfolios. For the UK it implements the
   composition requirements of Article 17. The Annex I formula itself was not transcribed.

### Outflows and inflows

Outflow = balance x run-off rate. Inflow = balance x inflow rate. Where a rule covers only
amounts due within the horizon (`within_horizon_only`), a position counts only if
`residual_days <= 30`.

| Category | Rate | Counted |
|---|---|---|
| Retail stable deposits | 5% | always (repayable on demand) |
| Retail less stable deposits | 10% | always |
| Wholesale operational deposits | 25% | due within 30 days |
| Wholesale non-operational (non-financial corporates) | 40% | due within 30 days |
| Financial institution deposits | 100% | due within 30 days |
| Own debt (`term_debt`) | 100% | due within 30 days |
| Undrawn committed facilities | 10% of the undrawn amount | always |
| Interest rate swaps (notional) | 0%: notional is not a cash flow (LCR40.49; UK Art 30(4), 32(5)) | n/a |
| Corporate loans (inflow) | 50% | due within 30 days |
| Retail mortgages (inflow) | 50% | due within 30 days |

- HQLA maturing within 30 days are **not** inflows (Basel LCR40.88; UK Art 32(6)), to avoid
  counting them twice.
- **Inflow cap:** inflows can offset at most 75% of outflows (LCR40.77; UK Art 33(1)).
  Net outflows = outflows - min(inflows, 75% x outflows).

## NSFR

NSFR = available stable funding (ASF) / required stable funding (RSF), over one year. It must
be at least 100% (Basel NSF20.2; UK Art 428b).

- **ASF** = each liability x its ASF factor, plus capital. Capital is the implied equity
  (on-balance-sheet assets minus liabilities) at 100%.
- **RSF** = each asset x its RSF factor, plus undrawn commitments x 5%.
- Factors depend on **residual maturity bands**: below 183 days (6 months), below 365 days,
  and one year or more.

| Item | ASF / RSF factor |
|---|---|
| Equity; any funding of one year or more | ASF 100% |
| Retail stable / less stable deposits (under one year) | ASF 95% / 90% |
| Operational and non-financial corporate funding (under one year) | ASF 50% |
| Financial institution deposits and own debt | ASF 0% under 6 months, 50% from 6 to 12 months |
| Central bank reserves | RSF 0% |
| Government bonds (Level 1) | RSF **5% Basel / 0% UK** |
| Covered bonds | RSF **15% Basel (Level 2A) / 7% UK (Level 1 covered bonds)** |
| Corporate bonds (Level 2B) | RSF 50% |
| Loans and mortgages under one year | RSF 50% |
| Corporate loans of one year or more (risk weight above 35%) | RSF 85% |
| Mortgages of one year or more (risk weight 35% or below) | RSF 65% |
| Undrawn committed facilities | RSF 5% |
| Interest rate swaps (notional) | RSF 0%: charged on replacement cost, zero at market (NSF30.8, 30.23, 30.32; UK Art 428ah(2), 428k(4), 428s(2)) |

## Cash-flow maturity ladder

A **contractual** view, the same in both regimes. Each asset's principal flows in, and each
liability's flows out, in the bucket where it matures. Buckets are set in
`config/liquidity/ladder.yaml` (overnight to over 5 years). Undrawn commitments are shown
separately as **contingent**. Every position falls in exactly one bucket, so the final
cumulative gap equals the equity. The tests check this on every entity and currency.

## Main differences between Basel and the UK PRA (for this bank)

| Item | Basel | UK PRA | Effect |
|---|---|---|---|
| Covered bonds in the LCR | Level 2A, 15% haircut, within the 40% Level 2 cap | Level 1, 7% haircut, within a 70% cap | UK LCR higher |
| Government bonds in the NSFR | RSF 5% | RSF 0% | UK NSFR higher |
| Covered bonds in the NSFR | RSF 15% | RSF 7% | UK NSFR higher |
| Own debt due within 30 days | Rate depends on the holder (taken as 100%, see assumptions) | Flat 100% (Art 28(6)) | Same number, different basis |

## Assumptions and simplifications

Our data does not include ratings, deposit insurance, counterparty types or encumbrance, so
these treatments rest on assumptions. Each one is also stated next to its factor in the
rulebook files.

**Data attributes assumed**

1. Central bank reserves can be drawn down in stress.
2. Government bonds are 0% risk-weight sovereigns (UK credit quality step 1 for non-UK).
3. Covered bonds are rated AA- or higher, not self-issued, and (UK) credit quality step 1 with an
   issue size of at least GBP 440m.
4. Corporate bonds are rated A+ to BBB- (UK credit quality step 2-3, issue size at least
   GBP 220m).
5. Retail stable deposits are fully insured with an established relationship. The Basel 3%
   option is not used, and no UK higher-outflow category applies.
6. The whole wholesale operational balance qualifies as operational. Non-operational wholesale
   depositors are non-financial corporates. None of these deposits are insured.
7. Wholesale funding beyond 30 days cannot be withdrawn early.
8. **Own debt holders are unknown,** so they are treated as financial institutions, the most
   conservative choice. Neither text has an NSFR rule for unknown holders, so the catch-all
   applies.
9. Undrawn lines are committed credit facilities to non-financial corporates.
10. Mortgages qualify for a 35% or lower risk weight; corporate loans do not.

**Method simplifications**

11. Book value is used as market value. All assets are unencumbered and meet the HQLA
    operational requirements.
12. There is no short-term secured funding, so Basel's "adjusted" amounts equal the unadjusted
    ones.
13. Principal only: interest cash flows are not in inflows, outflows or the ladder.
14. A negative implied equity counts as zero ASF. It is still reported.
15. 6 months is taken as 183 days.
16. **Group results assume liquidity and funding move freely between entities.** Surplus
    liquidity that cannot be transferred out of a subsidiary may not count at group level, so
    entity results are the stricter view.
17. The UK composition caps are implemented from Article 17. The Annex I formula was not
    transcribed.
17a. **Interest rate swaps** are at market with no collateral. Their notional is not an LCR cash
    flow, and their net settlement flows within 30 days, the collateral look-back (LCR40.56 / UK
    Chapter 4) and initial margin (85% RSF) are not modelled. The ladder leaves notionals out.

**Scope of the rule review**

18. UK rule versions taking effect after 1 January 2027 were not reviewed.
19. One UK reference (mortgages under one year, Art 428ad(c)) was not verified point by point.
    The 50% is the standard treatment for loans under one year.

## How the results were verified

| Check | Where |
|---|---|
| Every factor checked against the Basel Framework and the PRA Rulebook, with references in config; 30 key values pinned | `config/liquidity/*.yaml`, `tests/test_liquidity_rules.py` |
| A portfolio worked by hand **before** any code (rechecked with plain arithmetic); the code reproduces every figure for both regimes | `docs/liquidity_worked_example.md`, `tests/test_liquidity_worked_example.py` |
| Layered caps equal the Basel adjustment formula on 5,000 random portfolios; caps always hold on 5,000 random UK portfolios | `tests/test_lcr.py` |
| Band boundaries (182/183 and 364/365 days), the 30-day boundary, edge cases | `tests/test_lcr.py`, `tests/test_nsfr.py` |
| Ladder cumulative gap equals equity for every entity and currency | `tests/test_ladder.py` |
| Group equals the sum of per-currency results x FX on every date; a ratio does not depend on the currency unit | `tests/test_metrics_run.py` |
| End to end: generate, then the quality run, then metrics | `tests/test_metrics_run.py` |

## Latest results (synthetic data, illustrative)

On the generated bank's last date (2026-10-23, synthetic rates): group LCR is 186.6% (Basel)
and 188.0% (UK), and group NSFR is 144.2% (Basel) and 146.4% (UK). There are no breaches.
Foreign-currency books hold less central bank money and fewer government bonds than home books,
so their per-currency LCRs range from about 50% to 150%. That is monitoring only, with no minimum,
and normal for foreign books supported by the home book.
These numbers describe a synthetic bank and say nothing about any real institution.
