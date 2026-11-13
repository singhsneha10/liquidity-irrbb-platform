"""Publishing pipeline results to S3 (AWS object storage), each under its run id.

S3 cannot rename a folder in one step, which is how data/validated/ is published safely on local
disk. So S3 uses the standard pattern for batch jobs:

1. The run works on local disk, exactly as without S3 (in AWS, the container's own disk).
2. Afterwards every output is copied to S3 under its run id. Nothing is ever overwritten, so the
   bucket is also the audit trail.
3. A status record goes to runs/<run_id>.json for every run.
4. Only for a run that published validated data, latest.json is written **last**. Readers (the
   dashboard, later) follow latest.json, so they never see a half-uploaded or a failed run.

Layout under s3://<bucket>/<prefix>/:
    raw/<run_id>/  quality_runs/<run_id>/  validated/<run_id>/  metrics/<run_id>/
    irrbb/<run_id>/  logs/<log file>  runs/<run_id>.json  latest.json

Credentials are never in code or config: boto3 finds them itself (an AWS profile on a PC, the
task's IAM role in AWS).
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import boto3

from riskplatform.config import StorageConfig

logger = logging.getLogger(__name__)

MANIFEST = "_run.json"  # written by the quality run into data/validated/
PUBLISHED_STATUSES = ("ok", "findings")  # runs whose results are good to read


def make_client(cfg: StorageConfig):
    """An S3 client for the configured region (credentials found by boto3)."""
    return boto3.client("s3", region_name=cfg.s3.region)


def upload_tree(client, bucket: str, local_dir: Path, key_prefix: str) -> int:
    """Copy every file under local_dir to S3 under key_prefix, keeping relative paths."""
    count = 0
    for path in sorted(p for p in Path(local_dir).rglob("*") if p.is_file()):
        key = f"{key_prefix}/{path.relative_to(local_dir).as_posix()}"
        client.upload_file(str(path), bucket, key)
        count += 1
    return count


def put_json(client, bucket: str, key: str, payload: dict) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def _published_run_id(validated_dir: Path) -> str | None:
    manifest = validated_dir / MANIFEST
    if not manifest.exists():
        return None
    return json.loads(manifest.read_text(encoding="utf-8")).get("run_id")


def publish_run(
    client,
    cfg: StorageConfig,
    data_dir: str | Path,
    run_id: str,
    status: str,
    exit_code: int,
    stages: list[dict],
    log_file: Path | None = None,
) -> dict:
    """Copy one pipeline run's outputs to S3 (see the module notes) and return the record."""
    data_dir = Path(data_dir)
    bucket, prefix = cfg.s3.bucket, cfg.s3.prefix.strip("/")
    uploaded: dict[str, int] = {}

    def upload(local: Path, name: str) -> None:
        if local.exists():
            uploaded[name] = upload_tree(client, bucket, local, f"{prefix}/{name}/{run_id}")

    upload(data_dir / "raw", "raw")
    upload(data_dir / "quality_runs" / run_id, "quality_runs")
    published = _published_run_id(data_dir / "validated") == run_id
    if published:  # data/validated/ holds this run's data, not an earlier run's
        upload(data_dir / "validated", "validated")
    upload(data_dir / "metrics" / run_id, "metrics")
    upload(data_dir / "irrbb" / run_id, "irrbb")
    if log_file is not None and Path(log_file).exists():
        client.upload_file(str(log_file), bucket, f"{prefix}/logs/{Path(log_file).name}")
        uploaded["logs"] = 1

    record = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "published": published,
        "finished_at": datetime.now(UTC).isoformat(),
        "stages": stages,
        "files": uploaded,
        "prefixes": {name: f"{prefix}/{name}/{run_id}/" for name in uploaded if name != "logs"},
    }
    put_json(client, bucket, f"{prefix}/runs/{run_id}.json", record)
    if published and status in PUBLISHED_STATUSES:
        put_json(client, bucket, f"{prefix}/latest.json", record)  # last, so readers see it whole
    logger.info(
        "Published run %s to s3://%s/%s/ (%d files%s)",
        run_id,
        bucket,
        prefix,
        sum(uploaded.values()),
        "; latest.json updated" if published and status in PUBLISHED_STATUSES else "",
    )
    return record
