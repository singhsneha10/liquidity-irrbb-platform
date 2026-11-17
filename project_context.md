# Project Context

**Project:** Automated Liquidity & Interest Rate Risk Monitoring Platform
**Goal:** A production-quality, end-to-end, deployed portfolio project that impresses senior
people at banks such as JPMorgan (Risk Analyst roles). Business use comes later.
**Last updated:** 2026-11-17 (work PC). **Phase 9 (model validation) is done** (9.1-9.8):
`docs/validation_report.md`, overall rating **AMBER** after R1 (was RED; see "Design
decisions", Phase 9). Phase 7 is
deployed to AWS (2026-11-16); only checklist F is left (back up the Terraform state, schedule on
when wanted; personal PC only). 661 tests. Next: the open items in "Next steps", then Phase 10.
9.1-9.7 were committed by the author step by step; this file and the README line (9.8) were
**not committed when this was written**.

## Working rules
1. Think of the project as a whole; build step by step with the big picture in mind.
2. Code must not break as the project grows (config over hardcoding, schemas, tests).
3. The author must understand and be able to explain every line. Keep a list of anything
   skipped (see "Walkthrough debt") and go through it before it goes on a resume.
4. Regulatory correctness over polish; every simplification is labelled. Regulatory factors are
   checked against the official text and carry their paragraph/Article reference in config.
5. Small steps: one change at a time, explained in plain words before it is made. Never change
   several files without saying so first.
6. Run `pytest`, `ruff check .` and `ruff format --check .` after every change; a step is not
   done until all pass.
7. Never commit secrets. `.env` is ignored and untracked; secrets live in environment variables.
8. Use the latest stable tool versions. Update this file at the end of the day. **The author
   commits and pushes; Claude never runs git commit or git push.**
9. **AWS (decided 2026-11-16):** Claude may run AWS commands itself, **only on the personal PC**
   (the only one with the Terraform state), but before anything that creates, changes or
   destroys resources or costs money (`terraform apply`/`destroy`, `docker push`, `ecs run-task`,
   schedule on) it shows the plan in plain words and waits for the author's "yes". Read-only
   commands need no approval. The author wants AWS cost kept near zero: say what anything costs
   (USD and roughly INR) before doing it.

## Big picture
```
market data (synthetic, or saved FRED / ECB / Bank of England files)
  -> generator -> data/raw/ (positions, rates, fx, control totals)
  -> quality run -> data/validated/ (+ quarantine, breaks report, audit folder per run)
  -> metrics run -> data/metrics/<run>/ (LCR, NSFR, ladder: Basel and UK PRA)
  -> IRRBB run -> data/irrbb/<run>/ (delta EVE, outlier test, delta NII, gap: Basel and UK PRA)
  -> limits -> data/limits/<run>/ (utilisation and ok/warning/breach of config/limits.yaml)
  -> changes -> data/changes/<run>/ (latest day vs the one before; volume / FX balance drivers)
  -> extracts -> data/extracts/<run>/ + latest/ (Tableau CSVs)
  -> report -> data/reports/<run>/daily_report.pdf
  -> (backend s3) S3 under the run id; latest.json last -> query layer (DuckDB SQL views)
  -> one limit alert email via SNS if a limit is at warning or breach (topic set only in AWS)
Query layer -> Streamlit dashboard (riskplatform-dashboard), extracts and PDF (same numbers).
In AWS: EventBridge Scheduler -> ECS Fargate task (Docker image from ECR) -> S3 + CloudWatch
Logs; a failed run (any exit code except 0 and 2) -> SNS email; a limit at warning or breach ->
the limit alert email (same topic). Each run uses --as-of today.
Locally: the same stages as Dagster assets (dagster dev).
```
The data contracts (`src/riskplatform/schemas.py`) are the seam: real data can replace the
synthetic data without changing calculation code. Calculations are verified separately from the
data: hand-worked examples written before the code, and proofs against published formulas.

## Target stack
- Python 3.11+ (venv uses 3.12), pandas, numpy, pydantic v2, pandera, pytest, ruff
- Storage and queries: Parquet, DuckDB (2.0), S3 with a DuckDB query layer (done)
- Data quality layer with quarantine (done)
- LCR, NSFR and IRRBB (NII and EVE, six Basel shocks), all done, as pure functions, with
  regulatory parameters in YAML; **Basel and UK PRA rulebooks side by side**
- Model validation module: backtest of the LCR retail deposit run-off rates (done, Phase 9)
- Internal limits with warning / breach and "what changed since yesterday" (done, Phase 8)
- Outputs: Tableau-ready CSV extracts and a daily PDF report (done, Phase 8); Excel export and
  optional AI commentary not done (all numbers from code, never from the model)
- Dashboard: **Streamlit** (done, local; public deploy in Phase 10) plus **Tableau-ready
  extracts** (done); FastAPI backend optional later (Alteryx optional for data prep)
- Docker; GitHub Actions CI (lint, tests, image build and run, Terraform validate) (done)
- AWS (S3, ECS Fargate, ECR, EventBridge Scheduler, CloudWatch, SNS) with Terraform, billing
  alarm first (code done; not yet applied)
- **Orchestration is required**: Dagster (done; locally and for demos)

## Design decisions so far
- Config files (all validated by `src/riskplatform/config.py`, unknown keys rejected):
  `config/generator.yaml` (synthetic bank, default, offline), `config/generator_real.yaml`
  (extends it via `base:`; real curves and FX from saved files), `config/market_rates.yaml`
  (fetch sources), `config/quality.yaml` (outlier thresholds, reconciliation tolerance, report
  rows), `config/liquidity/basel.yaml`, `pra.yaml` (rulebooks), `ladder.yaml` (buckets),
  `config/irrbb/basel.yaml`, `pra.yaml`. No limits in code: rate sanity bounds (`rate_bounds`,
  -5% to 25%) are in `generator.yaml` and `market_rates.yaml` (a test keeps them equal).
- One command (`riskplatform-run`, `python -m riskplatform.pipeline`, or `make run`) runs
  generate -> raw checks -> quality -> metrics -> IRRBB -> limits -> changes -> extracts ->
  report and stops at the first stage that stops (changes, extracts and report are always ok). Exit codes: 0 clean, 1 stopped or error, 2 ran but found a regulatory breach, an
  IRRBB outlier, or an internal limit at warning or breach on the latest date (the
  separate commands still exit 1 on findings; schedulers should call the pipeline).
- Logging: one setup (`riskplatform/logs.py`), UTC timestamps, level from the
  `RISKPLATFORM_LOG_LEVEL` environment variable; each pipeline run writes
  `data/logs/pipeline-<UTC time>.log` (tracebacks included).
- A liquidity minimum is tested on amounts (HQLA >= 100% x net outflows; ASF >= 100% x RSF), so
  0/0 (nothing owed) meets it; summaries are strict JSON (NaN fails loudly).
- Calculations run on numpy arrays inside each function (pandas overhead per group was the cost):
  metrics 27s -> 3s, IRRBB 6.5s -> 2.5s, results unchanged (checked against a saved snapshot).
- **As-of date (decided 2026-11-13):** `--as-of DATE` (or `today`, UTC) on `generate`,
  `check_raw`, `quality.run` and the pipeline generates every business day from `start_date`
  up to DATE (`GeneratorConfig.through`, `numpy.busday_count`); without it, `n_days` (60), so
  local runs, tests and CI stay fixed. Seed and start date stay fixed: a later date only adds
  days, and `tests/test_generate.py` proves the first days are bit-identical when the series
  grows (planted series-dependent draws make it fail), so per-day random streams were not
  needed. AWS task: `--as-of today`; Dagster schedule: its scheduled date (run key per day).
  Run time grows with the series (75 days: about 15 s for the whole pipeline).
- Synthetic bank: 3 entities x 3 currencies x 14 product types (924 positions; 60 business days
  by default, or up to the as-of date; Mon-Fri, no holidays). Assets = liabilities + 6% equity per entity and currency on day one. Foreign books
  hold fewer reserves (5%) and government bonds (8%) than the 20% default foreign weight.
- Hedges: pay-fixed interest rate swaps (5-20y, at market), notional off balance sheet. LCR 0%
  and NSFR 0% (notional is not a cash flow; charged on replacement cost); not in the ladder;
  in IRRBB two legs (-notional at maturity, +notional at the next 3-month reset).
- Maturities count down in **calendar days**. Rates are fixed at reset/rollover from the
  currency's curve at the reference tenor plus a per-category spread, floored at 0%.
- Generator returns `RawData(positions, rates, fx)`; four independent random streams; same seed +
  config = identical data. It writes control totals like a source system.
- Real mode never downloads: `python -m riskplatform.ingestion.market_rates` saves curves (FRED
  USD, ECB EUR, BoE gilt curve) and BoE FX with provenance (SHA-256). Holidays filled from the
  previous day up to 4 days. Real snapshots are NOT committed (FRED terms forbid storing or
  redistributing).
- Quality layer: row problems are quarantined; reconciliation failures and STOP checks stop the
  run (nothing published); WARNING checks and outliers go in the breaks report.
- Metrics and IRRBB: three levels (entity-currency monitoring only; entity in home currency;
  group in GBP). Group figures assume free transfer between entities (labelled).
- IRRBB: shock sizes are the same in Basel SRP31 (2026) and UK ICAA 9.11 (01/07/2026) for GBP,
  EUR and USD. UK differs in the post-shock floor (-100bp rising to 0% at 20y) and in offsetting
  gains at 50% across currencies. Non-maturity deposits: 70% core / 4y (stable), 50% / 3y (less
  stable), judgement within the caps.
- CI (`.github/workflows/ci.yml`): ruff check, ruff format --check and pytest; builds the Docker
  image and runs the pipeline in it (exit 0 or 2 passes); terraform fmt and validate.
- Storage (`config/storage.yaml`, `riskplatform/storage.py`): `local` by default (tests and CI
  never touch AWS); `s3` via `RISKPLATFORM_STORAGE_BACKEND` and `RISKPLATFORM_S3_BUCKET`. Every
  run is uploaded under its run id (never overwritten), `runs/<run_id>.json` for every run, and
  `latest.json` written last and only for a run that published validated data. No credentials
  in code or config (AWS profile on a PC, the task's IAM role in AWS). Tested with moto.
- Query layer (`riskplatform/query.py`): DuckDB views `lcr, nsfr, ladder, eve, risk, nii, gap,
  limits, limit_changes, metric_changes, balance_drivers, quarantine, run` over the latest good
  run only (local: `data/validated/_run.json`; S3: `latest.json`).
  From S3 the run (about 1 MB) is downloaded once to `data/cache/`. Labelled choice over DuckDB
  httpfs (extensions downloaded at run time) and Athena (Glue catalogue, cost per query; the
  step up for querying many runs in place).
- Docker: two-stage build on `python:3.12-slim-trixie`, non-root user (uid 10001), configs in
  the image, `.dockerignore` keeps `.env`, data, tests and Terraform files out.
- Orchestration: Dagster assets (`riskplatform/orchestration/definitions.py`) call the same
  functions as the pipeline; stops raise a Failure, findings are WARN asset checks; only the S3
  publish retries; schedule weekdays 06:00 London, off by default. In AWS the daily run is a
  Fargate task (Dagster's daemon in AWS would cost money around the clock).
- AWS (`infra/terraform/`, guide in `infra/README.md`; the author applies, Claude never does):
  `billing/` (AWS Budget, 10 USD/month, emails at 50/80/100% actual and 100% forecast) applied
  first; `platform/` (private encrypted versioned HTTPS-only S3 bucket, immutable scanned ECR,
  Fargate in the default VPC with a public IP and outbound-HTTPS-only security group, so no
  NAT gateway; least-privilege IAM with no wildcard actions; EventBridge schedule off by
  default; SNS email on any exit code except 0 and 2, or a task that fails to start).
  **Alerts are for failures only (decided 2026-11-13):** exit 1 (stopped or error) and crash
  codes the program never sees (137 out of memory, 143 killed) email; findings (exit 2: a
  breach or an IRRBB outlier) do not; they are covered by the limit alert email (8.3). Exactly "1" was rejected because an out-of-memory kill would then
  be silent. BANK_UK was deliberately not resized. `tests/test_infra.py`
  checks the safety rules and that Terraform agrees with `storage.yaml` and the Dagster schedule.
- **Phase 8 choices (decided 2026-11-14):** dashboard = **Streamlit** (live, tested, public
  deploy in Phase 10) **plus Tableau-ready extracts** (CSV from the query layer); limit alerts
  = **email through the existing SNS topic** (no Slack, no new secrets).
- Internal limits (`config/limits.yaml`, validated by `LimitsConfig`; risk appetite, judgement,
  NOT regulatory parameters; each has a `why`): LCR >= 110%, NSFR >= 105% (management buffers
  above the 100% minimums), EVE loss <= 15% of Tier 1 (equal to the SRP31.82 outlier test).
  Thresholds: warning at 90% used, breach at 100% (inclusive). Levels: entity and group
  (per-currency stays monitoring only). Both regimes against the same limits. No NII limit yet
  (needs projected NII, which the platform does not calculate; labelled).
- Utilisation (`riskplatform/limits.py`, pure functions): floor = limit / actual; cap = actual /
  limit. Floor ratio 0/0 (NaN) or infinite = 0% used (nothing owed, as `_meets`); ratio 0 or
  below with something owed = infinite = breach. EVE share = eve_risk / tier1 (a loss with no
  positive Tier 1 = breach). An EVE breach equals an IRRBB outlier except at exactly 15.0%
  (limit breached when reached, >=; outlier test needs a loss above, >). Any regulatory
  shortfall (< 100%) is always a limit breach (tested).
- Limits stage (8.2): `run_limits(data_dir, run_id, cfg)` writes `data/limits/<run_id>/`
  `limits.parquet` (every date) and strict-JSON `summary.json` (as_of, limits used,
  latest_status counts, alerts: warnings and breaches on the latest date, breaches first; an
  infinite utilisation is `null`). Column is `limit_name` (not `limit`, a SQL keyword); JSON
  alert key stays `"limit"`. Pipeline stage `limits` after IRRBB: findings (exit 2) if any
  alert, including a warning alone (decided: 2 means "needs attention"; it sends no email).
  Each alert is a WARNING log line (`describe_alert`). Also: own command
  `python -m riskplatform.limits` (exit 1 on alerts), uploaded to S3 before latest.json,
  `limits` view in the query layer, Dagster asset `limit_status` with a WARN check
  `no_limit_warning_or_breach` (`published_run` waits for it).
- **Limit alert email (8.3, `riskplatform/notify.py`):** after publishing, ONE email per run to
  the existing SNS topic if any limit is at warning or breach (subject `riskplatform limits
  <date>: N breaches, M warnings`; one `describe_alert` line each, breaches first; then the
  "since the day before" section). Topic only from `RISKPLATFORM_ALERT_TOPIC_ARN` (Terraform
  sets it on the task; absent locally, in tests and CI: log only). Task role: `sns:Publish` on
  that one topic (`tests/test_infra.py`). A send failure raises (exit 1, so the failure alert
  fires). Repeats every run while a limit stays there (labelled). Tested with moto SNS + SQS.
- **Since the day before (8.4, `riskplatform/changes.py`, `config/reporting.yaml`):**
  "yesterday" = previous business day of the same run's series (equals the previous run's
  latest day, as the series regenerates identically; labelled). `limit_changes` (movement:
  new / worse / resolved / better / unchanged; no result yesterday counts as ok),
  `metric_changes` (the limits' actuals), `balance_drivers` (on balance sheet, per entity,
  category, side, GBP: volume = local change x FX yesterday, FX = today's balance x FX move;
  they add up exactly), `summary.json` (top drivers: `top_drivers: 10` in config). Pipeline
  stage `changes`, S3 `changes/<run>/`, three query views, Dagster `daily_changes`.
- **Dashboard (8.5, `riskplatform/dashboard/`):** Streamlit 1.67 (`.[dashboard]` extra, not in
  the Docker image). `data.py` = one SQL query per chart over the query views (no Streamlit
  import; tested against the run's files); `app.py` = header tiles, tabs Limits / Liquidity /
  IRRBB / Since the day before / Data quality, sidebar Basel vs PRA. Colours from the reference
  chart palette (entities fixed by name order, group neutral, status colours always with the
  word). One light theme (`.streamlit/config.toml`, labelled). Tested with Streamlit AppTest;
  layout checked by screenshot (Playwright + Edge, not part of the repo). Quarantine became a
  query view for its Data quality tab.
- **Tableau extracts (8.6, `riskplatform/extracts.py`):** one CSV per query view plus
  `liquidity_ratios.csv` (LCR and NSFR stacked with minimum and internal limit) and
  `manifest.json`; dates YYYY-MM-DD, ratios as fractions, `who` column, infinities written
  empty (one "inf" turns a Tableau column into text). `data/extracts/latest/` replaced by a
  copy (not atomic on Windows, labelled). Stage `extracts`, S3, Dagster `tableau_extracts`,
  command `riskplatform-extracts`.
- **Daily PDF (8.7, `riskplatform/report.py`):** ReportLab 5.1 (core dependency, so in the
  image), two A4 pages: headline, limits at warning or breach, liquidity + LCR trend chart,
  IRRBB (outlier test, NII +-200bp), since the day before, data quality. Uses the dashboard's
  `data.py`, so every output shows the same numbers. Invariant mode (same run, same bytes).
  Tests read the text back with pypdf (dev only). Stage `report`, S3 `reports/<run>/`, Dagster
  `daily_report`, command `riskplatform-report`.
- Current results (as of Mon 16 Nov 2026, 76 business days): LCR and NSFR limits ok
  everywhere; EVE: 2 breaches (BANK_UK 20.3% of Tier 1 = 135% used, Basel and PRA) and 4
  warnings (BANK_EU 97% Basel / 98% PRA, group 95% Basel / 96% PRA); BANK_US is back to ok
  (it breached at 75 days). So the limit email arrives every weekday until the EVE book
  changes. The whole pipeline takes about 30 s at 76 days.

- **Phase 9: model validation of the LCR retail run-off rates (decided and done 2026-11-17).**
  What: the 5% (stable; LCR40.7, UK Art 24(1)) and 10% (less stable; LCR40.13, Art 25(1))
  30-day run-off rates. Report: `docs/validation_report.md`, in model-risk style; sections 1-5
  (scope, tests, data, criteria, limitations) were WRITTEN before any data was fetched, and
  first COMMITTED (`2969e03`, with the H.8 code) after the data was downloaded but before any
  result was calculated (pre-registration; a separate earlier commit would have been cleaner).
  Section 9 logs every later change to them (two, both before any result, neither changing a
  criterion). Code: `src/riskplatform/model_validation/` (named so
  because `validation/` already means the raw data checks). One command: `riskplatform-validate`.
  - Data: Fed H.8 weekly deposits from FRED, not seasonally adjusted, 1973-2026, IDs checked on
    FRED: `DPSACBW027NBOG` (all commercial banks), `DPSSCBW027NBOG` (small banks). Saved by
    `python -m riskplatform.model_validation.h8` to `data/external/h8_deposits.parquet` with
    provenance and SHA-256 (FRED marks it copyrighted: not committed). Checks refuse a download
    with non-positive values, repeated dates, non-Wednesdays, wrong order, a missing week, or a
    reply that is not FRED's CSV. Learned from the Fed's H.8 notes: panel shifts and mergers
    are removed from past LEVELS, but nonbank structure activity of $5bn+ only from growth
    rates (limitation 7); the Fed's structure notes start in October 1989.
  - Tests: T1 backtest (4-week outflows, max(0, 1 - D(t+28d)/D(t)), 5 weeks as a check);
    T2 detection check on the synthetic bank; T3 LCR sensitivity on the latest validated day
    (both retail rates set to the worst T1 outflow; break-even rates by bisection); T4 99th
    percentile benchmark. Ratings green/amber/red fixed in advance; overall = worst of T1 and T3
    if T2 is green, else inconclusive.
  - Stress episode (`config/generator_stress.yaml`, off by default; `GeneratorConfig.stress`):
    BANK_UK sterling deposits lose 10% (stable) and 20% (less stable) over 15 business days
    from 2026-09-14, paid from central bank reserves then government bonds (each kept at 1% at
    least), so the books still balance and the LCR falls (186% -> 118%). First version did not
    pay the withdrawals and the stressed LCR ROSE: caught and fixed. Runs too large to pay are
    refused. No random draws: days before the start are bit-identical.
  - Results (H.8 to 2026-11-04, SHA-256 `f68ce321...`): T1 all banks AMBER (worst 6.43%, every
    spell a year-end, quarter-end or 9/11 spike unwinding); T1 small banks RED (10.69%, one week
    27 Mar - 3 Apr 1985 that looks like a panel reclassification but the Fed notes cannot
    confirm, so it counts; without it 4.98%; since Oct 1989 3.87%); T2 GREEN (planted 9.39%,
    found 9.30%); T3 GREEN (lowest stressed LCR 146.0%; break-even 19.2-29.4% at 110%,
    22.5-33.6% at 100%; a 25% run takes BANK_EU and BANK_UK below 100%); T4 2.92% and
    2.41%. **Overall RED.** Recommendations R1-R7 in the report (resolve 1985 first).
  - **Re-rated 2026-11-17 (R1, after the results; author's decision, logged in the report's
    section 9):** the 1985 fall is a confirmed break: FRED's large-bank deposits
    (`DPSLCBW027NBOG`) start on 1985-04-03, and the H.8 release of 5 April 1985 (FRASER) split
    banks by assets under/over $1.4bn ($902.7bn under $1.4bn vs FRED's reconstructed $316.4bn).
    Excluded via `confirmed_breaks` in `config/validation.yaml` (results keep the
    `..._before_breaks` ratings). Small banks GREEN (worst 4.98%); T3 stress 6.43%. That exposed
    a flaw: setting both rates to 6.43% LOWERED the 10% less-stable rate and raised the
    "stressed" LCR; fixed so a stress never lowers a rulebook rate (lowest LCR 161.6%, GREEN).
    T4 small banks 2.18%. **Overall AMBER** (only the all-bank calendar spikes; R5).
  - Frameworks, checked against the official texts 2026-11-17: **SR 11-7 is superseded by
    SR 26-2 (Fed/OCC/FDIC, 17 April 2026)**; SR 26-2's "model" excludes deterministic rule-based
    processes. **PRA SS1/23** current version April 2026 (effective 23 April 2026) applies to
    firms with internal model approval; followed here as good practice (3.3(b)(i)-(iii), 4.4).
    Independent validation (SS1/23 Principle 4) is NOT met: the builder validated its own work.
  - Keeping the report true: `tests/test_validation_run.py` checks the report's headline numbers
    equal the code (T2/T3 everywhere; T1/T4 only where the H.8 file exists, skipped in CI). It
    caught a rounding error in the report (173.1% -> 173.0%) on its first run.

- **Early-warning indicators (R3/R4, 2026-11-17; `riskplatform/early_warning.py`,
  `config/early_warning.yaml`):** retail outflow over 20 business days (fixed FX, alert above
  3%, about the T4 99th percentile) and break-even run-off to the 110% LCR limit (alert below
  20%, the one-day loss of each 2023 failed bank), per entity and group (and per rulebook). They
  reuse the backtest's outflow measure and T3's `break_even`. Pipeline stage `early_warning`
  after `changes` (findings if any alert), S3, query view `early_warning`, Dagster asset
  `early_warning_indicators` (WARN check), and shown in the email (an indicator alone now
  sends it; subject adds ", K early warnings"), dashboard Limits tab, PDF section and
  `early_warning.csv`. Today: break-even alerts for BANK_EU (19.18% / 19.45%) and BANK_UK
  Basel (19.99%); retail outflow quiet, and alerts at BANK_UK on the planted run (tested).

- **T5 single-bank runs (R2 case study, 2026-11-17; report only, no rating, added after the
  results and logged in the report's section 9):** documented March 2023 runs from the FDIC
  staff study "Dissecting Depositor Flight" (May 2026, banks' core systems): each of SVB,
  Signature, First Republic lost 20%+ in one day; 47-50% in 3 business days; 36-60% over 7-17
  March. In `config/validation.yaml` (`single_bank_runs`) with each quoted sentence (a test
  checks figure vs quote). T5 reruns the LCR at each rate (`sensitivity.t5`): at 20%, 3 of 8
  results below 110%, none below 100%; at 36%+, every entity and the group below 100% (lowest
  49.5%). Also checked: the Fed's SVB review says "over $40 billion" on 9 March; the earlier
  "about a quarter in one day" claim was replaced everywhere by the sourced figures. The
  early-warning 20% break-even threshold now rests on the 20% one-day figure. FDIC API checked
  (quarterly Call Reports): SVB 24735, Signature 57053, First Republic 59017; SVB has no
  2023 Q1 report, First Republic fell 40.8% in 2023 Q1.

## Status
| Phase | Status |
|---|---|
| 0. Setup | Done (CI incl. format check; venv untracked) |
| 1. Domain learning | Done; the formula cheat sheet (`docs/methodology.md`) was deliberately skipped |
| 2. Synthetic data | Done: generator, data dictionary + sync test, DuckDB independent checks, calendar-day maturities, curve-linked rates, real curves (FRED/ECB/BoE), FX (synthetic + real BoE) |
| 3. Data quality layer | Done: quarantine, reconciliation to control totals, STOP/WARNING checks, outliers, quality run, breaks report, error-injection demo |
| 4. Liquidity metrics | Done: Basel + UK PRA rulebooks (sourced), worked example, LCR, NSFR, maturity ladder, metrics run, methodology doc |
| 5. Interest rate risk (IRRBB) | Done: Basel + UK PRA rulebooks (sourced), worked example (reproduces SRP31.92), shocks + UK floor, repricing gap, delta EVE + aggregation + outlier test, delta NII, IRRBB run, methodology doc; swap hedges added to the generator |
| 6. Testing and hardening | Done: one-command pipeline with exit codes (6.1), shared logging + run log files (6.2), edge cases: missing curve now a STOP, missing book a WARNING, LCR 0/0, empty folders (6.3), limits moved to config (6.4), metrics/IRRBB about 6x faster, slow tests shortened (6.5), 98% branch coverage and 16/16 planted formula bugs caught (6.6). 423 tests, about 1 minute. |
| 7. Cloud and orchestration | Code done: S3 publishing (7.1), Docker + CI (7.2), Dagster (7.3), Terraform billing alarm + platform with schedule and alerts (7.4), DuckDB query layer over the latest run, local or S3 (7.5), alerts on failures only (7.6), `--as-of` dates so each daily run adds its day (7.7). **Deployed 2026-11-16** from the personal PC: billing alarm, platform (27 resources), image `0.1.0`, one manual Fargate run (1 min 36 s, exit 2, 191 files to S3, limit email received). Daily schedule still **off**. |
| 8. Alerts and dashboard | Done: limits config + utilisation (8.1), limits stage + S3 + query view + Dagster asset (8.2), SNS limit alert email (8.3), what changed since yesterday (8.4), Streamlit dashboard (8.5), Tableau CSV extracts (8.6), daily PDF report (8.7), README + `docs/reporting.md` + this file (8.8). 597 tests, 2 to 6 minutes (varies from run to run; not yet profiled). |
| 9. Model validation | Done: scope and criteria before results (9.1), Fed H.8 deposits from FRED (9.2), planted deposit-run stress episode (9.3), backtest T1 + detection check T2 (9.4), LCR sensitivity T3 + benchmark T4 (9.5), `riskplatform-validate` and report consistency tests (9.6), conclusion, recommendations and SS1/23 / SR 26-2 framing (9.7), this file (9.8). R1 done: the 1985 break confirmed and excluded, overall AMBER (was RED). |
| 10. Public deploy | Not started |

## Deployment checklist (personal PC; decided 2026-11-16)
**All Terraform work happens on the personal PC only.** Its `terraform.tfstate` files are the
only record of what exists in AWS (Git ignores them on purpose); running Terraform from another
device without them would try to create everything again. Keep developing on either PC as usual
(code goes through Git). The same commands, with more explanation, are in `infra/README.md`.

**Result, 2026-11-16 (personal PC, with Claude running the commands and the author approving):**
- Done: A (597 tests green, local run exit 2), B, C, D, E. Left: **F** (see below).
- Account security (B): root user has MFA; the root user's only access key (it was in this PC's
  `~/.aws/credentials`) was deactivated and **deleted** (verified). IAM Identity Center enabled
  "with AWS Organizations" in eu-west-2; user `sneha-admin` with the `AdministratorAccess`
  permission set (4-hour sessions); CLI profile `riskplatform` (SSO). Every new terminal:
  `$env:AWS_PROFILE = "riskplatform"`; when expired: `aws sso login --profile riskplatform`.
  The portal URL is on the IAM Identity Center dashboard (kept out of the repo).
- Billing alarm (C): budget `riskplatform-monthly-cost`, 10 USD/month, alerts to the author's
  email at 50/80/100% actual and 100% forecast. Account is on the **PAID** plan with **0 USD
  credits**, so usage is charged to the card (monthly invoice; Indian cards may need an OTP or a
  manual payment). Actual spend that evening: 0.00 USD.
- Platform (D): 27 resources, nothing destroyed; schedule created DISABLED; SNS email
  subscription confirmed.
- Image and run (E): `riskplatform:0.1.0` in ECR (205 MB compressed); one manual Fargate run as of
  2026-11-16 (76 business days): all nine stages, exit 2, 191 files to S3, latest.json updated,
  query and PDF work with `--source s3`, and the limit alert email arrived ("2 breaches, 4
  warnings"; BANK_US resolved since 2026-11-13). No failure email (correct for exit 2).
- Cost estimate (London prices, about 88 INR/USD): now, idle, about 2 INR/month (image storage);
  with the schedule on, about 0.08 USD = 7 INR/month (8-9 INR with 18% GST), creeping up by about
  0.5 INR/month as results accumulate in S3.
- Lessons (written into `infra/README.md`): on Windows, ECR login needs `cmd /c` for the pipe and
  a throwaway Docker config without the credential helper (ECR tokens are too long for Windows
  Credential Manager); quote `-chdir="$tf"` in PowerShell; build with `--provenance=false` so
  ECR can scan the image (`0.1.0` has no scan result); saved plans (`tfplan`) are now ignored by
  Git and Docker.

**Still to do on the personal PC only (F):**
- Back up `infra/terraform/billing/terraform.tfstate` and `infra/terraform/platform/terraform.tfstate`
  somewhere private (author; not Git, not public storage).
- Turn the schedule on when wanted (`schedule_enabled = true` in `platform/terraform.tfvars`,
  plan, approve, apply): about 7-9 INR/month, one limit email per weekday while BANK_UK breaches.
- Any future AWS change (new image tag, Terraform edit) also happens on the personal PC.

**A. Get the code and prove it is green (nothing changes in the code tonight)**
1. Commit and push this file from the work PC first (the 8.8 docs are already committed).
2. On the personal PC: `git pull` on branch `sneha` (clone first if needed).
3. Create or refresh the venv, then run the checks; all three must pass before going on:
   ```
   py -3.12 -m venv venv              # only if there is no venv yet
   .\venv\Scripts\Activate.ps1
   pip install -e ".[dev,orchestration,dashboard]"
   pytest; ruff check .; ruff format --check .      # 597 tests, 2 to 6 minutes
   riskplatform-run                                 # local run: expect exit code 2 (findings)
   ```
   If anything fails here, stop and bring the output to Claude; do not "fix" it on the spot.

**B. One-time setup (`infra/README.md` section 0)**
4. Install the tools, then restart the terminal (Docker Desktop needs a Windows restart the
   first time): `winget install Hashicorp.Terraform`, `winget install Amazon.AWSCLI`,
   `winget install Docker.DockerDesktop`. Check: `terraform -version`, `aws --version`,
   `docker version`.
5. AWS console as the root user: turn on root MFA; set up IAM Identity Center with a user for
   yourself (`AdministratorAccess` permission set). No access keys, ever.
6. Sign in from the terminal (region `eu-west-2`):
   ```
   aws configure sso --profile riskplatform
   aws sso login --profile riskplatform
   $env:AWS_PROFILE = "riskplatform"      # in every new terminal
   aws sts get-caller-identity             # shows your account id
   ```

**C. Billing alarm first (`infra/README.md` section 1)**
7. In `infra/terraform/billing`: `Copy-Item terraform.tfvars.example terraform.tfvars`, put your
   email in `terraform.tfvars`, then `terraform init` and `terraform plan`.
   **Paste the plan to Claude before applying.** Expected: exactly one `aws_budgets_budget`.
8. `terraform apply` (type `yes`).

**D. The platform (`infra/README.md` section 2)**
9. In `infra/terraform/platform`: copy the example the same way; set your email, keep
   `image_tag = "0.1.0"` and `schedule_enabled = false`. Then `terraform init`, `terraform plan`.
   **Paste the plan to Claude before applying.** Expected: bucket and its settings, ECR, log
   group, 3 IAM roles + policies (the task's includes `sns:Publish` on the alerts topic only),
   ECS cluster and task definition (with `RISKPLATFORM_ALERT_TOPIC_ARN`), the schedule
   (disabled), SNS topic, email subscription, EventBridge rules. Nothing destroyed.
10. `terraform apply`, then `terraform output`.
11. **Confirm the SNS subscription** from the email AWS sends ("AWS Notification - Subscription
    Confirmation"); without the click, no alert or limit email ever arrives.

**E. Image, one manual run, checks (`infra/README.md` sections 3 and 4)**
12. Start Docker Desktop. From the repository root, build and push tag `0.1.0` (it must equal
    `image_tag`; tags are immutable, so a rebuild needs a new tag, see "Releasing a new version").
13. Run the task once by hand (`aws ecs run-task ...` from `infra/README.md` section 4) and watch
    `aws logs tail /riskplatform/pipeline --follow`. Expected: every stage including changes,
    extracts and report, then `Pipeline FINDINGS (exit code 2)`.
14. Check: `aws s3 cp "s3://$bucket/riskplatform/latest.json" -` shows the run;
    `python -m riskplatform.query --source s3 --tables` lists the views (with
    `$env:RISKPLATFORM_S3_BUCKET = $bucket`); `riskplatform-report --source s3` writes the PDF;
    **one limit alert email** arrives (subject `riskplatform limits <date>: 2 breaches, ...`,
    with the "Since ..." section). No failure email (exit 2 is not a failure).

**F. Turn the schedule on and back up the state (`infra/README.md` section 5)**
15. Only after E worked: `schedule_enabled = true` in `platform/terraform.tfvars`, `terraform plan`
    (expected: only the schedule changes), `terraform apply`. Runs weekdays 06:00 London.
16. Back up `infra/terraform/billing/terraform.tfstate` and
    `infra/terraform/platform/terraform.tfstate` somewhere private (not Git, not a public
    drive): they are needed to change or remove anything later.

**Never tonight:** commit `terraform.tfvars`, `*.tfstate` or `.terraform/` (Git ignores them;
`tests/test_infra.py` checks); create access keys; edit code to make something work (bring the
error to Claude); run Terraform from the work PC.
**If something goes wrong:** copy the exact command and output to Claude. To remove everything:
"Removing everything" in `infra/README.md` (platform first, billing last).

## Next steps
1. **Phase 9 follow-ups** (from `docs/validation_report.md`, section 7.2), most important first:
   R1 DONE 2026-11-17 (1985 break confirmed and excluded; overall AMBER). R3/R4 DONE
   2026-11-17 (early-warning indicators). R2 case study DONE 2026-11-17 (T5, below); R2 part 2
   NEXT: distribution of quarterly deposit changes across all FDIC-insured banks (FDIC API,
   free, no key; filter mergers with FDIC structure data). Author chose "both, case study first".
   R3/R4 turn the break-even rates and the ~3% T4 percentile into early-warning indicators.
   Ask the author before starting any of them.
2. Phase 7 leftovers, **personal PC only**: back up the Terraform state; turn the schedule on when
   wanted (see "Deployment checklist", F). Decided before (done): failure alerts for exit codes
   except 0 and 2; limit alert email for findings; each run up to its own date (`--as-of today`).
3. Resolved 2026-11-17: the UK 15% outlier test is ICAA 9.4A (the binding rule: calculate,
   and notify the PRA "immediately" if EVE would decline by MORE than 15% of Tier 1), restated
   in SS31/15 2.7B (current version October 2025, effective 1 July 2026), which adds "outlier
   firm" status and a PRA review. Both were right, just incomplete; every file now cites
   "ICAA 9.4A, SS31/15 2.7B". A new SS31/15 takes effect 1 January 2027: not yet reviewed.
4. Done 2026-11-17, test suite profiled (`pytest --durations=30`): no slow test (slowest 6.4 s,
   the top 30 about 1 minute of a 2-minute run). Running in parallel (pytest-xdist 3.8.0, 12
   cores) took 99 s and then 201 s against 104-123 s serially: the time is disk I/O on this PC
   (many small Parquet writes, likely antivirus scanning), not CPU, so xdist was NOT added.
   The 2-6 minute spread is the machine; nothing to fix in the tests.
5. **Phase 10: public deploy of the dashboard** (not yet planned; decide hosting and cost with
   the author first).
6. Walk through the code in plain words (see "Walkthrough debt").
7. Not covered by tests on purpose: the live HTTP fetch and `main()` in
   `ingestion/market_rates.py` (needs the internet; the parsers are tested on sample replies).

## Assumptions to review before this goes on a resume
- `docs/liquidity_methodology.md`, "Assumptions and simplifications" (19 items + swaps).
- `docs/irrbb_methodology.md`, "Assumptions and simplifications" (13 items), especially the
  non-maturity deposit core share and maturity, and the NII scenarios and horizon.
- `docs/data_dictionary.md`, "Known simplifications and limitations" and the licence notes.
- `docs/data_quality.md`, "Simplifications and limitations".
- `docs/reporting.md`, "Assumptions and simplifications" (9 items), especially the internal
  limit values (judgement) and the repeating limit email.
- `docs/validation_report.md`, section 5 (7 limitations, stated before the results) and section
  7.3 (what the validation is and is not: good practice, not independent).
- Outlier thresholds in `config/quality.yaml` (review triggers, chosen by judgement).

## Walkthrough debt (code written but not yet explained line by line to the author)
Explained when written, but worth a second pass: everything under `src/riskplatform/` and
`tests/`. Most important first: `metrics/lcr.py`, `metrics/nsfr.py`, `irrbb/shocks.py`,
`irrbb/eve.py`, `irrbb/repricing.py`, `irrbb/run.py`, `metrics/run.py`, `quality/run.py`,
`quality/rows.py`, `ingestion/generate.py`, `validation/check_raw.py`.
Added in Phase 6, worth a line-by-line pass: `pipeline.py` (stages, exit codes), `logs.py`,
`_factors` in `metrics/nsfr.py` (the searchsorted band lookup), `_slotted` in
`irrbb/repricing.py`, the SQL of `curves_cover_every_currency` and `every_book_every_day` in
`validation/check_raw.py`, and `_meets` in `metrics/run.py` (minimum tested on amounts).
Added in Phase 7: `storage.py` (publish order, latest.json last), `query.py` (latest run, the
download cache and its marker), `orchestration/definitions.py`, the Dockerfile, and
`infra/terraform/platform/iam.tf` and `alerts.tf` (the event patterns, `anything-but [0, 2]`),
`GeneratorConfig.through` and `parse_as_of` in `config.py` (business-day count, UTC today), and
`daily_schedule` in `orchestration/definitions.py` (scheduled date, run key; run config
replaces the whole resource config).
Added in Phase 8: `limits.py` (`utilisation` floor vs cap and its edge cases, `measure_limits`,
`run_limits`, `latest_alerts` ordering), `LimitsConfig` in `config.py`, the `limits` stage in
`pipeline.py`, and `limit_status` in `orchestration/definitions.py`; `notify.py` (email text,
`region_of`, when nothing is sent); `changes.py` (`movement` with `np.select`, the merge on the
limit id with the group's empty entity, `balance_drivers` volume / FX split); `dashboard/data.py`
(the SQL, `IS NOT DISTINCT FROM` for the group join) and `dashboard/app.py` (Altair layers, the
hover selection, `st.cache_resource` with a cursor per page view); `extracts.py` (`_view_sql`,
`_clean`, the latest/ copy); `report.py` (ReportLab platypus story, `KeepTogether`, the LinePlot
chart, invariant mode).
Added in Phase 9: `model_validation/h8.py` (the checks, provenance and the hash check on load),
`StressEpisode` in `config.py` and `stress_factors` / `pay_withdrawals` in
`ingestion/generate.py` (geometric daily fraction, paying in order down to a floor),
`model_validation/backtest.py` (`forward_outflow` by rows, `spells`, fixed-FX
`retail_deposits`, `planted_size`), `model_validation/sensitivity.py` (`with_retail_run_off`,
the bisection in `break_even`) and `model_validation/run.py` (the bank built fresh through the
quality layer, exit codes).

## How to run
First time on a new machine:
```
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```
Run `pip install -e ".[dev]"` again after pulling a change to `pyproject.toml` (new packages such
as dagster, boto3 or moto); otherwise the tests that import them cannot even load.
Checks:
```
pytest
ruff check .
ruff format --check .
```
Whole pipeline in one command (synthetic, offline; exit 0 clean, 1 stopped, 2 findings):
```
riskplatform-run                                 # or: python -m riskplatform.pipeline / make run
riskplatform-run --as-of today                   # every business day up to today (as in AWS)
$env:RISKPLATFORM_LOG_LEVEL = "DEBUG"            # optional: more detail (default INFO)
```
Coverage report: `pytest --cov=riskplatform --cov-branch --cov-report=term-missing`

Query the latest good run with SQL (local data/, or `--source s3` with RISKPLATFORM_S3_BUCKET):
```
python -m riskplatform.query --tables
python -m riskplatform.query "SELECT regime, entity, lcr FROM lcr WHERE level = 'entity'"
python -m riskplatform.query "SELECT regime, entity, limit_name, utilisation, status FROM limits WHERE status <> 'ok' AND date = (SELECT max(date) FROM limits)"
python -m riskplatform.limits                    # limits for the latest run (exit 1 on alerts)
```
Dashboard, Tableau extracts and the daily PDF (for the latest good run):
```
pip install -e ".[dashboard]"; riskplatform-dashboard   # or: make dashboard (http://localhost:8501)
riskplatform-extracts                            # data/extracts/<run>/ and data/extracts/latest/
riskplatform-report                              # data/reports/<run>/daily_report.pdf
```
Model validation (Phase 9; needs the internet once for the H.8 data):
```
python -m riskplatform.model_validation.h8       # saves data/external/h8_deposits.parquet (+ provenance)
riskplatform-validate                            # T1-T4 -> data/validation/<UTC time>/ (exit 2: not all green)
python -m riskplatform.ingestion.generate --config config/generator_stress.yaml   # the planted run
```
Orchestration and cloud:
```
pip install -e ".[orchestration]"; dagster dev     # Dagster UI with the assets and schedule
make docker-build; make docker-run                 # or: docker build -t riskplatform .
# AWS: infra/README.md (billing alarm first; the author applies)
```

Stage by stage (the same functions the pipeline calls):
```
python -m riskplatform.ingestion.generate        # data/raw/
python -m riskplatform.validation.check_raw      # independent checks of the generator output
python -m riskplatform.quality.run               # data/validated/ + data/quality_runs/<run>/
python -m riskplatform.metrics.run               # data/metrics/<run>/ (Basel and UK PRA)
python -m riskplatform.irrbb.run                 # data/irrbb/<run>/ (exit 1 if an outlier)
python -m riskplatform.quality.demo              # error-injection demo (data/demo/)
```
With an as-of date, give the same `--as-of` to `generate`, `check_raw` and `quality.run` (they
check the number of days); `metrics.run` and `irrbb.run` read what the quality run published.
Real rates (needs internet once):
```
python -m riskplatform.ingestion.market_rates    # saves data/external/ (curves + FX)
python -m riskplatform.ingestion.generate --config config/generator_real.yaml
python -m riskplatform.quality.run --config config/generator_real.yaml
python -m riskplatform.metrics.run --config config/generator_real.yaml
python -m riskplatform.irrbb.run --config config/generator_real.yaml
```
All data is written under `data/` (ignored by Git).

## Resuming in a new chat (or on another device)
1. On this device: commit and push first (the author does this; Claude never commits or pushes).
2. On the other device: `git pull`, then create or refresh the venv (see "How to run":
   `pip install -e ".[dev]"`, plus `".[orchestration]"` for the Dagster UI and `".[dashboard]"`
   for Streamlit), and check the suite is green: `pytest`, `ruff check .`, `ruff format --check .`
   (661 tests, 2 to 6 minutes; on a PC without the H.8 file, 2 report tests are skipped).
   `data/` is not in Git: run `riskplatform-run --as-of today` once to recreate it, and
   `python -m riskplatform.model_validation.h8` for the H.8 deposits (Phase 9).
3. Ask Claude to read this file and the code base, and say the step, e.g. "Phase 9" or
   "deploy step 7.5".
Note: if the PC sleeps during a long run, timings include the sleep (on 2026-11-14 a 4-minute
suite showed 3.5 hours); keep it awake for long runs or check the Windows power log.
