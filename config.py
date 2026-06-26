"""Configuration for the HOBL Dashboard."""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# ── Data source ──────────────────────────────────────────────────────────────
# "json"  -> read metrics from local hobl_result*.json files (temporary stand-in
#            database used until the new Kusto columns/branch are merged &
#            ingested). All dashboard features are built against this.
# "kusto" -> query the Kusto table directly (the final, permanent source).
#
# Both backends expose the SAME query surface and schema, so switching sources
# is a one-line change here once the branch is merged.
#
# This is only the DEFAULT source on first run. The active source can be changed
# at runtime from the dashboard UI (top-right data-source control); the choice is
# persisted to RUNTIME_STATE_FILE and survives restarts.
DATA_SOURCE = "json"

# In JSON mode, the dashboard reads ONLY from files the user uploads through the
# UI. They are stored in this folder; when it is empty, no data is shown.
UPLOAD_DIR = os.path.join(_HERE, "uploaded_json")

# Persists the runtime-selected data source across restarts.
RUNTIME_STATE_FILE = os.path.join(_HERE, "runtime_state.json")

KUSTO_CLUSTER = os.getenv("KUSTO_CLUSTER", "https://<your-cluster>.kusto.windows.net")
KUSTO_DATABASE = os.getenv("KUSTO_DATABASE", "<your-database>")
KUSTO_TABLE = os.getenv("KUSTO_TABLE", "<your-table>")

# Flask settings
HOST = "127.0.0.1"
PORT = 5000
DEBUG = True

# ── AI Analysis (NL → analysis) ──────────────────────────────────────────────
# Azure OpenAI deployment used by the optional "AI Analysis" page. Auth is
# Entra ID (AAD) only — this resource has key auth disabled — so no API key is
# stored; azure-identity acquires a bearer token at request time. Values can be
# overridden via environment variables (e.g. a local .env exported into the env).
AZURE_OPENAI_ENDPOINT = os.getenv(
    "AZURE_OPENAI_ENDPOINT",
    "https://<your-resource>.cognitiveservices.azure.com",
).rstrip("/")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

# When true, the AI Analysis summary is polished into friendlier prose by a
# second model pass (it only rephrases the figures the server already computed,
# never inventing numbers). Set to "0" to use the fast, offline template only.
AI_SUMMARY_POLISH = os.getenv("AI_SUMMARY_POLISH", "1") not in ("0", "false", "False")
