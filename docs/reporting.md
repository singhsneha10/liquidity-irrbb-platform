# Limits, Alerts and Reporting (Phase 8)

> **All positions are synthetic.** The limits are the made-up bank's own risk appetite, not
> regulatory parameters, and every simplification is listed below.

This document covers what happens after the numbers are calculated:
- the bank's **internal limits** and how much of each is used;
- the **limit alert email**;
- **what changed since the day before**;
- the **Streamlit dashboard**, the **Tableau extracts** and the **daily PDF report**.

Every output reads the same results, so they always agree. Nothing in this phase recalculates a
regulatory figure.

```
metrics/<run>/  irrbb/<run>/                     (Phases 4 and 5)
        -> limits      data/limits/<run>/        utilisation and status of each limit
        -> changes     data/changes/<run>/       latest business day against the one before
        -> extracts    data/extracts/<run>/      Tableau-ready CSVs (+ data/extracts/latest/)
        -> report      data/reports/<run>/       daily_report.pdf
        -> (s3) publish, latest.json last -> one limit alert email if a limit is at warning or breach
query layer (DuckDB views over the latest good run) -> dashboard, extracts, report
```

## Commands

```
riskplatform-run                       # the whole pipeline, all of the above (make run)
python -m riskplatform.limits          # limits for the latest run (exit 1 on alerts)
riskplatform-dashboard                 # Streamlit dashboard (needs: pip install -e ".[dashboard]")
riskplatform-extracts                  # Tableau CSVs of the latest run
riskplatform-report                    # daily PDF of the latest run
python -m riskplatform.query "SELECT * FROM limit_changes WHERE movement <> 'unchanged'"
```

The commands read the latest good run: `data/validated/_run.json` locally, `latest.json` in S3
(`--source s3`, or `RISKPLATFORM_STORAGE_BACKEND=s3`). The pipeline writes all of them itself,
for its own run.

## 1. Internal limits (`config/limits.yaml`, `riskplatform/limits.py`)

| Limit | Kind | Value | Why |
|---|---|---|---|
| LCR | floor | >= 110% | 10 points of management buffer above the 100% minimum |
| NSFR | floor | >= 105% | 5 points of buffer above the 100% minimum |
| EVE loss / Tier 1 | cap | <= 15% | Equal to the supervisory outlier test (Basel SRP31.82; UK ICAA 9.4A, SS31/15 2.7B) |

- **Utilisation** is the share of a limit used: limit / actual for a floor, actual / limit for a
  cap.
- **Status:** warning from 90% used, breach from 100% (both inclusive).
- **Levels:** entity and group, under both rulebooks against the same limits. Per-currency
  results are monitoring only, as for the regulatory minimums.

Edge cases, each tested:
- A floor with nothing owed (0/0, or an infinite ratio) uses 0%.
- A ratio of 0 or below with something owed is infinitely used, so a breach.
- An EVE loss with no positive Tier 1 is a breach.
- Any regulatory shortfall (below 100%) is always also a limit breach.
- The EVE limit and the outlier test agree except at exactly 15.0%: the limit is breached when
  reached (>=), but the test needs a loss above it (>).

Outputs:
- `limits.parquet`: every date.
- `summary.json`: the latest date's status counts, and the alerts (warnings and breaches, breaches
  first).

Any alert makes the pipeline exit with 2 ("needs attention"), including a warning on its own.

## 2. Limit alert email (`riskplatform/notify.py`)

After publishing, if the run has alerts, **one** email goes to the SNS topic that already
carries the failure alerts. Its subject is `riskplatform limits <date>: N breaches, M warnings`.
The body has:
- one line per limit, breaches first;
- what changed since the day before (section 3);
- the thresholds;
- where the details are.

- **The topic** comes only from `RISKPLATFORM_ALERT_TOPIC_ARN`, which Terraform sets on the AWS
  task. Locally, in tests and in CI it isn't set, so nothing is sent and the alerts stay in the
  log.
- **Permissions:** the task role may publish to that one topic only (`tests/test_infra.py`
  checks). No credentials are in code.
- **Failures:** a failure to send makes the run exit 1, so the failure alert reports it. A limit
  alert is never lost silently.
- **Repeats:** the email repeats every run while a limit stays at warning or breach (labelled
  choice). The "since the day before" section shows whether anything moved.

## 3. Since the day before (`riskplatform/changes.py`)

"Yesterday" is the previous business day in the same run's series. Each daily run regenerates
the series identically and adds its own day, so this equals the previous run's latest day,
without reading another run.

| Output | What |
|---|---|
| `limit_changes` | Each limit's status and utilisation on both days, and its movement |
| `metric_changes` | The LCR, NSFR and EVE / Tier 1 behind each limit on both days, and the change |
| `balance_drivers` | On-balance-sheet balances per entity, category and side in GBP, split into volume and FX |
| `summary.json` | Both dates, movement counts, every movement except "unchanged", the top drivers |

**Movements:**

| Movement | Status change |
|---|---|
| new | ok → warning or breach |
| worse | warning → breach |
| resolved | warning or breach → ok |
| better | breach → warning |
| unchanged | no change |

A limit with no result the day before counts as previously ok.

**Volume and FX** add up exactly to the change in GBP. That's true by construction, and the
tests confirm it on real runs, where the gap is floating-point noise of about 1e-12 GBP mn.
- volume = (balance today − balance yesterday, in the position's currency) × FX yesterday
- FX = balance today × (FX today − FX yesterday)

Swap notionals are off balance sheet and are not balances, so they are left out. How many
drivers the summary and email list is set in `config/reporting.yaml` (`top_drivers`, 10).

## 4. Dashboard (`riskplatform/dashboard/`)

A Streamlit page over the latest good run.
- **Header tiles:** limits in breach and at warning, plus the group's LCR, NSFR and EVE / Tier 1
  with the change in bp.
- **Tabs:**
  - *Limits:* utilisation per entity and group, against the 90% and 100% lines.
  - *Liquidity:* LCR and NSFR trends against the minimum and the internal limit.
  - *IRRBB:* EVE / Tier 1 against the 15% test, and EVE loss and ΔNII by scenario.
  - *Since the day before.*
  - *Data quality:* rows, quarantine, file checks.
- **Sidebar:** switches between Basel and UK PRA.

- **One source of numbers:** `dashboard/data.py` holds one SQL query per chart over the query
  views. It doesn't import Streamlit and is tested against the run's own files. The PDF report
  uses the same functions.
- **Colours:**
  - entities keep a fixed colour, by name order;
  - the group is a neutral dark line;
  - status uses reserved colours, always with the status word next to it.
- **Testing:** Streamlit's `AppTest` runs the page headless in the tests. The layout was also
  checked by screenshot.
- **Location:** `RISKPLATFORM_DATA_DIR` sets the data folder (default `data/`).

## 5. Tableau extracts (`riskplatform/extracts.py`)

Each run writes `data/extracts/<run_id>/`:
- one CSV per query view;
- `liquidity_ratios.csv`: LCR and NSFR stacked with the regulatory minimum and the internal
  limit, the shape a trend chart wants;
- `manifest.json`: each file's rows and columns.

The format:
- UTF-8, with a header row;
- dates as `YYYY-MM-DD`;
- ratios as fractions (format them as % in Tableau);
- a `who` column (the entity, or GROUP);
- an empty cell for a missing value. An infinite value is written empty too, because one "inf"
  would make Tableau read the whole column as text.

`data/extracts/latest/` is then replaced by a copy, so a workbook can point at one fixed folder.

## 6. Daily PDF report (`riskplatform/report.py`)

One A4 document per run, `data/reports/<run_id>/daily_report.pdf`, two pages for the synthetic
bank:
1. the headline for both rulebooks;
2. limits at warning or breach;
3. liquidity, with an LCR trend chart;
4. IRRBB, with the outlier test and NII under ±200bp;
5. since the day before;
6. data quality.

It's written with ReportLab, which draws the chart too (no matplotlib), in invariant mode: the
same run always gives the same bytes. The tests read the PDF's text back with pypdf and compare
it with the run's numbers.

## Assumptions and simplifications

1. The internal limits are judgement (a made-up risk appetite), not calibrated to a real bank.
2. There's no NII limit: it would need projected NII, which the platform doesn't calculate.
3. Limits apply to entities and the group, not per currency.
4. The limit email repeats every run while a limit stays at warning or breach. There is no
   "first breach only" mode.
5. "Yesterday" is the previous business day in the same run's series, not the previous run's
   stored output. Here they agree, because the series is regenerated identically.
6. Balance drivers cover on-balance-sheet balances only, split into volume and FX; there's no
   rate or maturity attribution.
7. The dashboard has one light theme, so one set of colours. Utilisation off the chart's scale
   is drawn at 200%.
8. `data/extracts/latest/` is replaced by a copy, not atomically (Windows can't swap folders in
   one step).
9. The PDF uses the standard Helvetica font, so the text sticks to plain characters.

## How it was verified

- Every output is compared with the run's own Parquet and JSON files in the tests:
  - `test_limits.py`
  - `test_notify.py`
  - `test_changes.py`
  - `test_dashboard.py`
  - `test_extracts.py`
  - `test_report.py`
- The email is tested against moto's fake SNS, read back through an SQS subscription.
- For each new module a bug was planted on purpose to check the tests catch it, then removed:
  - volume priced at today's FX;
  - EVE share divided by the threshold instead of Tier 1;
  - infinities left in the CSVs;
  - warnings dropped from the PDF.
- The dashboard and the PDF were rendered and looked at, not only tested.
