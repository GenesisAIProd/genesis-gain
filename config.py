"""Runtime configuration for the Genesis GAIN market-signal pipeline.

Values are read from environment variables where they vary by deployment
(dev, staging, prod) and fall back to the development defaults. Nothing here
imports Spark or Databricks so the module stays importable in tests.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


CATALOG = os.environ.get("GAIN_CATALOG", "genesis_devp")
SCHEMA = os.environ.get("GAIN_SCHEMA", "dev_roohi")

SECRET_SCOPE = os.environ.get("GAIN_SECRET_SCOPE", "genesis")
APIFY_TOKEN_KEY = os.environ.get("GAIN_APIFY_KEY", "apify-token")

BACKUP_VOLUME = os.environ.get(
    "GAIN_BACKUP_VOLUME", f"/Volumes/{CATALOG}/{SCHEMA}/backups"
)
EXTERNAL_BACKUP_PATH = os.environ.get("GAIN_EXTERNAL_BACKUP", "")

EXTRACT_MODEL = os.environ.get("GAIN_EXTRACT_MODEL", "databricks-llama-4-maverick")
JUDGE_MODEL = os.environ.get("GAIN_JUDGE_MODEL", "databricks-claude-opus-4-8")
ESCALATE_MODEL = os.environ.get("GAIN_ESCALATE_MODEL", "databricks-claude-sonnet-4-6")
TRIAGE_MODEL = os.environ.get("GAIN_TRIAGE_MODEL", "databricks-llama-4-maverick")
SUPERVISOR_MODEL = os.environ.get("GAIN_SUPERVISOR_MODEL", "databricks-claude-opus-4-8")

FETCH_ACTOR = "memo23/google-news-scraper"
FETCH_WINDOW_DAYS = int(os.environ.get("GAIN_FETCH_WINDOW_DAYS", "10"))
FETCH_REGION = os.environ.get("GAIN_FETCH_REGION", "US:en")
MAX_ARTICLES_PER_QUERY = int(os.environ.get("GAIN_MAX_ARTICLES", "15"))
CACHE_EVERY = int(os.environ.get("GAIN_CACHE_EVERY", "5"))
FETCH_BATCH_SIZE = int(os.environ.get("GAIN_FETCH_BATCH_SIZE", "10"))
FETCH_TIMEOUT_SECS = int(os.environ.get("GAIN_FETCH_TIMEOUT_SECS", "300"))

PRODUCTION_WINDOW_DAYS = int(os.environ.get("GAIN_RECONCILE_WINDOW", "7"))

FX_TO_USD = {
    "usd": 1.0, "gbp": 1.27, "eur": 1.08,
    "jpy": 0.0067, "mxn": 0.058, "krw": 0.00075,
}


@dataclass(frozen=True)
class Tables:
    findings: str = "dra_findings"
    quarantine: str = "dra_quarantine"
    corpus: str = "dra_corpus_cache"
    registry: str = "dra_keyword_registry"
    feature_vector: str = "dra_feature_vector"
    feature_index: str = "dra_feature_index"
    run_log: str = "dra_run_log"

    def fq(self, name: str) -> str:
        return f"{CATALOG}.{SCHEMA}.{getattr(self, name)}"


TABLES = Tables()

AGENTS = ("supply_chain", "policy", "ai_news")
DEVICE_CATEGORIES = ("smartphone", "laptop_desktop_workstation", "all_categories")
