"""Dated history and export files for every DRA run. Added 22 Sep 2026.

The two feature tables are rebuilt from scratch on every run, so on their own they keep
no history: each week's market read replaced the last. After every successful run this
module:

  1. appends the run's market read (feature index) and signal table (feature vector) to
     two history tables that are only ever appended to, stamped with the run date and
     run ID, so the AI model and the dashboard can see every week, not just the latest;
  2. writes the market read, the signal table and the findings this run added, each as
     CSV and JSON, plus one Excel workbook with all three as sheets, into a folder named
     by date AND run ID, so a second run on the same day never replaces the first.

Recording the same run twice replaces that run's rows instead of duplicating them.
Any failure here raises, so the job fails and the failure email goes out, rather than
a week silently going missing.
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timezone

import pandas as pd

from . import config
from .context import spark

INDEX_HISTORY = "dra_feature_index_history"
VECTOR_HISTORY = "dra_feature_vector_history"
EXPORT_ROOT = os.environ.get(
    "GAIN_EXPORT_VOLUME", f"/Volumes/{config.CATALOG}/{config.SCHEMA}/exports"
)


def _neutral(value):
    # A news claim that starts with =, +, - or @ would run as a formula in Excel.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _for_files(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for col in out.columns:
        if isinstance(out[col].dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_convert(None)
        # Text columns may be "object" or pandas' dedicated string type, depending on
        # the pandas version; both must be neutralised.
        if out[col].dtype == object or pd.api.types.is_string_dtype(out[col]):
            out[col] = out[col].map(_neutral)
    return out


def write_exports(frames: dict, folder: str) -> dict:
    """frames: {sheet name: DataFrame}. Writes CSV and JSON per frame, and one workbook."""
    os.makedirs(folder, exist_ok=True)
    written = {}
    for name, frame in frames.items():
        clean = _for_files(frame)
        clean.to_csv(f"{folder}/{name}.csv", index=False)
        clean.to_json(f"{folder}/{name}.json", orient="records", date_format="iso", indent=2)
        written[name] = len(clean)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for name, frame in frames.items():
            _for_files(frame).to_excel(xl, sheet_name=name[:31], index=False)
    with open(f"{folder}/genesis_dra_{os.path.basename(folder)}.xlsx", "wb") as fh:
        fh.write(buf.getvalue())
    for name in frames:
        for ext in ("csv", "json"):
            if not os.path.exists(f"{folder}/{name}.{ext}"):
                raise RuntimeError(f"export file missing after writing: {folder}/{name}.{ext}")
    return written


def record(run_id: str, run_at: datetime | None = None) -> dict:
    s = spark()
    run_at = run_at or datetime.now(timezone.utc)
    stamp = run_at.strftime("%Y-%m-%d %H:%M:%S")

    # 1. history tables: append only, one run's rows replaced if recorded again
    for source, target in ((config.TABLES.feature_index, INDEX_HISTORY),
                           (config.TABLES.feature_vector, VECTOR_HISTORY)):
        frame = s.sql(
            f"SELECT '{run_id}' AS run_id, CAST(TIMESTAMP '{stamp}' AS DATE) AS run_date, "
            f"TIMESTAMP '{stamp}' AS run_at, * FROM {source}"
        )
        if frame.count() == 0:
            raise RuntimeError(f"{source} is empty after the run; nothing to record")
        if s.catalog.tableExists(target):
            s.sql(f"DELETE FROM {target} WHERE run_id = '{run_id}'")
        frame.write.mode("append").option("mergeSchema", "true").saveAsTable(target)

    # 2. dated files
    if EXPORT_ROOT.startswith(f"/Volumes/{config.CATALOG}/{config.SCHEMA}/exports"):
        s.sql(f"CREATE VOLUME IF NOT EXISTS {config.CATALOG}.{config.SCHEMA}.exports")
    folder = f"{EXPORT_ROOT}/{run_at:%Y_%m_%d}_{run_id[:8]}"
    frames = {
        "market_read": s.table(config.TABLES.feature_index).toPandas(),
        "signals": s.table(config.TABLES.feature_vector).toPandas(),
        "new_findings": s.sql(
            f"SELECT * FROM {config.TABLES.findings} WHERE run_id = '{run_id}'"
        ).toPandas(),
    }
    rows = write_exports(frames, folder)
    return {"folder": folder, "rows": rows, "run_date": run_at.strftime("%Y-%m-%d")}
