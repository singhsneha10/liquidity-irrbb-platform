"""Tableau extracts: every view as a clean CSV, the long ratio table, and the fixed latest/ folder."""

import csv
import dataclasses
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from riskplatform.extracts import (
    LATEST,
    VIEWS,
    _clean,
    extract_tables,
    main,
    write_extracts,
)
from riskplatform.pipeline import load_inputs, run_pipeline
from riskplatform.query import connect

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One whole pipeline run (2-day bank): its extracts stage has written the CSVs."""
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    data_dir = tmp_path_factory.mktemp("extracts")
    result = run_pipeline(dataclasses.replace(loaded, generator=small), data_dir)
    stage = next(stage for stage in result.stages if stage.name == "extracts")
    assert stage.status == "ok"
    return data_dir, result.run_id


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_the_pipeline_writes_every_extract_and_a_manifest(run):
    data_dir, run_id = run
    out = data_dir / "extracts" / run_id
    expected = {f"{name}.csv" for name in [*VIEWS, "liquidity_ratios"]}
    assert {p.name for p in out.glob("*.csv")} == expected
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == run_id and DATE.match(manifest["as_of"])
    for name, info in manifest["files"].items():
        rows = _rows(out / name)
        assert len(rows) == info["rows"], name
        with (out / name).open(encoding="utf-8") as handle:
            assert handle.readline().strip().split(",") == info["columns"], name


def test_each_extract_has_every_row_of_its_view(run):
    data_dir, run_id = run
    con = connect(data_dir, run_id)
    out = data_dir / "extracts" / run_id
    for name, view in VIEWS.items():
        (count,) = con.execute(f"SELECT count(*) FROM {view}").fetchone()
        assert len(_rows(out / f"{name}.csv")) == count, name


def test_values_are_the_pipelines_own(run):
    data_dir, run_id = run
    lcr = pd.read_csv(data_dir / "extracts" / run_id / "lcr.csv")
    source = pd.read_parquet(data_dir / "metrics" / run_id / "lcr.parquet")
    assert np.allclose(sorted(lcr["lcr"].dropna()), sorted(source["lcr"].dropna()))
    risk = pd.read_csv(data_dir / "extracts" / run_id / "irrbb_risk.csv")
    flagged = risk[risk["outlier"].eq(True)]  # empty per currency: the test is per entity / group
    assert "BANK_UK" in set(flagged["who"])


def test_cells_are_tableau_friendly(run):
    """Plain dates, no 'inf' / 'nan' / 'None' text, and GROUP wherever the entity is blank."""
    data_dir, run_id = run
    for path in (data_dir / "extracts" / run_id).glob("*.csv"):
        rows = _rows(path)
        for row in rows:
            for column, value in row.items():
                assert value.lower() not in {"inf", "-inf", "nan", "none", "nat"}, (
                    path.name,
                    column,
                )
                if column.endswith("date") and value:
                    assert DATE.match(value), (path.name, column, value)
            if "who" in row:
                assert row["who"] == (row["entity"] or "GROUP"), path.name


def test_liquidity_ratios_stack_lcr_and_nsfr_with_their_lines(run):
    data_dir, run_id = run
    out = data_dir / "extracts" / run_id
    ratios = pd.read_csv(out / "liquidity_ratios.csv")
    assert len(ratios) == len(pd.read_csv(out / "lcr.csv")) + len(pd.read_csv(out / "nsfr.csv"))
    assert set(ratios["metric"]) == {"LCR", "NSFR"}
    assert set(ratios["regulatory_minimum"]) == {1.0}
    limited = ratios[ratios["level"].isin(["entity", "group"])]
    assert set(limited.loc[limited["metric"] == "LCR", "internal_limit"]) == {1.10}
    assert set(limited.loc[limited["metric"] == "NSFR", "internal_limit"]) == {1.05}
    per_currency = ratios[ratios["level"] == "entity_currency"]
    assert len(per_currency) and per_currency["internal_limit"].isna().all()  # no limit there


def test_infinities_are_written_as_empty_cells(tmp_path):
    table = _clean(pd.DataFrame({"who": ["A", "B"], "utilisation": [0.5, np.inf]}))
    table.to_csv(tmp_path / "t.csv", index=False)
    assert _rows(tmp_path / "t.csv")[1]["utilisation"] == ""
    assert pd.read_csv(tmp_path / "t.csv")["utilisation"].dtype == float  # still a number


def test_latest_is_replaced_whole_by_the_newest_run(run, tmp_path):
    data_dir, run_id = run
    con = connect(data_dir, run_id)
    write_extracts(con, "run-a", tmp_path)
    (tmp_path / LATEST / "stale.csv").write_text("left over\n", encoding="utf-8")
    write_extracts(con, "run-b", tmp_path)
    latest = tmp_path / LATEST
    assert json.loads((latest / "manifest.json").read_text(encoding="utf-8"))["run_id"] == "run-b"
    assert not (latest / "stale.csv").exists()
    assert (tmp_path / "run-a" / "manifest.json").exists()  # earlier runs are kept


def test_the_command_writes_the_latest_local_run(run, tmp_path, capsys):
    data_dir, run_id = run
    args = ["--source", "local", "--data", str(data_dir), "--out", str(tmp_path / "out")]
    assert main(args) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "out" / LATEST)
    manifest = json.loads((tmp_path / "out" / run_id / "manifest.json").read_text("utf-8"))
    assert manifest["run_id"] == run_id
    empty = ["--source", "local", "--data", str(tmp_path / "none"), "--out", str(tmp_path / "x")]
    assert main(empty) == 1  # no published run: a clear error, nothing written
    assert not (tmp_path / "x").exists()


def test_extract_tables_needs_no_files(run):
    """The tables come from the views alone, so the same code serves local and S3 runs."""
    data_dir, run_id = run
    tables = extract_tables(connect(data_dir, run_id))
    assert set(tables) == {*VIEWS, "liquidity_ratios"}
