import os
os.environ["GAIN_CATALOG"] = "genesis_prod"
os.environ["GAIN_SCHEMA"] = "market_signals"

import sys, importlib
sys.path.insert(0, "/Workspace/Users/data.ai@genesisig.com")

run_pipeline = importlib.import_module("genesis_gain_multi_agent.run_pipeline")
result = run_pipeline.main()
print("exit", result)