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
   (Silicon Valley Bank lost over $40 billion of deposits in one day, 9 March 2023, according
   to the Federal Reserve's review) is largely offset
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

**Re-rated on 2026-11-17, after the results were seen** (section 9): the March 1985 small-bank
fall is a confirmed break in the series and is left out under limitation 7, and T3 no longer
lets a stress lower a rulebook rate. Every figure before those changes is kept below.

### 6.1 T1 Backtest (Fed H.8): all commercial banks AMBER, small banks GREEN

Windows spanning the confirmed March 1985 break (section 6.2) are left out; the last column
keeps the result before that exclusion.

| Series | 4-week windows | Worst 4-week outflow | Worst 5-week (check) | Weeks above 5% | Weeks above 10% | Rating | Before excluding the 1985 break |
|---|---:|---|---|---:|---:|---|---|
| All commercial banks | 2,806 | **6.43%** (from 1975-01-01) | 5.89% (from 2001-09-12) | 6 | 0 | **AMBER** | the same (no break) |
| Small banks | 2,802 | **4.98%** (from 1986-12-31) | 3.73% (from 2023-02-15) | 0 | 0 | **GREEN** | 10.69% (from 1985-03-27), 4 above 5%, 2 above 10%: RED |

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
- *Small banks:* the March 1985 fall **is a break in the series, not a withdrawal**. This was
  first counted, because the Fed's structure notes start only in October 1989. It was then
  confirmed from the Fed's own published data (recommendation R1, 2026-11-17, after the results
  were seen):
  - FRED's **large-bank** deposit series (`DPSLCBW027NBOG`) and small-bank total assets
    (`TLASCBW027NBOG`) both **begin on 3 April 1985**, the week of the fall: today's large/small
    split starts there.
  - The H.8 release of **5 April 1985** (FRASER, the St. Louis Fed's archive) split domestic banks
    by total assets **under and over $1.4 billion** (footnote 2: "Size classification is based on
    total assets in domestic offices as of December 31, 1982"), with **$902.7bn** of deposits at
    banks under $1.4bn on 6 March 1985. FRED's small-bank series shows **$316.4bn** that day, so
    its earlier levels are a later reconstruction on another basis. The all-bank totals agree
    ($1,633.6bn published, $1,639.5bn on FRED).
  - In the week of the fall, small banks -$30.1bn, other banks +$69.5bn, all banks +$39.5bn.

  Under limitation 7, the four windows spanning 27 March to 3 April 1985 are left out
  (`confirmed_breaks` in `config/validation.yaml`). The series is **GREEN**; it was **RED**
  before the exclusion.

### 6.3 Supplementary figures (not ratings)

The worst 4-week outflow in the period the Fed's structure notes cover (October 1989 onwards):

| Series | Worst since October 1989 |
|---|---|
| All commercial banks | 5.28% (2001-09-12, the 9/11 spike; 1 week above 5%) |
| Small banks | 3.87% (2023-03-01, Silicon Valley Bank; none above 5%) |

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
with both retail run-off rates raised to the worst T1 outflow, **6.43%**, but never
below their own rulebook rates (so the stable rate rises from 5% and the less-stable rate stays
at 10%). The LCR is recalculated by the platform's own code; with the rulebooks' own rates it
reproduces the published LCR exactly (`tests/test_sensitivity.py`).

| Rulebook | Who | LCR now | LCR at 6.43% | Break-even run-off at 110% | at 100% |
|---|---|---:|---:|---:|---:|
| Basel | BANK_EU | 171.6% | 165.4% | 19.2% | 22.5% |
| Basel | BANK_UK | 167.0% | **161.6%** (lowest) | 20.0% | 23.8% |
| Basel | BANK_US | 237.1% | 226.7% | 29.1% | 33.3% |
| Basel | Group | 186.6% | 179.7% | 22.4% | 26.1% |
| UK PRA | BANK_EU | 173.0% | 166.8% | 19.5% | 22.8% |
| UK PRA | BANK_UK | 168.2% | 162.9% | 20.3% | 24.1% |
| UK PRA | BANK_US | 238.7% | 228.2% | 29.4% | 33.6% |
| UK PRA | Group | 188.0% | 181.1% | 22.7% | 26.4% |

Every LCR stays above 110% (lowest 161.6%): **GREEN**. A break-even
rate is one run-off rate for both retail categories at which the LCR reaches the line.

Before the re-rating, T3 used 10.69% (the 1985 figure). It was GREEN then too: lowest LCR 146.0%
(BANK_UK, Basel). The rulebook floor did not matter there, because 10.69% is above both rates.

LCR as the run-off rate rises (both categories, each at least its rulebook rate; Basel; UK PRA is
at most 1.6 points higher):

| Run-off | 5% | 10% | 15% | 20% | 25% | 30% |
|---|---:|---:|---:|---:|---:|---:|
| BANK_EU | 171.6% | 151.6% | 125.7% | 107.4% | 93.7% | 83.1% |
| BANK_UK | 167.0% | 149.6% | 126.8% | 109.97% | 97.1% | 86.9% |
| BANK_US | 237.1% | 204.4% | 167.0% | 141.1% | 122.2% | 107.8% |
| Group | 186.6% | 164.7% | 137.2% | 117.5% | 102.8% | 91.4% |

**What T3 does not show (limitation 1).** The stress rate comes from system totals. A run on
one bank can be far larger: in March 2023, Silicon Valley Bank, Signature Bank and First Republic
each lost 20% or more of their deposits in a single day (section 6.8). At a 25% run-off,
BANK_EU and BANK_UK fall below the 100% minimum (93.7% and 97.1%, Basel)
and the group is at 102.8%. The bank withstands everything the system-wide data shows, but not
a run of that kind.

### 6.6 T4 Benchmark (report only)

The 99th percentile of all 4-week outflows, 1973-2026 (a rise counts as 0; the 4 windows
spanning the 1985 break are left out of the small-bank series; with them it was 2.41%):

| Series | 99th percentile | Assumed rates |
|---|---:|---|
| All commercial banks | 2.92% | 5% stable, 10% less stable |
| Small banks | 2.18% | 5% stable, 10% less stable |

A simple statistical alternative would set the run-off well below both regulatory rates: by
this measure the rates are conservative for system-wide deposits.

### 6.7 Ratings

| Test | Rating | Before the re-rating (section 9) |
|---|---|---|
| T1 All commercial banks | AMBER | AMBER |
| T1 Small banks | GREEN | RED |
| T2 Detection check | GREEN (T1 conclusive) | GREEN |
| T3 Sensitivity | GREEN | GREEN |
| T4 Benchmark | report only (2.92% and 2.18%) | report only (2.92% and 2.41%) |
| T5 Single-bank runs (section 6.8) | report only (added after the results) | not run |
| **Overall** (worst of T1 and T3, as T2 is green) | **AMBER** | RED |

### 6.8 T5 Single-bank runs (case study, report only)

**Added on 2026-11-17, after the results above** (recommendation R2; section 9). Not part of the
criteria fixed in section 4, so **it has no rating** and does not change the overall rating. It
answers the question limitation 1 leaves open: what would runs like those of March 2023 do to
this bank?

**Source.** FDIC staff study, "Dissecting Depositor Flight: An Analysis of the Spring 2023 Bank
Failures" (May 2026). It is built from the three banks' core deposit systems and gives the
authors' views, not an official FDIC position. Every figure below is copied from it, with the
sentence it comes from, in `config/validation.yaml` (`single_bank_runs`); a test checks each
figure against its sentence. Caveats from the study: part of the SVB and Signature outflows
happened after they failed, at the bridge banks, and First Republic's 7-17 March figure is net
of $30 billion deposited by 11 large banks on 16 March (54% without it).

Deposit outflow as a share of deposits on 6 March 2023, and this bank's LCR (latest validated
day) if its retail deposits ran off at that rate (each category at least its rulebook rate, as in
T3), across the 3 entities and the group under both rulebooks (8 results):

| Bank | Period | Outflow | Lowest LCR | Below 110% | Below 100% |
|---|---|---:|---:|---:|---:|
| Silicon Valley Bank | one day (9 March) | at least 20% | 107.4% | 3 of 8 | 0 of 8 |
| Silicon Valley Bank | 3 business days (9-14 March) | 50% | 57.2% | 8 of 8 | 8 of 8 |
| Silicon Valley Bank | 7-17 March | 60% | 49.5% | 8 of 8 | 8 of 8 |
| Signature Bank | 3 business days (10-14 March) | 50% | 57.2% | 8 of 8 | 8 of 8 |
| Signature Bank | 7-17 March | 58% | 50.9% | 8 of 8 | 8 of 8 |
| First Republic Bank | 3 business days (10-14 March) | 47% | 60.0% | 8 of 8 | 8 of 8 |
| First Republic Bank | 7-17 March | 36% | 73.2% | 8 of 8 | 8 of 8 |

**Finding.** Within days, each run was 4 to 12 times the LCR's 30-day stable rate (5%) and 2 to
6 times its less-stable rate (10%). At the
documented one-day loss (20% or more), three of the eight results fall below the 110% internal
limit and none below 100%. At any of the documented multi-day runs (36% to 60%), every entity and
the group, under both rulebooks, falls below the 100% minimum (lowest 49.5%). The
5% and 10% rates are a regulatory floor calibrated to system-wide stress; they say nothing about a
run concentrated on one bank with concentrated, largely uninsured deposits. The FDIC study finds
that roughly two-thirds or more of each bank's top depositors ran.

### 6.9 T6 Single-bank outflows across all US banks: PLAN (written before the data)

**Status: plan only.** Written on 2026-11-17, before any quarterly deposit figure of the
~4,500 banks was downloaded or examined (recommendation R2, part 2). Results will follow this
plan; any change after they are seen is logged in section 9. Already seen at the time of
writing: the quarterly deposits of Silicon Valley Bank, Signature Bank and First Republic
(section 6.8), and the counts and codes of the FDIC structure events below. Like T5, T6 is
**report only**: it was added after the main results and cannot change the overall rating.

**Question.** How often does a single US bank lose deposits faster than the LCR's run-off rates
assume, across all banks and all years, in normal times and in crises? T5 shows what three
famous runs were. T6 asks how common runs of that kind are.

**Data.** FDIC BankFind API (free, no key), quarterly Call Report figures for every
FDIC-insured institution (banks and savings institutions):
- total deposits (`DEP`) by certificate number (`CERT`) and quarter end (`REPDTE`), from the
  first quarter of 2001 to the latest available (about 710,000 bank-quarters);
- structure events (`/history`) with their change codes and effective dates.

Each file is saved locally with provenance and a SHA-256 hash, like the H.8 data, and is not
committed.

**Measure.**
- **Quarterly outflow** of bank *b* in quarter *t*: `q = max(0, 1 - DEP(b,t) / DEP(b,t-1))`, for
  consecutive quarters only.
- **Implied monthly outflow**, a lower bound on the worst month in that quarter:
  `m = 1 - (1 - q)^(1/3)`. If deposits fall by `q` over three months, at least one month fell by
  `m` or more. `m` is what is compared with the 30-day run-off rates of 5% and 10%. It
  understates runs that happen inside a quarter (Silicon Valley Bank's run never appears in its
  quarterly reports), so T6 shows how often outflows are at least this large.

**Exclusions, fixed now** (a bank-quarter is left out if any applies):
1. **A deposit-moving structure event** for the bank, with an effective date after the previous
   quarter end and on or before this one, on either side of the event:
   - failure, merger, absorption, consolidation, partial purchase and assumption, or closing
     (codes 211, 213, 215, 216, 217, 221, 222, 223, 224, 225, 230, 235, 240, 260);
   - the bank as acquirer (810, 811, 812, 820, 830);
   - branches bought, acquired or sold (712, 713, 722, 724).

   Other events (names, locations, regulators, branch openings and closings) do not move
   deposits and are kept.
2. **Previous-quarter deposits below $10 million.** Too small for a percentage to mean much.
3. **A gap**: the previous quarter is missing, so the change is not over one quarter.

**Results to report, all fixed now.** By size (previous-quarter deposits under $1bn, $1bn to
$10bn, over $10bn) and for all banks together:
- the number of bank-quarters;
- the median, 90th, 99th and 99.9th percentiles of `m`;
- the share of bank-quarters with `m` above 5% and above 10%;
- the same shares in the stress years 2008-2009, 2020 and 2023, and in all other years.

**The largest outflows, checked by hand.** The 20 largest `m` among banks over $1bn are listed
by name and checked against the FDIC history and public sources for anything the exclusions
missed. Examples: a branch sale recorded under another code, a bank moving deposits to an
affiliate, a data error. Each is reported as a run, as a missed structure event (and
excluded), or as unresolved (and kept), with the reason.

**Stated in advance about interpretation.**
- A share above 5% or 10% is a frequency of large outflows. It is not evidence that the rates
  are wrong for system-wide stress, which T1 tested.
- `DEP` is all deposits, not retail only, and includes brokered and wholesale deposits.
- Quarter-end figures can be window-dressed.
- The merger filter may miss transfers. The hand check of the largest outflows is there for
  this.

## 7. Conclusion and recommendations

### 7.1 Conclusion

**Overall rating: AMBER**, under the criteria fixed before the results (section 4), after
excluding one confirmed break in the data. It was RED before that exclusion (section 9).

1. **The original RED was a data break, now confirmed.** It rested entirely on four windows
   across one week, 27 March to 3 April 1985, in the small-bank series. The Fed's own data shows
   that today's large/small split begins that very week, and that the 1985 release measured
   small banks differently (section 6.2). With those windows left out, small banks are GREEN:
   worst 4.98%, and 3.87% since October 1989 (Silicon Valley Bank,
   March 2023).
2. **The amber is calendar effects, not withdrawals.** Every all-bank spell above 5% starts on a
   one-week spike and measures it unwinding: four year-ends, one quarter-end, and 11 September
   2001. None shows a sustained withdrawal. In the three named crises, system deposits fell by
   at most 2.4% in 4 weeks, and in 2020 they rose. A better measure would remove this amber
   (recommendation R5); it is not applied after the fact.
3. **Against system-wide history the rates are conservative.** The 99th percentile of 4-week
   outflows is 2.92% (all banks) and 2.18%
   (small banks), below both the 5% and 10% rates (T4). The backtest itself is reliable: it
   finds a planted run to within 0.1 point (T2).
4. **Against a single-bank run they are not, and that is the bank's real exposure.** At the
   worst system-wide outflow (6.43%), every LCR stays at or above
   161.6% (T3). But the LCR falls to the 110% internal limit at a
   19.2-29.4% run-off, and to the 100% minimum at 22.5-33.6%, depending on the entity. The
   documented runs of March 2023 were far larger: each failed bank lost 20% or more of its
   deposits in one day and 36% to 60% within days. Any of those multi-day runs would take every
   entity and the group below the 100% minimum (T5, section 6.8, lowest 49.5%). The
   system data cannot show this risk (limitation 1).

**In short:** the 5% and 10% rates are not contradicted by 54 years of US system-wide deposit
data, and the bank's LCR has ample room for the worst system-wide outflow. The evidence says
nothing reassuring about a run on one bank. That is where the recommendations focus.

### 7.2 Recommendations

| # | Recommendation | Why | Priority |
|---|---|---|---|
| R1 | **Resolve the 1985 data point.** *Done 2026-11-17:* confirmed as a break from the Fed's own data and the April 1985 H.8 release; excluded under limitation 7 and re-rated (sections 6.2 and 9). | The overall RED rested on it. | Done |
| R2 | **Add a single-bank view.** *Case study done 2026-11-17 (T5, section 6.8: the documented March 2023 runs).* Still to do: the distribution of quarterly deposit changes across all FDIC-insured banks, with mergers filtered out. | System totals hide single-bank runs (limitation 1); T5 shows such runs would breach the minimum. | High (half done) |
| R3 | *Done 2026-11-17 (`config/early_warning.yaml`, threshold 20%, docs/reporting.md section 7):* **Set a reverse stress trigger.** Report the break-even run-off rates (19-34%) monthly beside the LCR. Treat a fall in the lowest break-even below a set level (judgement, e.g. 20%) as an early warning for the contingency funding plan. | It turns T3 into ongoing monitoring of how close the bank is to its limits. | Medium |
| R4 | *Done 2026-11-17 (threshold 3%):* **Use the T4 percentile as an early-warning indicator.** Flag any 4-week retail outflow above about 3% (the system 99th percentile) at an entity. | It is rarer than 1 in 100 for the system, and far below the regulatory rates. | Medium |
| R5 | **Measure outflows from a robust base in a future version.** For example, use the average of the previous 4 weeks instead of a single week, or report window starts at year- and quarter-ends separately. Do not change this validation's criteria after the fact; apply it at the next validation. | The amber is calendar spikes unwinding (section 6.2); the current measure overstates outflows that start on a spike. | Low |
| R6 | **Re-run on new data.** Run `riskplatform-validate` after each H.8 refresh (weekly data), at least once a year. Treat a new spell above 5% as a trigger for review. | Ongoing monitoring (SS1/23 Principle 4.4; SR 26-2, section V). | Low |
| R7 | **Have it reviewed independently.** In a real bank, a validation function independent of the people who built the LCR calculation should re-perform this work. | Here the builder validated its own work (section 7.3). | Note |

### 7.3 Frameworks and the standing of this validation

This validation follows the elements of supervisory model risk guidance as **good practice**,
and does not claim to meet a requirement. Each source below was checked against its official
text on 2026-11-17.

**PRA SS1/23, "Model risk management principles for banks"** (Bank of England; April 2026
version, effective 23 April 2026, replacing the May 2023 version):
- **Scope (1.2):** it applies to UK firms with internal model approval for regulatory capital;
  other firms "may find the proposed principles useful". The synthetic bank has no such
  approval, so SS1/23 is followed here as good practice.
- **Principle 1.1(b):** material deterministic quantitative methods that are not models
  should still be considered for the model risk management framework. The run-off rates are
  prescribed parameters in a rule-based calculation, but they bear materially on the bank's
  liquidity position.
- **Principle 3.3(b)(i):** backward-looking tests "using actual observations across a variety
  of economic and market conditions". This is **T1** (54 years, three named crises).
- **Principle 3.3(b)(ii):** forward-looking scenarios, with "sensitivity analysis to determine
  the model operating boundaries". This is **T3** and its break-even rates.
- **Principles 3.3(b)(iii) and 4.4(c)(i):** comparison with challenger models and
  benchmarks. This is **T4**.
- **Principle 4.4(a):** performance assessed against thresholds for acceptable performance.
  Here those are the criteria in section 4, fixed before any result.
- **Principle 4 (independent validation):** **not met.** The work was done by the people who
  built the calculation (recommendation R7).

**Federal Reserve SR 26-2, "Revised Guidance on Model Risk Management"** (Fed, OCC and FDIC,
17 April 2026). It supersedes **SR 11-7** (2011), which is therefore not cited as current
guidance.
- **Section II:** its definition of a model **excludes** "deterministic rule-based processes",
  so the LCR with prescribed rates may fall outside its scope.
- **Section V:** its components of validation are followed here: conceptual soundness
  (section 1 and the limitations); outcomes analysis, "such as back-testing" (**T1**, with
  **T2** proving the back-test can find a run); and ongoing monitoring (R6).

**What this validation is not.** The run-off rates are set by the regulators (Basel LCR40,
the PRA's LCR (CRR) Part), and a bank must use them. Nothing here proposes changing them for
regulatory reporting. The findings bear on how much the bank should **rely** on the LCR
alone: its internal stress testing, its limits and its contingency funding plan.

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

After results were seen (2026-11-17), both recorded here with their reasons:
1. **Limitation 7 applied to the March 1985 small-bank fall** (recommendation R1). The section 5
   rule was always to check every outflow above 5% for a break before counting it. The Fed's
   structure notes (from 1989) could not settle this one, so it was first counted (overall RED).
   Documentary evidence found afterwards (section 6.2) confirms a break where today's large/small
   split begins (3 April 1985). The four windows spanning it are left out through
   `confirmed_breaks` in `config/validation.yaml`, which records the evidence. Small banks:
   RED -> GREEN. T3's stress rate (the worst T1 outflow): 10.69% -> 6.43%. T4 small
   banks: 2.41% -> 2.18%. Overall: RED -> **AMBER**. The author decided
   this, and the results keep the ratings from before the exclusion.
2. **T3 never lowers a rulebook rate.** Section 4 says the stress rate is applied to both retail
   categories. At 6.43%, below the 10% less-stable rate, that lowered the less-stable rate and
   gave "stressed" LCRs above today's, which is not a stress. Each category now takes the stress
   rate or its own rulebook rate, whichever is higher. This changes no rating: T3 was GREEN
   either way, and at 10.69% the floor did not apply.

Neither change touches a threshold in section 4.

3. **T5 added** (2026-11-17, after the results; recommendation R2): the LCR under documented
   single-bank runs (section 6.8). Report only, with no rating, so the overall rating is
   unchanged. The figures come from an FDIC staff study, each with its quoted sentence.
4. **Factual correction in limitation 1** (section 5): "lost about a quarter of its deposits in
   a single day" became the Federal Reserve's own figure, "over $40 billion ... on March 9" (its
   review of 28 April 2023). The quarter was an approximation not found in either source. The
   limitation's meaning is unchanged.
5. **T6 planned** (2026-11-17, before its data was downloaded; recommendation R2, part 2): the
   plan in section 6.9 fixes the data, the measure, the exclusions, the results to report and
   the hand check. It was committed before any result. Report only, like T5.

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
