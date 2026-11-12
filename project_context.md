# Project Context

**Project:** Automated Liquidity & Interest Rate Risk Monitoring Platform
**Goal:** A production-quality, end-to-end, deployed portfolio project that impresses senior
people at banks such as JPMorgan (Risk Analyst roles). Business use comes later.

## Working rules
1. Think of the project as a whole; build step by step with the big picture in mind.
2. Code must not break as the project grows (config over hardcoding, schemas, tests).
3. The author must understand and be able to explain every line. Keep a list of anything
   skipped (see "Walkthrough debt") and go through it before it goes on a resume.
4. Regulatory correctness over polish; every simplification is labelled.
5. Small steps: one change at a time, explained in plain words. Never change several files
   without saying so first.
6. Never commit secrets. `.env` is ignored and untracked; secrets live in environment variables.

## Big picture
Raw data -> Validated data (+ quarantine) -> Metrics (LCR, NSFR, IRRBB) -> Reporting.
The data contract (`src/riskplatform/schemas.py`) is the seam: real or public data can replace
the synthetic data later without changing any calculation code.
Realism plan for synthetic data: realistic shape (contract), realistic proportions (calibrate to
public disclosures, enforced by tests), exact numbers always labelled synthetic.
Calculations are verified separately from the data (published examples and hand calculations).

## Design decisions so far
- Settings live in `config/generator.yaml`, validated by `src/riskplatform/config.py`.
- Regulatory parameters (haircuts, run-off rates, caps) are NOT in the data; they go in config
  in Phase 4. There is deliberately no `lcr_class` column.
- Synthetic bank: 3 entities x 3 currencies x 13 product types (888 positions, 60 business days).
  Assets are scaled so assets = liabilities + 6% equity for every entity and currency on day one.
- Balances drift randomly; maturities count down and roll over; fixed-rate positions reprice at
  maturity, floating-rate positions on a repeating cycle.
- Rates are synthetic yield curves that move in parallel (simplification). A real-rates adapter
  (FRED / Bank of England) can be added later.
- Same seed + same config = identical data.
- `venv/` is not tracked by Git (it only works on the machine that built it); each machine
  builds its own. GitHub Actions CI (`.github/workflows/ci.yml`) runs `ruff check .` and
  `pytest` on a clean machine on every push and pull request.

## Stack plan (proposed, not yet built)
Python, Parquet, DuckDB; pandera for quality; unit-tested pure calculation functions;
GitHub Actions for tests and the daily run; Streamlit dashboard on a FastAPI service; Docker;
AWS deployment; run audit trail; optional LLM-written risk commentary (numbers computed by code).

## Status
| Phase | Status |
|---|---|
| 0. Setup | Done (CI added; venv untracked) |
| 1. Domain learning | Done (formulas get a worked-example pass in Phases 4-5) |
| 2. Synthetic data | 2.1 config, 2.2 data contract, 2.3 generator: done and pushed. 2.4 pending |
| 3. Data quality layer | Not started |
| 4. Liquidity metrics (LCR, NSFR) | Not started |
| 5. Interest rate risk (IRRBB) | Not started |
| 6-10. Testing, cloud, dashboard, validation, deploy | Not started |

## Next steps
1. Phase 2.4: data dictionary in `docs/` and an independent check of the generated files.
2. Walk through the code in plain words, one piece at a time (starting with `generator.yaml`).

## Walkthrough debt (code written but not yet explained line by line to the author)
- `config/generator.yaml` and `src/riskplatform/config.py`
- `src/riskplatform/schemas.py`
- `src/riskplatform/ingestion/generate.py` (build_book, _balance_sheet, build_positions,
  build_rates, write_outputs, main)
- `tests/` (conftest, test_config, test_schemas, test_generate)
- (`.github/workflows/ci.yml` was explained line by line when it was added.)

## How to run
First time on a new machine (builds the local venv):
```
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```
Every time:
```
.\venv\Scripts\Activate.ps1
pytest
ruff check .
python -m riskplatform.ingestion.generate
```
Data is written to `data/raw/` (ignored by Git).

## Resuming in a new chat
Paste this file and say which step you are on, for example "Phase 2.4, continuing".
