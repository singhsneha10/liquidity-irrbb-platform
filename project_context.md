# Project Context

**Project:** Automated Liquidity & Interest Rate Risk Monitoring Platform
**Goal:** A production-quality, end-to-end, deployed portfolio project that impresses senior
people at banks such as JPMorgan (Risk Analyst roles). Business use comes later.
**Last updated:** 2026-11-16. Phases 7 and 8 code complete. AWS deployment (7.5) is still
pending, run by the author on the personal laptop. 8.1-8.7 were committed by the author step by
step, and 8.8 (README, `docs/reporting.md`); the deployment checklist in this file was **not
committed when this was written**.
597 tests. Next: deploy, then Phase 9 (model validation).

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
- Model validation module: backtest deposit run-off assumptions
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
| 7. Cloud and orchestration | Code done: S3 publishing (7.1), Docker + CI (7.2), Dagster (7.3), Terraform billing alarm + platform with schedule and alerts (7.4), DuckDB query layer over the latest run, local or S3 (7.5), alerts on failures only (7.6), `--as-of` dates so each daily run adds its day (7.7). 490 tests, about 1.5 minutes. **Not yet applied to AWS** (no Terraform state on this PC; Terraform not installed): follow `infra/README.md`. |
| 8. Alerts and dashboard | Done: limits config + utilisation (8.1), limits stage + S3 + query view + Dagster asset (8.2), SNS limit alert email (8.3), what changed since yesterday (8.4), Streamlit dashboard (8.5), Tableau CSV extracts (8.6), daily PDF report (8.7), README + `docs/reporting.md` + this file (8.8). 597 tests, 2 to 6 minutes (varies from run to run; not yet profiled). |
| 9-10. Model validation, deploy | Not started |

## Deployment checklist (personal PC; decided 2026-11-16)
**All Terraform work happens on the personal PC only.** Its `terraform.tfstate` files are the
only record of what exists in AWS (Git ignores them on purpose); running Terraform from another
device without them would try to create everything again. Keep developing on either PC as usual
(code goes through Git). The same commands, with more explanation, are in `infra/README.md`.

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
1. **Deploy Phases 7 and 8 tonight on the personal PC: follow "Deployment checklist" above.**
   Paste each `terraform plan` for review before applying. Expect exit 2 and one limit alert
   email per run (BANK_UK EVE breach).
2. Decided before the schedule goes on (done): failure alerts for exit codes except 0 and 2;
   limit alert email for findings; each run generates up to its own date (`--as-of today`).
3. Open question: `config/limits.yaml` cites "UK SS31/15" for the 15% outlier test, while
   `docs/irrbb_methodology.md`, `docs/reporting.md` and the PDF cite UK ICAA 9.4A. Check the
   official text and make them agree.
4. Profile the test suite (it varies from 2 to 6 minutes): `pytest --durations=20`.
5. **Phase 9: model validation** (backtest deposit run-off assumptions); Phase 10: public deploy
   of the dashboard.
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
   (597 tests, 2 to 6 minutes).
   `data/` is not in Git: run `riskplatform-run --as-of today` once to recreate it.
3. Ask Claude to read this file and the code base, and say the step, e.g. "Phase 9" or
   "deploy step 7.5".
Note: if the PC sleeps during a long run, timings include the sleep (on 2026-11-14 a 4-minute
suite showed 3.5 hours); keep it awake for long runs or check the Windows power log.
