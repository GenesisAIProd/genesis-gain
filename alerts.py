"""Budget alert email.

When a run crosses the weekly or monthly budget, the whole distribution is
emailed a short summary with the run cost and the week and month totals. SMTP
settings come from the environment so no credentials sit in code. If SMTP is not
configured the alert is written to a table the Job email notification can watch.
"""
from __future__ import annotations

import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from .context import spark

ALERT_TABLE = "dra_budget_alerts"

RECIPIENTS = [
    "data.ai@genesisig.com",
    "rmenon@genesisig.com",
    "jsedej@genesisig.com",
    "edominguez@genesisig.com",
    "jortiz@genesisig.com",
    "rhernandez@genesisig.com",
    "fpantoja@genesisig.com",
]

ALERT_SCHEMA = StructType(
    [
        StructField("run_id", StringType()),
        StructField("week_to_date_usd", DoubleType()),
        StructField("month_to_date_usd", DoubleType()),
        StructField("weekly_exceeded", BooleanType()),
        StructField("monthly_exceeded", BooleanType()),
        StructField("body", StringType()),
        StructField("created_at", TimestampType()),
    ]
)


def _compose(report: dict) -> tuple[str, str]:
    budget = report.get("budget", {})
    subject = "Genesis GAIN budget alert"
    which = []
    if budget.get("weekly_exceeded"):
        which.append("weekly")
    if budget.get("monthly_exceeded"):
        which.append("monthly")
    body = (
        f"A {' and '.join(which)} budget threshold was crossed.\n\n"
        f"Run {report.get('run_id')} cost {report.get('run_cost_usd')} USD.\n"
        f"Week to date {budget.get('week_to_date_usd')} of {budget.get('weekly_budget_usd')} USD.\n"
        f"Month to date {budget.get('month_to_date_usd')} of {budget.get('monthly_budget_usd')} USD.\n\n"
        f"This is an alert only. The pipeline was not stopped. Usage detail is in "
        f"the usage log for review."
    )
    return subject, body


def _record(report: dict, body: str) -> None:
    budget = report.get("budget", {})
    row = {
        "run_id": report.get("run_id"),
        "week_to_date_usd": float(budget.get("week_to_date_usd", 0)),
        "month_to_date_usd": float(budget.get("month_to_date_usd", 0)),
        "weekly_exceeded": bool(budget.get("weekly_exceeded")),
        "monthly_exceeded": bool(budget.get("monthly_exceeded")),
        "body": body,
        "created_at": datetime.now(timezone.utc),
    }
    spark().createDataFrame([row], ALERT_SCHEMA).write.mode("append").saveAsTable(ALERT_TABLE)


def send_budget_alert(report: dict) -> str:
    subject, body = _compose(report)
    _record(report, body)

    host = os.environ.get("GAIN_SMTP_HOST")
    if not host:
        return "recorded"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ.get("GAIN_SMTP_FROM", "data.ai@genesisig.com")
    message["To"] = ", ".join(RECIPIENTS)
    message.set_content(body)

    port = int(os.environ.get("GAIN_SMTP_PORT", "587"))
    user = os.environ.get("GAIN_SMTP_USER")
    password = os.environ.get("GAIN_SMTP_PASSWORD")
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(message)
    return "sent"
