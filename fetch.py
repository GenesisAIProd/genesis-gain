"""News acquisition through the Apify google-news-scraper actor.

Each agent draws its query set from the keyword registry. Results are validated
for real article bodies, deduplicated by content hash, and cached to Delta in
batches so an interrupted run resumes from the last checkpoint rather than
re-fetching from the start.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from . import config
from .context import apify, spark
from .storage import cache_corpus, item_id

_JUNK = (
    "we use cookies", "cookie policy", "subscribe", "sign in",
    "page not found", "enable javascript", "advertisement",
    "privacy policy", "all rights reserved",
)


def is_real_content(text: str, minimum: int = 300) -> bool:
    if not text or len(text) < minimum:
        return False
    low = text.lower()
    junk_ratio = sum(low.count(p) * len(p) for p in _JUNK) / len(low)
    if junk_ratio > 0.10:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and len(set(lines)) / len(lines) < 0.5:
        return False
    sentences = [s for s in re.split(r"[.!?]", text) if len(s.split()) >= 6]
    return len(sentences) >= 3


def _dataset_id(run) -> str | None:
    if isinstance(run, dict):
        return run.get("defaultDatasetId")
    return getattr(run, "default_dataset_id", None)


def _last_success(agent: str) -> datetime | None:
    try:
        row = spark().sql(
            f"SELECT MAX(finished_at) m FROM {config.TABLES.run_log} "
            f"WHERE agent = '{agent}' AND status IN ('ok', 'degraded')"
        ).collect()[0]
        return row["m"]
    except Exception:
        return None


def _query_window(agent: str = "", incremental: bool = False) -> tuple[str, str]:
    until = datetime.now(timezone.utc)
    if incremental:
        last = _last_success(agent)
        if last is not None:
            since = last - timedelta(days=1)
            return since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d")
    since = until - timedelta(days=config.FETCH_WINDOW_DAYS)
    return since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d")


def fetch_batch(terms: list[str], limit: int, agent: str = "", incremental: bool = False) -> list[dict]:
    since, until = _query_window(agent, incremental)
    run = apify().actor(config.FETCH_ACTOR).call(
        run_input={
            "startInputs": terms,
            "maxArticlesPerInput": limit,
            "maxItems": limit * len(terms),
            "resolveUrls": True,
            "enrichBody": True,
            "enableCfBypass": True,
            "regionLanguage": config.FETCH_REGION,
            "since": since,
            "until": until,
        },
        run_timeout=timedelta(seconds=config.FETCH_TIMEOUT_SECS),
        wait_duration=timedelta(seconds=config.FETCH_TIMEOUT_SECS),
    )
    dataset_id = _dataset_id(run)
    if not dataset_id:
        return []
    out = []
    for record in apify().dataset(dataset_id).iterate_items():
        url = record.get("publisherUrl") or record.get("googleNewsUrl")
        if not url:
            continue
        body = record.get("body") or ""
        out.append(
            {
                "source_id": query_source(""),
                "url": url,
                "title": record.get("title"),
                "body_text": body,
                "has_body": is_real_content(body),
            }
        )
    return out


def query_source(query: str) -> str:
    return "memo23_gnews"


def registry_queries(agent: str) -> list[str]:
    terms = (
        spark()
        .sql(
            f"SELECT DISTINCT term FROM {config.TABLES.registry} WHERE agent = '{agent}'"
        )
        .toPandas()["term"]
        .tolist()
    )
    if not terms:
        raise RuntimeError(
            f"No watch terms for agent '{agent}' in "
            f"{config.CATALOG}.{config.SCHEMA}.{config.TABLES.registry}. "
            f"The registry is seed data, not derived: ensure_tables creates it "
            f"empty, so a fresh sandbox must clone it from a populated schema "
            f"(see prod_setup.SEED_TABLES)."
        )
    context = "(AI OR chip OR smartphone OR laptop OR GPU OR device OR launch)"
    if agent == "policy":
        context = "(tariff OR export OR trade OR sanction OR policy OR semiconductor)"
    elif agent == "supply_chain":
        context = "(chip OR memory OR wafer OR shortage OR supply OR foundry)"
    return [f"{term} {context}" for term in terms]


def _batches(terms: list[str], size: int) -> list[list[str]]:
    return [terms[i : i + size] for i in range(0, len(terms), size)]


def fetch_agent_corpus(agent: str, incremental: bool = False) -> int:
    queries = registry_queries(agent)
    batches = _batches(queries, config.FETCH_BATCH_SIZE)
    seen: set[str] = set()
    corpus: list[dict] = []
    for i, batch in enumerate(batches, start=1):
        try:
            for article in fetch_batch(batch, config.MAX_ARTICLES_PER_QUERY, agent, incremental):
                key = item_id(article["source_id"], article["url"])
                if key in seen:
                    continue
                seen.add(key)
                article["item_id"] = key
                article["agent"] = agent
                corpus.append(article)
        except Exception:
            continue
        if i % config.CACHE_EVERY == 0 and corpus:
            _flush(corpus, agent)
    return _flush(corpus, agent)


def _flush(corpus: list[dict], agent: str) -> int:
    now = datetime.now(timezone.utc)
    rows = [
        {
            "item_id": a["item_id"],
            "source_id": a["source_id"],
            "url": a["url"],
            "title": a.get("title") or "",
            "body_text": a.get("body_text") or "",
            "has_body": bool(a.get("has_body")),
            "agent": agent,
            "fetched_at": now,
        }
        for a in corpus
        if a.get("url")
    ]
    cache_corpus(rows, agent)
    return len(rows)
