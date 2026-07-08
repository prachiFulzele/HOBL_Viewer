# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for details.

"""LLM access, grounding, and prompt construction (everything sent to the model).

Auth is Entra ID (AAD): azure-identity acquires a bearer token for Azure OpenAI
at request time — no API key is stored, and the LLM never sees raw credentials.
``_data_context`` grounds the model on the distinct values actually present in
the current data source, and the prompt builders turn that context (plus, for
follow-ups, the previous plan and the on-screen result) into the system prompt.
"""

import json
import time
from collections import defaultdict

import requests
from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential

import config

from .spec import _AGGREGATIONS


# Fallback backend, used only when a caller does not pass one explicitly. In
# normal operation the active source is resolved per request by app.py (which
# honors the dashboard's runtime data-source selection) and passed into
# ``analyze`` / ``_data_context``, so the AI always reads from the same source
# the dashboard is currently showing.
if config.DATA_SOURCE == "kusto":
    import kusto_data as _default_backend
else:
    import json_data as _default_backend


_AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"
_credential = None


# ── Azure OpenAI (AAD) ────────────────────────────────────────────────────────

def _get_token() -> str:
    """Acquire an AAD bearer token for Azure OpenAI.

    Tries non-interactive credentials first (env / managed identity / Azure CLI /
    VS Code), then falls back to an interactive browser sign-in. azure-identity
    caches and refreshes tokens internally.
    """
    global _credential
    if _credential is None:
        try:
            _credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
            _credential.get_token(_AOAI_SCOPE)
        except Exception:
            _credential = InteractiveBrowserCredential()
    return _credential.get_token(_AOAI_SCOPE).token


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """Send a chat completion to the configured Azure OpenAI deployment."""
    url = (
        f"{config.AZURE_OPENAI_ENDPOINT}/openai/deployments/"
        f"{config.AZURE_OPENAI_DEPLOYMENT}/chat/completions"
        f"?api-version={config.AZURE_OPENAI_API_VERSION}"
    )
    headers = {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }
    body = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_completion_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(url, headers=headers, json=body, timeout=90)
    if not resp.ok:
        raise RuntimeError(f"Azure OpenAI request failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ── Data context (grounding) ──────────────────────────────────────────────────

# Grounding (distinct dimension values + metric names) changes rarely, so it is
# cached briefly per backend to avoid a broad fetch on every question. The actual
# analysis always fetches fresh, filtered rows via ``backend.get_metrics``.
_GROUNDING_CACHE: dict = {}
_GROUNDING_TTL_SEC = 300


def _data_context(backend=None) -> dict:
    """Distinct dimension values used to ground the model on real data.

    ``backend`` is the data-source module to read from (json_data or
    kusto_data). Defaults to the module-level fallback when not provided.
    Cached per backend for ``_GROUNDING_TTL_SEC`` seconds.
    """
    backend = backend or _default_backend
    name = getattr(backend, "__name__", "default")
    cached = _GROUNDING_CACHE.get(name)
    if cached and (time.time() - cached[0]) < _GROUNDING_TTL_SEC:
        return cached[1]

    rows = backend.get_metrics()
    devices, rams, scenarios = set(), set(), set()
    metric_names = defaultdict(set)
    for r in rows:
        if r.get("Device"):
            devices.add(r["Device"])
        if r.get("Ram"):
            rams.add(str(r["Ram"]))
        if r.get("Scenario"):
            scenarios.add(r["Scenario"])
        if r.get("MetricName"):
            metric_names[r.get("MetricType", "")].add(r["MetricName"])

    # Full distinct metric names per type (a high safety cap guards against a
    # runaway prompt on pathological datasets, but it is effectively complete).
    names_by_type = {mt: sorted(ns)[:500] for mt, ns in metric_names.items()}

    ctx = {
        "devices": sorted(devices),
        "rams": sorted(rams),
        "scenarios": sorted(scenarios),
        "metric_names_by_type": names_by_type,
        "row_count": len(rows),
    }
    _GROUNDING_CACHE[name] = (time.time(), ctx)
    return ctx


# ── Prompt construction ───────────────────────────────────────────────────────

def _build_system_prompt(ctx: dict) -> str:
    return f"""You translate natural-language questions about Windows device power/performance
test results into a STRICT JSON analysis spec. You never write prose outside JSON.

The data is one fact table. Each row has these fields:
  - Device       (string)  device name
  - Ram          (string)  usable RAM config in GB
  - Scenario     (string)  test scenario / workload
  - Date         (string)  run date, yyyy-MM-dd
  - Iteration    (int)     run number within a config
  - MetricName   (string)  metric, e.g. system_power, soc_power, battery_life
  - Value        (number)  the measured value
  - Unit         (string)  e.g. W, hrs, ms
  - MetricType   (string)  one of PowerMetrics, PowerCalculation, PerfMetrics

Available values in the CURRENT dataset (use these exact strings where relevant):
  devices: {json.dumps(ctx['devices'])}
  rams: {json.dumps(ctx['rams'])}
  scenarios: {json.dumps(ctx['scenarios'])}
  metric_names_by_type: {json.dumps(ctx['metric_names_by_type'])}

Underlying data model (for your understanding only — you still output the JSON
spec below, never KQL): the metrics come from two Kusto tables joined on
TestResultId — ``Hobl_RawMetrics`` (one row per metric: Name, Value, Unit,
MetricType, RunDate, TestName) and ``Hobl_TestResultMetadata`` (per-run DUT
attributes in a dynamic Metadata column: DeviceName, UsableRam, IterationNumber,
OS build, battery capacity, ...). Device and Ram come from Metadata; Scenario =
TestName; Date = RunDate; PerfMetrics names encode a Pt id. "Last N iterations"
means the N most recent RUNS ranked by RunDate per Device+Ram+Scenario (NOT by
iteration number).

You return ONE JSON object in ONE of two modes.

MODE A — ANSWER (use when the request is clear enough to run). Shape:
{{
  "action": "answer",
  "title": "short human title for the analysis",
  "explanation": "one or two sentences describing what the table shows",
  "filters": {{
     "devices": [list of exact Device strings] or [],
     "rams": [list of exact Ram strings] or [],
     "scenarios": [list of exact Scenario strings] or [],
     "metric_name_contains": [lowercase substrings to match MetricName] or [],
     "metric_types": [subset of PowerMetrics/PowerCalculation/PerfMetrics] or [],
     "value_filters": [{{"op": one of < <= > >= == !=, "value": number}}] or [],
     "last_n": integer or null
  }},
  "group_by": [subset of Device, Ram, Scenario, Date, Iteration, MetricName, MetricType],
  "columns_from": "metric" | one of the group_by fields | "none",
  "aggregations": [one or more of {sorted(_AGGREGATIONS)}],
  "derived": [{{"name": "label", "kind": "ratio"|"percent"|"diff",
               "numerator": an aggregation, "denominator": an aggregation}}] or [],
  "sort": {{"by": "an aggregation name, derived name, or a group_by field", "order": "asc"|"desc"}} or null,
  "limit": integer or null,
  "chart": {{"chart_type": "bar"|"grouped_bar"|"line"|"box"|"none", "reason": "short why this chart fits"}}
}}

MODE B — CLARIFY (use when the request is ambiguous or underspecified). Shape:
{{
  "action": "clarify",
  "question": "one short question that resolves the ambiguity",
  "options": ["concrete option 1", "concrete option 2"]
}}

Rules:
  - Decide the MODE first. If two reasonable interpretations would produce
    MATERIALLY DIFFERENT tables, or a key detail is missing or contradictory, use
    MODE B and ASK — do NOT guess. Otherwise use MODE A. Give 2-4 concrete options.
    Examples that need clarification: "add a column with iteration 1..10" (a
    sequential index per row, or iterations spread across columns?); "show the best
    device" (best by which metric?); "compare them" (which metric and statistic?).
  - Prefer answering (MODE A) for clear requests; only ask when genuinely unsure.
  - Pick the smallest filters that answer the question. Prefer metric_name_contains
    over guessing exact names (e.g. "system power" -> ["system_power","system power"]).
  - A value threshold like "system power under 13" or "less than 13" MUST go in
    value_filters (e.g. [{{"op": "<", "value": 13}}]) so it is actually applied —
    never describe it only in prose.
  - "aggregations" may contain SEVERAL statistics to show side by side. e.g. "show
    mean, P50 and P90 of system power per device" -> ["mean","p50","p90"]; each one
    becomes its own column. Use a single-element list for a simple question.
  - "derived" adds computed columns from two of the aggregations. e.g. "P90 to P50
    ratio" -> derived [{{"name":"P90/P50 ratio","kind":"ratio","numerator":"p90",
    "denominator":"p50"}}]; ratio = num/den, percent = num/den*100, diff = num-den.
    Always include the numerator/denominator aggregations in "aggregations" too.
  - "sort"/"limit" handle "top/bottom N" questions. e.g. "top 5 devices by battery
    life" -> aggregations ["mean"], sort {{"by":"mean","order":"desc"}}, limit 5.
  - For "compare X between devices", set group_by ["Device"] (plus Scenario if a
    scenario is named), columns_from "metric", aggregations ["mean"] unless the user
    asks for median/min/max/percentile.
  - For "last N iterations / runs", set filters.last_n = N (the server applies the
    dashboard's run-date ranking). Do NOT use "limit" for that — "limit" is only for
    top/bottom-N rankings of the result rows.
  - For "list / show the last N iterations", set group_by to include Device, Date and
    Iteration, columns_from "metric", aggregations ["first"], and set filters.last_n = N.
  - "chart" picks the best visualization for the RESULT: "bar" to compare one value
    across groups, "grouped_bar" for several statistics per group, "line" for a
    trend over Date/Iteration, "box" for a distribution across iterations, or "none"
    for a single value. Give a short "reason". The server picks the axes/columns.
  - If the user names devices/scenarios that resemble the available values, map them
    to the exact available strings.
  - Do NOT emit a KQL string; the server generates the query from this spec.
"""


# Spec fields that carry the meaning of a query (used when a follow-up refines an
# existing analysis). KQL, the alias ``aggregation`` and the resolved chart axes
# are derived, so they are intentionally excluded.
_CONTEXT_FIELDS = (
    "title", "filters", "group_by", "columns_from",
    "aggregations", "derived", "sort", "limit",
)


def _spec_for_context(spec: dict) -> dict:
    """Trim a validated spec down to the fields that describe the query intent."""
    context = {key: spec[key] for key in _CONTEXT_FIELDS if key in spec}
    chart = spec.get("chart") or {}
    if chart.get("chart_type"):
        context["chart_type"] = chart["chart_type"]
    return context


def _format_result_preview(result: dict, max_rows: int = 8) -> str:
    """Render the on-screen table (title + columns + a few rows) for refine context."""
    title = str(result.get("title") or "").strip()
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    lines = []
    if title:
        lines.append(f"Title: {title}")
    if columns:
        lines.append("Columns: " + " | ".join(str(c) for c in columns))
    for row in rows[:max_rows]:
        lines.append("  " + " | ".join("" if v is None else str(v) for v in row))
    if len(rows) > max_rows:
        lines.append(f"  … (+{len(rows) - max_rows} more rows)")
    return "\n".join(lines) if lines else "(no rows)"


def _build_refine_prompt(ctx: dict, prev_spec: dict, prev_result: dict | None = None) -> str:
    """Prompt for a follow-up that adjusts an existing analysis in place.

    Reuses the full base prompt (data schema + spec format) and adds both the
    previous query plan and the result currently on screen, so the model edits
    the existing view and stays anchored to what the user is actually looking at.
    """
    plan = json.dumps(_spec_for_context(prev_spec), indent=2, default=str)
    sections = [
        _build_system_prompt(ctx),
        "\n\nREFINEMENT MODE\n"
        "The user already has the analysis below and is DRILLING DOWN on it. Treat the "
        "user's message as an ADJUSTMENT to this existing view, NOT a brand-new question: "
        "start from this plan and the result shown, and change ONLY what the user asks. "
        "Keep the SAME metrics, filters, grouping and columns as the current result unless "
        "the user EXPLICITLY asks to broaden or replace them (e.g. \"now show all metrics\"). "
        "When the user says \"these\", \"them\", \"this\", or \"the parameters\", they mean the "
        "columns and configuration of the CURRENT RESULT below. Return the COMPLETE updated "
        "spec in the same JSON schema.\n\nCURRENT QUERY PLAN:\n" + plan,
    ]
    if prev_result:
        sections.append(
            "\n\nCURRENT RESULT (what the user is looking at right now):\n"
            + _format_result_preview(prev_result)
        )
    return "".join(sections)
