"""Row-level validation: split one raw file's table into valid rows and quarantined records.

Order (see docs/data_quality.md):
1. Keep each row's position in its source file, so quarantined records trace back to it.
2. Missing or extra columns are file-level problems: raise FileLevelError (the run stops).
3. Type pass: rows with a value that cannot be converted, or an empty value, are quarantined
   first, so one bad value cannot cause follow-on errors that have no row attached.
4. Contract pass: every remaining failure with a row is quarantined; one without a row is
   file-level. Multi-column rules and duplicate keys are recorded once per row, not per column.
"""

from dataclasses import dataclass

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

from riskplatform.schemas import quarantine_schema

QUARANTINE_COLUMNS = list(quarantine_schema().columns)


class FileLevelError(Exception):
    """A problem that cannot be pinned to rows, so the whole run must stop."""

    def __init__(self, table: str, source_file: str, problems: list[str]):
        self.table = table
        self.source_file = source_file
        self.problems = problems
        super().__init__(f"{table} {source_file}: " + "; ".join(problems))


@dataclass(frozen=True)
class RowValidation:
    valid: pd.DataFrame  # rows that passed every check, with contract types
    quarantine: pd.DataFrame  # one row per failed record per failed check


def _type_schema(schema: pa.DataFrameSchema) -> pa.DataFrameSchema:
    """The same columns and types as schema, with no value checks: conversion and nulls only."""
    columns = {
        name: pa.Column(column.dtype, nullable=column.nullable, coerce=True)
        for name, column in schema.columns.items()
    }
    return pa.DataFrameSchema(columns, strict=True)


def _failures(schema: pa.DataFrameSchema, frame: pd.DataFrame) -> pd.DataFrame:
    """Every failure pandera finds (check, column, failure_case, index); empty if none."""
    try:
        schema.validate(frame, lazy=True)
    except SchemaErrors as errors:
        return errors.failure_cases
    return pd.DataFrame(columns=["schema_context", "column", "check", "failure_case", "index"])


def _row_failures(failures: pd.DataFrame, table: str, source_file: str) -> pd.DataFrame:
    """Keep failures that belong to a row, one per (row, check, column); raise for the rest."""
    no_row = failures[failures["index"].isna()]
    if not no_row.empty:
        problems = [f"{row.check} ({row.failure_case})" for row in no_row.itertuples()]
        raise FileLevelError(table, source_file, problems)
    rows = failures.copy()
    rows["index"] = rows["index"].astype(int)
    # Checks on the whole table (multi-column rules, duplicate keys) are reported once per
    # column; they belong to the row, so record them once, with no column or value.
    whole_row = rows["schema_context"] == "DataFrameSchema"
    rows["column"] = rows["column"].where(~whole_row, None)
    rows["failure_case"] = rows["failure_case"].where(~whole_row, None)
    return rows.drop_duplicates(["index", "check", "column"])


def _quarantine_records(
    raw: pd.DataFrame,
    failures: pd.DataFrame,
    table: str,
    source_file: str,
    run_id: str,
    run_at: pd.Timestamp,
) -> pd.DataFrame:
    """Turn row failures into quarantine records, each carrying its original row as JSON."""
    row_numbers = failures["index"].to_numpy()
    originals = []  # pandas does not return an empty list for zero rows, so skip the call
    if len(row_numbers):
        lines = raw.loc[row_numbers].to_json(orient="records", lines=True, date_format="iso")
        originals = lines.splitlines()
    records = pd.DataFrame(
        {
            "run_id": run_id,
            "run_at": run_at,
            "table": table,
            "source_file": source_file,
            "row_number": row_numbers,
            "check": failures["check"].astype(str).to_numpy(),
            "column": failures["column"].to_numpy(),
            "failure_value": [None if pd.isna(v) else str(v) for v in failures["failure_case"]],
            "record": originals,
        }
    )
    return records.sort_values(["row_number", "check"], ignore_index=True)


def validate_rows(
    raw: pd.DataFrame,
    schema: pa.DataFrameSchema,
    *,
    table: str,
    source_file: str,
    run_id: str,
    run_at: pd.Timestamp,
) -> RowValidation:
    """Split one raw file's table into valid rows and quarantine records (see module notes)."""
    raw = raw.reset_index(drop=True)  # row label = position in the source file

    missing = sorted(set(schema.columns) - set(raw.columns))
    extra = sorted(set(raw.columns) - set(schema.columns))
    if missing or extra:
        problems = [f"missing columns {missing}"] if missing else []
        problems += [f"unexpected columns {extra}"] if extra else []
        raise FileLevelError(table, source_file, problems)

    type_failures = _row_failures(_failures(_type_schema(schema), raw), table, source_file)
    # A value that cannot be converted is reported twice (conversion, then type): keep one.
    type_failures = type_failures.drop_duplicates(["index", "column"])
    typed_ok = raw.drop(index=type_failures["index"].unique())

    rule_failures = _row_failures(_failures(schema, typed_ok), table, source_file)
    failures = pd.concat([type_failures, rule_failures], ignore_index=True)

    valid = schema.validate(raw.drop(index=failures["index"].unique()))
    quarantine = _quarantine_records(raw, failures, table, source_file, run_id, run_at)
    return RowValidation(
        valid=valid.reset_index(drop=True),
        quarantine=quarantine_schema().validate(quarantine[QUARANTINE_COLUMNS]),
    )
