"""MCP server exposing Genesis GAIN as callable tools.

Consumers (valuation models, internal agents, an LLM assistant, the dev team)
reach the market signal through a stable tool interface without touching the
pipeline internals or the table schema. Read tools serve the current tables; the
refresh tool triggers a run.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import config
from .context import spark
from .orchestrator import run_pipeline

server = FastMCP("genesis-gain")


def _rows(query: str) -> list[dict]:
    return spark().sql(query).toPandas().to_dict("records")


@server.tool()
def get_market_read(device_category: str = "all_categories") -> str:
    """Return the supervisor's reconciled net signal for a device category.

    device_category is one of smartphone, laptop_desktop_workstation,
    all_categories. The result gives net direction, strength, confidence, the
    per-domain contributions, and the reconciliation reasoning.
    """
    rows = _rows(
        f"SELECT * FROM {config.TABLES.feature_index} "
        f"WHERE device_category = '{device_category}'"
    )
    return json.dumps(rows[0] if rows else {"error": "no read for category"}, default=str)


@server.tool()
def get_component_signals(component: str) -> str:
    """Return the signals for a component group such as DDR5, HBM, AI-GPU or GPU.

    Each signal carries its direction, signed strength, any numeric value, and the
    underlying claim so the caller can see the evidence, not just the score.
    """
    rows = _rows(
        f"SELECT component_group, direction, signed_strength, raw_value, raw_unit, "
        f"finding_type, claim, source_url FROM {config.TABLES.feature_vector} "
        f"WHERE lower(component_group) = lower('{component}') "
        f"ORDER BY signed_strength DESC"
    )
    return json.dumps({"component": component, "count": len(rows), "signals": rows}, default=str)


@server.tool()
def get_launch_cascade() -> str:
    """Return how technology launches cascade onto the value of existing stock.

    For each event kind it gives how many signals depreciate older stock versus
    how many lift it, which is the core tension the business tracks.
    """
    rows = _rows(
        f"SELECT event_kind, "
        f"SUM(CASE WHEN direction < 0 THEN 1 ELSE 0 END) AS depreciates_old_stock, "
        f"SUM(CASE WHEN direction > 0 THEN 1 ELSE 0 END) AS lifts_old_stock, "
        f"COUNT(*) AS total FROM {config.TABLES.findings} "
        f"WHERE agent = 'ai_news' AND event_kind IS NOT NULL "
        f"GROUP BY event_kind ORDER BY total DESC"
    )
    return json.dumps({"cascade": rows}, default=str)


@server.tool()
def get_ingestion_stats() -> str:
    """Return coverage and quality for the most recent state of the system.

    Reports stories monitored, stories read in full, verified signals, and how
    many were held back on quality, so a caller can judge how much to trust the read.
    """
    stats = {
        "stories_monitored": _rows(f"SELECT COUNT(*) c FROM {config.TABLES.corpus}")[0]["c"],
        "read_in_full": _rows(
            f"SELECT COUNT(*) c FROM {config.TABLES.corpus} WHERE has_body = true"
        )[0]["c"],
        "verified_signals": _rows(f"SELECT COUNT(*) c FROM {config.TABLES.findings}")[0]["c"],
        "held_on_quality": _rows(f"SELECT COUNT(*) c FROM {config.TABLES.quarantine}")[0]["c"],
    }
    return json.dumps(stats, default=str)


@server.tool()
def refresh_signals(window_days: int = 7, use_cache: bool = False) -> str:
    """Run the pipeline and refresh every table.

    window_days sets the supervisor reconciliation window. use_cache reuses the
    cached corpus instead of fetching. Returns the run identifier and per-agent
    accept and quarantine counts.
    """
    report = run_pipeline(use_cache=use_cache, reconcile_window=window_days)
    summary = {
        "run_id": report["run_id"],
        "agents": report["agents"],
        "feature_rows": report.get("feature_rows"),
    }
    return json.dumps(summary, default=str)


@server.tool()
def get_cost_and_usage() -> str:
    """Return current spend, budget status, and per-model usage.

    Reports week-to-date and month-to-date cost against the budgets, plus a
    per-model token and cost breakdown, so a caller can see where the spend went.
    """
    from .observability import budget_status

    by_model = _rows(
        f"SELECT model, ROUND(SUM(est_cost_usd), 4) AS cost_usd, "
        f"SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
        f"SUM(calls) AS calls FROM dra_usage_log GROUP BY model ORDER BY cost_usd DESC"
    )
    return json.dumps({"budget": budget_status(), "by_model": by_model}, default=str)


if __name__ == "__main__":
    server.run()
