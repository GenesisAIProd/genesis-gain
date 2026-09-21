import os
os.environ["GAIN_CATALOG"] = "genesis_prod"
os.environ["GAIN_SCHEMA"] = "market_signals"

import sys, importlib
sys.path.insert(0, "/Workspace/Users/data.ai@genesisig.com")

run_pipeline = importlib.import_module("genesis_gain_multi_agent.run_pipeline")
result = run_pipeline.main()
print("exit", result)
# Raise, do not just print: before 21 Sep 2026 a failed pipeline still ended as a green job.
if result != 0:
    raise RuntimeError("DRA pipeline failed (exit %s). See FAILED AGENTS above." % result)