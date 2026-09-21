
# agent1_backend.py
# Everything the dashboard needs from Databricks, kept out of the page code.
#
# The app has no notebook Spark or dbutils, so it reaches Databricks through Databricks
# Connect on serverless compute, and reads and writes files through the Files API.
# The agent is built in QUESTION-ONLY mode (fetch=False): the app never reads the Apify
# token and cannot fetch websites. Only the monthly job, running as data.ai, fetches.
#
# Who is internal is decided by the sign-in email Databricks passes to the app: a
# genesisig.com address is Genesis staff; anyone else, such as an invited customer
# guest, is external and gets questions only, rate-limited, with no upload.

import os

CATALOG = os.environ.get("GAIN_CATALOG", "").strip()
INTERNAL_DOMAIN = "@genesisig.com"


def require_catalog():
    if not CATALOG:
        raise RuntimeError("GAIN_CATALOG is not set. It must be stated in app.yaml, "
                           "for example genesis_prod.")
    return CATALOG


def build():
    """Returns (spark, service, service_module). Built once per app process."""
    cat = require_catalog()
    from databricks.connect import DatabricksSession
    import genesis_agent1, genesis_agent1_matching as gm
    import genesis_agent1_queue as gq, genesis_agent1_service as gsvc
    spark = DatabricksSession.builder.serverless().getOrCreate()
    a1 = genesis_agent1.Agent1(spark, None, catalog=cat, fetch=False)
    mt = gm.Matcher(spark, catalog=cat)
    qs = gq.QueueService(spark, mt, a1, catalog=cat)
    svc = gsvc.Agent1Service(spark, qs, a1, mt, catalog=cat)
    return spark, svc, gsvc


def caller_for(email, gsvc):
    email = (email or "").strip().lower()
    if email.endswith(INTERNAL_DOMAIN):
        return gsvc.Caller("genesis_user", email)
    # external, or no identity at all: the safe default is the most restricted view
    return gsvc.Caller("session", email or "anonymous")


def exports_dir():
    return "/Volumes/" + require_catalog() + "/market_config/exports"


def list_exports():
    from databricks.sdk import WorkspaceClient
    names = [e.name for e in WorkspaceClient().files.list_directory_contents(exports_dir())
             if e.name]
    return sorted(names, reverse=True)


def read_file(path):
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient().files.download(path).contents.read()
