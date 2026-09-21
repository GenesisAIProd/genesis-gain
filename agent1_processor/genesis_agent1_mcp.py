
# genesis_agent1_mcp.py
# MCP tool declarations for Sub-Agent 1.
#
# Deliberately thin. Every tool is a few lines that call Agent1Service, which holds
# the logic. The Streamlit app and any REST endpoint call the same service, so there
# is one path rather than three implementations that drift apart.
#
# Caller identity is PASSED IN, never inferred from the environment.

TOOLS = [
  {"name": "ask",
   "access": "open",
   "description": "Answer for a single processor named in free text. Any phrasing: "
                  "an OEM string, a question, lowercase, typos. Returns cores, "
                  "single-core score and multi-core score with the source URL behind "
                  "each number. If the chip is not in the database it is recorded and "
                  "the caller is told it will be sourced in the next monthly run.",
   "input": {"query": "string, the customer text"},
   "output": {"status": "found | queued | no_processor_in_query | rate_limited",
              "processor_key": "string", "facts": "list of fact rows",
              "match_score": "float"}},

  {"name": "ask_many",
   "access": "open",
   "description": "Answer for EVERY processor named in a message. Handles comparisons "
                  "and lists. Up to 20 chips are answered on the spot; above that it "
                  "becomes a job automatically and returns a job id, so the caller "
                  "never has to choose. Hard cap 100, with any overflow recorded "
                  "rather than silently dropped.",
   "input": {"query": "string"},
   "output": {"chip_count": "int", "results": "one entry per chip",
              "job_id": "present only when it became a job"}},

  {"name": "job_status",
   "access": "open, owner only",
   "description": "Progress of a job. Returns not permitted unless the caller owns it.",
   "input": {"job_id": "string"},
   "output": {"status": "queued | running | complete | failed", "chip_count": "int"}},

  {"name": "job_results",
   "access": "open, owner only",
   "description": "Results of a completed job as rows, plus the path to a downloadable "
                  "file. Returns not permitted unless the caller owns it.",
   "input": {"job_id": "string"},
   "output": {"results": "one row per chip", "download": "file path"}},

  {"name": "source_health",
   "access": "open",
   "description": "Which benchmark sources are reachable, which route each needs, and "
                  "whether any is currently degraded.",
   "input": {},
   "output": {"sources": "list with state and consecutive_failures"}},

  {"name": "queue_status",
   "access": "open",
   "description": "How many chips are waiting for the monthly run, how many resolved, "
                  "and how many were unresolvable. times_asked shows which missing "
                  "chips customers actually want.",
   "input": {},
   "output": {"by_status": "counts"}},

  {"name": "submit_csv",
   "access": "internal only",
   "description": "Process a spreadsheet of machines. The processor column is detected "
                  "from the DATA, not from the column name, so a bid list with any "
                  "header works. Returns a job id.",
   "input": {"rows": "list of dicts", "column": "optional, otherwise detected"},
   "output": {"job_id": "string", "detected_column": "string"}},

  {"name": "run_backfill",
   "access": "internal only",
   "description": "Fetch and store facts for processors that have none. Costs time and "
                  "unblocker units, so it is reserved for the scheduled job and Genesis "
                  "staff.",
   "input": {"limit": "int, optional"},
   "output": {"attempted": "int", "tally": "counts by outcome"}},

  {"name": "process_queue",
   "access": "internal only",
   "description": "Monthly: take chips customers asked for that we did not hold, add "
                  "them to the backlog, source them, and close them out. Unresolvable "
                  "after two attempts.",
   "input": {"run_id": "string, optional"},
   "output": {"attempted": "int", "tally": "counts"}},
]


class Agent1MCP:
    # One server, all nine tools. Protection is by CALLER, not by hiding tools:
    # splitting read and write into two servers would mean two codebases for one agent.

    def __init__(self, service):
        self.svc = service

    def list_tools(self):
        out = []
        for t in TOOLS:
            out.append({"name": t["name"], "description": t["description"],
                        "access": t["access"], "input": t["input"]})
        return out

    def call(self, caller, tool, args=None):
        args = args or {}
        if tool not in [t["name"] for t in TOOLS]:
            return {"error": "unknown tool: " + str(tool)}
        if not caller.may_call(tool):
            return {"error": "not permitted",
                    "detail": tool + " is available to Genesis staff and the scheduled "
                              "job only"}
        try:
            if tool == "ask":
                return self.svc.ask(caller, args.get("query", ""))
            if tool == "ask_many":
                return self.svc.ask_many(caller, args.get("query", ""))
            if tool == "job_status":
                return self.svc.job_status(caller, args.get("job_id", ""))
            if tool == "job_results":
                return self.svc.job_results(caller, args.get("job_id", ""))
            if tool == "source_health":
                return {"sources": self.svc.source_health(caller)}
            if tool == "queue_status":
                return {"by_status": self.svc.queue_status(caller)}
            if tool == "run_backfill":
                todo = self.svc.agent.list_unresolved(limit=args.get("limit", 100000))
                return self.svc.agent.run_backfill(todo=todo, verbose=False)
            if tool == "process_queue":
                return self.svc.qs.process_queue(run_id=args.get("run_id"), verbose=False)
            if tool == "submit_csv":
                return self.svc.submit_csv(caller, args.get("rows", []),
                                           args.get("column"))
        except Exception as e:
            return {"error": type(e).__name__, "detail": str(e)[:200]}
        return {"error": "tool not wired"}
