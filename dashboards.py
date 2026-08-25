"""Dashboard views for cost monitoring and data ingestion.

Run once against the target catalog and schema to create the views the two
operational dashboards read from. Views are derived from the usage log and the
run log and recompute on every query, so the dashboards always show live state.
"""
from __future__ import annotations

from . import config
from .context import spark

_VIEWS = {
    "v_cost_by_run": """
        SELECT run_id,
               ROUND(SUM(est_cost_usd), 4) AS run_cost_usd,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(calls) AS calls,
               MIN(logged_at) AS run_at
        FROM dra_usage_log
        GROUP BY run_id
        ORDER BY run_at DESC
    """,
    "v_cost_by_model": """
        SELECT model,
               ROUND(SUM(est_cost_usd), 4) AS cost_usd,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(calls) AS calls
        FROM dra_usage_log
        GROUP BY model
        ORDER BY cost_usd DESC
    """,
    "v_cost_by_agent_stage": """
        SELECT agent, stage, model,
               ROUND(SUM(est_cost_usd), 4) AS cost_usd,
               SUM(calls) AS calls,
               ROUND(AVG(latency_ms), 1) AS avg_latency_ms
        FROM dra_usage_log
        GROUP BY agent, stage, model
        ORDER BY cost_usd DESC
    """,
    "v_cost_weekly": """
        SELECT date_trunc('week', logged_at) AS week,
               ROUND(SUM(est_cost_usd), 2) AS cost_usd,
               SUM(input_tokens + output_tokens) AS total_tokens
        FROM dra_usage_log
        GROUP BY date_trunc('week', logged_at)
        ORDER BY week DESC
    """,
    "v_cost_monthly": """
        SELECT date_trunc('month', logged_at) AS month,
               ROUND(SUM(est_cost_usd), 2) AS cost_usd,
               SUM(input_tokens + output_tokens) AS total_tokens
        FROM dra_usage_log
        GROUP BY date_trunc('month', logged_at)
        ORDER BY month DESC
    """,
    "v_ingest_by_run": """
        SELECT run_id,
               MAX(finished_at) AS run_at,
               SUM(stories_fetched) AS stories_fetched,
               SUM(accepted) AS accepted,
               SUM(quarantined) AS quarantined,
               CASE WHEN SUM(accepted) + SUM(quarantined) > 0
                    THEN ROUND(SUM(accepted) / (SUM(accepted) + SUM(quarantined)), 3)
                    ELSE NULL END AS accept_rate,
               MAX(status) AS status
        FROM dra_run_log
        GROUP BY run_id
        ORDER BY run_at DESC
    """,
    "v_ingest_by_agent": """
        SELECT agent,
               SUM(stories_fetched) AS stories_fetched,
               SUM(accepted) AS accepted,
               SUM(quarantined) AS quarantined
        FROM dra_run_log
        GROUP BY agent
        ORDER BY accepted DESC
    """,
    "v_latency_by_agent_stage": """
        SELECT agent, stage,
               ROUND(AVG(latency_ms), 1) AS avg_latency_ms,
               ROUND(PERCENTILE(latency_ms, 0.95), 1) AS p95_latency_ms,
               SUM(calls) AS calls
        FROM dra_usage_log
        GROUP BY agent, stage
        ORDER BY avg_latency_ms DESC
    """,
    "v_signal_breakdown": """
        SELECT agent, finding_type, device_category,
               COUNT(*) AS signals
        FROM dra_findings
        GROUP BY agent, finding_type, device_category
        ORDER BY signals DESC
    """,
}


def create_dashboard_views() -> list[str]:
    spark().sql(f"USE CATALOG {config.CATALOG}")
    spark().sql(f"USE SCHEMA {config.SCHEMA}")
    created = []
    for name, body in _VIEWS.items():
        spark().sql(f"CREATE OR REPLACE VIEW {name} AS {body}")
        created.append(name)
    return created
