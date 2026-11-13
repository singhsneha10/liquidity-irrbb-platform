"""Query layer: the latest good run as SQL views, from local data/ or from S3 (moto's fake S3).

Key property: local and S3 give the same answers, and both only ever show the run that last
published validated data.
"""

import dataclasses
import json
import shutil
from pathlib import Path

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from riskplatform.config import load_storage_config
from riskplatform.pipeline import load_inputs, run_pipeline
from riskplatform.query import (
    COMPLETE,
    TABLES,
    QueryInputError,
    connect,
    main,
    open_latest,
)
from riskplatform.storage import publish_run

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
BUCKET, REGION = "riskplatform-test", "eu-west-2"
LOCAL = load_storage_config(CONFIG / "storage.yaml", env={})
S3 = load_storage_config(
    CONFIG / "storage.yaml",
    env={"RISKPLATFORM_STORAGE_BACKEND": "s3", "RISKPLATFORM_S3_BUCKET": BUCKET},
)


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory) -> Path:
    """One whole pipeline run (2-day bank) whose results the tests query."""
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    data = tmp_path_factory.mktemp("data")
    result = run_pipeline(dataclasses.replace(loaded, generator=small), data)
    assert result.status in ("ok", "findings")
    return data


def _run_id(data_dir: Path) -> str:
    return json.loads((data_dir / "validated" / "_run.json").read_text(encoding="utf-8"))["run_id"]


# --- Local ---------------------------------------------------------------------------------------


def test_every_view_is_the_latest_runs_file(data_dir, tmp_path):
    con, run_id = open_latest("local", data_dir, tmp_path / "cache", LOCAL)
    assert run_id == _run_id(data_dir)
    assert con.execute("SELECT run_id FROM run").fetchone() == (run_id,)
    for name, stage in TABLES.items():
        expected = pd.read_parquet(data_dir / stage / run_id / f"{name}.parquet")
        assert con.execute(f"SELECT count(*) FROM {name}").fetchone() == (len(expected),), name


def test_sql_gives_the_same_answer_as_the_table(data_dir, tmp_path):
    con, run_id = open_latest("local", data_dir, tmp_path / "cache", LOCAL)
    answer = con.execute(
        "SELECT regime, lcr FROM lcr WHERE level = 'group' AND date = (SELECT max(date) FROM lcr) "
        "ORDER BY regime"
    ).df()
    lcr = pd.read_parquet(data_dir / "metrics" / run_id / "lcr.parquet")
    latest = lcr[(lcr["level"] == "group") & (lcr["date"] == lcr["date"].max())]
    assert answer["lcr"].tolist() == latest.sort_values("regime")["lcr"].tolist()


def test_no_published_run_is_a_clear_error(tmp_path):
    with pytest.raises(QueryInputError, match="no run has published validated data"):
        open_latest("local", tmp_path, tmp_path / "cache", LOCAL)


def test_a_run_without_results_is_a_clear_error(data_dir, tmp_path):
    """Published validated data, but the metrics stage never ran for it."""
    shutil.copytree(data_dir / "validated", tmp_path / "validated")
    with pytest.raises(QueryInputError, match="is missing results"):
        open_latest("local", tmp_path, tmp_path / "cache", LOCAL)


def test_run_id_is_never_pasted_into_sql_unescaped(data_dir, tmp_path):
    """A quote in a folder name or run id must not break (or change) the SQL."""
    run_id = _run_id(data_dir)
    odd = tmp_path / "it's"
    for stage in ("metrics", "irrbb"):
        shutil.copytree(data_dir / stage / run_id, odd / stage / "a'b")
    con = connect(odd, "a'b")
    assert con.execute("SELECT run_id FROM run").fetchone() == ("a'b",)
    assert con.execute("SELECT count(*) FROM lcr").fetchone()[0] > 0


# --- S3 ------------------------------------------------------------------------------------------


@pytest.fixture
def s3(monkeypatch):
    """A fake S3 with one empty bucket; dummy credentials exist only inside the test."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        yield client


def _publish(client, data_dir: Path) -> str:
    run_id = _run_id(data_dir)
    publish_run(client, S3, data_dir, run_id, "findings", 2, stages=[])
    return run_id


def test_s3_gives_the_same_answers_as_local(s3, data_dir, tmp_path):
    run_id = _publish(s3, data_dir)
    remote, remote_id = open_latest("s3", tmp_path / "unused", tmp_path / "cache", S3, client=s3)
    local, _ = open_latest("local", data_dir, tmp_path / "unused", LOCAL)
    assert remote_id == run_id
    for name in TABLES:
        pd.testing.assert_frame_equal(
            remote.execute(f"SELECT * FROM {name}").df(),
            local.execute(f"SELECT * FROM {name}").df(),
        )
    assert (tmp_path / "cache" / COMPLETE / run_id).exists()


def test_a_downloaded_run_is_not_downloaded_again(s3, data_dir, tmp_path, monkeypatch):
    _publish(s3, data_dir)
    open_latest("s3", tmp_path, tmp_path / "cache", S3, client=s3)

    def refuse(*args, **kwargs):
        raise AssertionError("downloaded a second time")

    monkeypatch.setattr(s3, "download_file", refuse)
    con, _ = open_latest("s3", tmp_path, tmp_path / "cache", S3, client=s3)
    assert con.execute("SELECT count(*) FROM risk").fetchone()[0] > 0


def test_an_interrupted_download_is_repeated(s3, data_dir, tmp_path):
    run_id = _publish(s3, data_dir)
    partial = tmp_path / "cache" / "metrics" / run_id
    partial.mkdir(parents=True)
    (partial / "lcr.parquet").write_bytes(b"half a file")  # no .complete marker
    con, _ = open_latest("s3", tmp_path, tmp_path / "cache", S3, client=s3)
    assert con.execute("SELECT count(*) FROM lcr").fetchone()[0] > 0


def test_s3_without_a_published_run_is_a_clear_error(s3, tmp_path):
    with pytest.raises(QueryInputError, match="latest.json not found"):
        open_latest("s3", tmp_path, tmp_path / "cache", S3, client=s3)


def test_s3_without_a_bucket_is_a_clear_error(tmp_path):
    with pytest.raises(QueryInputError, match="RISKPLATFORM_S3_BUCKET"):
        open_latest("s3", tmp_path, tmp_path / "cache", LOCAL)


# --- Command -------------------------------------------------------------------------------------


def _args(data_dir: Path, tmp_path: Path, *rest: str) -> list[str]:
    storage = str(CONFIG / "storage.yaml")
    return ["--source", "local", "--storage", storage, "--data", str(data_dir),
            "--cache", str(tmp_path / "cache"), *rest]  # fmt: skip


def test_command_prints_the_answer(data_dir, tmp_path, capsys):
    assert (
        main(
            _args(
                data_dir, tmp_path, "SELECT count(*) AS days FROM (SELECT DISTINCT date FROM lcr)"
            )
        )
        == 0
    )
    assert capsys.readouterr().out.split() == ["days", "2"]


def test_command_lists_every_view(data_dir, tmp_path, capsys):
    assert main(_args(data_dir, tmp_path, "--tables")) == 0
    listed = [line.split(":")[0] for line in capsys.readouterr().out.splitlines()]
    assert listed == [*TABLES, "run"]


def test_bad_sql_exits_1_and_says_why(data_dir, tmp_path, caplog):
    assert main(_args(data_dir, tmp_path, "SELECT no_such_column FROM lcr")) == 1
    assert "no_such_column" in caplog.text


def test_command_without_a_query_is_a_usage_error(data_dir, tmp_path):
    with pytest.raises(SystemExit):
        main(_args(data_dir, tmp_path))
