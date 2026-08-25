"""End-to-end run.

Agents run concurrently. Each metered call is accounted to a shared meter. If an
agent fails, the run is marked degraded and the previous market read is held
rather than replaced by a partial. The supervisor and feature vector rebuild only
when at least one agent succeeded, every run snapshots a backup, and usage plus
budget status are logged.
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import config, feature_vector, prompts
from .agents import pipeline
from .fetch import fetch_agent_corpus
from .observability import Meter, budget_status, ensure_usage_table
from .storage import ensure_tables, load_corpus, log_run, snapshot_backup
from .supervisor import run_supervisor


def _corpus_for(agent, use_cache, limit, incremental=False):
    if use_cache:
        cached = load_corpus(agent)
        if cached:
            return cached[:limit] if limit else cached
    fetch_agent_corpus(agent, incremental)
    corpus = load_corpus(agent)
    return corpus[:limit] if limit else corpus


def _run_one_agent(agent, run_id, use_cache, limit, meter, incremental=False):
    started = datetime.now(timezone.utc)
    articles = _corpus_for(agent, use_cache, limit, incremental)
    bundle = prompts.for_agent(agent, meter)
    counts = pipeline.run_agent(
        articles, bundle["extract"], bundle["triage"], bundle["judge"],
        agent, run_id, bundle.get("version", "v3_cot"), meter,
    )
    log_run(
        {
            "run_id": run_id, "started_at": started,
            "finished_at": datetime.now(timezone.utc), "agent": agent,
            "stories_fetched": len(articles),
            "accepted": counts.get("accepted", 0) + counts.get("rescued", 0),
            "quarantined": counts.get("quarantined", 0),
            "status": "degraded" if counts.get("errors") else "ok",
        }
    )
    return counts


def run_pipeline(agents=config.AGENTS, use_cache=True, reconcile_window=None,
                 limit_articles=None, max_workers=3, incremental=False):
    run_id = uuid.uuid4().hex
    ensure_tables()
    ensure_usage_table()
    meter = Meter(run_id)
    window = reconcile_window or config.PRODUCTION_WINDOW_DAYS

    report = {"run_id": run_id, "agents": {}, "degraded": False, "failed_agents": [],
              "mode": "incremental" if incremental else "full_sweep"}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_one_agent, agent, run_id, use_cache, limit_articles,
                        meter, incremental): agent
            for agent in agents
        }
        for future in as_completed(futures):
            agent = futures[future]
            try:
                report["agents"][agent] = future.result()
            except Exception as error:
                report["degraded"] = True
                report["failed_agents"].append(agent)
                report["agents"][agent] = {"error": str(error)[:200]}

    succeeded = [a for a in agents if a not in report["failed_agents"]]
    if succeeded:
        report["supervisor"] = run_supervisor(window)
        report["feature_rows"] = feature_vector.build(run_id)
    else:
        report["supervisor"] = {"held": "all agents failed, previous read retained"}

    report["backup_path"] = snapshot_backup(run_id)
    report["run_cost_usd"] = meter.flush()
    report["budget"] = budget_status()
    report["status"] = "degraded" if report["degraded"] else "ok"
    return report
