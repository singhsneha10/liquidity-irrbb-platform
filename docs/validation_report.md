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

Step 9.4, 2026-11-17. H.8 data as downloaded on 2026-11-17 (2,810 weeks per series, 1973-01-03
to 2026-11-04, SHA-256 `f68ce321...`); synthetic banks from `config/generator.yaml` and
`config/generator_stress.yaml` (60 business days to 2026-10-23). Run-off rates read from
`config/liquidity/` (both rulebooks: 5% and 10%). T3 and T4: step 9.5, same day.

### 6.1 T1 Backtest (Fed H.8): all commercial banks AMBER, small banks RED

| Series | 4-week windows | Worst 4-week outflow | Worst 5-week (check) | Weeks above 5% | Weeks above 10% | Rating |
|---|---:|---|---|---:|---:|---|
| All commercial banks | 2,806 | **6.43%** (from 1975-01-01) | 5.89% (from 2001-09-12) | 6 | 0 | **AMBER** |
| Small banks | 2,806 | **10.69%** (from 1985-03-27) | 10.82% (from 1985-03-20) | 4 | 2 | **RED** |

Worst 4-week outflow in the named stress episodes (windows starting in the episode):

| Episode | All commercial banks | Small banks |
|---|---:|---:|
| 2008 Lehman Brothers (Sep-Dec 2008) | 2.41% | 0.21% |
| 2020 COVID-19 (Mar-May 2020) | 0.00% | 0.00% |
| 2023 Silicon Valley Bank (Mar-May 2023) | 1.79% | 3.87% |

In none of the three named crises did system deposits come near the assumed rates; deposits
rose in 2020. This is limitation 1 at work: in 2023, money left small banks but much of it
moved to other banks (the all-bank total fell less than the small-bank total).

### 6.2 Limitation 7 check: every spell above 5%

Every run of consecutive weeks above 5% was examined against the levels of both series and the
Fed's H.8 "Notes on Data" (federalreserve.gov/releases/h8/h8notes.htm, read 2026-11-17).

| Series | Window starts | Worst | What the levels show | Fed notes |
|---|---|---:|---|---|
| All banks | 1975-01-01 | 6.43% | Year-end spike: +3.9% in the week to 1 Jan, then back | Not covered (notes start October 1989) |
| All banks | 1975-12-31 | 5.35% | Year-end spike: +3.3%, then back | Not covered |
| All banks | 1981-09-30 | 6.13% | Quarter-end spike: +3.4%, then back | Not covered |
| All banks | 1986-01-01 | 5.35% | Year-end spike: +3.5%, then back | Not covered |
| All banks | 1986-12-31 | 5.83% | Year-end spike: +4.0%, then back | Not covered |
| All banks | 2001-09-12 | 5.28% | Day after 11 September 2001: +5.8% in a week (payments held up), then back | None in September 2001. In the window's last week (to 10 October 2001) banks acquired nonbank institutions with $13.2bn of deposits, which raised the end level: without it the outflow would be 5.60% (still amber) |
| Small banks | 1985-03-06 to 1985-03-27 (4 windows) | 10.69% | One week, 27 March to 3 April 1985: small banks -$30.1bn (-9.6%) while large and foreign-related banks +$69.5bn and the all-bank total +$39.5bn | Not covered |

**Outcome.**
- *All banks:* each spell starts on a one-week spike (four year-ends, a quarter-end, and
  11 September 2001) and measures the spike unwinding. These are real balance movements, not
  breaks, so **they count** under section 4, and the series stays AMBER. No sustained
  withdrawal is visible.
- *Small banks:* the March 1985 fall has the signature of a reclassification between the bank
  panels. Money leaving the banking system does not reappear at other banks in the same week,
  and here the all-bank total rose. But the Fed's notes, which document structure events only
  from October 1989, **cannot confirm a break**. The section 5 rule counts an outflow unless
  the check shows otherwise, so the 1985 outflow **counts**, and the series stays **RED**.
  (A secondary source, not confirmed on the Fed's site, states that H.8 reported large and
  small banks separately only from 1988; if so, earlier small-bank levels are reconstructions.)

### 6.3 Supplementary figures (not ratings)

Shown so the reader can see what the RED rests on. They do not replace the ratings above.

| Series | Worst 4-week outflow without the 4 windows crossing 27 March to 3 April 1985 | Worst since October 1989 (period covered by the Fed's notes) |
|---|---|---|
| All commercial banks | 6.43% (1975-01-01), 6 weeks above 5% | 5.28% (2001-09-12), 1 week above 5% |
| Small banks | 4.98% (1986-12-31), none above 5% | 3.87% (2023-03-01), none above 5% |

The small-bank RED rests entirely on one week in 1985.

### 6.4 T2 Detection check: GREEN

| Half | Measured | Criterion | Pass |
|---|---|---|---|
| Normal bank, every entity and retail category | Worst 20-business-day outflow 0.24% (BANK_EU stable) | No outflow above 5% | yes |
| Stressed bank, BANK_UK stable deposits | Planted 9.39% of BANK_UK's stable deposits (10% of the sterling book); measured 9.30% (from 2026-09-08); 25-day check 9.29% | Above 5% and within 1 point of planted (distance 0.09 points) | yes |

The backtest stays quiet when there is no run and finds the planted run to within 0.1
percentage point. **T1 is therefore conclusive.**

### 6.5 T3 Sensitivity: GREEN

On the latest validated day of the default synthetic bank (2026-10-23; `riskplatform-validate`
generates it and runs the quality layer on it afresh, so the same configs give the same numbers),
with both retail run-off rates replaced by the worst T1 outflow,
**10.69%**, and the LCR recalculated by the platform's own code (with the rulebooks' own rates
it reproduces the published LCR exactly, `tests/test_sensitivity.py`).

| Rulebook | Who | LCR now | LCR at 10.69% | Break-even run-off at 110% | at 100% |
|---|---|---:|---:|---:|---:|
| Basel | BANK_EU | 171.6% | 147.4% | 19.2% | 22.5% |
| Basel | BANK_UK | 167.0% | **146.0%** (lowest) | 20.0% | 23.8% |
| Basel | BANK_US | 237.1% | 198.2% | 29.1% | 33.3% |
| Basel | Group | 186.6% | 160.2% | 22.4% | 26.1% |
| UK PRA | BANK_EU | 173.0% | 148.6% | 19.5% | 22.8% |
| UK PRA | BANK_UK | 168.2% | 147.1% | 20.3% | 24.1% |
| UK PRA | BANK_US | 238.7% | 199.6% | 29.4% | 33.6% |
| UK PRA | Group | 188.0% | 161.4% | 22.7% | 26.4% |

Every LCR stays above 110% (lowest 146.0%): **GREEN**. A break-even rate is one run-off rate
for both retail categories at which the LCR reaches the line.

LCR as the run-off rate rises (both categories, Basel; UK PRA is at most 1.8 points higher):

| Run-off | 5% | 10% | 15% | 20% | 25% | 30% |
|---|---:|---:|---:|---:|---:|---:|
| BANK_EU | 190.9% | 151.6% | 125.7% | 107.4% | 93.7% | 83.1% |
| BANK_UK | 182.6% | 149.6% | 126.8% | 109.97% | 97.1% | 86.9% |
| BANK_US | 263.5% | 204.4% | 167.0% | 141.1% | 122.2% | 107.8% |
| Group | 206.0% | 164.7% | 137.2% | 117.6% | 102.8% | 91.4% |

**What T3 does not show (limitation 1).** The stress rate comes from system totals. A run on
one bank can be far larger: Silicon Valley Bank lost about a quarter of its deposits in a single
day. At a 25% run-off, BANK_EU and BANK_UK fall below the 100% minimum (93.7% and 97.1%, Basel)
and the group is at 102.8%. The bank withstands everything the system-wide data shows, but not
a run of that kind.

### 6.6 T4 Benchmark (report only)

The 99th percentile of all 4-week outflows, 1973-2026 (2,806 windows each; a rise counts as 0):

| Series | 99th percentile | Assumed rates |
|---|---:|---|
| All commercial banks | 2.92% | 5% stable, 10% less stable |
| Small banks | 2.41% | 5% stable, 10% less stable |

A simple statistical alternative would set the run-off well below both regulatory rates: by
this measure the rates are conservative for system-wide deposits.

### 6.7 Ratings

| Test | Rating |
|---|---|
| T1 All commercial banks | AMBER |
| T1 Small banks | RED |
| T2 Detection check | GREEN (T1 conclusive) |
| T3 Sensitivity | GREEN |
| T4 Benchmark | report only (2.92% and 2.41%) |
| **Overall** (worst of T1 and T3, as T2 is green) | **RED** |

## 7. Conclusion and recommendations

*To be added in step 9.7.*

## 8. Reproducing this validation

```
python -m riskplatform.model_validation.h8     # once: saves the H.8 deposits (needs the internet)
riskplatform-validate                          # T1-T4: python -m riskplatform.model_validation.run
```

`riskplatform-validate` writes `data/validation/<UTC time>/`:
- `results.json`: every number in section 6, the ratings, the H.8 file's SHA-256 and the
  quality run id of the synthetic bank;
- `t1_spells.csv`: every run of weeks above 5%, for the section 6.2 check;
- `t3_sensitivity.csv`: the section 6.5 tables at full precision.

Exit code 0 if every rating is green, 2 if any is amber, red or inconclusive (as now), 1 on an
error such as a missing or changed H.8 file. A newer H.8 download adds weeks and can change T1
and T4; the hash in `results.json` says which download a set of results used.

`tests/test_validation_run.py` checks that the headline numbers in this report equal what the
code computes. The T2 and T3 checks run everywhere, including CI. The T1 and T4 checks need the
H.8 file, which is not in Git, so they run where it has been downloaded and are skipped
elsewhere.

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
