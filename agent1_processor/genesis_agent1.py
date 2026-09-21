
# genesis_agent1.py  --  Sub-Agent 1: Processor Reference Agent
# Public methods of Agent1 ARE the MCP tool signatures.
# All URLs, selectors, routes and pacing come from Delta config tables.

import re, time, hashlib, datetime, requests, urllib3
from bs4 import BeautifulSoup
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

AGENT = "agent_1_processor"
RULE_VERSION = "title_gate_v3 / slug_v1 / sep_v2"

HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
           "Accept-Language": "en-US,en;q=0.9"}

CLOCK      = re.compile(r"\b\d{1,2}[\.,]?\d{0,2}\s*(ghz|mhz)\b", re.I)
CORECOUNT  = re.compile(r"\b(\d{1,2})\s*[- ]?\s*core\b", re.I)
VARIANT    = {"pro","max","ultra","plus","elite","ti","super","bionic","ada","k","kf","x","xt"}
FORMFACTOR = {"mobile","laptop","desktop","notebook"}
NOISE      = {"nvidia","amd","intel","apple","qualcomm","samsung","google","mediatek",
              "geforce","core","radeon","rx","benchmark","benchmarks","specs","cpu"}
MODELCODE  = re.compile(r"^[a-z]{0,2}\d{2,5}[a-z]{0,3}$")

# Cyrillic lookalikes appear in real product names and in nanoreview's own catalogue.
# "M5 (8-\u0441ore GPU)" uses a Cyrillic es, which the tokeniser strips, leaving "ore"
# where the slug has "sore" -- one token that failed the whole match while every other
# rule passed. Map them to Latin before tokenising.
CYRILLIC = {"\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c",
            "\u0443": "y", "\u0445": "x", "\u0456": "i", "\u0501": "d",
            "\u0410": "A", "\u0415": "E", "\u041e": "O", "\u0420": "P",
            "\u0421": "C", "\u0425": "X"}

def delookalike(s):
    if not s:
        return s
    return "".join(CYRILLIC.get(ch, ch) for ch in s)

def toks(s):
    return [t for t in re.split(r"[^a-z0-9]+", delookalike(s or "").lower()) if t]


# Tier is part of a processor's identity: Core i5-11700 does not exist, the 11700 is a
# Core i7. Before this check, matching compared only model numbers and variant words, so
# a query for "Core i5 11700" silently took the Core i7 11700's page and numbers. Found
# 21 Sep 2026 when seven invented test chips got real i7 and i9 facts attached.
_TIER_RULES = [
    (re.compile(r"\bi([3579])\b"), "i"),                  # Core i3 / i5 / i7 / i9
    (re.compile(r"\bultra ([3579])\b"), "ultra"),          # Core Ultra 5 / 7 / 9
    (re.compile(r"\bryzen (?:ai )?([3579])\b"), "ryzen"),   # Ryzen 5, Ryzen AI 9
    (re.compile(r"\bryzen([3579])\b"), "ryzen"),            # typo form "Ryzen7"
    (re.compile(r"\bcore ([3579])\b"), "core"),            # new naming, Core 5 120U
]

def tiers(text):
    t = " ".join(toks(text))
    found = set()
    for rx, label in _TIER_RULES:
        for d in rx.findall(t):
            found.add(label + d)
    return found


# Brand is part of a processor's identity too. Brand names were treated as filler, so
# "Intel Core i5 9400" matched "MediaTek Dimensity 9400", a phone chip, and took its
# numbers. Found 21 Sep 2026. An explicit brand word wins; otherwise the family implies
# one (snapdragon means Qualcomm, dimensity means MediaTek). "Core" alone implies nothing,
# because Apple titles say "16 Core"; only Intel's tier forms like "Core i5" count.
_EXPLICIT_BRANDS = {"intel", "amd", "apple", "qualcomm", "mediatek", "samsung", "google",
                    "hisilicon", "nvidia", "unisoc", "xiaomi"}
_IMPLIED_BRANDS = [
    (re.compile(r"\bcore (i[3579]|ultra|[3579])\b|\bceleron\b|\bpentium\b|\bxeon\b|\batom\b"), "intel"),
    (re.compile(r"\bryzen|\bathlon\b|\bepyc\b|\bthreadripper\b|\bradeon\b"), "amd"),
    (re.compile(r"\bm[1-5]\b|\ba1[0-9]\b|\bbionic\b"), "apple"),
    (re.compile(r"\bsnapdragon\b"), "qualcomm"),
    (re.compile(r"\bdimensity\b|\bhelio\b"), "mediatek"),
    (re.compile(r"\bexynos\b"), "samsung"),
    (re.compile(r"\btensor\b"), "google"),
    (re.compile(r"\bkirin\b"), "hisilicon"),
    (re.compile(r"\bgeforce\b|\brtx\b|\bgtx\b|\bquadro\b"), "nvidia"),
    (re.compile(r"\bxring\b"), "xiaomi"),
]

def brands(text):
    tk = toks(text)
    explicit = set(tk) & _EXPLICIT_BRANDS
    if explicit:
        return explicit
    t = " ".join(tk)
    return {label for rx, label in _IMPLIED_BRANDS if rx.search(t)}


# ONE shape for every processor key: brand|family|number, as the Genesis table design
# requires. Used by EVERY code path that writes a key, so data converted on 21 Sep 2026
# cannot drift back. Phone and Apple chips have an empty number. Snapdragon X part codes
# are the number. The family carries the tier, and PRO stays with the family.
_BRAND_NAME = {"intel": "Intel", "amd": "AMD", "apple": "Apple", "qualcomm": "Qualcomm",
               "mediatek": "MediaTek", "samsung": "Samsung", "google": "Google",
               "hisilicon": "HiSilicon", "nvidia": "NVIDIA", "unisoc": "Unisoc",
               "xiaomi": "Xiaomi"}

def _sp(s):
    return re.sub(r"\s+", " ", s or "").strip()

def canonical_parts(company, family, model, raw=""):
    comp, fam, mod = [_sp(delookalike(x or "")) for x in (company, family, model)]
    if not fam and not mod:
        fam = _sp(delookalike(raw))
    fam, mod = _sp(re.sub(r"[()]", " ", fam)), _sp(re.sub(r"[()]", " ", mod))
    fam = _sp(re.sub(r"(\d+)\s*-?\s*core\b", r"\1-Core", fam, flags=re.I))
    m = re.search(r"\b(X\d?[A-Z]?-\d{2}-\d{3})\b", fam, re.I)
    if m and not mod:
        mod, fam = m.group(1).upper(), _sp(fam[:m.start()] + " " + fam[m.end():])
    if re.match(r"(?i)pro\s+", mod):
        fam, mod = fam + " PRO", _sp(re.sub(r"(?i)^pro\s+", "", mod))
    m = re.match(r"(?i)^processor\s+(n\d{2,3})$", fam)
    if m and not mod:
        fam, mod = "Processor", m.group(1).upper()
    m = re.match(r"(?i)^tiger\s+(t\d{3,4})$", fam)
    if m and not mod:
        fam, mod = "Tiger", m.group(1).upper()
    if comp.lower() in _BRAND_NAME:
        comp = _BRAND_NAME[comp.lower()]
    if not comp:
        if re.match(r"(?i)^tiger\b", fam):
            comp = "Unisoc"
        elif fam == "Processor":
            comp = "Intel"
        else:
            b = brands(" ".join([fam, mod, raw]))
            if len(b) == 1:
                comp = _BRAND_NAME[next(iter(b))]
    if comp and fam.lower().startswith(comp.lower() + " "):
        fam = _sp(fam[len(comp):])
    return comp, fam, mod

def canonical_key(company, family, model, raw=""):
    c, f, m = canonical_parts(company, family, model, raw)
    return (c + "|" + f + "|" + m) if (c and f) else None

def norm_key(key):
    return re.sub(r"[^a-z0-9]", "", (key or "").lower())


class Agent1:

    def __init__(self, spark, dbutils, catalog, fetch=True):
        if not catalog:
            raise ValueError("catalog must be stated, for example genesis_prod")
        self.spark = spark
        self.cat = catalog
        self._last_call = {}
        # fetch=False is QUESTION-ONLY mode, used by the dashboard app: it answers from
        # the database and never reads the Apify token, so the app's identity needs no
        # access to that secret. Only the monthly job, running as data.ai, fetches.
        self.fetch_enabled = bool(fetch)
        self.proxies = None
        if self.fetch_enabled:
            from apify_client import ApifyClient
            tok = dbutils.secrets.get(scope="genesis", key="apify-token")
            u = ApifyClient(tok).user("me").get()
            pwd = u.proxy.password if hasattr(u, "proxy") else u["proxy"]["password"]
            px = "http://groups-UNBLOCKER:" + pwd + "@proxy.apify.com:8000"
            self.proxies = {"http": px, "https": px}
        self.reload_config()

    def reload_config(self):
        c = self.cat
        self.registry = {r.source_name: r.asDict() for r in
                         self.spark.table(c + ".market_config.source_registry")
                             .filter("agent = '" + AGENT + "' AND is_current").collect()}
        self.health = {r.source_name: r.asDict() for r in
                       self.spark.table(c + ".market_config.source_health").collect()}
        self.selectors = {}
        for r in (self.spark.table(c + ".market_config.source_selectors")
                      .filter("is_current").collect()):
            self.selectors.setdefault(r.source_name, []).append(r.asDict())
        self.catalog = {r.slug: r.kind for r in
                        self.spark.table(c + ".market_config.accelerator_catalog").collect()}
        return {"sources": sorted(self.registry), "slugs": len(self.catalog)}

    # ---------------- fetching ----------------
    def _pace(self, source):
        gap = self.health.get(source, {}).get("min_seconds_between_requests") or 0
        last = self._last_call.get(source)
        if last:
            w = gap - (time.time() - last)
            if w > 0:
                time.sleep(w)
        self._last_call[source] = time.time()

    def _fetch(self, source, url):
        if not self.fetch_enabled:
            raise RuntimeError("This Agent1 was built in question-only mode (fetch=False) and "
                               "cannot fetch websites. Only the monthly job fetches.")
        route = self.health.get(source, {}).get("required_route") or "direct"
        self._pace(source)
        kw = {"proxies": self.proxies, "verify": False} if route == "unblocker" else {}
        try:
            r = requests.get(url, headers=HEADERS, timeout=150, **kw)
            return r.status_code, r.text, route, None
        except Exception as e:
            return None, "", route, type(e).__name__ + ": " + str(e)[:60]

    # ---------------- url building ----------------
    def _build_url(self, source, company, family, model):
        reg = self.registry[source]
        if reg["url_method"] != "constructed":
            return None
        pat, kt = reg["url_pattern"], (reg["key_transform"] or "")
        c, f, m = company, family, model
        sep = "+"
        if re.match(r"^core\s+i[3579]$", f.strip(), re.I):
            sep = "-"
        if "lowercase" in kt:
            c, f, m = c.lower(), f.lower(), m.lower()
        if "spaces and + to -" in kt:
            c, f, m = [x.replace(" ", "-").replace("+", "-") for x in (c, f, m)]
            sep = "-"
        elif "spaces to _" in kt:
            c, f, m = [x.replace(" ", "_") for x in (c, f, m)]
            sep = "_"
        else:
            c, f, m = [x.replace(" ", "+") for x in (c, f, m)]
        u = (pat.replace("{company}", c).replace("{family}", f)
                .replace("{sep}", sep if m else "").replace("{model_number}", m))
        return re.sub(r"[+_]{2,}", lambda x: x.group(0)[0], u).rstrip("-+_")

    def _url_candidates(self, source, company, family, model):
        outs = [self._build_url(source, company, family, model)]
        if company.upper() == "AMD" and family.strip().lower() == "ryzen":
            for tier in ["9", "7", "5", "3"]:
                outs.append(self._build_url(source, company, "Ryzen " + tier, model))
        return [u for u in outs if u]

    def find_slug(self, company, family, model, kinds=("soc", "cpu")):
        q = toks(" ".join([company, family, model])); qs = set(q)
        qvar = qs & VARIANT
        qcode = {t for t in q if MODELCODE.fullmatch(t)
                 and t not in FORMFACTOR and not t.isalpha()}
        qgen = set(re.findall(r"gen\s*(\d)", (family + " " + model).lower()))
        qtier = tiers(" ".join([company, family, model]))
        qbrand = brands(" ".join([company, family, model]))
        cands = []
        for slug, kind in self.catalog.items():
            if kind not in kinds:
                continue
            ss = set(toks(slug))
            if qcode and not qcode <= ss:
                continue
            if (ss & VARIANT) != qvar:
                continue
            if qgen != set(re.findall(r"gen-(\d)", slug)):
                continue
            # tier must match when the query states one: i5 never takes an i7 slug
            if qtier and tiers(slug.replace("-", " ")) != qtier:
                continue
            # brand must not conflict: an Intel query never takes a MediaTek slug
            sbrand = brands(slug.replace("-", " "))
            if qbrand and sbrand and not (qbrand & sbrand):
                continue
            if not qcode:
                core = [t for t in qs if t not in NOISE and t not in FORMFACTOR]
                if not set(core) <= ss:
                    continue
            extra = {e for e in (ss - qs - FORMFACTOR - NOISE) if MODELCODE.fullmatch(e)}
            cands.append((len(extra), -len(qs & ss), slug))
        cands.sort()
        return cands[0][2] if cands else None

    def _slug_url(self, source, slug):
        pat = self.registry[source]["url_pattern"]
        return pat.replace("{kind}", self.catalog.get(slug, "soc")).replace("{slug}", slug)

    # ---------------- title gate v3 ----------------
    @staticmethod
    def title_ok(html, company, family, model_number):
        s = BeautifulSoup(html, "lxml")
        ttl_raw = s.title.get_text(" ", strip=True) if s.title else ""
        h1_raw = s.find("h1").get_text(" ", strip=True) if s.find("h1") else ""
        ttl = CORECOUNT.sub(" ", CLOCK.sub(" ", ttl_raw))
        h1 = CORECOUNT.sub(" ", CLOCK.sub(" ", h1_raw))
        bset, tset = set(toks(ttl + " " + h1)), set(toks(ttl))
        qtext = CORECOUNT.sub(" ", " ".join([company, family, model_number]))
        q = toks(qtext); qset = set(q)
        qcode = {t for t in q if MODELCODE.fullmatch(t) and not t.isalpha()}
        qvar, pvar = qset & VARIANT, bset & VARIANT
        if qcode and not qcode <= bset:
            return False, ttl_raw[:70], "model code not a whole token"
        if (pvar - qvar) - FORMFACTOR:
            return False, ttl_raw[:70], "title variant: " + ",".join(sorted(pvar - qvar))
        # tier must match when the query states one, judged on the page title
        qtier = tiers(qtext)
        if qtier and tiers(ttl) != qtier:
            return False, ttl_raw[:70], ("tier differs: asked " + ",".join(sorted(qtier)) +
                                         ", page is " + (",".join(sorted(tiers(ttl))) or "none"))
        # brand must not conflict with the page title
        qbrand, pbrand = brands(qtext), brands(ttl)
        if qbrand and pbrand and not (qbrand & pbrand):
            return False, ttl_raw[:70], ("brand differs: asked " + ",".join(sorted(qbrand)) +
                                         ", page is " + ",".join(sorted(pbrand)))
        if not qcode:
            fam = [t for t in toks(CORECOUNT.sub(" ", family)) if t not in NOISE]
            if fam and not set(fam) <= bset:
                return False, ttl_raw[:70], "family absent"
        extra = {t for t in tset if MODELCODE.fullmatch(t) and not t.isalpha()} - qcode
        if extra:
            return False, ttl_raw[:70], "another part: " + ",".join(sorted(extra))
        return True, ttl_raw[:70], ""


    # ---------------- health: 404 is a CHIP miss, not a source failure ----------------
    def bump_health(self, source, http_status, err=None):
        src = source.replace("'", "")
        c = self.cat
        if http_status in (200, 404):
            self.spark.sql("UPDATE " + c + ".market_config.source_health "
                           "SET consecutive_failures=0, state=CASE WHEN state='excluded' "
                           "THEN 'excluded' ELSE 'healthy' END, "
                           "last_success_at=current_timestamp() WHERE source_name='" + src + "'")
        elif err is not None or http_status is None or http_status == 403 or http_status >= 500:
            why = (err or ("HTTP " + str(http_status)))[:180].replace("'", " ")
            self.spark.sql("UPDATE " + c + ".market_config.source_health "
                           "SET consecutive_failures=consecutive_failures+1, "
                           "last_failure_at=current_timestamp(), "
                           "last_failure_reason='" + why + "', "
                           "state=CASE WHEN state='excluded' THEN 'excluded' "
                           "WHEN consecutive_failures+1>=3 THEN 'degraded' ELSE state END "
                           "WHERE source_name='" + src + "'")

    # ============================ MCP TOOL: resolve_processor ============================
    def resolve_processor(self, processor_key, company=None, family=None,
                          model_number=None, device_scope="computer", only_sources=None):
        p = processor_key.split("|")
        company = company if company is not None else (p[0] if len(p) > 0 else "")
        family = family if family is not None else (p[1] if len(p) > 1 else "")
        model = model_number if model_number is not None else (p[2] if len(p) > 2 else "")
        out = []
        for source in sorted(self.registry, key=lambda s: self.registry[s]["priority"]):
            if only_sources and source not in only_sources:
                continue
            reg = self.registry[source]
            if self.health.get(source, {}).get("state") in ("excluded", "degraded"):
                continue
            scope = reg.get("device_scope") or "computer"
            if scope != "all" and scope != device_scope:
                continue
            if reg["url_method"] == "slug_lookup":
                kinds = ("soc",) if device_scope == "mobile" else ("cpu", "soc")
                slug = self.find_slug(company, family, model, kinds)
                urls = [self._slug_url(source, slug)] if slug else []
            else:
                slug = None
                urls = self._url_candidates(source, company, family, model)
            for url in urls:
                status, html, route, err = self._fetch(source, url)
                pid = hashlib.sha256((url + str(datetime.datetime.now()))
                                     .encode()).hexdigest()[:24]
                out.append({"kind": "page", "page_id": pid, "url": url,
                            "source_name": source,
                            "http_status": status if status else -1,
                            "fetch_route": route, "content_bytes": len(html),
                            "html": html[:900000], "err": err})
                if status != 200:
                    continue
                gate_ok, ttl, why = True, "", ""
                if reg.get("verify_in_title"):
                    gate_ok, ttl, why = self.title_ok(html, company, family, model)
                text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
                vals = {}
                for sel in self.selectors.get(source, []):
                    m = re.search(sel["expression"], text)
                    if m:
                        v = m.group(1)
                        if "strip_commas" in (sel["post_process"] or ""):
                            v = v.replace(",", "")
                        vals[sel["fact_name"]] = (v.strip(), sel["fact_unit"],
                                                  m.group(0)[:200])
                out.append({"kind": "score", "source_name": source, "gate_ok": gate_ok,
                            "cores": vals.get("cores", (None,))[0], "title": ttl,
                            "reason": why, "url": url})
                if not gate_ok:
                    out.append({"kind": "miss", "source_name": source,
                                "url": url, "reason": why})
                    break
                for fname, (val, unit, raw) in vals.items():
                    fid = hashlib.sha256("|".join([processor_key, AGENT, fname, source])
                                         .encode()).hexdigest()[:24]
                    out.append({"kind": "fact", "fact_id": fid,
                                "subject_key": processor_key, "fact_name": fname,
                                "fact_value": val, "fact_unit": unit,
                                "source_name": source, "source_url": url,
                                "page_id": pid, "raw_value": raw,
                                "parse_method": "selector", "fetch_route": route,
                                "company": company, "family": family,
                                "model_number": model, "matched_slug": slug})
                break
        return out

    # ============================ MCP TOOL: list_unresolved ============================
    def list_unresolved(self, source_name=None, device_scope=None, limit=100000):
        c = self.cat
        cond = ("AND f.source_name = '" + source_name + "'") if source_name \
               else "AND f.source_name <> 'Genesis reference sheet'"
        scope = ("AND b.device_scope = '" + device_scope + "'") if device_scope else ""
        return self.spark.sql(
            "SELECT b.processor_key, b.company, b.family, b.model_number, b.device_scope "
            "FROM " + c + ".market_config.processor_backlog b "
            "WHERE b.company IS NOT NULL AND b.company <> '' " + scope + " "
            "AND NOT EXISTS (SELECT 1 FROM " + c + ".market_silver.reference_facts f "
            "WHERE f.subject_key = b.processor_key AND f.agent = '" + AGENT + "' "
            + cond + ") LIMIT " + str(int(limit))).collect()

    # ============================ MCP TOOL: source_health ============================
    def source_health(self):
        return [r.asDict() for r in
                self.spark.table(self.cat + ".market_config.source_health").collect()]

    # ---------------- writing ----------------
    def flush(self, pages, facts):
        from pyspark.sql.types import (StructType, StructField, StringType,
                                       IntegerType, BooleanType, TimestampType)
        c, now = self.cat, datetime.datetime.now()
        if pages:
            ps = StructType([StructField("page_id", StringType()),
                             StructField("url", StringType()),
                             StructField("source_name", StringType()),
                             StructField("http_status", IntegerType()),
                             StructField("fetch_route", StringType()),
                             StructField("content_bytes", IntegerType()),
                             StructField("html", StringType()),
                             StructField("fetched_at", TimestampType())])
            self.spark.createDataFrame(
                [(p["page_id"], p["url"], p["source_name"], p["http_status"],
                  p["fetch_route"], p["content_bytes"], p["html"], now)
                 for p in pages], ps).write.format("delta").mode("append") \
                .saveAsTable(c + ".market_bronze.reference_pages")
        if facts:
            fs = StructType([StructField("fact_id", StringType()),
                             StructField("subject_type", StringType()),
                             StructField("subject_key", StringType()),
                             StructField("agent", StringType()),
                             StructField("fact_name", StringType()),
                             StructField("fact_value", StringType()),
                             StructField("fact_unit", StringType()),
                             StructField("source_name", StringType()),
                             StructField("source_url", StringType()),
                             StructField("retrieved_at", TimestampType()),
                             StructField("is_ruling", BooleanType()),
                             StructField("ruling_reason", StringType())])
            self.spark.createDataFrame(
                [(f["fact_id"], "processor", f["subject_key"], AGENT, f["fact_name"],
                  f["fact_value"], f["fact_unit"], f["source_name"], f["source_url"],
                  now, False, None) for f in facts], fs).createOrReplaceTempView("_wf")
            self.spark.sql("MERGE INTO " + c + ".market_silver.reference_facts t "
                           "USING _wf s ON t.fact_id = s.fact_id "
                           "WHEN MATCHED THEN UPDATE SET * "
                           "WHEN NOT MATCHED THEN INSERT *")
            ds = StructType([StructField("fact_id", StringType()),
                             StructField("processor_key", StringType()),
                             StructField("company", StringType()),
                             StructField("family", StringType()),
                             StructField("model_number", StringType()),
                             StructField("source_name", StringType()),
                             StructField("source_url", StringType()),
                             StructField("matched_slug", StringType()),
                             StructField("raw_value", StringType()),
                             StructField("parse_method", StringType()),
                             StructField("fetch_route", StringType()),
                             StructField("page_id", StringType()),
                             StructField("retrieved_at", TimestampType())])
            self.spark.createDataFrame(
                [(f["fact_id"], f["subject_key"], f["company"], f["family"],
                  f["model_number"], f["source_name"], f["source_url"],
                  f.get("matched_slug"), f["raw_value"], f["parse_method"],
                  f["fetch_route"], f["page_id"], now)
                 for f in facts], ds).createOrReplaceTempView("_wd")
            self.spark.sql("MERGE INTO " + c + ".market_silver.processor_detail t "
                           "USING _wd s ON t.fact_id = s.fact_id "
                           "WHEN MATCHED THEN UPDATE SET * "
                           "WHEN NOT MATCHED THEN INSERT *")

    # ============================ MCP TOOL: run_backfill ============================
    def run_backfill(self, todo=None, only_sources=None, truth=None,
                     flush_every=10, log_every=25, verbose=True):
        from collections import Counter
        todo = todo if todo is not None else self.list_unresolved()
        pages, facts, ev = [], [], []
        cm = Counter(); t0 = time.time()
        for i, r in enumerate(todo, 1):
            try:
                res = self.resolve_processor(r.processor_key, r.company, r.family,
                                             r.model_number,
                                             device_scope=r.device_scope or "computer",
                                             only_sources=only_sources)
            except Exception as e:
                cm["error"] += 1
                continue
            t = (truth or {}).get(r.processor_key)
            got = False
            for x in res:
                if x["kind"] == "page":
                    pages.append(x)
                    self.bump_health(x["source_name"], x["http_status"], x.get("err"))
                    if x["http_status"] == 404:
                        cm["404 no page"] += 1
                elif x["kind"] == "fact":
                    facts.append(x); got = True
                elif x["kind"] == "miss":
                    cm["gate rejected"] += 1
                elif x["kind"] == "score" and t:
                    g = x["cores"]
                    agree = g is not None and str(g) == str(t)
                    cls = ("TP" if x["gate_ok"] and agree else
                           "FP" if x["gate_ok"] and g is not None and not agree else
                           "FN" if (not x["gate_ok"]) and agree else
                           "TN" if not x["gate_ok"] else "NA")
                    cm[cls] += 1
                    ev.append((r.processor_key, x["source_name"], x["gate_ok"], t, g,
                               cls, x["title"], x["reason"]))
            if got:
                cm["resolved"] += 1
            if verbose and i % log_every == 0:
                print("%5d/%5d  %s  %.0fs" % (i, len(todo), dict(cm), time.time() - t0))
            if len(pages) >= flush_every:
                self.flush(pages, facts); pages, facts = [], []
        self.flush(pages, facts)
        return {"attempted": len(todo), "tally": dict(cm), "eval_rows": ev,
                "seconds": round(time.time() - t0)}
