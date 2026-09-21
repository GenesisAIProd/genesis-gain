
# genesis_agent1_upload.py
# Internal file upload for Sub-Agent 1: a Genesis user uploads a CSV or Excel file of
# machines, and gets the same file back with processor details added on the right.
#
# INTERNAL ONLY. The external demo link can query but never upload. Enforced in the
# service by caller type, not by hiding a button.
#
# Rules agreed 21 Sep 2026:
#   The processor column is found from the VALUES, never the header, so any bid list
#   layout works. If two columns look equally processor-like, we ASK rather than guess.
#   Each DISTINCT processor is looked up once and applied to every row that has it.
#   Up to 100 distinct processors and 10 MB per file.
#   Numbers come from market_gold.processor_reference, so they follow the same Geekbench
#   rule as the view MSV joins on and the monthly download.
#   Chips we do not hold go to the queue for the first-Monday monthly run.
#   Never silent: an unreadable, empty or processor-less file gets a plain message.
#   Any cell starting with = + - @ is neutralised so nothing runs as an Excel formula.

import io, csv, json, uuid, datetime

MAX_BYTES = 10 * 1024 * 1024
MAX_DISTINCT = 100
SAMPLE = 20
MIN_SCORE = 0.5
TIE_MARGIN = 0.15

OUT_COLS = ["genesis_status", "genesis_processor", "genesis_cores", "genesis_threads",
            "genesis_geekbench_single", "genesis_geekbench_multi",
            "genesis_geekbench_version", "genesis_passmark_single", "genesis_passmark_multi"]

STATUS = {"found": "found",
          "queued": "not in the database yet, scheduled for the next monthly run",
          "no_processor_in_query": "no processor found in this row",
          "empty": "no processor given in this row",
          "family": "a processor family, not one processor: please give the model number",
          "over_limit": "not checked: the file has more than 100 different processors"}


def neutralise(value):
    # Formula injection: a cell like =HYPERLINK(...) runs when opened in Excel. Prefixing
    # an apostrophe makes Excel show it as text. Applied to the customer's cells too.
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def parse_upload(filename, data):
    """Returns (rows, columns, error). rows are dicts of strings."""
    if not data:
        return None, None, "The file is empty."
    if len(data) > MAX_BYTES:
        return None, None, ("The file is %.1f MB; the limit is 10 MB. Please split it."
                            % (len(data) / 1048576.0))
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext not in ("xlsx", "xlsm", "xls", "csv", "txt", ""):
        return None, None, "Only CSV and Excel files are accepted."
    try:
        import pandas as pd
        if ext in ("xlsx", "xlsm", "xls"):
            df = pd.read_excel(io.BytesIO(data), dtype=str, na_filter=False)
        else:
            df = None
            for enc in ("utf-8-sig", "cp1252", "latin-1"):
                try:
                    df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False,
                                     encoding=enc, sep=None, engine="python")
                    break
                except UnicodeDecodeError:
                    continue
            if df is None:
                return None, None, "The file could not be read as text."
    except Exception as e:
        return None, None, "The file could not be read: " + type(e).__name__ + "."
    df.columns = [str(c).strip() or ("column_" + str(i + 1)) for i, c in enumerate(df.columns)]
    rows = [{c: ("" if v is None else str(v)) for c, v in r.items()}
            for r in df.to_dict("records")]
    if not rows:
        return None, None, "The file has column headings but no rows."
    return rows, list(df.columns), None


def processor_likeness(value):
    import genesis_agent1 as ga
    v = (value or "").strip()
    return 1.0 if v and (ga.brands(v) or ga.tiers(v)) else 0.0


def detect_column(rows, cols):
    """Returns (column, scores, message). column is None when we must not guess."""
    scores = {}
    for c in cols:
        seen, vals = set(), []
        for r in rows:
            v = (r.get(c) or "").strip()
            if v and v not in seen:
                seen.add(v); vals.append(v)
            if len(vals) >= SAMPLE:
                break
        scores[c] = round(sum(processor_likeness(v) for v in vals) / len(vals), 2) if vals else 0.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, top = ranked[0] if ranked else (None, 0.0)
    if top < MIN_SCORE:
        return None, scores, ("No column looks like it holds processors. Columns found: " +
                              ", ".join(cols) + ". Please say which column to use.")
    close = [c for c, s in ranked[1:] if s >= top - TIE_MARGIN]
    if close:
        return None, scores, ("More than one column looks like it holds processors: " +
                              ", ".join([best] + close) + ". Please say which one to use.")
    return best, scores, None


def build_output(rows, column, answers, reference):
    """answers: raw value -> (status_code, processor_key). reference: key -> dict."""
    out = []
    for r in rows:
        raw = (r.get(column) or "").strip()
        code, key = answers.get(raw, ("empty" if not raw else "over_limit", None))
        ref = reference.get(key, {}) if key else {}
        row = {c: neutralise(v) for c, v in r.items()}
        row.update({
            "genesis_status": STATUS.get(code, code),
            "genesis_processor": neutralise(key or ""),
            "genesis_cores": ref.get("cores") or "",
            "genesis_threads": ref.get("threads") or "",
            "genesis_geekbench_single": ref.get("geekbench_single") or "",
            "genesis_geekbench_multi": ref.get("geekbench_multi") or "",
            "genesis_geekbench_version": ref.get("geekbench_version") or "",
            "genesis_passmark_single": ref.get("passmark_single") or "",
            "genesis_passmark_multi": ref.get("passmark_multi") or "",
        })
        out.append(row)
    return out


def to_csv(rows, columns):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def process(svc, caller, rows, cols, column=None, entry_point="csv", label=""):
    """The whole job. svc is an Agent1Service; it supplies the lookups and the writers."""
    if not rows:
        return {"status": "rejected", "message": "The file has no rows."}
    if column is not None:
        if column not in cols:
            return {"status": "rejected",
                    "message": "There is no column called '" + str(column) + "'. Columns found: " +
                               ", ".join(cols) + "."}
        scores = {}
    else:
        column, scores, msg = detect_column(rows, cols)
        if column is None:
            return {"status": "needs_column", "message": msg, "scores": scores,
                    "columns": cols}

    distinct, seen = [], set()
    for r in rows:
        v = (r.get(column) or "").strip()
        if v and v not in seen:
            seen.add(v); distinct.append(v)
    overflow = max(0, len(distinct) - MAX_DISTINCT)
    todo = distinct[:MAX_DISTINCT]
    note = (str(overflow) + " further distinct processors were not checked; the limit is " +
            str(MAX_DISTINCT) + " per file") if overflow else None

    job_id = uuid.uuid4().hex
    svc._write_job(job_id, caller, entry_point, label or ("column " + column), len(todo),
                   "running", note)

    answers, items = {}, []
    for i, raw in enumerate(todo, 1):
        try:
            r = svc.qs.ask(raw)
            code, key = r.get("status", "error"), r.get("processor_key")
        except Exception as e:
            code, key = "could not be checked: " + type(e).__name__, None
        answers[raw] = (code, key)

    reference = svc._reference_rows([k for _, k in answers.values() if k])
    for i, raw in enumerate(todo, 1):
        code, key = answers[raw]
        ref = reference.get(key, {}) if key else {}
        c, f, m = (key.split("|") + ["", "", ""])[:3] if key else ("", "", "")
        def _i(v):
            try:
                return int(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None
        items.append((job_id, i, raw, c, f, m, STATUS.get(code, code), key,
                      _i(ref.get("cores")), _i(ref.get("geekbench_single")),
                      _i(ref.get("geekbench_multi")), ref.get("geekbench_source") or "",
                      0.0, datetime.datetime.now()))
    svc._write_items(items)

    out = build_output(rows, column, answers, reference)
    columns = list(cols) + OUT_COLS
    csv_text = to_csv(out, columns)
    payload = json.dumps({"job_id": job_id, "column_used": column, "rows": out},
                         indent=2, default=str)
    csv_path, json_path = svc._write_files(job_id, csv_text, payload)
    svc._finish_job(job_id, json_path, csv_path)

    tally = {}
    for code, _ in answers.values():
        tally[STATUS.get(code, code)] = tally.get(STATUS.get(code, code), 0) + 1
    return {"status": "complete", "job_id": job_id, "column_used": column,
            "column_scores": scores, "rows": len(rows), "distinct_processors": len(todo),
            "result_by_processor": tally, "note": note,
            "download": csv_path, "json": json_path, "preview": out[:10]}
