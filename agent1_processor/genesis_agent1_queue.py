
# genesis_agent1_queue.py
# The unknown-chip path: answer from the database, or queue for the monthly run.
#
# Proven live 16 Sep 2026, all four states:
#   matched      - held already, answered immediately
#   queued       - not held, recorded, customer told it is scheduled
#   resolved     - the monthly run sourced it (Ryzen 5 5600H: 6 cores, multi 16,347)
#   unresolvable - two failed monthly attempts, stop retrying (Intel Core i9 99999ZZ)

import re, json, hashlib, datetime

MULTI_PROMPT = (
 "<task>Find EVERY processor named in the text. A message may mention one, several, "
 "or none.</task>"
 "<rules>"
 "<rule>Drop generation words (11th Gen, 8th gen), (R), (TM), and the words CPU, "
 "processor, chip, with, Graphics.</rule>"
 "<rule>Drop clock speeds such as @ 2.40GHz.</rule>"
 "<rule>Keep PRO, Ultra, Max, Plus, Elite, Ti and plus signs when part of the name.</rule>"
 "<rule>Apple and Qualcomm SoCs have no separate model_number; put the whole name in "
 "family.</rule>"
 "<rule>Correct misspellings and non-Latin lookalike characters.</rule>"
 "<rule>Return one entry per DISTINCT processor. Comparisons name two or more.</rule>"
 "<rule>If no processor is named, return an empty list.</rule>"
 "</rules>"
 "<examples>"
 "<example><input>snapdragon 8+ gen1 vs snapdragon 8 gen 3 which is faster</input>"
 "<reasoning>A comparison naming two chips. The plus sign is part of the first name.</reasoning>"
 '<output>{"processors":[{"company":"Qualcomm","family":"Snapdragon 8 Plus Gen 1",'
 '"model_number":""},{"company":"Qualcomm","family":"Snapdragon 8 Gen 3",'
 '"model_number":""}]}</output></example>'
 "<example><input>client sent this: Core Ultra 7 265H and i9 13900KS, need scores</input>"
 "<reasoning>Two Intel chips in one message.</reasoning>"
 '<output>{"processors":[{"company":"Intel","family":"Core Ultra 7","model_number":"265H"},'
 '{"company":"Intel","family":"Core i9","model_number":"13900KS"}]}</output></example>'
 "<example><input>11th Gen Intel(R) Core(TM) i5-11400 @ 2.60GHz</input>"
 "<reasoning>One chip. Drop the generation word, legal marks and clock speed.</reasoning>"
 '<output>{"processors":[{"company":"Intel","family":"Core i5",'
 '"model_number":"11400"}]}</output></example>'
 "<example><input>Dell Latitude 5401 laptop, 16GB RAM</input>"
 "<reasoning>No processor named.</reasoning>"
 '<output>{"processors":[]}</output></example>'
 "</examples>"
 '<output_format>Return JSON only, no fences: {"processors":[{"company":"","family":"",'
 '"model_number":""}]}</output_format><input>')

Q_COLS = ["query_id","raw_query","parsed_company","parsed_family","parsed_model",
          "device_class","matched_key","matched_slug","match_score","status","attempts",
          "first_seen_at","last_seen_at","resolved_at","resolved_in_run","times_asked","note"]

def _schema():
    from pyspark.sql.types import (StructType, StructField, StringType, DoubleType,
                                   IntegerType, TimestampType)
    return StructType([
        StructField("query_id", StringType()),       StructField("raw_query", StringType()),
        StructField("parsed_company", StringType()), StructField("parsed_family", StringType()),
        StructField("parsed_model", StringType()),   StructField("device_class", StringType()),
        StructField("matched_key", StringType()),    StructField("matched_slug", StringType()),
        StructField("match_score", DoubleType()),    StructField("status", StringType()),
        StructField("attempts", IntegerType()),      StructField("first_seen_at", TimestampType()),
        StructField("last_seen_at", TimestampType()),StructField("resolved_at", TimestampType()),
        StructField("resolved_in_run", StringType()),StructField("times_asked", IntegerType()),
        StructField("note", StringType())])

def query_id(text):
    """Same question twice is one row, however it was punctuated."""
    return hashlib.sha256(re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
                          .strip().encode()).hexdigest()[:24]


class QueueService:

    def __init__(self, spark, matcher, agent, catalog):
        if not catalog:
            raise ValueError("catalog must be stated, for example genesis_prod")
        for _part in (matcher, agent):
            if _part is not None and getattr(_part, "cat", catalog) != catalog:
                raise ValueError("catalog mismatch: " + type(_part).__name__ + " uses " +
                                 str(_part.cat) + ", this " + type(self).__name__ +
                                 " uses " + catalog + ". Build every part on one catalog.")
        self.spark = spark
        self.mt = matcher
        self.agent = agent
        self.cat = catalog

    # ============================ MCP TOOL: ask_many ============================
    def split_many(self, raw_query):
        """Return every processor named in the text. Comparisons name two or more."""
        p = (MULTI_PROMPT + (raw_query or "").replace("'", " ") +
             "</input>").replace("'", "''")
        out = self.spark.sql("SELECT ai_query('databricks-claude-sonnet-4-6','" +
                             p + "') AS o").first()["o"]
        m = re.search(r"\{.*\}", re.sub(r"```[a-z]*", "", out), re.S)
        if not m:
            return []
        try:
            return json.loads(m.group(0)).get("processors", []) or []
        except Exception:
            return []

    def ask_many(self, raw_query, verbose=False):
        """Answer for EVERY chip named. Returns {"results": [...]} , one entry per chip."""
        chips = self.split_many(raw_query)
        if not chips:
            one = self.ask(raw_query, verbose)
            return {"raw_query": raw_query, "chip_count": 0, "results": [one]}
        out = []
        for c in chips:
            # do NOT re-parse: the chip is already split. Re-running ask() would send a
            # clean string back through the splitter and can change it.
            out.append(self.answer_parts(c.get("company","") or "",
                                         c.get("family","") or "",
                                         c.get("model_number","") or "",
                                         raw_query))
        return {"raw_query": raw_query, "chip_count": len(chips), "results": out}

    def answer_parts(self, comp, fam, mod, raw_query=None):
        """Answer from already-split parts. Shared by ask() and ask_many().

        The splitter sometimes repeats the brand in the family ("Apple" + "Apple A18"),
        which builds "Apple Apple A18" and matches nothing. Drop the duplicate."""
        import genesis_agent1_matching as gm
        comp, fam, mod = (comp or "").strip(), (fam or "").strip(), (mod or "").strip()
        if comp and fam.lower().startswith(comp.lower()):
            fam = fam[len(comp):].strip()
        now = datetime.datetime.now()
        piece = " ".join(x for x in [comp, fam, mod] if x).strip()
        raw = raw_query or piece
        qid = query_id(piece)
        dc = gm.device_class_for(piece)
        if not piece:
            return {"status": "no_processor_in_query", "query_id": qid,
                    "message": "I could not find a processor in that request."}
        res = self.mt.match(self.agent, comp, fam, mod)
        slug, score = res.get("slug"), float(res.get("score") or 0.0)
        key = self._key_for(slug, comp, fam, mod)
        facts = self._facts_for(key) if key else []
        if facts:
            self._write(qid, raw, comp, fam, mod, dc, key, slug, score, "matched", now, None)
            return {"status": "found", "processor_key": key, "matched_slug": slug,
                    "match_score": score, "facts": facts, "query_id": qid,
                    "asked_about": piece}
        # A FAMILY named without a model number, such as "Core i5" or "Snapdragon 8", is not
        # one processor. If we hold members of it, list them. Never report "not in the
        # database" for it, and never queue a row with no model number for the monthly run,
        # which would try to source a processor that does not exist. Found 21 Sep 2026.
        if not mod:
            members = self._family_members(comp, fam)
            if members:
                name = (comp + " " + fam).strip()
                return {"status": "family", "query_id": qid, "asked_about": piece,
                        "family": name, "members": members,
                        "message": (name + " is a processor family, not one processor. "
                                    "The database holds " + str(len(members)) + " of them. "
                                    "Ask about a specific model for a single answer.")}
        self._write(qid, raw, comp, fam, mod, dc, None, slug, score, "queued", now,
                    "awaiting the next monthly run")
        return {"status": "queued", "query_id": qid, "asked_about": piece,
                "parsed": {"company": comp, "family": fam, "model_number": mod},
                "device_class": dc,
                "message": ("This processor is not in the database yet. It has been "
                            "recorded and is scheduled to be sourced in the next "
                            "monthly run.")}

    # ============================ MCP TOOL: ask ============================
    def ask(self, raw_query, verbose=False):
        import genesis_agent1_matching as gm
        now = datetime.datetime.now()
        qid = query_id(raw_query)
        split_txt, parts = self.mt.split(raw_query)
        comp = (parts or {}).get("company", "") or ""
        fam  = (parts or {}).get("family", "") or ""
        mod  = (parts or {}).get("model_number", "") or ""
        dc = gm.device_class_for(split_txt or raw_query)

        if not (comp or fam or mod):
            self._write(qid, raw_query, comp, fam, mod, dc, None, None, 0.0,
                        "unresolvable", now, "no processor named in the query")
            return {"status": "no_processor_in_query", "query_id": qid,
                    "message": "I could not find a processor in that request."}

        # One path for every question: answer_parts holds the matching, the family rule,
        # the doubled-brand fix and the queuing. ask() used to keep its own copy, which had
        # already drifted from answer_parts. Consolidated 21 Sep 2026.
        return self.answer_parts(comp, fam, mod, raw_query)

    def _family_members(self, comp, fam):
        # Members of a family, fastest first. Whole-word prefix, so "Snapdragon 8" finds
        # "Snapdragon 8 Gen 3" but never "Snapdragon 888".
        import genesis_agent1 as ga
        c, f, _ = ga.canonical_parts(comp, fam, "")
        f = (f or "").lower().replace("'", "")
        if not f:
            return []
        where = "(lower(processor_family) = '" + f + "' OR lower(processor_family) LIKE '" + f + " %')"
        if c:
            where = "lower(processor_brand) = '" + c.lower().replace("'", "") + "' AND " + where
        return [r["processor_key"] for r in self.spark.sql(
            "SELECT processor_key FROM " + self.cat + ".market_gold.processor_reference WHERE " +
            where + " ORDER BY geekbench_single DESC NULLS LAST, processor_key LIMIT 200").collect()]

    def _key_for(self, slug, comp, fam, mod):
        """Find the processor_key for a chip. THREE routes, tried in order, and every
        one must fall through on a miss rather than returning None.

        Bug found 16 Sep 2026: the slug route returned early when no processor_detail
        row carried the slug, so chips that WERE in the backlog with facts came back
        queued. matched_slug is only written for chips resolved through slug_lookup;
        anything resolved from PassMark, Geekbench or the Genesis sheet has none.
        Snapdragon 8 Gen 3 (3 facts), Core i9 13900KS (7) and Ryzen 7 7840HS (8) were
        all told they were not in the database."""
        if slug:
            r = self.spark.sql("SELECT processor_key FROM " + self.cat +
                               ".market_silver.processor_detail WHERE matched_slug='" +
                               slug.replace("'", "") + "' LIMIT 1").first()
            if r:
                return r["processor_key"]
            # fall through: no detail row for this slug does NOT mean we lack the chip
        cand = " ".join(x for x in [comp, fam, mod] if x).strip().lower().replace("'", "")
        # normalise BOTH sides: concat_ws keeps empty strings, so a chip with no
        # model_number stores "Apple A18 " with a trailing space and an exact
        # comparison fails. This threw away a 0.9999 match.
        norm = ("regexp_replace(lower(trim(concat_ws(' ', company, family, "
                "model_number))), '\\\\s+', ' ')")
        cand_n = " ".join(cand.split())
        r = self.spark.sql("SELECT processor_key FROM " + self.cat +
                           ".market_config.processor_backlog WHERE " + norm +
                           "='" + cand_n + "' LIMIT 1").first()
        if r:
            return r["processor_key"]
        # last resort: a chip may have FACTS without a backlog row (sheet-sourced
        # chips such as the i7-4980HQ were never added to the backlog).
        r = self.spark.sql("SELECT subject_key FROM " + self.cat +
                           ".market_silver.reference_facts WHERE "
                           "regexp_replace(lower(replace(subject_key,'|',' ')), "
                           "'\\\\s+',' ')='" + cand_n + "' LIMIT 1").first()
        return r["subject_key"] if r else None

    def _facts_for(self, key):
        return [r.asDict() for r in self.spark.sql(
            "SELECT fact_name, fact_value, fact_unit, source_name, source_url, retrieved_at "
            "FROM " + self.cat + ".market_silver.reference_facts WHERE subject_key='" +
            key.replace("'", "") + "' AND agent='agent_1_processor' "
            "ORDER BY fact_name, source_name").collect()]

    def _write(self, qid, raw, comp, fam, mod, dc, key, slug, score, status, now, note):
        row = [(qid, raw, comp, fam, mod, dc, key, slug, score, status, 0,
                now, now, None, None, 1, note)]
        self.spark.createDataFrame(row, _schema()).createOrReplaceTempView("_q")
        self.spark.sql(
            "MERGE INTO " + self.cat + ".market_config.query_queue t USING _q s "
            "ON t.query_id = s.query_id "
            "WHEN MATCHED THEN UPDATE SET t.last_seen_at = s.last_seen_at, "
            "  t.times_asked = t.times_asked + 1, "
            "  t.status = CASE WHEN t.status='resolved' THEN 'resolved' ELSE s.status END, "
            "  t.matched_key = COALESCE(t.matched_key, s.matched_key) "
            "WHEN NOT MATCHED THEN INSERT *")

    # ============================ MCP TOOL: process_queue ============================
    def process_queue(self, run_id=None, max_attempts=2, verbose=True):
        from pyspark.sql.types import (StructType, StructField, StringType, TimestampType)
        run_id = run_id or ("run_" + datetime.datetime.now().strftime("%Y%m"))
        now = datetime.datetime.now()
        pending = self.spark.sql(
            "SELECT query_id, parsed_company, parsed_family, parsed_model, device_class, "
            "attempts, times_asked FROM " + self.cat + ".market_config.query_queue "
            "WHERE status='queued' AND attempts < " + str(max_attempts) +
            " ORDER BY times_asked DESC, first_seen_at").collect()
        if verbose:
            print("queued chips to work on:", len(pending))
        if not pending:
            return {"run_id": run_id, "attempted": 0, "tally": {}}

        BL = StructType([StructField("processor_key", StringType()),
                         StructField("company", StringType()),
                         StructField("family", StringType()),
                         StructField("model_number", StringType()),
                         StructField("device_scope", StringType()),
                         StructField("origin", StringType()),
                         StructField("added_at", TimestampType())])
        # Every key goes through canonical_key, the SAME rules that converted the data on
        # 21 Sep 2026, so new chips cannot bring back brackets, lookalike letters or
        # half-split names. And if the processor already exists under an equivalent key
        # (same letters and digits, ignoring case and spacing), that key is reused rather
        # than creating a near-duplicate row.
        import genesis_agent1 as ga
        existing = {ga.norm_key(r.processor_key): r.processor_key for r in self.spark.sql(
            "SELECT processor_key FROM " + self.cat + ".market_config.processor_backlog").collect()}
        keys = {}
        for p in pending:
            c, f, m = ga.canonical_parts(p.parsed_company, p.parsed_family, p.parsed_model)
            k = ga.canonical_key(c, f, m)
            if k:
                k = existing.get(ga.norm_key(k), k)
            keys[p.query_id] = (k, c, f, m, p.device_class)
        rows = [(v[0], v[1], v[2], v[3], "mobile" if v[4] == "mobile" else "computer",
                 "customer_query", now) for v in keys.values() if v[0]]
        self.spark.createDataFrame(rows, BL).dropDuplicates(["processor_key"]) \
            .createOrReplaceTempView("_nb")
        self.spark.sql("MERGE INTO " + self.cat + ".market_config.processor_backlog t "
                       "USING _nb s ON t.processor_key = s.processor_key "
                       "WHEN NOT MATCHED THEN INSERT *")

        todo = self.spark.sql(
            "SELECT processor_key, company, family, model_number, device_scope FROM " +
            self.cat + ".market_config.processor_backlog WHERE origin='customer_query' "
            "AND NOT EXISTS (SELECT 1 FROM " + self.cat + ".market_silver.reference_facts f "
            "WHERE f.subject_key = processor_key AND f.source_name <> "
            "'Genesis reference sheet')").collect()
        res = self.agent.run_backfill(todo=todo, log_every=5, verbose=verbose) \
              if todo else {"tally": {}}

        for p in pending:
            key = keys[p.query_id][0] or ""
            got = self.spark.sql("SELECT COUNT(*) c FROM " + self.cat +
                                 ".market_silver.reference_facts WHERE subject_key='" +
                                 key.replace("'", "") + "' AND source_name <> "
                                 "'Genesis reference sheet'").first()[0]
            att = (p.attempts or 0) + 1
            q = self.cat + ".market_config.query_queue"
            if got:
                self.spark.sql("UPDATE " + q + " SET status='resolved', matched_key='" +
                               key.replace("'", "") + "', resolved_at=current_timestamp(), "
                               "resolved_in_run='" + run_id + "', attempts=" + str(att) +
                               " WHERE query_id='" + p.query_id + "'")
            elif att >= max_attempts:
                self.spark.sql("UPDATE " + q + " SET status='unresolvable', attempts=" +
                               str(att) + ", note='no source carries this chip after " +
                               str(max_attempts) + " monthly attempts' WHERE query_id='" +
                               p.query_id + "'")
            else:
                self.spark.sql("UPDATE " + q + " SET attempts=" + str(att) +
                               " WHERE query_id='" + p.query_id + "'")
        return {"run_id": run_id, "attempted": len(pending), "tally": res.get("tally", {})}

    # ============================ MCP TOOL: queue_status ============================
    def queue_status(self):
        return [r.asDict() for r in self.spark.sql(
            "SELECT status, COUNT(*) n, SUM(times_asked) total_asks FROM " + self.cat +
            ".market_config.query_queue GROUP BY status ORDER BY status").collect()]
