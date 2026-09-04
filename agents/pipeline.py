"""The extraction pipeline shared by every sub-agent.

One article moves through triage, extraction, a device-routed quality judge, and
a single escalation pass that hands the judge's complaint back to a stronger
model. Accepted findings and quarantined findings are both keyed by content hash
so repeated runs never double-count the same signal.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .. import config
from ..context import extract_json, spark
from ..reliability import guarded_call
from ..security import sanitize, wrap_untrusted
from ..storage import (
    FINDINGS_SCHEMA,
    QUARANTINE_SCHEMA,
    finding_identity,
    merge_rows,
)


def _num(value):
    if isinstance(value, (int, float)) and not (
        isinstance(value, float) and math.isnan(value)
    ):
        return float(value)
    return None


def _int(value):
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _body(article: dict) -> str:
    return article.get("body_text") or article.get("body") or ""


def triage(article: dict, prompt: str, agent: str, meter) -> bool:
    user = (
        f"<article><title>{sanitize(article.get('title'), 300)}</title>"
        f"{wrap_untrusted(_body(article)[:2000])}</article>"
    )
    raw, _ = guarded_call(
        config.TRIAGE_MODEL, prompt, user, agent, "triage", meter, max_tokens=50
    )
    parsed, _ = extract_json(raw)
    if parsed is None:
        return True
    return bool(parsed.get("relevant", False))


def extract(article: dict, prompt: str, model: str, agent: str, meter) -> tuple[dict, str]:
    user = (
        f"<article><source>{sanitize(article.get('source_id'), 120)}</source>"
        f"<url>{sanitize(article.get('url'), 400)}</url>"
        f"<title>{sanitize(article.get('title'), 300)}</title>"
        f"{wrap_untrusted(_body(article)[:6000])}</article>"
    )
    raw, used = guarded_call(model, prompt, user, agent, "extract", meter)
    parsed, _ = extract_json(raw)
    if parsed and parsed.get("has_finding") and not parsed.get("device_category"):
        parsed["device_category"] = "all_categories"
    return parsed or {}, used


def re_extract(article: dict, prompt: str, verdict: dict, prior: dict, model: str,
               agent: str, meter) -> tuple[dict, str]:
    revision = (
        f"\n\n<revision_request>"
        f"<judge_verdict>{verdict.get('verdict')}</judge_verdict>"
        f"<judge_score>{verdict.get('total')}/12</judge_score>"
        f"<judge_complaint>{sanitize(verdict.get('reason', ''), 500)}</judge_complaint>"
        f"<prior_finding>{prior}</prior_finding>"
        f"Return a corrected finding as one JSON object addressing the complaint."
        f"</revision_request>"
    )
    user = (
        f"<article><source>{sanitize(article.get('source_id'), 120)}</source>"
        f"<url>{sanitize(article.get('url'), 400)}</url>"
        f"<title>{sanitize(article.get('title'), 300)}</title>"
        f"{wrap_untrusted(_body(article)[:6000])}</article>{revision}"
    )
    raw, used = guarded_call(model, prompt, user, agent, "escalate", meter)
    parsed, _ = extract_json(raw)
    if parsed and parsed.get("has_finding") and not parsed.get("device_category"):
        parsed["device_category"] = "all_categories"
    return parsed or {}, used


def save_finding(article: dict, finding: dict, verdict: dict, run_id: str,
                 model_name: str, agent: str, prompt_version: str) -> str:
    identity = finding_identity(article.get("url", ""), finding.get("support_span", ""))
    now = datetime.now(timezone.utc)
    base = {
        "finding_id": identity,
        "source_id": article.get("source_id"),
        "source_url": article.get("url"),
        "article_title": article.get("title"),
        "claim": finding.get("claim"),
        "finding_type": finding.get("finding_type"),
        "device_category": finding.get("device_category"),
        "value_num": _num(finding.get("value_num")),
        "value_unit": finding.get("value_unit"),
        "direction": _int(finding.get("direction")),
        "effective_from": finding.get("effective_from"),
        "basis": finding.get("basis"),
        "support_span": finding.get("support_span"),
        "source_reason": finding.get("source_reason"),
        "agent": agent,
        "run_id": run_id,
        "prompt_version": prompt_version,
        "model_name": model_name,
        "policy_type": finding.get("policy_type"),
        "corridor_relevance": finding.get("corridor_relevance"),
        "event_kind": finding.get("event_kind"),
    }
    if verdict.get("verdict") == "accept":
        row = {
            **base,
            "horizon_days": _int(finding.get("horizon_days")),
            "confidence": round(verdict.get("total", 0) / 12.0, 3),
            "ingested_at": now,
        }
        merge_rows(config.TABLES.findings, [row], FINDINGS_SCHEMA, "finding_id")
        return "accepted"
    row = {
        **base,
        "judge_total": _int(verdict.get("total")),
        "judge_verdict": verdict.get("verdict"),
        "judge_reason": verdict.get("reason", ""),
        "quarantined_at": now,
    }
    merge_rows(config.TABLES.quarantine, [row], QUARANTINE_SCHEMA, "finding_id")
    return "quarantined"


def run_agent(articles, extract_prompt, triage_prompt, judge_fn, agent, run_id,
              prompt_version="v3_cot", meter=None):
    counts = {"triaged_out": 0, "no_finding": 0, "accepted": 0,
              "quarantined": 0, "escalated": 0, "rescued": 0, "errors": 0}
    for article in articles:
        try:
            if not triage(article, triage_prompt, agent, meter):
                counts["triaged_out"] += 1
                continue
            finding, model_used = extract(
                article, extract_prompt, config.EXTRACT_MODEL, agent, meter
            )
            if not finding or not finding.get("has_finding"):
                counts["no_finding"] += 1
                continue
            verdict = judge_fn(article, finding)
            if verdict.get("verdict") in ("revise", "reject"):
                counts["escalated"] += 1
                revised, esc_model = re_extract(
                    article, extract_prompt, verdict, finding,
                    config.ESCALATE_MODEL, agent, meter
                )
                if revised.get("has_finding"):
                    second = judge_fn(article, revised)
                    if second.get("total", 0) >= verdict.get("total", 0):
                        if second.get("verdict") == "accept" and verdict.get("verdict") != "accept":
                            counts["rescued"] += 1
                        finding, verdict, model_used = revised, second, esc_model
            status = save_finding(
                article, finding, verdict, run_id, model_used, agent, prompt_version
            )
            counts[status] += 1
        except Exception:
            counts["errors"] += 1
            continue
    return counts
