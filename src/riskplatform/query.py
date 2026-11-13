"""Query the published results with SQL (DuckDB): always the latest good run, local or from S3.

Run with:  python -m riskplatform.query "SELECT entity, regime, lcr FROM lcr WHERE level = 'entity'"
           python -m riskplatform.query --source s3 "SELECT * FROM risk WHERE outlier"
           python -m riskplatform.query --tables          (the views and their columns)

Readers only ever see the latest run that published validated data, as riskplatform.storage
promises:
- local: the run named in data/validated/_run.json (written by the quality run when it publishes);
- s3:    the run named in latest.json (written last, so never a half-uploaded or a stopped run).

Each result table is a DuckDB view over that run's Parquet files, plus `run` (which run it is):
    lcr, nsfr, ladder           from metrics/<run_id>/
    eve, risk, nii, gap         from irrbb/<run_id>/

From S3, the run's metrics and IRRBB files (about 1 MB) are first downloaded into --cache
(default data/cache/, same layout as data/), once per run, and queried on local disk. Labelled
choice: DuckDB can read S3 directly, but only through extensions it downloads at run time; Athena
queries S3 in place, but needs a Glue catalogue and charges per query. Downloading one small run
costs nothing and uses the same credentials as publishing (boto3, never in config). Athena is
the step up when the history of many runs must be queried in place.

The source defaults to the backend in config/storage.yaml (RISKPLATFORM_STORAGE_BACKEND), so the
same command reads S3 in AWS and data/ on a PC. Results go to stdout; messages to the log.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Literal

import duckdb

from riskplatform.config import STORAGE_ENV, StorageConfig, load_storage_config
from riskplatform.logs import setup_logging
from riskplatform.storage import MANIFEST, make_client

logger = logging.getLogger(__name__)

Source = Literal["local", "s3"]
TABLES = {  # view name -> the stage folder its file is in
    "lcr": "metrics",
    "nsfr": "metrics",
    "ladder": "metrics",
    "eve": "irrbb",
    "risk": "irrbb",
    "nii": "irrbb",
    "gap": "irrbb",
}
STAGES = sorted(set(TABLES.values()))
COMPLETE = ".complete"  # cache_dir/.complete/<run_id> exists once a run is fully downloaded


class QueryInputError(Exception):
    """There is no published run to query, or its results are missing."""


def _sql_literal(text: str) -> str:
    """Text as a SQL string literal (quotes escaped), e.g. a file path or a run id."""
    return "'" + text.replace("'", "''") + "'"


def latest_local_run(data_dir: str | Path) -> str:
    """The run that last published validated data under data_dir."""
    manifest = Path(data_dir) / "validated" / MANIFEST
    if not manifest.exists():
        raise QueryInputError(
            f"{manifest} not found: no run has published validated data yet (run: riskplatform-run)"
        )
    return json.loads(manifest.read_text(encoding="utf-8"))["run_id"]


def latest_s3_run(client, cfg: StorageConfig) -> str:
    """The run named in latest.json: the last run that published validated data to S3."""
    bucket, key = cfg.s3.bucket, f"{cfg.s3.prefix.strip('/')}/latest.json"
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except client.exceptions.NoSuchKey:
        raise QueryInputError(f"s3://{bucket}/{key} not found: no run has published yet") from None
    return json.loads(body)["run_id"]


def download_run(client, cfg: StorageConfig, run_id: str, cache_dir: str | Path) -> Path:
    """Copy one run's metrics and IRRBB results from S3 into cache_dir, once; return cache_dir.

    cache_dir mirrors data/ (metrics/<run_id>/..., irrbb/<run_id>/...). A run is only marked
    complete after every file arrived, so an interrupted download is simply repeated.
    """
    cache_dir = Path(cache_dir)
    marker = cache_dir / COMPLETE / run_id
    if marker.exists():
        logger.info("Run %s already downloaded to %s", run_id, cache_dir)
        return cache_dir
    bucket, prefix = cfg.s3.bucket, cfg.s3.prefix.strip("/")
    pages = client.get_paginator("list_objects_v2")
    for stage in STAGES:
        stage_prefix = f"{prefix}/{stage}/{run_id}/"
        keys = [
            item["Key"]
            for page in pages.paginate(Bucket=bucket, Prefix=stage_prefix)
            for item in page.get("Contents", [])
        ]
        if not keys:
            raise QueryInputError(f"s3://{bucket}/{stage_prefix} is empty: run {run_id} has no "
                                  f"{stage} results")  # fmt: skip
        for key in keys:
            target = cache_dir / stage / run_id / key.removeprefix(stage_prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"downloaded from s3://{bucket}/{prefix}/\n", encoding="utf-8")
    logger.info("Downloaded run %s from s3://%s/%s/ to %s", run_id, bucket, prefix, cache_dir)
    return cache_dir


def connect(results_dir: str | Path, run_id: str) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB with one view per result table of run_id, plus the view `run`."""
    results_dir = Path(results_dir)
    paths = {
        name: results_dir / stage / run_id / f"{name}.parquet" for name, stage in TABLES.items()
    }
    missing = [path.as_posix() for path in paths.values() if not path.exists()]
    if missing:
        raise QueryInputError(f"run {run_id} is missing results: {', '.join(missing)}")
    con = duckdb.connect()
    for name, path in paths.items():
        con.execute(
            f"CREATE VIEW {name} AS SELECT * FROM read_parquet({_sql_literal(path.as_posix())})"
        )
    con.execute(f"CREATE VIEW run AS SELECT {_sql_literal(run_id)} AS run_id")
    return con


def open_latest(
    source: Source,
    data_dir: str | Path,
    cache_dir: str | Path,
    storage: StorageConfig,
    client=None,
) -> tuple[duckdb.DuckDBPyConnection, str]:
    """A connection to the latest good run from source, and that run's id."""
    if source == "local":
        run_id = latest_local_run(data_dir)
        return connect(data_dir, run_id), run_id
    if not storage.s3.bucket:
        raise QueryInputError(f"source is s3 but no bucket is set (set {STORAGE_ENV['bucket']})")
    client = client if client is not None else make_client(storage)
    run_id = latest_s3_run(client, storage)
    return connect(download_run(client, storage, run_id, cache_dir), run_id), run_id


def describe(con: duckdb.DuckDBPyConnection) -> str:
    """Every view and its columns, one view per line."""
    lines = []
    for name in [*TABLES, "run"]:
        columns = con.execute(f"DESCRIBE {name}").fetchall()
        lines.append(f"{name}: " + ", ".join(f"{column} {kind}" for column, kind, *_ in columns))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query the latest published results with SQL.")
    parser.add_argument("sql", nargs="?", help="a SQL query over the views (see --tables)")
    parser.add_argument("--tables", action="store_true", help="list the views and their columns")
    parser.add_argument("--source", choices=["local", "s3"], help="default: storage.yaml backend")
    parser.add_argument("--storage", type=Path, default=Path("config/storage.yaml"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--cache", type=Path, default=Path("data/cache"))
    args = parser.parse_args(argv)
    if not args.sql and not args.tables:
        parser.error("give a SQL query, or --tables")

    setup_logging()
    storage = load_storage_config(args.storage)
    source = args.source or storage.backend
    try:
        con, run_id = open_latest(source, args.data, args.cache, storage)
        logger.info("Querying run %s (%s)", run_id, source)
        output = describe(con) if args.tables else con.execute(args.sql).df().to_string(index=False)
    except QueryInputError as error:
        logger.error("%s", error)
        return 1
    except duckdb.Error as error:  # a mistake in the SQL: say what DuckDB said
        logger.error("Query failed: %s", error)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
