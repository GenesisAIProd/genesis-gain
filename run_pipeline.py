"""Job entrypoint for the scheduled pipeline run.

Runs as a Databricks Job task once a week on Monday. Every run is a full sweep
over the fetch window, which is set wider than the weekly cadence so overlapping
days re-catch anything published late. On completion it evaluates the budget and
emails an alert if a threshold is crossed. Exits non-zero on failure so the
scheduler surfaces it.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from . import config
from .alerts import send_budget_alert
from .orchestrator import run_pipeline


def _incremental_today() -> bool:
    return datetime.now(timezone.utc).weekday() == 3


def main() -> int:
    incremental = False
    try:
        report = run_pipeline(
            use_cache=False,
            reconcile_window=config.PRODUCTION_WINDOW_DAYS,
            incremental=incremental,
        )
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        return 1

    budget = report.get("budget", {})
    if budget.get("weekly_exceeded") or budget.get("monthly_exceeded"):
        try:
            send_budget_alert(report)
        except Exception:
            pass

    print(json.dumps(
        {
            "status": report.get("status"),
            "mode": report.get("mode"),
            "run_id": report["run_id"],
            "agents": report["agents"],
            "run_cost_usd": report.get("run_cost_usd"),
            "budget": budget,
            "degraded": report.get("degraded"),
            "backup_path": report.get("backup_path"),
        },
        default=str,
    ))
    return 0


def run() -> int:
    return main()


if __name__ == "__main__":
    sys.exit(main())
