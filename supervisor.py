"""Cross-agent reconciliation.

For each device category the supervisor pulls that category's findings plus every
signal tagged all_categories, then reasons over confidence and finding type to
produce one net direction with an explicit account of what aligned and what
conflicted. The result is written as the feature index, one row per category.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pandas as pd

from pyspark.sql.types import (
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from . import config
from .context import call_model, extract_json, spark

SUPERVISOR_PROMPT = """<persona>
You are the Supervisor of the Genesis market-signal system for a refurbished-electronics valuation business. Three sub-agents have produced quality-judged findings. Reconcile them into one coherent net signal for a device category.
</persona>

<objective>
Decide the NET effect on the value of existing, used stock. Domains may align or conflict. Weigh by confidence, recency, and finding type to produce one net direction with reasoning.
</objective>

<how_to_weigh>
Confidence carries most weight. Finding type ranks event_fact over forecast over unconfirmed_report over sentiment. direction plus one means existing stock value rises, minus one means it falls. If findings conflict, the net reflects the heavier side and you state the tension rather than averaging it away. Report each domain's contribution.
</how_to_weigh>

<output_format>
Return only this JSON:
{
  "device_category": "the category",
  "net_direction": -1 | 0 | 1,
  "net_strength": 0.0 to 1.0,
  "confidence": 0.0 to 1.0,
  "aligning_count": integer,
  "conflicting_count": integer,
  "domain_contribution": {"supply_chain": -1.0 to 1.0, "policy": -1.0 to 1.0, "ai_news": -1.0 to 1.0},
  "reasoning": "two to four sentences on what aligned, what conflicted, how you resolved it",
  "top_drivers": ["short phrase per decisive finding"]
}
</output_format>

<rules>
Reason only over the findings given. Do not invent signals. When findings conflict, weight by confidence and finding type and explain the resolution. domain_contribution values are each domain's net push.
</rules>"""

FEATURE_INDEX_SCHEMA = StructType(
    [
        StructField("device_category", StringType()),
        StructField("net_direction", IntegerType()),
        StructField("net_strength", DoubleType()),
        StructField("confidence", DoubleType()),
        StructField("aligning_count", IntegerType()),
        StructField("conflicting_count", IntegerType()),
        StructField("contrib_supply_chain", DoubleType()),
        StructField("contrib_policy", DoubleType()),
        StructField("contrib_ai_news", DoubleType()),
        StructField("n_findings", IntegerType()),
        StructField("reasoning", StringType()),
        StructField("top_drivers", StringType()),
        StructField("window_days", IntegerType()),
        StructField("computed_at", TimestampType()),
    ]
)


def reconcile(device_category: str, window_days: int | None = None) -> dict:
    if device_category != "all_categories":
        where = f"device_category IN ('all_categories', '{device_category}')"
    else:
        where = "device_category = 'all_categories'"
    window = (
        f"AND ingested_at >= current_timestamp() - INTERVAL {window_days} DAYS"
        if window_days
        else ""
    )
    rows = (
        spark()
        .sql(
            f"SELECT agent, claim, finding_type, direction, confidence, basis, "
            f"event_kind, policy_type, corridor_relevance FROM {config.TABLES.findings} "
            f"WHERE {where} {window} ORDER BY confidence DESC"
        )
        .toPandas()
    )
    if len(rows) == 0:
        return {
            "device_category": device_category,
            "net_direction": 0,
            "net_strength": 0.0,
            "confidence": 0.0,
            "n_findings": 0,
            "reasoning": "no findings in window",
        }
    lines = []
    for _, r in rows.head(120).iterrows():
        d = int(r["direction"]) if pd.notna(r["direction"]) else 0
        conf = float(r["confidence"]) if pd.notna(r["confidence"]) else 0.0
        event_kind = r["event_kind"] if pd.notna(r["event_kind"]) else ""
        extra = f" [{event_kind}]" if event_kind else ""
        if pd.notna(r["policy_type"]) and r["policy_type"]:
            corridor = r["corridor_relevance"] if pd.notna(r["corridor_relevance"]) else ""
            extra += f" [{r['policy_type']}/{corridor}]"
        lines.append(
            f"- ({r['agent']}, conf={conf:.2f}, {r['finding_type']}, "
            f"dir={d:+d}){extra}: {r['claim']}"
        )
    user = (
        f"<device_category>{device_category}</device_category>"
        f"<finding_count>{len(rows)}</finding_count>"
        f"<findings>\n{chr(10).join(lines)}\n</findings>\n"
        f"Reconcile these into one net signal. Return only the JSON."
    )
    parsed, err = extract_json(
        call_model(config.SUPERVISOR_MODEL, SUPERVISOR_PROMPT, user, max_tokens=1200)
    )
    if not parsed:
        return {"device_category": device_category, "error": err, "n_findings": len(rows)}
    parsed["n_findings"] = len(rows)
    return parsed


def write_feature_index(results: dict, window_days: int) -> int:
    now = datetime.now(timezone.utc)
    rows = []
    for category, result in results.items():
        contribution = result.get("domain_contribution", {})
        rows.append(
            {
                "device_category": category,
                "net_direction": int(result.get("net_direction", 0)),
                "net_strength": float(result.get("net_strength", 0.0)),
                "confidence": float(result.get("confidence", 0.0)),
                "aligning_count": int(result.get("aligning_count", 0)),
                "conflicting_count": int(result.get("conflicting_count", 0)),
                "contrib_supply_chain": float(contribution.get("supply_chain", 0.0)),
                "contrib_policy": float(contribution.get("policy", 0.0)),
                "contrib_ai_news": float(contribution.get("ai_news", 0.0)),
                "n_findings": int(result.get("n_findings", 0)),
                "reasoning": result.get("reasoning", ""),
                "top_drivers": json.dumps(result.get("top_drivers", [])),
                "window_days": window_days,
                "computed_at": now,
            }
        )
    spark().createDataFrame(rows, FEATURE_INDEX_SCHEMA).write.mode(
        "overwrite"
    ).option("overwriteSchema", "true").saveAsTable(config.TABLES.feature_index)
    return len(rows)


def run_supervisor(window_days: int | None = None) -> dict:
    results = {c: reconcile(c, window_days) for c in config.DEVICE_CATEGORIES}
    write_feature_index(results, window_days or 0)
    return results
