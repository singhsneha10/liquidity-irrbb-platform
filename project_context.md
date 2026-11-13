# Project Context

**Project:** Automated Liquidity & Interest Rate Risk Monitoring Platform
**Goal:** A production-quality, end-to-end, deployed portfolio project that impresses senior
people at banks such as JPMorgan (Risk Analyst roles). Business use comes later.
**Last updated:** 2026-11-13 (Phase 7 code complete; deployment to AWS pending, run by the author).

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
  -> (backend s3) S3 under the run id; latest.json last -> query layer (DuckDB SQL views)
  -> reporting / dashboard (later)
In AWS: EventBridge Scheduler -> ECS Fargate task (Docker image from ECR) -> S3 + CloudWatch
Logs; a failed run (any exit code except 0 and 2) -> SNS email. Each run uses --as-of today.
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
- UK entity limits and early-warning indicators; "what changed since yesterday" analysis
- Outputs: Tableau-ready extracts, Excel export, one-page summary report, optional AI commentary
  (all numbers from code, never from the model)
- FastAPI backend; dashboard in **Tableau** (Alteryx optional for data prep)
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
  generate -> raw checks -> quality -> metrics -> IRRBB and stops at the first stage that stops.
  Exit codes: 0 clean, 1 stopped or error, 2 ran but found a breach or an IRRBB outlier (the
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
  run` over the latest good run only (local: `data/validated/_run.json`; S3: `latest.json`).
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
  breach or an IRRBB outlier) do not, they stay in the logs, S3 and latest.json and wait for
  the Phase 8 limit alerts. Exactly "1" was rejected because an out-of-memory kill would then
  be silent. BANK_UK was deliberately not resized. `tests/test_infra.py`
  checks the safety rules and that Terraform agrees with `storage.yaml` and the Dagster schedule.

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
| 8-10. Alerts + Tableau, model validation, deploy | Not started |

## Next steps
1. Deploy Phase 7 (the author runs these): install Terraform, apply `billing/` first, then
   `platform/`, push the image, one manual run, then `schedule_enabled = true`.
2. Decided before the schedule goes on (done, see design decisions): alerts on failures only,
   findings wait for Phase 8; each run generates up to its own date (`--as-of today`).
   BANK_UK stays an IRRBB outlier (20.5% of Tier 1; group 14.2%), so runs exit 2 without email.
3. Phase 8: limit utilisation alerts (90% warning, 100% breach; this is where findings get
   their alerts) and the dashboard, reading through `riskplatform.query`. The series now grows
   by a day per run, so "what changed since yesterday" compares the last two dates.
4. Walk through the code in plain words (see "Walkthrough debt").
5. Not covered by tests on purpose: the live HTTP fetch and `main()` in
   `ingestion/market_rates.py` (needs the internet; the parsers are tested on sample replies).

## Assumptions to review before this goes on a resume
- `docs/liquidity_methodology.md`, "Assumptions and simplifications" (19 items + swaps).
- `docs/irrbb_methodology.md`, "Assumptions and simplifications" (13 items), especially the
  non-maturity deposit core share and maturity, and the NII scenarios and horizon.
- `docs/data_dictionary.md`, "Known simplifications and limitations" and the licence notes.
- `docs/data_quality.md`, "Simplifications and limitations".
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

## Resuming in a new chat
Paste this file (or ask Claude to read it and the code base) and say which step you are on, for
example "Phase 7: deploying" or "Phase 8, step 8.1". Claude never commits or pushes.
