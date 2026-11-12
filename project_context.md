# Project Context

**Project:** Automated Liquidity & Interest Rate Risk Monitoring Platform
**Goal:** A production-quality, end-to-end, deployed portfolio project that impresses senior
people at banks such as JPMorgan (Risk Analyst roles). Business use comes later.

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
8. Use the latest stable tool versions. Commit, push and update this file at the end of the day
   unless asked sooner; commit with explicit file lists.

## Big picture
```
market data (synthetic, or saved FRED / ECB / Bank of England files)
  -> generator -> data/raw/ (positions, rates, fx, control totals)
  -> quality run -> data/validated/ (+ quarantine, breaks report, audit folder per run)
  -> metrics run -> data/metrics/<run>/ (LCR, NSFR, ladder: Basel and UK PRA)
  -> IRRBB run -> data/irrbb/<run>/ (delta EVE, outlier test, delta NII, gap: Basel and UK PRA)
  -> reporting / dashboard (later)
```
The data contracts (`src/riskplatform/schemas.py`) are the seam: real data can replace the
synthetic data without changing calculation code. Calculations are verified separately from the
data: hand-worked examples written before the code, and proofs against published formulas.

## Target stack
- Python 3.11+ (venv uses 3.12), pandas, numpy, pydantic v2, pandera, pytest, ruff
- Storage and queries: Parquet, DuckDB (2.0); later S3
- Data quality layer with quarantine (done)
- LCR, NSFR and IRRBB (NII and EVE, six Basel shocks), all done, as pure functions, with
  regulatory parameters in YAML; **Basel and UK PRA rulebooks side by side**
- Model validation module: backtest deposit run-off assumptions
- UK entity limits and early-warning indicators; "what changed since yesterday" analysis
- Outputs: Tableau-ready extracts, Excel export, one-page summary report, optional AI commentary
  (all numbers from code, never from the model)
- FastAPI backend; dashboard in **Tableau** (Alteryx optional for data prep)
- Docker; GitHub Actions CI/CD
- AWS (S3, ECS Fargate or Lambda, CloudWatch), Terraform or CDK, billing alarm set first
- **Orchestration is required** (Dagster or Airflow)

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
- Synthetic bank: 3 entities x 3 currencies x 14 product types (924 positions, 60 business days,
  Mon-Fri). Assets = liabilities + 6% equity per entity and currency on day one. Foreign books
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
- CI (`.github/workflows/ci.yml`) runs ruff check, ruff format --check and pytest on every push.

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
| 7-10. Cloud + orchestration, alerts + Tableau, model validation, deploy | Not started |

## Next steps
1. Phase 7 (cloud and orchestration): set the AWS billing alarm first; then storage (S3, or
   DuckDB locally) and a scheduled daily run (Dagster or Airflow) calling the pipeline command.
2. Generator realism, still open: hedge size per book (BANK_UK is an IRRBB outlier at 20.5% of
   Tier 1 while the group is 14.2%; the pipeline exits 2 on every run because of it); calibrate
   proportions to named public disclosures.
3. Walk through the code in plain words (see "Walkthrough debt").
4. Not covered by tests on purpose: the live HTTP fetch and `main()` in
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

## How to run
First time on a new machine:
```
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```
Checks:
```
pytest
ruff check .
ruff format --check .
```
Whole pipeline in one command (synthetic, offline; exit 0 clean, 1 stopped, 2 findings):
```
riskplatform-run                                 # or: python -m riskplatform.pipeline / make run
$env:RISKPLATFORM_LOG_LEVEL = "DEBUG"            # optional: more detail (default INFO)
```
Coverage report: `pytest --cov=riskplatform --cov-branch --cov-report=term-missing`

Stage by stage (the same functions the pipeline calls):
```
python -m riskplatform.ingestion.generate        # data/raw/
python -m riskplatform.validation.check_raw      # independent checks of the generator output
python -m riskplatform.quality.run               # data/validated/ + data/quality_runs/<run>/
python -m riskplatform.metrics.run               # data/metrics/<run>/ (Basel and UK PRA)
python -m riskplatform.irrbb.run                 # data/irrbb/<run>/ (exit 1 if an outlier)
python -m riskplatform.quality.demo              # error-injection demo (data/demo/)
```
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
Paste this file and say which step you are on, for example "Phase 6, step 6.1".
