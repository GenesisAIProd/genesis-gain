"""Delta storage layer.

All writes are append-or-merge. Findings and quarantine rows are keyed by a
content hash so a re-scraped article never produces a duplicate. Nothing here
overwrites prior data; the only full-overwrite objects are the derived feature
tables, which are rebuilt from the immutable findings table.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from . import config
from .context import spark

_S = StructField


FINDINGS_SCHEMA = StructType(
    [
        _S("finding_id", StringType()),
        _S("source_id", StringType()),
        _S("source_url", StringType()),
        _S("article_title", StringType()),
        _S("claim", StringType()),
        _S("finding_type", StringType()),
        _S("device_category", StringType()),
        _S("value_num", DoubleType()),
        _S("value_unit", StringType()),
        _S("direction", IntegerType()),
        _S("effective_from", StringType()),
        _S("horizon_days", IntegerType()),
        _S("basis", StringType()),
        _S("support_span", StringType()),
        _S("source_reason", StringType()),
        _S("agent", StringType()),
        _S("run_id", StringType()),
        _S("prompt_version", StringType()),
        _S("model_name", StringType()),
        _S("confidence", DoubleType()),
        _S("ingested_at", TimestampType()),
        _S("policy_type", StringType()),
        _S("corridor_relevance", StringType()),
        _S("event_kind", StringType()),
    ]
)

QUARANTINE_SCHEMA = StructType(
    [
        _S("finding_id", StringType()),
        _S("source_id", StringType()),
        _S("source_url", StringType()),
        _S("article_title", StringType()),
        _S("claim", StringType()),
        _S("finding_type", StringType()),
        _S("device_category", StringType()),
        _S("value_num", DoubleType()),
        _S("value_unit", StringType()),
        _S("direction", IntegerType()),
        _S("effective_from", StringType()),
        _S("basis", StringType()),
        _S("support_span", StringType()),
        _S("source_reason", StringType()),
        _S("agent", StringType()),
        _S("run_id", StringType()),
        _S("prompt_version", StringType()),
        _S("model_name", StringType()),
        _S("judge_total", IntegerType()),
        _S("judge_verdict", StringType()),
        _S("judge_reason", StringType()),
        _S("quarantined_at", TimestampType()),
        _S("policy_type", StringType()),
        _S("corridor_relevance", StringType()),
        _S("event_kind", StringType()),
    ]
)

CORPUS_SCHEMA = StructType(
    [
        _S("item_id", StringType()),
        _S("source_id", StringType()),
        _S("url", StringType()),
        _S("title", StringType()),
        _S("body_text", StringType()),
        _S("has_body", BooleanType()),
        _S("agent", StringType()),
        _S("fetched_at", TimestampType()),
    ]
)

RUN_LOG_SCHEMA = StructType(
    [
        _S("run_id", StringType()),
        _S("started_at", TimestampType()),
        _S("finished_at", TimestampType()),
        _S("agent", StringType()),
        _S("stories_fetched", IntegerType()),
        _S("accepted", IntegerType()),
        _S("quarantined", IntegerType()),
        _S("status", StringType()),
    ]
)


def item_id(source_id: str, url: str) -> str:
    return hashlib.sha256(f"{source_id}|{url}".encode()).hexdigest()[:24]


def finding_identity(url: str, support_span: str) -> str:
    return hashlib.sha256(f"{url}|{support_span}".encode()).hexdigest()[:24]


def ensure_tables() -> None:
    s = spark()
    specs = {
        config.TABLES.findings: FINDINGS_SCHEMA,
        config.TABLES.quarantine: QUARANTINE_SCHEMA,
        config.TABLES.corpus: CORPUS_SCHEMA,
        config.TABLES.run_log: RUN_LOG_SCHEMA,
    }
    for name, schema in specs.items():
        s.createDataFrame([], schema).write.mode("append").saveAsTable(name)


def merge_rows(table: str, rows: list[dict], schema: StructType, key: str) -> int:
    if not rows:
        return 0
    s = spark()
    staged = s.createDataFrame(rows, schema)
    staged.createOrReplaceTempView("_staged")
    s.sql(
        f"MERGE INTO {table} t USING _staged x ON t.{key} = x.{key} "
        f"WHEN NOT MATCHED THEN INSERT *"
    )
    return len(rows)


def cache_corpus(rows: list[dict], agent: str) -> int:
    if not rows:
        return 0
    s = spark()
    s.createDataFrame(rows, CORPUS_SCHEMA).write.mode("overwrite").option(
        "replaceWhere", f"agent = '{agent}'"
    ).saveAsTable(config.TABLES.corpus)
    return len(rows)


def load_corpus(agent: str, bodied_only: bool = True) -> list[dict]:
    clause = "AND has_body = true" if bodied_only else ""
    frame = spark().sql(
        f"SELECT * FROM {config.TABLES.corpus} WHERE agent = '{agent}' {clause}"
    ).toPandas()
    return frame.to_dict("records")


def log_run(entry: dict) -> None:
    spark().createDataFrame([entry], RUN_LOG_SCHEMA).write.mode("append").saveAsTable(
        config.TABLES.run_log
    )


def snapshot_backup(run_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = f"{config.BACKUP_VOLUME}/run_{stamp}_{run_id[:8]}"
    for name in ("findings", "quarantine", "feature_vector", "feature_index"):
        table = getattr(config.TABLES, name)
        try:
            spark().table(table).write.mode("overwrite").parquet(f"{base}/{name}")
        except Exception:
            continue
    if config.EXTERNAL_BACKUP_PATH:
        ext = f"{config.EXTERNAL_BACKUP_PATH}/run_{stamp}_{run_id[:8]}"
        for name in ("findings", "feature_vector"):
            table = getattr(config.TABLES, name)
            try:
                spark().table(table).write.mode("overwrite").parquet(f"{ext}/{name}")
            except Exception:
                continue
    return base
