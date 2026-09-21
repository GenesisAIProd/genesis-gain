# agent1_processor

Genesis GAIN Sub-Agent 1: the processor reference database for laptops, desktops,
smartphones and tablets. For every processor it holds cores, threads, Geekbench and
PassMark scores, and the source page behind every number.

## How production runs it

A Databricks Workflow in dbw-genesis-prod, run as data.ai, on the first Monday of every
month at 3:00 AM India time. It imports these files by folder name, builds every part on
catalog genesis_prod, and calls MonthlyRun.run(). Each run first checks the catalog is
complete and stops with a plain message if not. It then sources chips customers asked
for, fills the rest of the backlog, and exports the full database as dated CSV and JSON.

## Files

genesis_agent1.py            fetching, matching rules, tier and brand checks, keys
genesis_agent1_matching.py   embeddings and character matching propose candidates
genesis_agent1_queue.py      answers questions; queues chips we do not hold yet
genesis_agent1_service.py    one entry point for the dashboard, MCP and API; permissions
genesis_agent1_mcp.py        the nine MCP tools
genesis_agent1_monthly.py    the monthly run, export and quality checks
genesis_agent1_setup.py      creates every table and view; the preflight check
genesis_agent1_upload.py     internal CSV and Excel upload

## Rules that must not be broken

Every processor is keyed brand|family|number, as the Genesis table design requires.
MSV and the AI models join on market_gold.processor_reference, never on the raw facts.
Geekbench columns: Geekbench 7 if available, else Geekbench 6, else any Geekbench.
PassMark is a different scale and always stays in its own columns.
Every class must be given its catalog; mixing catalogs is refused.
File upload is for Genesis staff only; the external link can only ask questions.
Changes reach production only through a reviewed pull request on main.
