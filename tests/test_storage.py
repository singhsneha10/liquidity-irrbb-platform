"""Publishing to S3, tested against moto's fake S3 (no AWS account, no cost, no real credentials).

Key property: readers follow latest.json, which only ever points at a run that published
validated data, and is written last.
"""

import dataclasses
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from riskplatform import pipeline
from riskplatform.config import StorageConfig, load_storage_config
from riskplatform.pipeline import load_inputs, main, run_pipeline
from riskplatform.storage import publish_run, upload_tree

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
BUCKET, REGION = "riskplatform-test", "eu-west-2"


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


@pytest.fixture
def storage() -> StorageConfig:
    env = {"RISKPLATFORM_STORAGE_BACKEND": "s3", "RISKPLATFORM_S3_BUCKET": BUCKET}
    return load_storage_config(CONFIG / "storage.yaml", env=env)


def _keys(client, prefix="riskplatform/") -> set[str]:
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix)
    return {item["Key"] for page in pages for item in page.get("Contents", [])}


def _json(client, key) -> dict:
    return json.loads(client.get_object(Bucket=BUCKET, Key=key)["Body"].read())


# --- Config ------------------------------------------------------------------------------------


def test_local_is_the_default():
    cfg = load_storage_config(CONFIG / "storage.yaml", env={})
    assert (cfg.backend, cfg.s3.bucket, cfg.s3.region) == ("local", None, "eu-west-2")


def test_environment_switches_to_s3_and_sets_the_bucket(storage):
    assert (storage.backend, storage.s3.bucket) == ("s3", BUCKET)


def test_s3_without_a_bucket_is_refused():
    with pytest.raises(ValidationError, match="RISKPLATFORM_S3_BUCKET"):
        load_storage_config(CONFIG / "storage.yaml", env={"RISKPLATFORM_STORAGE_BACKEND": "s3"})


# --- Uploading ---------------------------------------------------------------------------------


def test_upload_tree_keeps_relative_paths(s3, tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "top.txt").write_text("1")
    (tmp_path / "a" / "b" / "deep.txt").write_text("2")
    assert upload_tree(s3, BUCKET, tmp_path, "p/x") == 2
    assert _keys(s3, "p/") == {"p/x/top.txt", "p/x/a/b/deep.txt"}


@pytest.fixture(scope="module")
def inputs():
    loaded = load_inputs(
        CONFIG / "generator.yaml", CONFIG / "quality.yaml", CONFIG / "liquidity", CONFIG / "irrbb"
    )
    small = loaded.generator.model_validate({**loaded.generator.model_dump(), "n_days": 2})
    return dataclasses.replace(loaded, generator=small)


def _run_and_publish(s3, storage, inputs, data_dir):
    result = run_pipeline(inputs, data_dir)
    stages = [{"name": s.name, "status": s.status} for s in result.stages]
    record = publish_run(
        s3, storage, data_dir, result.run_id, result.status, result.exit_code, stages
    )
    return result, record


def test_published_run_is_uploaded_under_its_id_and_becomes_latest(s3, storage, inputs, tmp_path):
    result, record = _run_and_publish(s3, storage, inputs, tmp_path)
    run_id = result.run_id
    keys = _keys(s3)
    for folder in ("raw", "quality_runs", "validated", "metrics", "irrbb"):
        assert any(k.startswith(f"riskplatform/{folder}/{run_id}/") for k in keys), folder
    assert f"riskplatform/validated/{run_id}/_run.json" in keys
    assert _json(s3, f"riskplatform/runs/{run_id}.json")["run_id"] == run_id
    latest = _json(s3, "riskplatform/latest.json")
    assert (latest["run_id"], latest["published"]) == (run_id, True)
    assert record["files"]["validated"] > 0


def test_stopped_run_leaves_latest_pointing_at_the_last_good_run(
    s3, storage, inputs, tmp_path, monkeypatch
):
    good, _ = _run_and_publish(s3, storage, inputs, tmp_path)

    # The next run's source sends no control totals, so its quality run stops.
    write = pipeline.write_outputs

    def write_without_control_totals(raw, raw_dir):
        write(raw, raw_dir)
        (Path(raw_dir) / "control_totals.json").unlink()

    monkeypatch.setattr(pipeline, "write_outputs", write_without_control_totals)
    stopped, record = _run_and_publish(s3, storage, inputs, tmp_path)

    assert stopped.status == "stopped" and stopped.run_id != good.run_id
    keys = _keys(s3)
    assert any(k.startswith(f"riskplatform/quality_runs/{stopped.run_id}/") for k in keys)
    assert not any(k.startswith(f"riskplatform/validated/{stopped.run_id}/") for k in keys)
    assert _json(s3, f"riskplatform/runs/{stopped.run_id}.json")["status"] == "stopped"
    assert _json(s3, "riskplatform/latest.json")["run_id"] == good.run_id
    assert record["published"] is False


def test_a_second_run_does_not_overwrite_the_first(s3, storage, inputs, tmp_path):
    first, _ = _run_and_publish(s3, storage, inputs, tmp_path / "one")
    second, _ = _run_and_publish(s3, storage, inputs, tmp_path / "two")
    keys = _keys(s3)
    for run_id in (first.run_id, second.run_id):
        assert f"riskplatform/validated/{run_id}/_run.json" in keys
    assert _json(s3, "riskplatform/latest.json")["run_id"] == second.run_id


def test_pipeline_command_publishes_when_the_backend_is_s3(s3, tmp_path, monkeypatch):
    monkeypatch.setenv("RISKPLATFORM_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("RISKPLATFORM_S3_BUCKET", BUCKET)
    small = tmp_path / "small.yaml"
    small.write_text(f"base: {(CONFIG / 'generator.yaml').as_posix()}\nn_days: 2\n")
    code = main(["--config", str(small), "--data", str(tmp_path / "data")])
    latest = _json(s3, "riskplatform/latest.json")
    assert code in (0, 2) and latest["exit_code"] == code
    assert any(k.startswith("riskplatform/logs/pipeline-") for k in _keys(s3))
