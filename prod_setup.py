"""One-time production setup.

Creates the production catalog, schema, and backup volume, then seeds the tables
from the development baseline so the money already spent on the current findings
is carried forward. Run once from a workspace attached to the target metastore by
someone who can create catalogs. After this, the scheduled Job writes here.
"""
from __future__ import annotations

from .context import spark

PROD_CATALOG = "genesis_prod"
PROD_SCHEMA = "market_signals"
DEV_CATALOG = "genesis_devp"
DEV_SCHEMA = "dev_roohi"

SEED_TABLES = [
    "dra_findings",
    "dra_quarantine",
    "dra_corpus_cache",
    "dra_keyword_registry",
    "dra_feature_vector",
    "dra_feature_index",
]


def create_prod_objects() -> None:
    s = spark()
    s.sql(f"CREATE CATALOG IF NOT EXISTS {PROD_CATALOG}")
    s.sql(f"CREATE SCHEMA IF NOT EXISTS {PROD_CATALOG}.{PROD_SCHEMA}")
    s.sql(f"CREATE VOLUME IF NOT EXISTS {PROD_CATALOG}.{PROD_SCHEMA}.backups")


def seed_from_dev() -> dict:
    s = spark()
    result = {}
    for table in SEED_TABLES:
        src = f"{DEV_CATALOG}.{DEV_SCHEMA}.{table}"
        dst = f"{PROD_CATALOG}.{PROD_SCHEMA}.{table}"
        try:
            s.sql(f"CREATE TABLE IF NOT EXISTS {dst} DEEP CLONE {src}")
            result[table] = s.table(dst).count()
        except Exception as error:
            result[table] = f"skipped: {str(error)[:80]}"
    return result


def run() -> dict:
    create_prod_objects()
    counts = seed_from_dev()
    return {"catalog": PROD_CATALOG, "schema": PROD_SCHEMA, "seeded": counts}
