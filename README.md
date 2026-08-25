# Genesis GAIN

Multi-agent market signal pipeline for ITAD refurbished-electronics valuation. Three specialized agents (supply chain, policy, AI news) gather news evidence, extract and quality-judge findings, and a supervisor reconciles them into a per-category market read. Output feeds downstream valuation and forecasting.

## Structure

    genesis_gain/
      config.py          environment-driven configuration (catalog, schema, models, budgets, fetch window)
      context.py         cached Spark, Apify, and model-call clients
      fetch.py           batched Apify news acquisition with per-run timeouts
      prompts.py         extract, triage, and judge rubrics per agent
      agents/pipeline.py per-agent triage, extract, judge, escalate, save
      supervisor.py      cross-agent reconciliation into a net market read
      feature_vector.py  per-finding numeric feature index
      orchestrator.py    concurrent run of all agents with graceful degradation
      reliability.py     circuit breaker, retries, model fallback chain
      observability.py   token and cost metering, budget status
      security.py        prompt-injection sanitizing, untrusted-text fencing
      alerts.py          budget alert email
      dashboards.py      operational SQL views
      mcp_server.py      MCP tools exposing the pipeline capabilities
      prod_setup.py      production catalog and schema creation, baseline seed
      run_pipeline.py    job entrypoint
    deploy/
      genesis_gain_job.yml  Databricks Asset Bundle job definition
    pyproject.toml

## Configuration

All environment-specific values come from environment variables so the same code runs in development and production without change. Key variables:

    GAIN_CATALOG            target catalog (genesis_devp or genesis_prod)
    GAIN_SCHEMA             target schema (dev_roohi or market_signals)
    GAIN_FETCH_WINDOW_DAYS  news lookback window in days
    GAIN_FETCH_BATCH_SIZE   query terms per Apify run
    GAIN_FETCH_TIMEOUT_SECS per-run Apify timeout
    GAIN_WEEKLY_BUDGET      weekly cost alert threshold
    GAIN_MONTHLY_BUDGET     monthly cost alert threshold

The Apify token is read from the Databricks secret scope, not the environment.

## Running

The pipeline runs as a scheduled Databricks Job on serverless compute. The job entrypoint is `run_pipeline.run`. It fetches the configured window, runs the three agents concurrently, reconciles, rebuilds the feature index, writes to the target schema, snapshots a backup, and evaluates the budget.

## Storage

Every run appends and merges; nothing is overwritten. Findings merge on a content-derived identity so re-processed news is never double-counted. Each run writes a Parquet snapshot to the backups volume.
