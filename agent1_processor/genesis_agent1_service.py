
# genesis_agent1_service.py
# The one place the logic lives. The MCP server, the Streamlit app and any REST
# endpoint all call this, so there is one path rather than three.
#
# Caller identity is PASSED IN, never inferred. Databricks Apps supplies the signed-in
# user; outside an App there is no such header and an inferred identity would silently
# treat an anonymous caller as internal.

import re, json, uuid, datetime, io, csv

INLINE_CHIP_LIMIT = 20
JOB_CHIP_LIMIT    = 100
SESSION_QUERIES_PER_HOUR = 20
SESSION_JOBS_PER_HOUR    = 3

OPEN_TOOLS       = {"ask", "ask_many", "job_status", "job_results",
                    "source_health", "queue_status"}
RESTRICTED_TOOLS = {"run_backfill", "process_queue", "submit_csv"}


def _put_file(path, text):
    # The Databricks Files API works in notebooks, jobs and apps alike. dbutils exists only
    # in the first two, and the dashboard app has no dbutils of its own. Replaces a branch
    # that, without dbutils, skipped writing and still returned the paths as if written.
    from databricks.sdk import WorkspaceClient
    WorkspaceClient().files.upload(path, io.BytesIO(text.encode("utf-8")), overwrite=True)


class Caller:
    # Who is asking. genesis_user and api are internal; session is an external
    # customer on the shared demo link.

    def __init__(self, owner_type, owner_id):
        if owner_type not in ("genesis_user", "session", "api"):
            raise ValueError("owner_type must be genesis_user, session or api")
        self.owner_type = owner_type
        self.owner_id = owner_id

    @property
    def internal(self):
        return self.owner_type in ("genesis_user", "api")

    def may_call(self, tool):
        if tool in OPEN_TOOLS:
            return True
        return self.internal


class Agent1Service:

    def __init__(self, spark, queue_service, agent, matcher, catalog):
        if not catalog:
            raise ValueError("catalog must be stated, for example genesis_prod")
        for _part in (queue_service, agent, matcher):
            if _part is not None and getattr(_part, "cat", catalog) != catalog:
                raise ValueError("catalog mismatch: " + type(_part).__name__ + " uses " +
                                 str(_part.cat) + ", this " + type(self).__name__ +
                                 " uses " + catalog + ". Build every part on one catalog.")
        self.spark = spark
        self.qs = queue_service
        self.agent = agent
        self.mt = matcher
        self.cat = catalog

    # ---------------- rate limiting, external sessions only ----------------
    def _check_limit(self, caller, kind):
        if caller.internal:
            self._touch_limit(caller, kind, enforce=False)
            return True, ""
        row = self.spark.sql(
            "SELECT queries_used, jobs_used FROM " + self.cat +
            ".market_config.session_limit WHERE owner_id='" +
            caller.owner_id.replace("'", "") + "' AND window_start = date_trunc('hour', "
            "current_timestamp())").first()
        q = (row["queries_used"] if row else 0) or 0
        j = (row["jobs_used"] if row else 0) or 0
        if kind == "query" and q >= SESSION_QUERIES_PER_HOUR:
            return False, ("Rate limit reached: " + str(SESSION_QUERIES_PER_HOUR) +
                           " queries per hour on the demo link. Please try again shortly.")
        if kind == "job" and j >= SESSION_JOBS_PER_HOUR:
            return False, ("Rate limit reached: " + str(SESSION_JOBS_PER_HOUR) +
                           " jobs per hour on the demo link.")
        self._touch_limit(caller, kind, enforce=True)
        return True, ""

    def _touch_limit(self, caller, kind, enforce):
        qd = 1 if kind == "query" else 0
        jd = 1 if kind == "job" else 0
        blocked_expr = "false"
        if enforce:
            blocked_expr = ("(t.queries_used + " + str(qd) + " >= " +
                            str(SESSION_QUERIES_PER_HOUR) + " OR t.jobs_used + " +
                            str(jd) + " >= " + str(SESSION_JOBS_PER_HOUR) + ")")
        self.spark.sql(
            "MERGE INTO " + self.cat + ".market_config.session_limit t USING (SELECT '" +
            caller.owner_id.replace("'", "") + "' AS owner_id, '" + caller.owner_type +
            "' AS owner_type, date_trunc('hour', current_timestamp()) AS window_start) s "
            "ON t.owner_id=s.owner_id AND t.window_start=s.window_start "
            "WHEN MATCHED THEN UPDATE SET t.queries_used = t.queries_used + " + str(qd) +
            ", t.jobs_used = t.jobs_used + " + str(jd) +
            ", t.last_seen_at = current_timestamp(), t.blocked = " + blocked_expr +
            " WHEN NOT MATCHED THEN INSERT (owner_id, owner_type, window_start, "
            "queries_used, jobs_used, last_seen_at, blocked) VALUES (s.owner_id, "
            "s.owner_type, s.window_start, " + str(qd) + ", " + str(jd) +
            ", current_timestamp(), false)")

    # ============================ TOOL: ask ============================
    def ask(self, caller, query):
        if not caller.may_call("ask"):
            return {"error": "not permitted"}
        ok, msg = self._check_limit(caller, "query")
        if not ok:
            return {"status": "rate_limited", "message": msg}
        return self.qs.ask(query)

    # ============================ TOOL: ask_many ============================
    def ask_many(self, caller, query):
        # Inline when small, a job when large. The customer never has to choose.
        if not caller.may_call("ask_many"):
            return {"error": "not permitted"}
        chips = self.qs.split_many(query)
        if len(chips) > INLINE_CHIP_LIMIT:
            return self.submit_job(caller, query, chips, entry_point="chat")
        ok, msg = self._check_limit(caller, "query")
        if not ok:
            return {"status": "rate_limited", "message": msg}
        return self.qs.ask_many(query)

    # ============================ TOOL: submit_job ============================
    def submit_job(self, caller, raw_input, chips=None, entry_point="chat"):
        ok, msg = self._check_limit(caller, "job")
        if not ok:
            return {"status": "rate_limited", "message": msg}
        chips = chips if chips is not None else self.qs.split_many(raw_input)
        overflow = max(0, len(chips) - JOB_CHIP_LIMIT)
        chips = chips[:JOB_CHIP_LIMIT]
        job_id = uuid.uuid4().hex
        note = None
        if overflow:
            note = ("capped at " + str(JOB_CHIP_LIMIT) + ", " + str(overflow) +
                    " items not processed")
        self._write_job(job_id, caller, entry_point, raw_input, len(chips), "queued", note)
        return {"job_id": job_id, "status": "queued", "chip_count": len(chips),
                "note": note,
                "message": "Your request is running. Results will appear here shortly."}

    # ============================ TOOL: run_job ============================
    def run_job(self, job_id):
        # Called by the app or the scheduler, not by a customer.
        j = self.spark.sql("SELECT * FROM " + self.cat + ".market_config.job "
                           "WHERE job_id='" + job_id + "'").first()
        if not j:
            return {"error": "no such job"}
        self.spark.sql("UPDATE " + self.cat + ".market_config.job SET status='running', "
                       "started_at=current_timestamp() WHERE job_id='" + job_id + "'")
        chips = self.qs.split_many(j["raw_input"])[:JOB_CHIP_LIMIT]
        items = []
        for i, c in enumerate(chips, 1):
            r = self.qs.answer_parts(c.get("company", "") or "", c.get("family", "") or "",
                                     c.get("model_number", "") or "", j["raw_input"])
            f = {}
            srcs = []
            for x in (r.get("facts") or []):
                f[x["fact_name"]] = x["fact_value"]
                srcs.append(x["source_name"])
            def _i(k):
                v = f.get(k, "")
                return int(v) if str(v).isdigit() else None
            items.append((job_id, i, r.get("asked_about", ""), c.get("company", ""),
                          c.get("family", ""), c.get("model_number", ""), r["status"],
                          r.get("processor_key"), _i("cores"), _i("single_core_score"),
                          _i("multi_core_score"), ", ".join(sorted(set(srcs))),
                          float(r.get("match_score") or 0.0), datetime.datetime.now()))
        self._write_items(items)
        jp, fp = self._write_results(job_id, items)
        self.spark.sql("UPDATE " + self.cat + ".market_config.job SET status='complete', "
                       "completed_at=current_timestamp(), result_json_path='" + jp +
                       "', result_file_path='" + fp + "' WHERE job_id='" + job_id + "'")
        return {"job_id": job_id, "status": "complete", "items": len(items),
                "download": fp}

    # ============================ TOOL: job_status ============================
    def job_status(self, caller, job_id):
        j = self.spark.sql("SELECT job_id, owner_id, status, chip_count, submitted_at, "
                           "completed_at, note FROM " + self.cat + ".market_config.job "
                           "WHERE job_id='" + job_id.replace("'", "") + "'").first()
        if not j:
            return {"error": "no such job"}
        if j["owner_id"] != caller.owner_id and not caller.internal:
            return {"error": "not permitted"}
        out = {}
        for k in ["job_id", "status", "chip_count", "submitted_at", "completed_at", "note"]:
            out[k] = j[k]
        return out

    # ============================ TOOL: job_results ============================
    def job_results(self, caller, job_id):
        st = self.job_status(caller, job_id)
        if "error" in st:
            return st
        rows = []
        for r in self.spark.sql(
                "SELECT seq, raw_text, status, matched_key, cores, single_core, "
                "multi_core, source_names, match_score FROM " + self.cat +
                ".market_config.job_item WHERE job_id='" + job_id.replace("'", "") +
                "' ORDER BY seq").collect():
            rows.append(r.asDict())
        j = self.spark.sql("SELECT result_file_path FROM " + self.cat +
                           ".market_config.job WHERE job_id='" + job_id + "'").first()
        return {"job_id": job_id, "status": st["status"], "results": rows,
                "download": j["result_file_path"] if j else None}

    # ============================ TOOL: source_health ============================
    def source_health(self, caller):
        return self.agent.source_health()

    # ============================ TOOL: queue_status ============================
    def queue_status(self, caller):
        return self.qs.queue_status()

    # ============================ TOOL: submit_csv / submit_file ============================
    # INTERNAL ONLY: external sessions can query but never upload. Enforced here by caller
    # type, so it holds whatever any screen shows. The logic lives in genesis_agent1_upload.
    def submit_file(self, caller, filename, data, column=None):
        if not caller.may_call("submit_csv"):
            return {"error": "not permitted", "detail": "File upload is for Genesis staff only."}
        import genesis_agent1_upload as up
        rows, cols, err = up.parse_upload(filename, data)
        if err:
            return {"status": "rejected", "message": err}
        return up.process(self, caller, rows, cols, column, "csv", filename)

    def submit_csv(self, caller, rows, column=None):
        if not caller.may_call("submit_csv"):
            return {"error": "not permitted", "detail": "File upload is for Genesis staff only."}
        import genesis_agent1_upload as up
        rows = [{str(k): ("" if v is None else str(v)) for k, v in (r or {}).items()}
                for r in (rows or [])]
        cols = list(rows[0].keys()) if rows else []
        return up.process(self, caller, rows, cols, column, "api", "rows sent through the API")

    def _reference_rows(self, keys):
        # the SAME view MSV joins on, so an upload follows the same Geekbench rule
        keys = sorted({k for k in keys if k})
        if not keys:
            return {}
        inlist = ",".join("'" + k.replace("'", "") + "'" for k in keys)
        return {r["processor_key"]: r.asDict() for r in self.spark.sql(
            "SELECT * FROM " + self.cat + ".market_gold.processor_reference "
            "WHERE processor_key IN (" + inlist + ")").collect()}

    def _write_files(self, job_id, csv_text, json_text):
        base = "/Volumes/" + self.cat + "/market_config/exports/upload_" + job_id[:12]
        _put_file(base + ".csv", csv_text)
        _put_file(base + ".json", json_text)
        return base + ".csv", base + ".json"

    def _finish_job(self, job_id, json_path, csv_path):
        self.spark.sql("UPDATE " + self.cat + ".market_config.job SET status='complete', "
                       "completed_at=current_timestamp(), result_json_path='" + json_path +
                       "', result_file_path='" + csv_path + "' WHERE job_id='" + job_id + "'")

    # ---------------- writers ----------------
    def _write_job(self, job_id, caller, entry_point, raw_input, n, status, note):
        from pyspark.sql.types import (StructType, StructField, StringType,
                                       IntegerType, TimestampType)
        s = StructType([StructField("job_id", StringType()),
                        StructField("owner_type", StringType()),
                        StructField("owner_id", StringType()),
                        StructField("entry_point", StringType()),
                        StructField("raw_input", StringType()),
                        StructField("chip_count", IntegerType()),
                        StructField("status", StringType()),
                        StructField("submitted_at", TimestampType()),
                        StructField("started_at", TimestampType()),
                        StructField("completed_at", TimestampType()),
                        StructField("result_json_path", StringType()),
                        StructField("result_file_path", StringType()),
                        StructField("note", StringType())])
        now = datetime.datetime.now()
        self.spark.createDataFrame(
            [(job_id, caller.owner_type, caller.owner_id, entry_point,
              (raw_input or "")[:4000], n, status, now, None, None, None, None, note)],
            s).write.format("delta").mode("append") \
            .saveAsTable(self.cat + ".market_config.job")

    def _write_items(self, items):
        from pyspark.sql.types import (StructType, StructField, StringType,
                                       IntegerType, DoubleType, TimestampType)
        if not items:
            return
        s = StructType([StructField("job_id", StringType()),
                        StructField("seq", IntegerType()),
                        StructField("raw_text", StringType()),
                        StructField("parsed_company", StringType()),
                        StructField("parsed_family", StringType()),
                        StructField("parsed_model", StringType()),
                        StructField("status", StringType()),
                        StructField("matched_key", StringType()),
                        StructField("cores", IntegerType()),
                        StructField("single_core", IntegerType()),
                        StructField("multi_core", IntegerType()),
                        StructField("source_names", StringType()),
                        StructField("match_score", DoubleType()),
                        StructField("created_at", TimestampType())])
        self.spark.createDataFrame(items, s).write.format("delta").mode("append") \
            .saveAsTable(self.cat + ".market_config.job_item")

    def _write_results(self, job_id, items):
        # Always BOTH: JSON for the screen, a file for download.
        base = "/Volumes/" + self.cat + "/market_config/exports/job_" + job_id[:12]
        cols = ["job_id", "seq", "raw_text", "company", "family", "model", "status",
                "matched_key", "cores", "single_core", "multi_core", "sources",
                "match_score", "created_at"]
        recs = []
        for it in items:
            vals = []
            for x in it:
                vals.append(str(x) if isinstance(x, datetime.datetime) else x)
            recs.append(dict(zip(cols, vals)))
        jp = base + ".json"
        fp = base + ".csv"
        payload = json.dumps({"job_id": job_id, "results": recs}, indent=2, default=str)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for r in recs:
            w.writerow(r)
        _put_file(jp, payload)
        _put_file(fp, buf.getvalue())
        return jp, fp
