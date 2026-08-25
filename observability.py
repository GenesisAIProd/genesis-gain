"""Token, latency, and cost accounting.

Every model call is metered and written to a usage table keyed by run, agent,
model, and pipeline stage. Cost is estimated from a per-model rate card so the
cost dashboard and the weekly and monthly budget alerts read from real usage
rather than guesses.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pyspark.sql.types import (
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from . import config
from .context import spark

USAGE_TABLE = "dra_usage_log"

USAGE_SCHEMA = StructType(
    [
        StructField("run_id", StringType()),
        StructField("agent", StringType()),
        StructField("stage", StringType()),
        StructField("model", StringType()),
        StructField("input_tokens", IntegerType()),
        StructField("output_tokens", IntegerType()),
        StructField("calls", IntegerType()),
        StructField("latency_ms", DoubleType()),
        StructField("est_cost_usd", DoubleType()),
        StructField("logged_at", TimestampType()),
    ]
)

RATE_CARD_USD_PER_1K = {
    "databricks-claude-opus-4-8": {"in": 0.015, "out": 0.075},
    "databricks-claude-sonnet-4-6": {"in": 0.003, "out": 0.015},
    "databricks-llama-4-maverick": {"in": 0.0005, "out": 0.0015},
}

WEEKLY_BUDGET_USD = float(os.environ.get("GAIN_WEEKLY_BUDGET", "75"))
MONTHLY_BUDGET_USD = float(os.environ.get("GAIN_MONTHLY_BUDGET", "300"))


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rate = RATE_CARD_USD_PER_1K.get(model)
    if not rate:
        return 0.0
    return round(
        input_tokens / 1000 * rate["in"] + output_tokens / 1000 * rate["out"], 4
    )


def approx_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


@dataclass
class Meter:
    run_id: str
    rows: list[dict] = field(default_factory=list)

    def record(self, agent: str, stage: str, model: str, input_tokens: int,
               output_tokens: int, latency_ms: float, calls: int = 1) -> None:
        self.rows.append(
            {
                "run_id": self.run_id,
                "agent": agent,
                "stage": stage,
                "model": model,
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "calls": calls,
                "latency_ms": round(latency_ms, 1),
                "est_cost_usd": estimate_cost(model, input_tokens, output_tokens),
                "logged_at": datetime.now(timezone.utc),
            }
        )

    def flush(self) -> float:
        if not self.rows:
            return 0.0
        spark().createDataFrame(self.rows, USAGE_SCHEMA).write.mode("append").saveAsTable(
            USAGE_TABLE
        )
        total = sum(r["est_cost_usd"] for r in self.rows)
        self.rows.clear()
        return round(total, 4)


@contextmanager
def timed():
    start = time.perf_counter()
    box = {}
    yield box
    box["ms"] = (time.perf_counter() - start) * 1000


def ensure_usage_table() -> None:
    spark().createDataFrame([], USAGE_SCHEMA).write.mode("append").saveAsTable(USAGE_TABLE)


def week_to_date_cost() -> float:
    row = spark().sql(
        f"SELECT COALESCE(SUM(est_cost_usd), 0) c FROM {USAGE_TABLE} "
        f"WHERE logged_at >= date_trunc('week', current_timestamp())"
    ).collect()[0]
    return round(float(row["c"]), 2)


def month_to_date_cost() -> float:
    row = spark().sql(
        f"SELECT COALESCE(SUM(est_cost_usd), 0) c FROM {USAGE_TABLE} "
        f"WHERE logged_at >= date_trunc('month', current_timestamp())"
    ).collect()[0]
    return round(float(row["c"]), 2)


def budget_status() -> dict:
    week, month = week_to_date_cost(), month_to_date_cost()
    return {
        "week_to_date_usd": week,
        "weekly_budget_usd": WEEKLY_BUDGET_USD,
        "weekly_exceeded": week > WEEKLY_BUDGET_USD,
        "month_to_date_usd": month,
        "monthly_budget_usd": MONTHLY_BUDGET_USD,
        "monthly_exceeded": month > MONTHLY_BUDGET_USD,
    }
