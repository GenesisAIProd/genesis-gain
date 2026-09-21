
# genesis_agent1_monthly.py
# The monthly run. Scheduled as a Databricks Workflow; runs as a service identity so
# the restricted tools are callable.
#
# Order matters: the customer queue goes FIRST because those are chips people actually
# asked for, then the rest of the backlog.
#
# Every run exports the FULL database, dated, so nothing is overwritten and any month
# can be recovered. CSV for people, JSON for machines, plus a summary of what changed.

import json, io, csv, datetime


class MonthlyRun:

    def __init__(self, spark, dbutils, agent, queue_service, catalog):
        if not catalog:
            raise ValueError("catalog must be stated, for example genesis_prod")
        for _part in (agent, queue_service):
            if _part is not None and getattr(_part, "cat", catalog) != catalog:
                raise ValueError("catalog mismatch: " + type(_part).__name__ + " uses " +
                                 str(_part.cat) + ", this " + type(self).__name__ +
                                 " uses " + catalog + ". Build every part on one catalog.")
        self.spark = spark
        self.du = dbutils
        self.agent = agent
        self.qs = queue_service
        self.cat = catalog
        self.exports = "/Volumes/" + catalog + "/market_config/exports"

    def run(self, run_id=None, verbose=True):
        run_id = run_id or ("run_" + datetime.datetime.now().strftime("%Y%m"))
        started = datetime.datetime.now()
        out = {"run_id": run_id, "started_at": str(started)}

        # Stop before doing any work if this catalog is incomplete. A run against
        # empty config must FAIL so the job turns red and the failure email goes
        # out, never report success having done nothing. Lesson from the DRA.
        import genesis_agent1_setup as gsetup
        gsetup.ensure_tables(self.spark, self.cat)
        out["preflight"] = gsetup.preflight(self.spark, self.cat, self.du)

        before = self._counts()

        if verbose:
            print("=" * 60)
            print("MONTHLY RUN", run_id)
            print("=" * 60)
            print("before:", before)

        # 1. customer demand first
        if verbose:
            print("\n[1/5] chips customers asked for")
        out["queue"] = self.qs.process_queue(run_id=run_id, verbose=verbose)

        # 2. the rest of the backlog
        if verbose:
            print("\n[2/5] the rest of the backlog")
        todo = self.agent.list_unresolved()
        out["backfill"] = self.agent.run_backfill(todo=todo, verbose=verbose) \
                          if todo else {"attempted": 0, "tally": {}}

        # 3. export the full database, dated
        if verbose:
            print("\n[3/5] export")
        out["export"] = self.export(run_id)

        # 4. quality checks
        if verbose:
            print("\n[4/5] quality checks")
        out["quality"] = self.quality()
        if verbose:
            for k, v in out["quality"].items():
                print("   %-30s %s" % (k, v))

        # 5. what changed
        after = self._counts()
        out["before"] = before
        out["after"] = after
        out["added"] = {k: after.get(k, 0) - before.get(k, 0) for k in after}
        out["completed_at"] = str(datetime.datetime.now())
        out["seconds"] = round((datetime.datetime.now() - started).total_seconds())

        if verbose:
            print("\n[5/5] summary")
            print("   added:", out["added"])
            print("   files:", out["export"])
            print("   took : %d s" % out["seconds"])

        self._write_summary(run_id, out)
        self._log_mlflow(run_id, out)
        return out

    # ---------------- counts, for the before and after ----------------
    def _counts(self):
        c = self.cat
        q = lambda s: self.spark.sql(s).first()[0]
        return {
          "processors_with_facts": q(
            "SELECT COUNT(DISTINCT subject_key) FROM " + c +
            ".market_silver.reference_facts WHERE source_name <> 'Genesis reference sheet'"),
          "total_facts": q("SELECT COUNT(*) FROM " + c + ".market_silver.reference_facts"),
          "backlog": q("SELECT COUNT(*) FROM " + c + ".market_config.processor_backlog"),
          "queue_resolved": q("SELECT COUNT(*) FROM " + c +
                              ".market_config.query_queue WHERE status='resolved'"),
          "queue_waiting": q("SELECT COUNT(*) FROM " + c +
                             ".market_config.query_queue WHERE status='queued'"),
        }

    # ---------------- the full export ----------------
    def export(self, run_id):
        stamp = run_id.replace("run_", "")
        stamp = stamp[:4] + "_" + stamp[4:] if len(stamp) == 6 else stamp
        base = self.exports + "/genesis_processor_database_" + stamp

        # Reads market_gold.processor_reference, the SAME view MSV and the models join
        # on, so the download follows the same rules: Geekbench 7 if available, else
        # Geekbench 6, else any Geekbench, always as a pair from one source; PassMark in
        # its own columns because it is a different scale. The JSON below still carries
        # EVERY fact from EVERY source for anyone who wants them all.
        cols = ["processor_key", "processor_brand", "processor_family", "processor_number",
                "device_class", "cores", "threads", "geekbench_single", "geekbench_multi",
                "geekbench_version", "geekbench_source", "passmark_single", "passmark_multi",
                "last_updated", "geekbench_url", "passmark_url"]
        rows = self.spark.sql("SELECT " + ", ".join(cols) + " FROM " + self.cat +
                              ".market_gold.processor_reference ORDER BY device_class, "
                              "processor_brand, processor_family, processor_number").collect()
        recs = []
        for r in rows:
            d = r.asDict()
            d["last_updated"] = str(d["last_updated"]) if d["last_updated"] else None
            recs.append(d)

        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for d in recs:
            w.writerow(d)
        self.du.fs.put(base + ".csv", buf.getvalue(), True)

        # JSON keeps every fact row separately, including several sources per fact
        facts = {}
        for r in self.spark.sql(
                "SELECT subject_key, fact_name, fact_value, fact_unit, source_name, "
                "source_url, retrieved_at FROM " + self.cat +
                ".market_silver.reference_facts ORDER BY subject_key, fact_name, "
                "source_name").collect():
            d = r.asDict()
            d["retrieved_at"] = str(d["retrieved_at"])
            facts.setdefault(d.pop("subject_key"), []).append(d)
        payload = {"run_id": run_id, "generated_at": str(datetime.datetime.now()),
                   "processor_count": len(recs),
                   "processors": [dict(rec, facts=facts.get(rec["processor_key"], []))
                                  for rec in recs]}
        self.du.fs.put(base + ".json", json.dumps(payload, indent=2, default=str), True)
        return {"csv": base + ".csv", "json": base + ".json", "rows": len(recs)}

    # ---------------- quality checks, every month ----------------
    def quality(self):
        c = self.cat
        q = lambda s: self.spark.sql(s).first()[0]
        return {
          "total_facts": q("SELECT COUNT(*) FROM " + c + ".market_silver.reference_facts"),
          "duplicate_fact_ids": q(
            "SELECT COUNT(*) - COUNT(DISTINCT fact_id) FROM " + c +
            ".market_silver.reference_facts"),
          "facts_without_url": q(
            "SELECT COUNT(*) FROM " + c + ".market_silver.reference_facts WHERE "
            "(source_url IS NULL OR source_url='') AND source_name <> "
            "'Genesis reference sheet'"),
          "facts_without_timestamp": q(
            "SELECT COUNT(*) FROM " + c +
            ".market_silver.reference_facts WHERE retrieved_at IS NULL"),
          "detail_without_page_link": q(
            "SELECT COUNT(*) FROM " + c +
            ".market_silver.processor_detail WHERE page_id IS NULL"),
          "chips_where_sources_disagree": q(
            "SELECT COUNT(*) FROM (SELECT subject_key FROM " + c +
            ".market_silver.reference_facts WHERE fact_name='cores' GROUP BY subject_key "
            "HAVING COUNT(DISTINCT fact_value) > 1)"),
          "impossible_scores": q(
            "SELECT COUNT(*) FROM " + c + ".market_silver.reference_facts a JOIN " + c +
            ".market_silver.reference_facts b ON a.subject_key=b.subject_key AND "
            "a.source_name=b.source_name WHERE a.fact_name='multi_core_score' AND "
            "b.fact_name='single_core_score' AND CAST(a.fact_value AS DOUBLE) < "
            "CAST(b.fact_value AS DOUBLE) AND a.source_name <> 'Genesis reference sheet'"),
          "cores_out_of_range": q(
            "SELECT COUNT(*) FROM " + c + ".market_silver.reference_facts WHERE "
            "fact_name='cores' AND (CAST(fact_value AS INT) < 1 OR "
            "CAST(fact_value AS INT) > 256)"),
        }

    def _write_summary(self, run_id, out):
        stamp = run_id.replace("run_", "")
        stamp = stamp[:4] + "_" + stamp[4:] if len(stamp) == 6 else stamp
        path = self.exports + "/genesis_monthly_summary_" + stamp + ".json"
        self.du.fs.put(path, json.dumps(out, indent=2, default=str), True)
        out["export"]["summary"] = path

    def _log_mlflow(self, run_id, out):
        try:
            import mlflow
            # dev keeps its existing experiment; every other catalog gets its own, so
            # production history is never mixed with development test runs
            base = "/Shared/genesis_agent_1_processor"
            mlflow.set_experiment(base if self.cat == "genesis_devp" else
                                  base + "_" + self.cat.replace("genesis_", ""))
            with mlflow.start_run(run_name="monthly_" + run_id):
                mlflow.log_params({"run_id": run_id, "catalog": self.cat,
                                   "export_csv": out["export"]["csv"],
                                   "export_json": out["export"]["json"]})
                m = {}
                for k, v in out["after"].items():
                    m["after_" + k] = float(v)
                for k, v in out["added"].items():
                    m["added_" + k] = float(v)
                for k, v in out["quality"].items():
                    m["quality_" + k] = float(v)
                m["seconds"] = float(out["seconds"])
                mlflow.log_metrics(m)
        except Exception as e:
            out["mlflow_error"] = str(e)[:120]
