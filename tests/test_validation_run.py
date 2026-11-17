"""riskplatform-validate, and the report's headline numbers against the code.

docs/validation_report.md quotes numbers; these tests recompute them and check the report shows
them as written. T2 and T3 run everywhere (synthetic data). T1, T4 and the whole command need
the H.8 file, which is not in Git: they run where it was downloaded, and skip elsewhere.
"""

import json
from pathlib import Path

import pytest
import yaml

from riskplatform.config import load_generator_config, load_limits_config, load_validation_config
from riskplatform.ingestion.generate import generate
from riskplatform.metrics.run import load_liquidity_regimes
from riskplatform.model_validation import run as validate
from riskplatform.model_validation.backtest import run_off_rates, t2
from riskplatform.model_validation.sensitivity import latest_groups, t3

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
REPORT = (ROOT / "docs" / "validation_report.md").read_text(encoding="utf-8")
VCFG = load_validation_config(CONFIG / "validation.yaml")
H8_SAVED = (ROOT / VCFG.h8.output_file).exists()
needs_h8 = pytest.mark.skipif(not H8_SAVED, reason="H.8 file not downloaded (not in Git)")
# The worst T1 outflow at full precision (results.json; 6.43% in the report, after the 1985
# break was excluded). The H.8 test below checks it still equals what the code computes.
REPORTED_STRESS_RATE = 0.06426785419759196


def _pct(value: float, places: int = 2) -> str:
    return f"{value:.{places}%}"


def _in_report(text: str) -> None:
    assert text in REPORT, f"{text} is not in docs/validation_report.md"


@pytest.fixture(scope="module")
def banks(tmp_path_factory):
    """The validated default bank (as the command builds it) and the stressed bank."""
    normal_cfg = load_generator_config(ROOT / VCFG.backtest.normal_config)
    stress_cfg = load_generator_config(ROOT / VCFG.backtest.stress_config)
    work = tmp_path_factory.mktemp("validated")
    positions, fx, run_id = validate.validated_bank(normal_cfg, CONFIG / "quality.yaml", work)
    stressed = generate(stress_cfg)
    return normal_cfg, stress_cfg, (positions, fx), (stressed.positions, stressed.fx), run_id


def test_the_bank_is_built_through_the_quality_layer(banks):
    normal_cfg, _, (positions, _), _, run_id = banks
    assert run_id and positions["date"].nunique() == normal_cfg.n_days
    assert positions["date"].max().date().isoformat() == "2026-10-23"  # section 6.5's date


def test_report_t2_numbers_equal_the_code(banks):
    _, stress_cfg, normal, stressed, _ = banks
    result = t2(normal, stressed, stress_cfg, VCFG.backtest, run_off_rates(CONFIG / "liquidity"))
    assert result["rating"] == "green"
    _in_report(f"Planted {_pct(result['planted'])} of BANK_UK's stable deposits")
    _in_report(f"measured {_pct(result['found']['worst'])} (from {result['found']['worst_date']})")
    _in_report(f"Worst 20-business-day outflow {_pct(max(result['normal_worst'].values()))}")


def test_report_t3_numbers_equal_the_code(banks):
    normal_cfg, _, (positions, fx), _, _ = banks
    limit = load_limits_config(CONFIG / "limits.yaml").limits["lcr"].value
    result = t3(
        latest_groups(positions, fx, normal_cfg), load_liquidity_regimes(CONFIG / "liquidity"),
        REPORTED_STRESS_RATE, limit, VCFG.sensitivity.rate_grid,
    )  # fmt: skip
    table = result["table"]
    assert result["rating"] == "green"
    lowest = table["stressed_lcr"].idxmin()
    for index, row in table.iterrows():
        regime = {"basel": "Basel", "pra": "UK PRA"}[row["regime"]]
        who = "Group" if row["who"] == "GROUP" else row["who"]
        stressed = _pct(row["stressed_lcr"], 1)
        if index == lowest:
            stressed = f"**{stressed}** (lowest)"
        _in_report(
            f"| {regime} | {who} | {_pct(row['lcr'], 1)} | {stressed} | "
            f"{_pct(row['break_even_limit'], 1)} | {_pct(row['break_even_minimum'], 1)} |"
        )


# --- Needs the H.8 file ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def full_run():
    return validate.run_validation(
        VCFG, CONFIG / "liquidity", CONFIG / "limits.yaml", CONFIG / "quality.yaml", ROOT
    )


@needs_h8
def test_report_t1_t4_numbers_and_ratings_equal_the_code(full_run):
    results = full_run["results"]
    for name, label in (("all_commercial_banks", "All commercial banks"),
                        ("small_banks", "Small banks")):  # fmt: skip
        main = results["t1"][name]["main"]
        windows = results["t1"][name]["windows"]
        _in_report(
            f"| {label} | {windows:,} | **{_pct(main['worst'])}** (from {main['worst_date']})"
        )
        _in_report(f"| {label} | {_pct(results['t4']['values'][name])} |")
    assert results["t3"]["stress_rate"] == pytest.approx(REPORTED_STRESS_RATE, rel=1e-12)
    assert results["ratings"] == {
        "t1_all_commercial_banks": "amber",
        "t1_small_banks": "green",
        "t1_all_commercial_banks_before_breaks": "amber",
        "t1_small_banks_before_breaks": "red",  # kept: the rating before the 1985 break
        "t2": "green",
        "t3": "green",
        "overall": "amber",
    }
    raw = results["t1"]["small_banks"]["raw"]
    _in_report(f"{_pct(raw['worst'])} (from {raw['worst_date']}), {raw['above_stable']} above 5%")
    _in_report("| **Overall** (worst of T1 and T3, as T2 is green) | **AMBER** | RED |")


@needs_h8
def test_the_command_writes_every_result_and_exits_2_unless_green(full_run, tmp_path, monkeypatch,
                                                              capsys):  # fmt: skip
    monkeypatch.setattr(validate, "run_validation", lambda *args, **kwargs: full_run)
    assert validate.main(["--out", str(tmp_path)]) == 2  # overall amber: needs attention
    out_dir = Path(capsys.readouterr().out.strip())
    assert {p.name for p in out_dir.iterdir()} == {"results.json", "t1_spells.csv",
                                                  "t3_sensitivity.csv",
                                                  "t5_single_bank_runs.csv"}  # fmt: skip
    saved = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    assert saved["ratings"]["overall"] == "amber"
    assert saved["h8"]["sha256"] == json.loads(
        (ROOT / VCFG.h8.provenance_file).read_text(encoding="utf-8"))["sha256"]  # fmt: skip


def test_without_the_h8_file_the_command_exits_1(tmp_path, caplog):
    raw = yaml.safe_load((CONFIG / "validation.yaml").read_text(encoding="utf-8"))
    raw["h8"]["output_file"] = str(tmp_path / "missing.parquet")
    raw["h8"]["provenance_file"] = str(tmp_path / "missing.json")
    config = tmp_path / "validation.yaml"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert validate.main(["--config", str(config), "--out", str(tmp_path / "out")]) == 1
    assert "fetch it first" in caplog.text
    assert not (tmp_path / "out").exists()


def test_report_t5_table_equals_the_code(banks):
    """Section 6.8: every row, computed from the validated bank and the documented runs."""
    from riskplatform.model_validation.sensitivity import t5

    normal_cfg, _, (positions, fx), _, _ = banks
    limit = load_limits_config(CONFIG / "limits.yaml").limits["lcr"].value
    runs = VCFG.single_bank_runs.runs
    table = t5(latest_groups(positions, fx, normal_cfg), load_liquidity_regimes(
        CONFIG / "liquidity"), runs, limit)  # fmt: skip
    for run in runs:
        rows = table[(table["bank"] == run.bank) & (table["period"] == run.period)]
        size = ("at least " if run.at_least else "") + _pct(run.outflow, 0)
        _in_report(
            f"| {run.bank} | {run.period} | {size} | {_pct(rows['lcr'].min(), 1)} | "
            f"{int(rows['below_limit'].sum())} of {len(rows)} | "
            f"{int(rows['below_minimum'].sum())} of {len(rows)} |"
        )
