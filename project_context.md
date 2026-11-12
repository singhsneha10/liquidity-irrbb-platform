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
  -> IRRBB (Phase 5) -> reporting / dashboard (later)
```
The data contracts (`src/riskplatform/schemas.py`) are the seam: real data can replace the
synthetic data without changing calculation code. Calculations are verified separately from the
data: hand-worked examples written before the code, and proofs against published formulas.

## Target stack
- Python 3.11+ (venv uses 3.12), pandas, numpy, pydantic v2, pandera, pytest, ruff
- Storage and queries: Parquet, DuckDB (2.0); later S3
- Data quality layer with quarantine (done)
- LCR, NSFR (done) and IRRBB (NII and EVE, six Basel shocks) as pure functions, with
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
  (fetch sources), `config/quality.yaml` (outlier thresholds), `config/liquidity/basel.yaml`,
  `pra.yaml` (rulebooks), `ladder.yaml` (buckets).
- Synthetic bank: 3 entities x 3 currencies x 13 product types (900 positions, 60 business days,
  Mon-Fri). Assets = liabilities + 6% equity per entity and currency on day one.
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
- Metrics: three levels (entity-currency monitoring only; entity in home currency; group in GBP).
  Group figures assume free transfer between entities (labelled).
- CI (`.github/workflows/ci.yml`) runs ruff check, ruff format --check and pytest on every push.

## Status
| Phase | Status |
|---|---|
| 0. Setup | Done (CI incl. format check; venv untracked) |
| 1. Domain learning | Done; the formula cheat sheet (`docs/methodology.md`) was deliberately skipped |
| 2. Synthetic data | Done: generator, data dictionary + sync test, DuckDB independent checks, calendar-day maturities, curve-linked rates, real curves (FRED/ECB/BoE), FX (synthetic + real BoE) |
| 3. Data quality layer | Done: quarantine, reconciliation to control totals, STOP/WARNING checks, outliers, quality run, breaks report, error-injection demo |
| 4. Liquidity metrics | Done: Basel + UK PRA rulebooks (sourced), worked example, LCR, NSFR, maturity ladder, metrics run, methodology doc |
| 5. Interest rate risk (IRRBB) | **Next** |
| 6-10. Hardening, cloud + orchestration, alerts + Tableau, model validation, deploy | Not started |

## Next steps
1. Phase 5 (IRRBB): rulebook parameters sourced first (six Basel shocks, both regimes), a
   hand-worked example, then repricing gap, delta NII and delta EVE.
2. Generator realism: calibrate proportions to public disclosures (small foreign-currency books
   have unrealistically high LCRs).
3. Walk through the code in plain words (see "Walkthrough debt").

## Assumptions to review before this goes on a resume
- `docs/liquidity_methodology.md`, "Assumptions and simplifications" (19 items).
- `docs/data_dictionary.md`, "Known simplifications and limitations" and the licence notes.
- `docs/data_quality.md`, "Simplifications and limitations".
- Outlier thresholds in `config/quality.yaml` (review triggers, chosen by judgement).

## Walkthrough debt (code written but not yet explained line by line to the author)
Explained when written, but worth a second pass: everything under `src/riskplatform/` and
`tests/`. Most important first: `metrics/lcr.py`, `metrics/nsfr.py`, `metrics/run.py`,
`quality/run.py`, `quality/rows.py`, `ingestion/generate.py`, `validation/check_raw.py`.

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
Pipeline (synthetic, offline):
```
python -m riskplatform.ingestion.generate        # data/raw/
python -m riskplatform.validation.check_raw      # independent checks of the generator output
python -m riskplatform.quality.run               # data/validated/ + data/quality_runs/<run>/
python -m riskplatform.metrics.run               # data/metrics/<run>/ (Basel and UK PRA)
python -m riskplatform.quality.demo              # error-injection demo (data/demo/)
```
Real rates (needs internet once):
```
python -m riskplatform.ingestion.market_rates    # saves data/external/ (curves + FX)
python -m riskplatform.ingestion.generate --config config/generator_real.yaml
python -m riskplatform.quality.run --config config/generator_real.yaml
python -m riskplatform.metrics.run --config config/generator_real.yaml
```
All data is written under `data/` (ignored by Git).

## Resuming in a new chat
Paste this file and say which step you are on, for example "Phase 5, step 5.1".
