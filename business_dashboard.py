"""Business dashboard views.

These views are the single data source both dashboards read from: the Databricks
Lakeview dashboard and the standalone HTML build. They read from the production
tables the weekly job maintains (dra_feature_index, dra_feature_vector,
dra_findings), so once the job refreshes those tables the views, and therefore
both dashboards, reflect the new data with no manual step.

Run create_business_views() once against the target catalog and schema. The
views recompute on every query, so nothing here needs re-running after a data
refresh. Point any dashboard at the v_biz_ views, never at the raw tables, so the
presentation layer stays decoupled from table internals.
"""
from __future__ import annotations

from . import config
from .context import spark

_VIEWS = {
    "v_biz_market_read": """
        SELECT device_category,
               net_direction,
               net_strength,
               confidence,
               n_findings,
               aligning_count,
               conflicting_count,
               contrib_supply_chain,
               contrib_policy,
               contrib_ai_news,
               reasoning,
               top_drivers,
               window_days,
               computed_at,
               CASE
                   WHEN net_direction = 1 THEN 'VALUE SUPPORT'
                   WHEN net_direction = -1 THEN 'DEPRECIATION PRESSURE'
                   WHEN aligning_count > 0 AND conflicting_count > 0 THEN 'SIGNALS MIXED'
                   ELSE 'WATCH DEVELOPING'
               END AS expected_read
        FROM dra_feature_index
        ORDER BY device_category
    """,
    "v_biz_component_signals": """
        SELECT component_group,
               device_category,
               COUNT(*) AS signals,
               ROUND(AVG(signed_strength), 4) AS avg_signed_strength,
               SUM(CASE WHEN direction = 1 THEN 1 ELSE 0 END) AS support_count,
               SUM(CASE WHEN direction = -1 THEN 1 ELSE 0 END) AS pressure_count
        FROM dra_feature_vector
        WHERE component_group IS NOT NULL AND component_group <> 'other'
        GROUP BY component_group, device_category
        ORDER BY signals DESC
    """,
    "v_biz_top_drivers": """
        SELECT device_category,
               domain,
               finding_type,
               signed_strength,
               confidence,
               claim,
               article_title,
               source_url,
               effective_from
        FROM dra_feature_vector
        WHERE ABS(signed_strength) >= 0.5
        ORDER BY ABS(signed_strength) DESC
    """,
    "v_biz_domain_contribution": """
        SELECT device_category,
               ROUND(contrib_supply_chain, 4) AS supply_chain,
               ROUND(contrib_policy, 4) AS policy,
               ROUND(contrib_ai_news, 4) AS ai_news
        FROM dra_feature_index
        ORDER BY device_category
    """,
    "v_biz_launch_cascade": """
        SELECT device_category,
               event_kind,
               COUNT(*) AS events,
               SUM(CASE WHEN direction = -1 THEN 1 ELSE 0 END) AS depreciating,
               SUM(CASE WHEN direction = 1 THEN 1 ELSE 0 END) AS supporting
        FROM dra_feature_vector
        WHERE domain = 'ai_news' AND event_kind IS NOT NULL
        GROUP BY device_category, event_kind
        ORDER BY events DESC
    """,
    "v_biz_policy_exposure": """
        SELECT device_category,
               policy_type,
               corridor_relevance,
               COUNT(*) AS signals,
               ROUND(AVG(signed_strength), 4) AS avg_signed_strength
        FROM dra_feature_vector
        WHERE domain = 'policy' AND policy_type IS NOT NULL
        GROUP BY device_category, policy_type, corridor_relevance
        ORDER BY signals DESC
    """,
    "v_biz_value_magnitudes": """
        SELECT component_group,
               value_family,
               raw_value,
               raw_unit,
               magnitude_z,
               claim,
               source_url
        FROM dra_feature_vector
        WHERE magnitude_z IS NOT NULL
        ORDER BY ABS(magnitude_z) DESC
    """,
    "v_biz_freshness": """
        SELECT MAX(computed_at) AS last_reconciled_at,
               (SELECT MAX(ingested_at) FROM dra_findings) AS last_finding_at,
               (SELECT COUNT(*) FROM dra_findings) AS total_findings
        FROM dra_feature_index
    """,
}


def create_business_views() -> list[str]:
    spark().sql(f"USE CATALOG {config.CATALOG}")
    spark().sql(f"USE SCHEMA {config.SCHEMA}")
    created = []
    for name, body in _VIEWS.items():
        spark().sql(f"CREATE OR REPLACE VIEW {name} AS {body}")
        created.append(name)
    return created
