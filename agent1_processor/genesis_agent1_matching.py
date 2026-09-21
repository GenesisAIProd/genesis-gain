
# genesis_agent1_matching.py
# Matching layer for Agent 1: embeddings propose candidates, strict rules decide.
# Chosen by measured bake-off on 16 cases against 831 slugs:
#   BGE + few-shot prompt: 13/16 top-1, 14/16 top-5, margin 0.034
#   GTE + same prompt:     13/16 top-1, 14/16 top-5, margin 0.026
#   Adding chain-of-thought made both models WORSE, so P2 is the prompt.
#   Splitting helps medium queries and hurts hard ones, so both paths are kept.

import re, json, numpy as np
from collections import Counter

CHAT       = "databricks-claude-sonnet-4-6"
BEST_MODEL = "databricks-bge-large-en"
ALL_MODELS = ["databricks-gte-large-en", "databricks-bge-large-en"]

RULES = ("<rules>"
 "<rule>Drop generation words such as 11th Gen, 8th gen, 13th gen.</rule>"
 "<rule>Drop (R), (TM), (C) and the words CPU, processor, chip, with, Graphics.</rule>"
 "<rule>Drop clock speeds such as @ 2.40GHz or 1.90 GHz.</rule>"
 "<rule>Keep PRO, Ultra, Max, Plus, Elite, Ti and plus signs when part of the product name.</rule>"
 "<rule>Apple and Qualcomm SoCs usually have no separate model_number; put the whole name in family.</rule>"
 "<rule>Correct obvious misspellings and non-Latin lookalike characters.</rule>"
 "<rule>If the text names no processor at all, return empty strings. Never guess.</rule>"
 "</rules>")

TRAPS = ("<examples>"
 "<example><input>11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz</input>"
 "<reasoning>Drop 11th Gen, (R), (TM) and the clock speed.</reasoning>"
 '<output>{"company":"Intel","family":"Core i5","model_number":"1135G7"}</output></example>'
 "<example><input>AMD Ryzen 5 PRO 4650U with Radeon Graphics</input>"
 "<reasoning>PRO is part of the identity and stays.</reasoning>"
 '<output>{"company":"AMD","family":"Ryzen 5 PRO","model_number":"4650U"}</output></example>'
 "<example><input>Apple M2 chip 8-core CPU</input>"
 "<reasoning>Apple silicon has no model number; core count dropped unless it distinguishes products.</reasoning>"
 '<output>{"company":"Apple","family":"M2","model_number":""}</output></example>'
 "<example><input>M5 Max (40-\u0441ore GPU)</input>"
 "<reasoning>Cyrillic lookalike; read as core. The GPU core count separates this from the 32-core version.</reasoning>"
 '<output>{"company":"Apple","family":"M5 Max 40 core GPU","model_number":""}</output></example>'
 "<example><input>Ryzen7 7840-HS</input>"
 "<reasoning>Spacing and hyphenation wrong.</reasoning>"
 '<output>{"company":"AMD","family":"Ryzen 7","model_number":"7840HS"}</output></example>'
 "<example><input>snapdragon 8+ gen 1 qualcomm</input>"
 "<reasoning>Word order reversed; the plus sign is part of the name.</reasoning>"
 '<output>{"company":"Qualcomm","family":"Snapdragon 8 Plus Gen 1","model_number":""}</output></example>'
 "<example><input>Dell Latitude 5401 laptop, 16GB RAM, 512GB SSD</input>"
 "<reasoning>No processor named. Return nulls.</reasoning>"
 '<output>{"company":"","family":"","model_number":""}</output></example>'
 "</examples>")

P2 = ("<task>Extract the processor identity from the text.</task>" + RULES + TRAPS +
      '<output_format>Return JSON only, no markdown fences: '
      '{"company":"","family":"","model_number":""}</output_format><input>')

TEST_CASES = [
 ("Intel Core i5 12600K","intel-core-i5-12600k","easy"),
 ("AMD Ryzen 7 7840HS","amd-ryzen-7-7840hs","easy"),
 ("Apple A18","apple-a18","easy"),
 ("Intel Core Ultra 7 155H","intel-core-ultra-7-155h","easy"),
 ("Qualcomm Snapdragon 8 Gen 3","qualcomm-snapdragon-8-gen-3","easy"),
 ("11th Gen Intel(R) Core(TM) i5-11400 @ 2.60GHz","intel-core-i5-11400","medium"),
 ("Intel(R) Core(TM) i7-13700H CPU @ 2.40GHz","intel-core-i7-13700h","medium"),
 ("AMD Ryzen 5 PRO 4650G with Radeon Graphics","amd-ryzen-5-pro-4650g","medium"),
 ("Core i9 13th gen 13900KS","intel-core-i9-13900ks","medium"),
 ("Apple M3 Max chip 16-core CPU","apple-m3-max","medium"),
 ("M5 Max processor",              "apple-m5-max-18-core",        "hard"),
 ("apple m5 pro 18 core cpu",      "apple-m5-pro-18-core",        "hard"),
 ("Snapdragon X Elite (X1E-84-100)","qualcomm-snapdragon-x-elite","hard"),
 ("i5 12600 H intel core","intel-core-i5-12600h","hard"),
 ("Ryzen5 7640-HS","amd-ryzen-5-7640hs","hard"),
 ("snapdragon 8+ gen 1 qualcomm","snapdragon-8-plus-gen-1","hard"),
]

SOC_HINT = ("snapdragon","dimensity","exynos","tensor","kirin","apple a","bionic","unisoc")

def kinds_for(text):
    return ("soc",) if any(w in (text or "").lower() for w in SOC_HINT) else ("cpu","soc")

# Snapdragon X and X2 are LAPTOP parts despite the name. Fifteen sit in the computer
# class by list membership, so a keyword rule on "snapdragon" gets all of them wrong.
LAPTOP_SOC = re.compile(r"snapdragon\s*x\d?\b", re.I)

# nanoreview files each Apple chip TWICE: the CPU row (apple-m5-max-18-core) and the
# GPU row (apple-m5-max-gpu-40-core). A query naming GPU, or a GPU core count, wants the
# GPU row. Defaulting want_gpu to False hid three Apple entries from the pool entirely
# even though the strict rules accepted them once shown.
GPU_HINT = re.compile(r"\bgpu\b|\bgraphics\b", re.I)

def wants_gpu(text):
    return bool(GPU_HINT.search(text or ""))

def device_class_for(text):
    t = (text or "").lower()
    if LAPTOP_SOC.search(t):
        return "computer"
    return "mobile" if any(w in t for w in SOC_HINT) else "computer"


class Matcher:

    def __init__(self, spark, catalog, model=BEST_MODEL):
        if not catalog:
            raise ValueError("catalog must be stated, for example genesis_prod")
        self.spark = spark
        self.cat = catalog
        self.model = model
        self.load()

    def load(self):
        rows = self.spark.sql(
            "SELECT entity_key, entity_kind, embedding FROM " + self.cat +
            ".market_silver.entity_embeddings WHERE entity_type='slug' "
            "AND model_name='" + self.model + "'").collect()
        if not rows:
            raise RuntimeError("no slug vectors for " + self.model)
        self.keys  = [r.entity_key for r in rows]
        self.kinds = [r.entity_kind for r in rows]
        dc = {r.slug: (r.device_class, r.is_gpu) for r in self.spark.sql(
              "SELECT slug, device_class, is_gpu FROM " + self.cat +
              ".market_config.slug_device_class").collect()}
        self.dclass = [dc.get(k, ('computer', False))[0] for k in self.keys]
        self.is_gpu = [dc.get(k, ('computer', False))[1] for k in self.keys]
        V = np.array([r.embedding for r in rows], dtype=float)
        self.V = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
        self.ng = [self._ngrams(k.replace("-", " ")) for k in self.keys]
        self.ngn = np.array([np.sqrt(sum(v*v for v in g.values())) or 1e-9 for g in self.ng])
        return len(self.keys)

    @staticmethod
    def _ngrams(s, n=3):
        s = " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip() + " "
        return Counter(s[i:i+n] for i in range(max(len(s)-n+1, 1)))

    def embed(self, text, model=None):
        m = model or self.model
        r = self.spark.sql("SELECT ai_query('" + m + "', '" +
                           (text or "").replace("'", " ") + "') AS v").first()
        return np.array(r["v"], dtype=float)

    def split(self, text):
        p = (P2 + (text or "").replace("'", " ") + "</input>").replace("'", "''")
        out = self.spark.sql("SELECT ai_query('" + CHAT + "', '" + p + "') AS o").first()["o"]
        blob = re.search(r"<output>(.*?)</output>", out, re.S)
        src = blob.group(1) if blob else out
        m = re.search(r"\{.*?\}", re.sub(r"```[a-z]*", "", src), re.S)
        if not m:
            return None, None
        try:
            j = json.loads(m.group(0))
            txt = " ".join(x for x in [j.get("company",""), j.get("family",""),
                                       j.get("model_number","")] if x).strip()
            return (txt or None), j
        except Exception:
            return None, None

    def _emb_sims(self, text):
        v = self.embed(text); v = v / (np.linalg.norm(v) + 1e-9)
        return self.V @ v

    def _char_sims(self, text):
        q = self._ngrams(text)
        qn = np.sqrt(sum(v*v for v in q.values())) or 1e-9
        out = np.zeros(len(self.ng))
        for i, g in enumerate(self.ng):
            small, big = (q, g) if len(q) < len(g) else (g, q)
            out[i] = sum(c * big.get(k, 0) for k, c in small.items()) / (qn * self.ngn[i])
        return out

    def candidates(self, query, device_class=None, want_gpu=False, k=10, w_char=0.5):
        """Embeddings and character n-grams propose; the strict matcher decides.
        Filter is device_class (computer or mobile) derived from WHICH nanoreview
        ranking list a slug appears on, plus is_gpu. The old per-slug kind field was
        unreliable: it tagged Apple M5 Max as gpu and Snapdragon X Elite as cpu, which
        excluded both correct answers. List membership puts all 15 Snapdragon X parts
        in computer, which no keyword rule would do."""
        device_class = device_class or device_class_for(query)
        split_txt, parts = self.split(query)
        clean = split_txt or query
        s_raw   = self._emb_sims(query)
        s_split = self._emb_sims(clean)
        s_char  = np.maximum(self._char_sims(query), self._char_sims(clean))
        blend   = np.maximum(s_raw, s_split) * (1 - w_char) + s_char * w_char
        pools = {}
        for nm, arr in [("blend", blend), ("raw", s_raw), ("split", s_split), ("char", s_char)]:
            idx = [i for i in np.argsort(-arr)
                   if (device_class is None or self.dclass[i] == device_class)
                   and (self.is_gpu[i] == want_gpu)][:k]
            pools[nm] = [(self.keys[i], float(arr[i])) for i in idx]
        seen, union = set(), []
        for nm in ["blend", "raw", "split", "char"]:
            for s, sc in pools[nm]:
                if s not in seen:
                    seen.add(s); union.append((s, sc, nm))
        return {"pools": pools, "union": union, "split_text": clean, "parts": parts}

    def match(self, agent, company, family, model_number, kinds=None, k=10):
        """Stage 2: the strict matcher decides over the proposed candidates only."""
        query = " ".join(x for x in [company, family, model_number] if x).strip()
        res = self.candidates(query, device_class=device_class_for(query),
                              want_gpu=wants_gpu(query), k=k)
        saved = dict(agent.catalog)
        try:
            for slug, score, via in res["union"]:
                agent.catalog = {slug: saved.get(slug, "cpu")}
                if agent.find_slug(company, family, model_number,
                                   kinds or kinds_for(query)) == slug:
                    return {"slug": slug, "score": score, "via": via, "status": "accepted"}
            return {"slug": None, "score": res["union"][0][1] if res["union"] else 0.0,
                    "via": None, "status": "no candidate passed the rules"}
        finally:
            agent.catalog = saved
