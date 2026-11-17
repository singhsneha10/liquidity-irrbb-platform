# Model Validation: Retail Deposit Run-off Rates in the LCR (Phase 9)

> **All positions are synthetic.** The real data used (Federal Reserve H.8) is public and
> aggregated across the US banking system, not the deposits of any one bank.

| | |
|---|---|
| Model | The 30-day run-off rates for retail deposits in the LCR calculation (`metrics/lcr.py`, `config/liquidity/`) |
| Status of this document | **Sections 1-5 (scope, tests, data, criteria, limitations) were written on 2026-11-17, before any data was fetched or any result was calculated** (step 9.1). Results are added in later steps and must not change these sections; any change after a result is seen is recorded in section 9 with its reason. |
| Rulebooks | Basel III (LCR40) and UK PRA (LCR (CRR) Part); the rates are the same in both |

## 1. Scope

**What is validated.** The share of retail deposits assumed to leave a bank within 30 days of
stress:

| Deposit category | Run-off rate | Basel | UK PRA |
|---|---|---|---|
| Retail stable | 5% | LCR40.7 | LCR Art 24(1) |
| Retail less stable | 10% | LCR40.13 | LCR Art 25(1) |

These rates are regulatory parameters, not the bank's own estimates. So the question is not
"are they right?" but the question a model risk team asks of any assumption the bank relies on:
**is there evidence that deposits leave faster than assumed, and how much would it matter for
this bank?**

**Why these rates.** Retail deposits are 56-63% of each entity's liabilities in the synthetic
bank (2026-10-23), the largest single source of LCR outflows. A wrong run-off rate moves the LCR
of every entity.

**Out of scope.** Wholesale, secured and contingent outflow rates; the NSFR factors; IRRBB
behavioural assumptions (the non-maturity deposit core share and maturity, already listed in
`docs/irrbb_methodology.md`); the HQLA haircuts.

## 2. Tests

| # | Test | Question | Data |
|---|---|---|---|
| T1 | **Backtest** | How often, and by how much, did observed 30-day deposit outflows exceed the assumed rates? | Fed H.8 (real) |
| T2 | **Detection check** | Does the backtest catch a run when one is there, and stay quiet when there is none? | Synthetic: the normal series, and a planted stress episode |
| T3 | **Sensitivity** | How does each entity's LCR fall as the run-off rates rise, and at what rate does it reach 110% (the internal limit) and 100% (the minimum)? | The synthetic bank's latest day |
| T4 | **Benchmark** | How do the assumed rates compare with a simple statistical alternative, the 99th percentile of observed 30-day outflows? | Fed H.8 (real) |

**How a 30-day outflow is measured.** For each observation date *t*, the outflow is the fall in
deposits over the following 30 days as a share of deposits at *t*:
`outflow(t) = max(0, 1 - D(t + 30 days) / D(t))`. A rise counts as no outflow. H.8 is weekly
(dated Wednesdays), so 30 days is measured as **4 weeks (28 days)**, the closest weekly step that
does not overshoot. As a conservative check, the same tests are repeated with **5 weeks (35
days)**, which can only show larger outflows. Ratings use the 4-week figures; the 5-week figures
are reported beside them.

**Named stress episodes** are reported on their own as well as within the full history:
- September to December 2008 (the failure of Lehman Brothers, 15 September 2008);
- March to May 2020 (the start of the COVID-19 pandemic);
- March to May 2023 (the failure of Silicon Valley Bank, 10 March 2023).

## 3. Data

**Real (T1, T4): Federal Reserve H.8, "Assets and Liabilities of Commercial Banks in the United
States"**, weekly, on FRED. Two series, both not seasonally adjusted (a stress shows up in the actual
balances), in billions of US dollars, weekly ending Wednesday, from 3 January 1973 (checked on
FRED on 2026-11-17, step 9.2):

| Series | FRED ID |
|---|---|
| Deposits, all commercial banks | `DPSACBW027NBOG` |
| Deposits, small domestically chartered commercial banks (all but the largest 25), which carried the March 2023 outflows | `DPSSCBW027NBOG` |

Each download is recorded with its date and a SHA-256 hash (`data/external/h8_deposits_provenance.json`).
The files are saved under `data/external/` and **not committed** (as for the other FRED data).

**Synthetic (T2, T3).**
- T2 uses the generator's normal series (deposits drift +0.02% a day with 0.4% daily
  volatility, `config/generator.yaml`).
- T2 also uses a **planted stress episode** in a separate `config/generator_stress.yaml`, off by
  default, so every existing result stays the same. Its size and timing are set in step 9.3 by
  judgement and stated there. It is a test instrument, not an estimate of a real run.
- T3 applies higher run-off rates to the latest validated day of the default synthetic bank,
  using the LCR code itself.

## 4. Pass / fail criteria

Fixed now, before any result. Each test gets **green**, **amber** or **red**.

**T1 Backtest** (each H.8 series, 4-week outflows, full history):
- **Green:** no observed outflow above **5%** (the stable rate, the lower and therefore stricter
  of the two).
- **Amber:** an outflow above 5% but none above **10%** (the less-stable rate).
- **Red:** any observed outflow above 10%.

**T2 Detection check.** The backtest of T1 is run, unchanged, on each synthetic entity's total
retail deposits (stable and less stable separately), measured on the same 4-week horizon. The
planted episode must take more than 5% out of the stressed entity's stable deposits within
30 days (step 9.3 sets the exact size).
- **Green:** both halves pass:
  - in the normal series, **no** outflow above 5% in any entity and category;
  - in the stressed series, the measured worst outflow of the stressed entity's stable deposits
    is **above 5%** and **within 1 percentage point** of the planted size.
- **Red:** either half fails. The backtest then cannot be trusted, and T1 is **inconclusive**,
  not green.

**T3 Sensitivity** (each entity and the group, both rulebooks). The worst observed 4-week outflow
from T1 (the larger of the two series) is applied to **both** retail categories, as the stress
case:
- **Green:** every LCR stays at or above **110%** (the internal limit, `config/limits.yaml`).
- **Amber:** an LCR falls below 110% but stays at or above **100%**.
- **Red:** any LCR below 100%.
- The break-even run-off rates at 110% and at 100% are reported for every entity, without a
  rating.

**T4 Benchmark:** report only, no rating. The 99th percentile of 4-week outflows for each series,
next to the 5% and 10% assumptions.

**Overall:**
- the **worst** of T1 and T3, provided T2 is green;
- **inconclusive** if T2 is red.

## 5. Known limitations (stated before the results)

1. **System totals, not one bank.** H.8 adds up deposits across many banks. A run at one bank
   (Silicon Valley Bank lost about a quarter of its deposits in a single day) is largely offset
   by deposits moving to other banks, so the system total hardly moves. A green T1 therefore
   means only that the rates are **not contradicted at system level**. It does not show that any
   one bank's deposits are safe. T2 and T3 exist because of this.
2. **All deposits, not retail only.** H.8 deposits include wholesale and corporate balances, and
   do not separate stable from less-stable retail deposits. The real outflows are compared with
   both rates for that reason, and neither comparison is exact.
3. **US data, UK and EU entities.** H.8 covers US banks; the synthetic bank has UK, EU and US
   entities. The comparison assumes deposit behaviour under stress is broadly similar.
4. **No stress in the history used for T3.** T3 applies observed outflows to a bank whose own
   history has none. It shows the effect of a run, not its likelihood.
5. **Weekly data.** Outflows within a week, and a 30-day window that does not fall on the weekly
   dates, are seen only approximately (the 4-week and 5-week figures bound it).
6. **Withdrawals versus the LCR's 30-day stress.** The LCR rate is a regulatory stress
   assumption, with central bank support absent. Observed outflows happened with deposit
   insurance, central bank facilities and, in 2023, extended guarantees. The real history may
   therefore show less outflow than an unsupported stress would.
7. **Breaks in the H.8 levels.** The Fed removes mergers and shifts between the large and small
   bank panels from past levels, but adjusts for nonbank structure activity of $5 billion or more
   (for example a charter conversion into or out of the commercial bank universe) and for
   accounting changes **only in its published percent changes, not in the levels** (H.8 "About"
   notes, federalreserve.gov). A jump in the weekly levels on FRED can therefore be a
   reclassification rather than a withdrawal. Every observed outflow above 5% is checked against
   the H.8 release notes before it is counted as an outflow; the check and its outcome are
   reported with the results.

## 6. Results

*To be added in step 9.4 (T1, T2), 9.5 (T3, T4).*

## 7. Conclusion and recommendations

*To be added in step 9.7.*

## 8. Reproducing this validation

*Commands added in step 9.6; every number above will come from `data/validation/`.*

## 9. Changes to sections 1-5

After results were seen: none so far.

Before any result (step 9.2, 2026-11-17, while checking the data source):
- Section 3: the FRED series IDs, units and start date, as checked on FRED.
- Section 5, limitation 7 (breaks in the H.8 levels), after reading the Fed's H.8 notes. It adds
  a check on any outflow above 5%; it does not change a criterion.

Before any result (step 9.3, 2026-11-17, while building the stress episode):
- The planted run (`config/generator_stress.yaml`) hits BANK_UK's **sterling** deposits only:
  10% of retail stable and 20% of retail less-stable deposits over 15 business days from
  2026-09-14, paid out of central bank reserves and then government bonds (so the balance sheet
  still balances and the LCR falls, as in a real run). BANK_UK's euro and dollar books hold too
  few liquid assets to pay a run of that size. T2's "planted size" is therefore the share of
  BANK_UK's **total** stable deposits (all currencies) that the episode removes, computed from
  the configured episode in step 9.4; it is a little under 10%. No criterion changes.
