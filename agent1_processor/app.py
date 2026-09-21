
# app.py
# Genesis GAIN, Sub-Agent 1 dashboard. Streamlit on Databricks Apps.
#
# Internal (genesisig.com sign-in): Ask, Upload, Database, Monthly updates, Requests.
# External (invited guests):          Ask, Database, Monthly updates. Questions only,
# rate-limited, no upload. Access is enforced by the service layer, by caller type,
# not only by which pages are shown here.

import io, csv, json
import pandas as pd
import streamlit as st
import agent1_backend as be

st.set_page_config(page_title="Genesis GAIN: Processor Reference", layout="wide")

STATUS_TEXT = {"found": "found",
               "queued": "not in the database yet, scheduled for the next monthly run",
               "no_processor_in_query": "no processor found",
               "unresolvable": "could not be found in any source",
               "family": "one of the family you asked about"}
SHOW = ["processor", "status", "device_class", "cores", "threads", "geekbench_single",
        "geekbench_multi", "geekbench_version", "passmark_single", "passmark_multi",
        "geekbench_url", "passmark_url"]


@st.cache_resource(show_spinner="Connecting to the Genesis database...")
def backend():
    return be.build()


def neutral(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def downloads(df, stem):
    safe = df.apply(lambda col: col.map(neutral))
    buf = io.StringIO()
    safe.to_csv(buf, index=False)
    c1, c2 = st.columns(2)
    c1.download_button("Download CSV", buf.getvalue(), stem + ".csv", "text/csv")
    c2.download_button("Download JSON", safe.to_json(orient="records", indent=2),
                       stem + ".json", "application/json")


def answer_table(svc, rows):
    """rows: list of (asked_about, status, processor_key). Adds reference columns."""
    ref = svc._reference_rows([k for _, _, k in rows if k])
    out = []
    for asked, status, key in rows:
        x = ref.get(key, {}) if key else {}
        rec = {"asked about": asked, "processor": key or "",
               "status": STATUS_TEXT.get(status, status)}
        for c in SHOW[2:]:
            rec[c] = x.get(c)
        out.append(rec)
    return pd.DataFrame(out)


# ---------------- who is this ----------------
spark, svc, gsvc = backend()
email = st.context.headers.get("X-Forwarded-Email", "")
caller = be.caller_for(email, gsvc)
internal = caller.internal

st.sidebar.title("Genesis GAIN")
st.sidebar.caption("Processor reference for laptops, desktops, smartphones and tablets")
st.sidebar.write("Signed in as " + (email or "unknown") +
                 (" (Genesis)" if internal else " (guest)"))
pages = ["Ask", "Upload", "Database", "Monthly updates", "Requests"] if internal \
        else ["Ask", "Database", "Monthly updates"]
page = st.sidebar.radio("Page", pages)

# ---------------- Ask ----------------
if page == "Ask":
    st.header("Ask about any processor")
    st.write("Write it any way you like: an OEM string, a comparison, a list. "
             "For example: 11th Gen Intel(R) Core(TM) i5-11400 @ 2.60GHz, or "
             "snapdragon 8+ gen 1 vs snapdragon 8 gen 3.")
    q = st.text_area("Your question", height=90)
    if st.button("Ask", type="primary") and q.strip():
        with st.spinner("Finding the processors and their scores..."):
            r = svc.ask_many(caller, q)
        if r.get("status") == "rate_limited" or r.get("error"):
            st.warning(r.get("message") or r.get("detail") or r.get("error"))
        elif r.get("job_id"):
            with st.spinner("That is a long list, working through it..."):
                svc.run_job(r["job_id"])
                res = svc.job_results(caller, r["job_id"])
            rows = [(x["raw_text"], x["status"], x["matched_key"]) for x in res["results"]]
            df = answer_table(svc, rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
            downloads(df, "genesis_answer")
        else:
            rows, families = [], []
            for x in r.get("results", []):
                if x["status"] == "family":
                    families.append((x["family"], x["members"]))
                    rows += [(x.get("asked_about", q), "family", k) for k in x["members"]]
                else:
                    rows.append((x.get("asked_about", q), x["status"], x.get("processor_key")))
            for name, members in families:
                eg = members[0].replace("|", " ").strip()
                st.info('"%s" is a processor family, not one processor. Showing the %d we hold, '
                        'fastest first. Ask about a specific model, such as %s, for a single '
                        'answer.' % (name, len(members), eg))
            if not rows:
                st.info("No processor was found in that question.")
            else:
                df = answer_table(svc, rows)
                st.dataframe(df, use_container_width=True, hide_index=True)
                if any(s == "queued" for _, s, _ in rows):
                    st.info("Processors not in the database yet have been recorded and will "
                            "be sourced in the next monthly run.")
                downloads(df, "genesis_answer")

# ---------------- Upload (internal only) ----------------
elif page == "Upload" and internal:
    st.header("Upload a list of machines")
    st.write("CSV or Excel, up to 10 MB and 100 different processors. The processor column "
             "is found from the values, so any layout works. Your file comes back with "
             "processor details added on the right.")
    f = st.file_uploader("File", type=["csv", "xlsx", "xls"])
    col = st.session_state.get("upload_column")
    if f is not None and st.button("Process file", type="primary"):
        with st.spinner("Reading the file and looking up each processor..."):
            res = svc.submit_file(caller, f.name, f.getvalue(), col)
        st.session_state["upload_result"] = res
    res = st.session_state.get("upload_result")
    if res:
        if res.get("status") == "needs_column":
            st.warning(res["message"])
            st.session_state["upload_column"] = st.selectbox("Which column holds the processors?",
                                                             res["columns"])
            st.write("Choose the column, then press Process file again.")
        elif res.get("status") != "complete":
            st.error(res.get("message") or res.get("detail") or "The file could not be processed.")
        else:
            st.success("Done: %d rows, %d different processors, using column '%s'."
                       % (res["rows"], res["distinct_processors"], res["column_used"]))
            st.write(res["result_by_processor"])
            if res.get("note"):
                st.warning(res["note"])
            st.dataframe(pd.DataFrame(res["preview"]), use_container_width=True, hide_index=True)
            c1, c2 = st.columns(2)
            c1.download_button("Download the full file as CSV", be.read_file(res["download"]),
                               "genesis_enriched.csv", "text/csv")
            c2.download_button("Download as JSON", be.read_file(res["json"]),
                               "genesis_enriched.json", "application/json")

# ---------------- Database ----------------
elif page == "Database":
    st.header("The processor database")
    df = spark.sql("SELECT * FROM " + be.require_catalog() +
                   ".market_gold.processor_reference").toPandas()
    c1, c2, c3 = st.columns(3)
    dev = c1.multiselect("Device", sorted(df["device_class"].dropna().unique()))
    brand = c2.multiselect("Brand", sorted(df["processor_brand"].dropna().unique()))
    text = c3.text_input("Search")
    v = df
    if dev:
        v = v[v["device_class"].isin(dev)]
    if brand:
        v = v[v["processor_brand"].isin(brand)]
    if text:
        v = v[v["processor_key"].str.contains(text, case=False, regex=False)]
    st.write("%d of %d processors" % (len(v), len(df)))
    st.dataframe(v.drop(columns=["join_key"]), use_container_width=True, hide_index=True)
    months = [n for n in be.list_exports() if n.startswith("genesis_processor_database_")]
    if months:
        latest = sorted({n.rsplit(".", 1)[0] for n in months})[-1]
        st.subheader("Latest monthly release: " + latest.replace("genesis_processor_database_", ""))
        d1, d2 = st.columns(2)
        d1.download_button("Download CSV", be.read_file(be.exports_dir() + "/" + latest + ".csv"),
                           latest + ".csv", "text/csv")
        d2.download_button("Download JSON", be.read_file(be.exports_dir() + "/" + latest + ".json"),
                           latest + ".json", "application/json")

# ---------------- Monthly updates ----------------
elif page == "Monthly updates":
    st.header("What each monthly run added")
    recs = []
    for n in sorted(be.list_exports()):
        if n.startswith("genesis_monthly_summary_") and n.endswith(".json"):
            s = json.loads(be.read_file(be.exports_dir() + "/" + n))
            q = s.get("quality", {})
            recs.append({"month": n[len("genesis_monthly_summary_"):-5].replace("_", "-"),
                         "processors with data": s["after"]["processors_with_facts"],
                         "processors added": s["added"]["processors_with_facts"],
                         "facts added": s["added"]["total_facts"],
                         "customer requests resolved": s["added"]["queue_resolved"],
                         "quality checks": "all passed" if not any(
                             q.get(k) for k in ["duplicate_fact_ids", "facts_without_url",
                                                "facts_without_timestamp", "impossible_scores",
                                                "cores_out_of_range"]) else "see details",
                         "minutes": round(s["seconds"] / 60, 1)})
    if not recs:
        st.info("No monthly run has completed yet.")
    else:
        m = pd.DataFrame(recs)
        st.dataframe(m, use_container_width=True, hide_index=True)
        st.line_chart(m.set_index("month")["processors with data"])

# ---------------- Requests (internal only) ----------------
elif page == "Requests" and internal:
    st.header("Processors customers asked for")
    st.write("Chips not in the database are recorded here and sourced in the next monthly "
             "run. Two failed monthly attempts mark a chip unresolvable.")
    r = spark.sql("SELECT raw_query, status, times_asked, attempts, matched_key, first_seen_at, "
                  "note FROM " + be.require_catalog() + ".market_config.query_queue "
                  "ORDER BY times_asked DESC, first_seen_at DESC").toPandas()
    st.dataframe(r, use_container_width=True, hide_index=True)
