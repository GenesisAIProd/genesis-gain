"""Shared runtime handles: Spark, secret retrieval, and the model client.

Everything that touches the Databricks runtime is centralised here so the rest
of the package depends on small, mockable functions rather than global state.
"""
from __future__ import annotations

import functools
import json
import re
from typing import Any

from . import config


@functools.lru_cache(maxsize=1)
def spark():
    from pyspark.sql import SparkSession

    session = SparkSession.builder.getOrCreate()
    session.sql(f"USE CATALOG {config.CATALOG}")
    session.sql(f"USE SCHEMA {config.SCHEMA}")
    return session


@functools.lru_cache(maxsize=1)
def workspace():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def secret(key: str) -> str:
    from pyspark.dbutils import DBUtils

    dbutils = DBUtils(spark())
    return dbutils.secrets.get(scope=config.SECRET_SCOPE, key=key)


@functools.lru_cache(maxsize=1)
def apify():
    from apify_client import ApifyClient

    return ApifyClient(secret(config.APIFY_TOKEN_KEY))


def call_model(endpoint: str, system: str, user: str, max_tokens: int = 1500) -> str:
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

    request = {
        "name": endpoint,
        "messages": [
            ChatMessage(role=ChatMessageRole.SYSTEM, content=system),
            ChatMessage(role=ChatMessageRole.USER, content=user),
        ],
        "max_tokens": max_tokens,
    }
    if "opus" not in endpoint:
        request["temperature"] = 0.0
    response = workspace().serving_endpoints.query(**request)
    return response.choices[0].message.content


def extract_json(raw: str) -> tuple[dict | None, str | None]:
    if not raw:
        return None, "empty response"
    text = raw.strip().replace("```json", "").replace("```", "")
    depth, start, candidates = 0, None, []
    for i, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start : i + 1])
    for block in reversed(candidates):
        try:
            return json.loads(block), None
        except json.JSONDecodeError:
            continue
    return None, "no parseable json"
