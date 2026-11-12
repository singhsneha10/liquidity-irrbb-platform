# Data Quality Layer (Phase 3): Design

> Status: design agreed; step 1 (quarantine contract) built. Steps 2-5 are pending.

## Purpose

The quality layer sits between the raw files and the metrics. **Metrics only ever read
validated data.** Every record that fails a check is kept, with the reason it failed, in a
quarantine. Nothing is silently dropped or fixed.

```
data/raw/  ->  quality run  ->  data/validated/    rows that passed every check
                            ->  data/quarantine/   rows that failed, with check and reason
                            ->  run summary        rows in / passed / quarantined, per check
```

## Two kinds of failure

| Kind | Examples | What happens |
|---|---|---|
| **Row-level**: the problem belongs to specific rows | negative balance, unknown currency, `reprice_days > residual_days`, value that cannot be read as its type, duplicate key | The row goes to quarantine; the run **continues** with the remaining rows. |
| **File-level**: the problem cannot be pinned to rows | missing or extra column, missing day, positions and rates on different dates, balance sheet does not balance | The run **stops**. Nothing is written to `data/validated/` for this run, and the run summary records why. |

**Why stop on file-level failures:** a bank risk report must not publish LCR, NSFR or IRRBB from
incomplete or structurally wrong data. A missing day or a missing column changes every total.
"No report today, with the reason" is the safe outcome, and the dashboard will show it.

**Duplicates:** if two rows share a key (`date`, `position_id`), the layer cannot tell which one
is right, so **all** copies are quarantined.

## How a run works (steps 2-4)

1. Read each raw file and give each row its **row number within its source file**, so every
   quarantined record can be traced back to the exact line it came from.
2. **Type conversion first.** Rows with a value that cannot be converted to its contract type
   (e.g. text in `balance_mn`) are quarantined before the value checks run. Otherwise one bad
   value also triggers follow-on errors with no row attached, which would wrongly look like a
   file-level failure.
3. **Contract checks** (`schemas.py`, run with pandera `lazy=True` so every failure is
   collected, not just the first). Failures with a row are row-level; failures without one are
   file-level.
4. **File-level checks** (the DuckDB checks in `riskplatform.validation.check_raw`) run on the
   rows that passed.
5. Write the outputs. Validated files are written to a temporary folder and moved into place
   only when the whole run succeeds, so a stopped run never leaves half-written data.

## Quarantine contract

Defined in `quarantine_schema()` in `src/riskplatform/schemas.py`. There is one row per failed
record per failed check. A record that breaks two rules appears twice.

| Column | Type | Rule | Meaning |
|---|---|---|---|
| `run_id` | string | | ID of the quality run that quarantined the record. |
| `run_at` | datetime (UTC) | | When that run started. |
| `table` | string | `positions` or `rates` | Which raw table the record came from. |
| `source_file` | string | | The raw file the record came from. |
| `row_number` | integer | >= 0 | Position of the record in its source file (0 = first row). |
| `check` | string | | Name of the failed check, e.g. `greater_than(0)` or `reprice_days must not exceed residual_days`. |
| `column` | string, may be empty | | The column the check is on. Empty for checks on several columns, which are recorded once per record rather than once per column. |
| `failure_value` | string, may be empty | | The offending value, as text. Empty for checks on several columns. |
| `record` | string | a JSON object | The whole original record, so it can be inspected or reprocessed without the raw file. |

Table rules: `(run_id, source_file, row_number, check, column)` is unique, no extra columns are
allowed, and values are converted to the declared types.

## Simplifications

1. The raw files are trusted to be readable Parquet. A file that cannot be opened at all is a
   file-level failure (the run stops); there is no partial reading of damaged files.
2. There is no automatic reprocessing of quarantined records. They are kept for inspection and
   for the run summary.
