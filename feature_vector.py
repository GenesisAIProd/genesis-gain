"""Per-finding feature vector.

Every accepted finding is tagged to a fine-grained component group and given a
cross-comparable signed strength (direction times confidence). Numeric values are
sorted into families and z-scored within their own family so a percentage and a
capacity never share a scale. Money is converted to USD on a static table; a live
rate feed is a production follow-up. The table is derived and rebuilt in full from
the immutable findings table.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd

from . import config
from .context import spark

_PRICE_PCT = re.compile(r"%|percent|pct", re.I)
_MONEY = re.compile(r"\$|usd|gbp|eur|jpy|krw|mxn|dollar|won|yen|euro|pound", re.I)
_CAPACITY = re.compile(r"\b(gb|tb|mb|gbps|tops|tflop|nm|mhz|ghz|wh|mah)\b", re.I)
_DURATION = re.compile(r"\b(day|days|week|weeks|month|months|quarter|year|years)\b", re.I)

_RULES = [
    ("DDR5", r"ddr5"),
    ("DDR4", r"ddr4"),
    ("LPDDR5", r"lpddr5|lpddr5x"),
    ("LPDDR", r"lpddr(?!5)"),
    ("HBM4", r"hbm4"),
    ("HBM3E", r"hbm3e"),
    ("HBM", r"\bhbm\b"),
    ("DRAM (generic)", r"dram"),
    ("NAND / SSD", r"nand|\bssd\b|solid.state|3d nand"),
    ("AI-GPU", r"h100|h200|a100|b200|blackwell|hopper|mi300|instinct|tpu|ai accelerator|ai gpu"),
    ("GPU", r"\bgpu\b|geforce|radeon|graphics card|gddr|vram"),
    ("CPU", r"\bcpu\b|ryzen|\bcore i\d|xeon|epyc|processor"),
    ("Mobile-PC SoC", r"snapdragon|mediatek|dimensity|exynos|apple silicon|\bm\d chip|soc"),
    ("Camera / CMOS", r"cmos|image sensor|camera sensor|isocell"),
    ("Display", r"oled|amoled|lcd|display panel|micro.?led"),
    ("Battery", r"battery|lithium|\bcell\b|mah\b"),
    ("Wafer / Substrate", r"wafer|substrate|abf|cowos|packaging|foundry capacity"),
    ("Raw Material", r"helium|neon|palladium|platinum|titanium|nickel|tungsten|tantalum|gallium|germanium|rare earth|cobalt|lithium carbonate"),
    ("Fabrication", r"asml|euv|duv|lithography|globalfoundries|tsmc|photonics|semiconductor sector|fab\b|node"),
    ("Logistics", r"shipping|freight|logistics|port|tariff route|supply route"),
    ("MLCC / Passives", r"mlcc|capacitor|passive component"),
    ("Policy / Trade", r"\bact\b|free trade agreement|import ban|export control|\bfcc\b|sanction|tariff|entity list|de minimis|connected vehicle|weee|dpa"),
    ("Smartphone", r"iphone|galaxy|pixel|xiaomi|huawei|foldable|smartphone|handset"),
    ("Wireless", r"\b5g\b|\b6g\b|modem|wi-?fi|network chip"),
    ("AI Model", r"gpt|llama|gemini|claude|model release|frontier model|foundation model"),
    ("Data Center", r"data center|datacenter|hyperscaler|server buildout|cloud capacity"),
    ("Semiconductor (broad)", r"semiconductor|chipmaker|integrated circuit"),
    ("Memory (generic)", r"\bmemory\b|\bram\b|\d+\s?gb ram"),
]

_COMPILED = [(name, re.compile(pat, re.I)) for name, pat in _RULES]


def component_tag(text: str) -> str:
    blob = text or ""
    for name, pattern in _COMPILED:
        if pattern.search(blob):
            return name
    return "Other"


def _value_family(unit: str, claim: str) -> str:
    unit = (unit or "").lower()
    blob = f"{unit} {claim or ''}"
    if _PRICE_PCT.search(blob):
        return "price_pct"
    if _MONEY.search(blob):
        return "price_money"
    if _CAPACITY.search(blob):
        return "capacity_perf"
    if _DURATION.search(blob):
        return "duration"
    return "other"


def _value_usd(value, unit: str):
    if value is None:
        return None
    unit = (unit or "").lower()
    for code, rate in config.FX_TO_USD.items():
        if code in unit:
            return round(float(value) * rate, 2)
    return None


def build(run_id: str) -> int:
    findings = (
        spark()
        .sql(
            f"SELECT finding_id, agent AS domain, device_category, finding_type, "
            f"direction, confidence, value_num, value_unit, event_kind, policy_type, "
            f"corridor_relevance, claim, basis, support_span, source_url, article_title, "
            f"effective_from FROM {config.TABLES.findings}"
        )
        .toPandas()
    )
    if len(findings) == 0:
        return 0

    findings["component_group"] = (
        findings["claim"].fillna("") + " " + findings["article_title"].fillna("")
    ).map(component_tag)
    findings["signed_strength"] = findings.apply(
        lambda r: round(
            (r["direction"] if pd.notna(r["direction"]) else 0)
            * (r["confidence"] if pd.notna(r["confidence"]) else 0.0), 4
        ),
        axis=1,
    )
    findings["value_family"] = findings.apply(
        lambda r: _value_family(r["value_unit"], r["claim"]), axis=1
    )
    findings["value_usd"] = findings.apply(
        lambda r: _value_usd(r["value_num"], r["value_unit"]), axis=1
    )
    findings["raw_value"] = findings["value_num"]
    findings["raw_unit"] = findings["value_unit"]

    findings["magnitude_z"] = None
    for family, group in findings.groupby("value_family"):
        numeric = group["value_num"].dropna()
        if len(numeric) >= 3 and numeric.std(ddof=0) > 0:
            mean, std = numeric.mean(), numeric.std(ddof=0)
            for idx in group.index:
                val = findings.at[idx, "value_num"]
                if val is not None and val == val:
                    findings.at[idx, "magnitude_z"] = round((val - mean) / std, 3)

    findings["built_run_id"] = run_id
    findings["built_at"] = datetime.now(timezone.utc)

    spark().createDataFrame(findings).write.mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable(config.TABLES.feature_vector)
    return len(findings)
