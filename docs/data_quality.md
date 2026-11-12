# Data Quality Layer (Phase 3)

> Status: **complete**. Every rule below is enforced in code and covered by tests.

## Purpose

The quality layer sits between the raw files and the metrics. **Metrics only ever read
validated data.** Every record that fails a check is kept, with the reason, in a quarantine.
Nothing is silently dropped or fixed.

```
data/raw/  ->  quality run  ->  data/validated/            latest good data (+ _run.json)
                            ->  data/quality_runs/<run>/   breaks report + quarantine, every run
```

## Commands

```
python -m riskplatform.quality.run                                       # synthetic data
python -m riskplatform.quality.run --config config/generator_real.yaml   # real-curve data
python -m riskplatform.quality.demo                                      # error-injection demo
```

`run` exits with 0 when the data was published and 1 when the run stopped, so a scheduler can
react. Settings: `config/generator.yaml` (the contract's allowed values) and
`config/quality.yaml` (outlier thresholds).

## Stages of a run

| # | Stage | Code | On a problem |
|---|---|---|---|
| 1 | Read every raw file and the source's control totals | `quality/run.py` | Unreadable file, no files or no control totals: **stop** |
| 2 | Row validation, file by file, against the data contract | `quality/rows.py` | Row problems: **quarantine** and continue; missing or extra column: **stop** |
| 3 | Reconciliation to the control totals | `quality/reconcile.py` | Data lost or changed after sending: **stop** |
| 4 | File-level checks on the validated rows | `quality/files.py` | STOP checks: **stop**; WARNING checks: **warn** |
| 5 | Outlier checks on the validated rows | `quality/outliers.py` | **Warn** only |
| 6 | Publish and report | `quality/run.py`, `quality/report.py` | (see Outputs) |

## The rules

### Row level (quarantine; the run continues)

These come from the data contract (`schemas.py`), checked with pandera `lazy=True`, so every
failure is collected, not just the first.

- **Type pass first.** A value that cannot be converted to its type (e.g. text in
  `balance_mn`) or an empty value is quarantined before the value checks run. Otherwise one
  bad value causes follow-on errors with no row attached, which would wrongly look like a
  file-level failure.
- **Value and cross-column rules:** allowed values, ranges, `reprice_days <= residual_days`,
  fixed-rate positions repricing at maturity, side matching category.
- **Duplicates:** all copies of a duplicated key are quarantined, because the layer cannot tell
  which one is right.
- Rules on several columns are recorded **once per row**, with no column, rather than once per
  column.

### Reconciliation (stop)

The source sends `control_totals.json` with its files (`riskplatform.controls`). It holds each
file's row count and, for positions, `balance_mn` per entity, currency and side. Sender and
receiver calculate totals with the same function.

- **Stop** if a file was sent but did not arrive, or arrived but was not sent, or if its row
  count or balance totals differ from what was sent. Data was lost or changed on the way.
- **Explain, do not stop:** the gap between arrived and validated is the quarantine. It is
  reported per file in rows and in `balance_mn`.
- Totals describe the files **as sent**. If the source sends a bad value, its totals include
  it, reconciliation agrees, and row validation quarantines the row.

### File level (on validated rows)

| Group | Checks | Effect |
|---|---|---|
| STOP | files hold only their own date; expected number of days; positions and rates cover the same dates | The run stops |
| WARNING | attributes never change; countdowns fall by calendar days; rate changes only at a reset | Listed in the breaks report |
| Generator only | every position every day; balance sheet balances on day one; curve shape and parallel moves | Not run here; they verify the synthetic generator (`riskplatform.validation.check_raw`) |

"Every position every day" is deliberately not a quality rule. It would turn every quarantined
row into a stop, and real books gain and lose positions daily. Missing rows are caught by
reconciliation instead. The WARNING checks were tested to give correct results when rows are
missing.

### Outliers (warn; never quarantined)

Valid but unusual values for an analyst to review. The thresholds in `config/quality.yaml` are
review triggers, **not regulatory limits**.

| Check | Flags | Threshold |
|---|---|---|
| `balance_jump` | balance moving more than X since the previous business day | 25% |
| `rate_gap_to_curve` | rate more than X from its curve at its remaining maturity | 5 pp |
| `concentration_share` | one position more than X of its entity/currency/side total (balance sheet only; central bank reserves excluded) | 25% |
| `curve_move` | a curve point moving more than X in one day | 50 bp |

Normal generated data (synthetic and real-curve) produces no outlier warnings.

## Outputs

- **`data/validated/`**: the validated files from the latest successful run, plus `_run.json`
  (run ID, time, raw folder, rates source, path to the breaks report). It is written to a
  staging folder and swapped into place only when the whole run succeeds. A stopped run leaves
  it exactly as it was. Later phases check `_run.json`, so they always know which run their
  input came from.
- **`data/quality_runs/<run_id>/`**: written for **every** run, published or stopped. This is
  the audit trail. It holds:
  - `breaks_report.md`: a one-page summary for people;
  - `breaks_report.json`: everything, for machines;
  - `quarantine.parquet`: every quarantined record, in the quarantine contract below.

Run IDs look like `20261112T090157Z-da2535`. They sort by time (UTC) and are unique even for
two runs in the same second.

### Breaks report contents

- Status (PUBLISHED or STOPPED) and the stop reasons.
- Rows arrived and validated per table, and rows quarantined.
- Quarantine by check (table, check, column, records).
- Reconciliation: quarantined amounts per file, entity, currency and side.
- File-level STOP and WARNING checks, each passed or failed.
- Outlier warnings.

A section whose stage never ran says "Not run". That separates "checked and fine" from
"not checked".

## Error-injection demo

`python -m riskplatform.quality.demo` works only under `data/demo/`, and it refuses to clear any
folder it did not create.

1. **The source sends bad data.** Seven known errors are planted in one day's file: negative
   balance, unknown currency, empty entity, text in a number column, duplicate row, reset after
   maturity, and a 50% overnight balance jump. The source's control totals include them. Each
   error must be caught by its expected check. The six bad values are quarantined, the jump is
   only a warning, and the run publishes.
2. **Data lost after sending.** A row disappears but the control totals still count it. The
   run must stop.

It prints a scorecard (CAUGHT or MISSED per error) and exits with 0 only if everything behaved
as expected, so it doubles as an end-to-end test.

## Quarantine contract

Defined in `quarantine_schema()` in `src/riskplatform/schemas.py`. There is one row per failed
record per failed check. A record that breaks two rules appears twice.

| Column | Type | Rule | Meaning |
|---|---|---|---|
| `run_id` | string | | ID of the quality run that quarantined the record. |
| `run_at` | datetime (UTC) | | When that run started. |
| `table` | string | `positions` or `rates` | Which raw table the record came from. |
| `source_file` | string | | The raw file, relative to the raw folder. |
| `row_number` | integer | >= 0 | Position of the record in its source file (0 = first row). |
| `check` | string | | Name of the failed check, e.g. `greater_than(0)`. |
| `column` | string, may be empty | | The column checked. Empty for checks on several columns. |
| `failure_value` | string, may be empty | | The offending value, as text. Empty for checks on several columns and for empty values. |
| `record` | string | a JSON object | The whole original record, so it can be inspected without the raw file. |

Table rules: `(run_id, source_file, row_number, check, column)` is unique, no extra columns are
allowed, and values are converted to the declared types.

## Simplifications and limitations

1. **One run checks the whole history** (all days at once), not one new day at a time.
2. **The publish swap is not instant.** For a moment `data/validated/` is renamed away before
   the new one takes its place. On Windows, a rename refused because antivirus or indexing
   briefly locks new files is retried for up to 10 seconds.
3. **No automatic reprocessing** of quarantined records. They are kept for inspection.
4. **Concentration is a rough signal.** Positions are aggregated lines, not counterparties, so
   it is not a large-exposures or funding-concentration measure.
5. **Reconciliation is per entity, currency and side**, not per position. A change that leaves
   those totals unchanged (e.g. two balances swapped within one group) is not caught by
   reconciliation, though row checks and outliers may still flag it.
6. **Unreadable files stop the run.** There is no partial reading of damaged files.
