# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for details.

"""AI Analysis backend for the HOBL Dashboard (optional "AI Analysis" page).

Turns a natural-language question (e.g. "compare system power between the Surface
and the XPS") into a concrete, read-only analysis over whatever data source the
dashboard is currently using. It is intentionally backend-agnostic: it reads rows
through the same ``backend.get_metrics`` surface that powers the rest of the app,
so it works identically against the JSON stand-in today and against Kusto later.

How it stays accurate (and safe):
    * The LLM never sees raw credentials and never executes code. It only emits a
      small, validated JSON *spec* describing the filters / grouping / aggregation
      it wants.
    * All numbers are computed deterministically in Python from real rows, so the
      figures in the table are never hallucinated.
    * The operation is strictly read-only — it filters and aggregates in memory.

Auth is Entra ID (AAD): azure-identity acquires a bearer token for Azure OpenAI
at request time. No API key is stored.
"""

import json
import re
from collections import defaultdict

import requests
from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential

import config

# Resolve the active data backend exactly like app.py does, so the AI analysis
# always reads from the same source the dashboard is showing.
if config.DATA_SOURCE == "kusto":
    import kusto_data as backend
else:
    import json_data as backend


_AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"
_credential = None

# Aggregations the executor supports. Percentiles use linear interpolation,
# matching the dashboard's existing percentile math.
_AGGREGATIONS = {"mean", "median", "min", "max", "count", "first", "p50", "p70", "p90"}

# Fields a row can be grouped / pivoted on.
_DIMENSIONS = {"Device", "Ram", "Scenario", "Date", "Iteration", "MetricName", "MetricType"}

# Comparison operators allowed in value_filters.
_VALUE_OPS = {"<", "<=", ">", ">=", "==", "!="}

# Chart types the AI may choose; the server validates and falls back as needed.
_CHART_TYPES = {"bar", "grouped_bar", "line", "box", "none"}


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

def _data_context() -> dict:
    """Distinct dimension values used to ground the model on real data."""
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

    # Cap metric-name lists so the prompt stays small but representative.
    sample_metrics = {}
    for mtype, names in metric_names.items():
        sample_metrics[mtype] = sorted(names)[:40]

    return {
        "devices": sorted(devices),
        "rams": sorted(rams),
        "scenarios": sorted(scenarios),
        "metric_names_by_type": sample_metrics,
        "rows": rows,
    }


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
  - For "list / show the last N rows", set group_by to include Device, Date and
    Iteration, columns_from "metric", aggregations ["first"], and set "limit" to N.
  - "chart" picks the best visualization for the RESULT: "bar" to compare one value
    across groups, "grouped_bar" for several statistics per group, "line" for a
    trend over Date/Iteration, "box" for a distribution across iterations, or "none"
    for a single value. Give a short "reason". The server picks the axes/columns.
  - If the user names devices/scenarios that resemble the available values, map them
    to the exact available strings.
  - Do NOT emit a KQL string; the server generates the query from this spec.
"""


# ── Spec validation ───────────────────────────────────────────────────────────

def _validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("Model did not return a JSON object.")

    filters = spec.get("filters") or {}

    def _strlist(v):
        if not isinstance(v, list):
            return []
        return [str(x) for x in v if str(x).strip()]

    last_n = filters.get("last_n")
    try:
        last_n = int(last_n) if last_n not in (None, "", "null") else None
    except (TypeError, ValueError):
        last_n = None

    limit = spec.get("limit")
    try:
        limit = int(limit) if limit not in (None, "", "null") else None
    except (TypeError, ValueError):
        limit = None
    if limit is not None and limit <= 0:
        limit = None

    # Parse structured value thresholds (e.g. {"op": "<", "value": 13}).
    value_filters = []
    for vf in (filters.get("value_filters") or []):
        if not isinstance(vf, dict):
            continue
        op = str(vf.get("op", "")).strip()
        try:
            threshold = float(vf.get("value"))
        except (TypeError, ValueError):
            continue
        if op in _VALUE_OPS:
            value_filters.append({"op": op, "value": threshold})

    group_by = [g for g in (spec.get("group_by") or []) if g in _DIMENSIONS]
    if not group_by:
        group_by = ["Device"]

    # Aggregations: accept a list, fall back to the legacy single "aggregation".
    aggs = spec.get("aggregations")
    if isinstance(aggs, list):
        aggs = [a for a in aggs if a in _AGGREGATIONS]
    else:
        aggs = []
    if not aggs:
        single = spec.get("aggregation")
        aggs = [single] if single in _AGGREGATIONS else ["mean"]
    # De-duplicate while preserving order.
    _seen = set()
    aggs = [a for a in aggs if not (a in _seen or _seen.add(a))]

    # Derived columns computed from two aggregations.
    derived = []
    for d in (spec.get("derived") or []):
        if not isinstance(d, dict):
            continue
        num = d.get("numerator")
        den = d.get("denominator")
        kind = str(d.get("kind") or "ratio").lower()
        if num in _AGGREGATIONS and den in _AGGREGATIONS and kind in ("ratio", "percent", "diff"):
            derived.append({
                "name": str(d.get("name") or "").strip(),
                "kind": kind,
                "numerator": num,
                "denominator": den,
            })

    # Sorting.
    sort = None
    s = spec.get("sort")
    if isinstance(s, dict):
        by = str(s.get("by") or "").strip()
        order = str(s.get("order") or "desc").lower()
        if order not in ("asc", "desc"):
            order = "desc"
        if by:
            sort = {"by": by, "order": order}

    columns_from = spec.get("columns_from") or "metric"
    if columns_from not in ("metric", "none") and columns_from not in _DIMENSIONS:
        columns_from = "metric"

    # Chart preference (the LLM's choice; the server still picks the axes/columns
    # and falls back to a heuristic when this is missing or unsuitable).
    chart_pref = {"chart_type": None, "reason": ""}
    c = spec.get("chart")
    if isinstance(c, dict):
        ctype = str(c.get("chart_type") or "").strip().lower()
        chart_pref = {
            "chart_type": ctype if ctype in _CHART_TYPES else None,
            "reason": str(c.get("reason") or "").strip(),
        }

    validated = {
        "title": str(spec.get("title") or "Analysis"),
        "explanation": str(spec.get("explanation") or ""),
        "filters": {
            "devices": _strlist(filters.get("devices")),
            "rams": _strlist(filters.get("rams")),
            "scenarios": _strlist(filters.get("scenarios")),
            "metric_name_contains": [s.lower() for s in _strlist(filters.get("metric_name_contains"))],
            "metric_types": _strlist(filters.get("metric_types")),
            "value_filters": value_filters,
            "last_n": last_n,
        },
        "group_by": group_by,
        "columns_from": columns_from,
        "aggregations": aggs,
        "aggregation": aggs[0],  # primary, for backward-compatible callers
        "derived": derived,
        "sort": sort,
        "limit": limit,
        "chart": chart_pref,
    }
    # Always generate the displayed query from the validated spec so what the
    # user sees can never diverge from what actually executes.
    validated["kql"] = _spec_to_kql(validated)
    return validated


def _spec_to_kql(spec: dict) -> str:
    """Render a read-only, KQL-style query reflecting exactly what executes."""
    f = spec["filters"]
    lines = ["Hobl_RawMetrics"]

    def _inlist(field, values):
        items = ", ".join(f'"{v}"' for v in values)
        lines.append(f"| where {field} in ({items})")

    if f["devices"]:
        _inlist("Device", f["devices"])
    if f["rams"]:
        _inlist("Ram", f["rams"])
    if f["scenarios"]:
        _inlist("Scenario", f["scenarios"])
    if f["metric_types"]:
        _inlist("MetricType", f["metric_types"])
    if f["metric_name_contains"]:
        terms = ", ".join(f'"{s}"' for s in f["metric_name_contains"])
        lines.append(f"| where MetricName has_any ({terms})")
    for vf in f["value_filters"]:
        lines.append(f"| where Value {vf['op']} {vf['value']:g}")
    if f["last_n"]:
        lines.append(
            f"| // keep the most recent {f['last_n']} iteration(s) per Device, Ram, Scenario"
        )

    by = ", ".join(spec["group_by"])
    aggs = spec["aggregations"]
    is_listing = aggs == ["first"] or aggs == ["count"]
    if is_listing:
        proj = "Device, Ram, Scenario, Date, Iteration, MetricName, Value, Unit"
        lines.append(f"| project {proj}")
        lines.append("| order by Date desc, Iteration desc")
    else:
        agg_exprs = ", ".join(f"{a}={_agg_kql(a)}" for a in aggs)
        lines.append(f"| summarize {agg_exprs} by {by}")
        for d in spec["derived"]:
            name = (d["name"] or f"{d['numerator']}_{d['kind']}_{d['denominator']}").replace(" ", "_")
            if d["kind"] == "ratio":
                expr = f"{d['numerator']} / {d['denominator']}"
            elif d["kind"] == "percent":
                expr = f"{d['numerator']} / {d['denominator']} * 100"
            else:
                expr = f"{d['numerator']} - {d['denominator']}"
            lines.append(f"| extend {name} = {expr}")
    if spec.get("sort"):
        s = spec["sort"]
        lines.append(f"| order by {s['by'].replace(' ', '_')} {s['order']}")
    if spec["limit"]:
        lines.append(f"| take {spec['limit']}")
    return "\n".join(lines)


# Aggregation → KQL expression, for the displayed (transparency) query.
_AGG_KQL = {
    "mean": "avg(Value)", "median": "percentile(Value, 50)",
    "p50": "percentile(Value, 50)", "p70": "percentile(Value, 70)",
    "p90": "percentile(Value, 90)", "min": "min(Value)", "max": "max(Value)",
    "count": "count()", "first": "any(Value)",
}


def _agg_kql(agg):
    return _AGG_KQL.get(agg, "avg(Value)")


# ── Deterministic execution ───────────────────────────────────────────────────

def _percentile(sorted_vals, p):
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = (p / 100) * (n - 1)
    lo, hi = int(pos), min(int(pos) + 1, n - 1)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (pos - lo) * (sorted_vals[hi] - sorted_vals[lo])


def _aggregate(values, agg):
    nums = [v for v in values if isinstance(v, (int, float))]
    if agg == "count":
        return len(values)
    if not nums:
        return None
    if agg == "first":
        return nums[0]
    if agg == "min":
        return min(nums)
    if agg == "max":
        return max(nums)
    if agg == "mean":
        return sum(nums) / len(nums)
    s = sorted(nums)
    if agg in ("median", "p50"):
        return _percentile(s, 50)
    if agg == "p70":
        return _percentile(s, 70)
    if agg == "p90":
        return _percentile(s, 90)
    return sum(nums) / len(nums)


# Human-readable label for an aggregation, used in column headers.
_AGG_LABELS = {
    "mean": "Mean", "median": "Median", "min": "Min", "max": "Max",
    "count": "Count", "first": "", "p50": "P50", "p70": "P70", "p90": "P90",
}


def _agg_label(agg):
    return _AGG_LABELS.get(agg, agg.capitalize())


def _apply_last_n(rows, last_n):
    """Keep the most recent ``last_n`` iterations per Device+Ram+Scenario."""
    if not last_n or last_n <= 0:
        return rows
    groups = defaultdict(set)
    for r in rows:
        groups[(r["Device"], r["Ram"], r["Scenario"])].add((r["Date"], r["Iteration"]))
    keep = {}
    for key, vals in groups.items():
        ordered = sorted(vals, key=lambda x: (x[0], x[1] if x[1] is not None else 0), reverse=True)[:last_n]
        keep[key] = set(ordered)
    return [r for r in rows if (r["Date"], r["Iteration"]) in keep[(r["Device"], r["Ram"], r["Scenario"])]]


def _value_passes(value, value_filters):
    """True when ``value`` satisfies every value_filter (AND)."""
    if not value_filters:
        return True
    if not isinstance(value, (int, float)):
        return False
    import operator
    ops = {
        "<": operator.lt, "<=": operator.le, ">": operator.gt,
        ">=": operator.ge, "==": operator.eq, "!=": operator.ne,
    }
    return all(ops[vf["op"]](value, vf["value"]) for vf in value_filters)


def _filter_rows(rows, f):
    devices = {d.lower() for d in f["devices"]}
    rams = {r.lower() for r in f["rams"]}
    scenarios = {s.lower() for s in f["scenarios"]}
    contains = f["metric_name_contains"]
    types = {t.lower() for t in f["metric_types"]}
    value_filters = f.get("value_filters") or []

    out = []
    for r in rows:
        if devices and str(r.get("Device", "")).lower() not in devices:
            continue
        if rams and str(r.get("Ram", "")).lower() not in rams:
            continue
        if scenarios and str(r.get("Scenario", "")).lower() not in scenarios:
            continue
        if types and str(r.get("MetricType", "")).lower() not in types:
            continue
        if contains:
            name = str(r.get("MetricName", "")).lower()
            if not any(sub in name for sub in contains):
                continue
        if not _value_passes(r.get("Value"), value_filters):
            continue
        out.append(r)
    return _apply_last_n(out, f["last_n"])


def _build_table(rows, spec):
    """Pivot filtered rows into {columns, rows} per the spec, computing values."""
    group_by = spec["group_by"]
    columns_from = spec["columns_from"]
    agg = spec["aggregation"]

    if columns_from == "metric":
        row_dims = [g for g in group_by if g != "MetricName"]
        col_key = lambda r: r.get("MetricName", "")
    elif columns_from in _DIMENSIONS:
        row_dims = [g for g in group_by if g != columns_from]
        col_key = lambda r: str(r.get(columns_from, ""))
    else:  # "none"
        row_dims = group_by
        col_key = lambda r: "Value"

    if not row_dims:
        row_dims = ["Device"]

    # bucket[row_key][col_key] -> list of values
    buckets = defaultdict(lambda: defaultdict(list))
    units = {}
    col_set = set()
    for r in rows:
        rkey = tuple(str(r.get(d, "")) for d in row_dims)
        ckey = str(col_key(r))
        buckets[rkey][ckey].append(r.get("Value"))
        col_set.add(ckey)
        if r.get("Unit"):
            units.setdefault(ckey, r.get("Unit"))

def _build_table(rows, spec):
    """Pivot filtered rows into {columns, rows} per the spec, computing values.

    Supports several aggregations side by side, derived (ratio/percent/diff)
    columns computed from two aggregations, and sorting / top-N limiting.
    """
    group_by = spec["group_by"]
    columns_from = spec["columns_from"]
    aggs = spec["aggregations"]
    derived = spec["derived"]

    pivot_is_metric = columns_from == "metric"
    if pivot_is_metric:
        row_dims = [g for g in group_by if g != "MetricName"]
        col_key = lambda r: str(r.get("MetricName", ""))
    elif columns_from in _DIMENSIONS:
        row_dims = [g for g in group_by if g != columns_from]
        col_key = lambda r: str(r.get(columns_from, ""))
    else:  # "none"
        row_dims = group_by
        col_key = lambda r: "Value"

    if not row_dims:
        row_dims = ["Device"]

    # bucket[row_key][col_key] -> list of values
    buckets = defaultdict(lambda: defaultdict(list))
    units = {}
    col_set = set()
    for r in rows:
        rkey = tuple(str(r.get(d, "")) for d in row_dims)
        ckey = str(col_key(r))
        buckets[rkey][ckey].append(r.get("Value"))
        col_set.add(ckey)
        if r.get("Unit"):
            units.setdefault(ckey, r.get("Unit"))
    col_keys = sorted(col_set)
    multi_col = len(col_keys) > 1

    def _unit_suffix(ckey):
        unit = units.get(ckey)
        return f" ({unit})" if pivot_is_metric and unit and unit != "N/A" else ""

    def _agg_header(ckey, agg):
        label = _agg_label(agg)
        if pivot_is_metric or columns_from in _DIMENSIONS:
            base = f"{label} {ckey}" if label else ckey
        else:  # "none"
            base = label or "Value"
        return base + _unit_suffix(ckey)

    def _derived_header(ckey, d):
        name = d["name"] or f"{_agg_label(d['numerator']) or d['numerator']}"
        if not d["name"]:
            sym = {"ratio": "/", "percent": "% of", "diff": "−"}[d["kind"]]
            name = f"{_agg_label(d['numerator'])} {sym} {_agg_label(d['denominator'])}"
        return f"{name} ({ckey})" if multi_col and pivot_is_metric else name

    # Column plan: (kind, ckey, payload). kind is "agg" or "derived".
    col_plan = []
    for ckey in col_keys:
        for agg in aggs:
            col_plan.append(("agg", ckey, agg))
    for ckey in col_keys:
        for d in derived:
            col_plan.append(("derived", ckey, d))

    headers = list(row_dims)
    for kind, ckey, payload in col_plan:
        headers.append(_agg_header(ckey, payload) if kind == "agg" else _derived_header(ckey, payload))

    def _derive(kind, num, den):
        if not isinstance(num, (int, float)) or not isinstance(den, (int, float)):
            return None
        if kind == "diff":
            return num - den
        if den == 0:
            return None
        return num / den * 100 if kind == "percent" else num / den

    table_rows = []
    for rkey in sorted(buckets.keys()):
        cells = list(rkey)
        for kind, ckey, payload in col_plan:
            vals = buckets[rkey].get(ckey, [])
            if kind == "agg":
                cells.append(_aggregate(vals, payload))
            else:
                num = _aggregate(vals, payload["numerator"])
                den = _aggregate(vals, payload["denominator"])
                cells.append(_derive(payload["kind"], num, den))
        table_rows.append(cells)

    n_dims = len(row_dims)
    table_rows = _sort_and_limit(table_rows, headers, n_dims, row_dims, col_plan, spec)

    return {
        "columns": headers,
        "rows": table_rows,
        "row_dims": row_dims,
        "value_start": n_dims,
    }


def _sort_and_limit(table_rows, headers, n_dims, row_dims, col_plan, spec):
    """Order rows per spec['sort'] (or a sensible default) then apply limit."""
    sort = spec.get("sort")
    target = None  # column index to sort by
    order_desc = True

    if sort:
        by = sort["by"].strip().lower()
        order_desc = sort["order"] == "desc"
        # 1) a row dimension name
        for i, d in enumerate(row_dims):
            if d.lower() == by:
                target = i
                break
        # 2) an aggregation or derived column (match its plan / header)
        if target is None:
            for j, (kind, ckey, payload) in enumerate(col_plan):
                ident = payload if kind == "agg" else (payload.get("name") or payload["kind"])
                if str(ident).lower() == by:
                    target = n_dims + j
                    break
        # 3) fall back to a header substring match
        if target is None:
            for i, h in enumerate(headers):
                if by in h.lower():
                    target = i
                    break
    elif spec.get("limit"):
        # Default ordering for "last N" listings: Date then Iteration, descending.
        order_cols = [row_dims.index(n) for n in ("Date", "Iteration") if n in row_dims]
        if order_cols:
            def _compound(row):
                key = []
                for idx in order_cols:
                    v = row[idx]
                    try:
                        key.append((0, float(v)))
                    except (TypeError, ValueError):
                        key.append((1, str(v)))
                return key
            table_rows = sorted(table_rows, key=_compound, reverse=True)

    if target is not None:
        def _key(row):
            v = row[target]
            if isinstance(v, (int, float)):
                return (0, v)
            return (1, str(v))
        table_rows = sorted(table_rows, key=_key, reverse=order_desc)

    if spec.get("limit"):
        table_rows = table_rows[: spec["limit"]]
    return table_rows


# ── Summary (domain-aware, human-readable) ────────────────────────────────────

# Metrics where a HIGHER value is the better outcome; everything else (power,
# latency, etc.) is treated as "lower is better".
_HIGHER_BETTER_HINTS = ("battery", "life", "fps", "score", "throughput", "duration_h")
_HIGHER_BETTER_UNITS = ("hrs", "hr", "hours", "fps")


def _pretty_metric(text):
    """Turn a raw header/metric like 'P90 system_power (W)' into 'P90 System Power'."""
    text = re.sub(r"\s*\([^)]*\)\s*$", "", str(text))  # drop trailing unit
    text = text.replace("_", " ").strip()
    # Title-case words but keep short stat tokens (P50/P90) uppercase.
    out = []
    for w in text.split():
        if re.fullmatch(r"[pP]\d{1,3}", w):
            out.append(w.upper())
        else:
            out.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(out)


def _unit_of(header):
    m = re.search(r"\(([^)]*)\)\s*$", str(header))
    return m.group(1) if m else ""


def _direction(header):
    """'higher' if a larger value is better, else 'lower'. None for ratios/diffs."""
    h = str(header).lower()
    if any(k in h for k in ("ratio", "% of", "diff", "−", " - ")):
        return None
    unit = _unit_of(header).lower()
    if any(k in h for k in _HIGHER_BETTER_HINTS) or unit in _HIGHER_BETTER_UNITS:
        return "higher"
    return "lower"


def _fmt(v, unit=""):
    if not isinstance(v, (int, float)):
        return str(v)
    s = str(int(v)) if float(v).is_integer() else (f"{v:.1f}" if abs(v) >= 100 else f"{v:.3f}")
    return f"{s} {unit}".strip()


def _build_summary(table, spec):
    """Build a structured, domain-aware summary: {headline, points, conclusion}.

    All figures come from the already-computed table, so nothing is invented.
    """
    rows = table["rows"]
    headers = table["columns"]
    n_dims = table["value_start"]
    value_cols = list(range(n_dims, len(headers)))
    if not rows or not value_cols:
        return {"headline": "No numeric results to summarize.", "points": [], "conclusion": ""}

    def label_of(row):
        return " / ".join(x for x in row[:n_dims] if x) or "result"

    is_listing = "first" in spec.get("aggregations", [])

    # Wide listing of many metrics → give an overview, not an arbitrary ranking.
    if is_listing and len(value_cols) > 1:
        return _summary_for_listing(table)

    # Single row → describe its values plainly.
    if len(rows) == 1:
        row = rows[0]
        name = label_of(row)
        pieces = []
        for ci in value_cols:
            v = row[ci]
            if isinstance(v, (int, float)):
                pieces.append(f"{_pretty_metric(headers[ci])} = {_fmt(v, _unit_of(headers[ci]))}")
        headline = f"{name}: " + ", ".join(pieces) if pieces else f"{name}: no numeric values."
        return {"headline": headline, "points": [], "conclusion": ""}

    # Multi-row → rank by the first value column (the focus metric).
    focus = value_cols[0]
    unit = _unit_of(headers[focus])
    direction = _direction(headers[focus])
    metric = _pretty_metric(headers[focus])

    pairs = [(label_of(r), r[focus]) for r in rows if isinstance(r[focus], (int, float))]
    if len(pairs) < 2:
        return {"headline": f"{metric} computed for {len(rows)} group(s).", "points": [], "conclusion": ""}

    pairs.sort(key=lambda x: x[1])
    lo_name, lo_val = pairs[0]
    hi_name, hi_val = pairs[-1]
    vals = [v for _, v in pairs]
    avg = sum(vals) / len(vals)

    if direction == "higher":
        best_name, best_val, worst_name, worst_val = hi_name, hi_val, lo_name, lo_val
        best_tag, worst_tag, cmp_word = "best", "lowest", "higher"
    elif direction == "lower":
        best_name, best_val, worst_name, worst_val = lo_name, lo_val, hi_name, hi_val
        best_tag, worst_tag, cmp_word = "most efficient", "highest", "lower"
    else:  # neutral (ratios)
        best_name, best_val, worst_name, worst_val = hi_name, hi_val, lo_name, lo_val
        best_tag, worst_tag, cmp_word = "highest", "lowest", "higher"

    gap = abs(hi_val - lo_val)
    pct = (gap / lo_val * 100) if abs(lo_val) > 1e-9 else None
    pct_str = f"about {pct:.0f}% {cmp_word}" if pct is not None else f"{_fmt(gap, unit)} apart"

    if direction in ("higher", "lower"):
        headline = (
            f"{best_name} leads on {metric} ({_fmt(best_val, unit)}) — "
            f"{pct_str} than {worst_name} ({_fmt(worst_val, unit)})."
        )
    else:
        headline = (
            f"{hi_name} has the highest {metric} ({_fmt(hi_val, unit)}); "
            f"{lo_name} the lowest ({_fmt(lo_val, unit)})."
        )

    points = [
        f"{best_tag.capitalize()}: {best_name} — {_fmt(best_val, unit)}",
        f"{worst_tag.capitalize()}: {worst_name} — {_fmt(worst_val, unit)}",
    ]
    spread = f"Difference: {_fmt(gap, unit)} between best and worst"
    if pct is not None:
        spread += f" (~{pct:.0f}%)"
    spread += f", across {len(pairs)} groups."
    points.append(spread)
    points.append(f"Average {metric}: {_fmt(avg, unit)} (across {len(pairs)} groups).")
    points.append(f"Range: {_fmt(lo_val, unit)} to {_fmt(hi_val, unit)}.")

    # If the table also carries the SAME metric at several percentiles, note the
    # tail behaviour (how far P90 sits above P50) — a useful consistency signal.
    tail = _tail_insight(table)
    if tail:
        points.append(tail)

    # Mention any additional computed columns so the reader knows what's in the table.
    extra = [_pretty_metric(headers[ci]) for ci in value_cols[1:]]
    if extra:
        shown = ", ".join(extra[:4]) + ("…" if len(extra) > 4 else "")
        points.append(f"Also in the table: {shown}.")

    # Plain-language conclusion / recommendation.
    if direction == "lower":
        conclusion = f"Conclusion: {best_name} is the more efficient choice on {metric}"
    elif direction == "higher":
        conclusion = f"Conclusion: {best_name} performs best on {metric}"
    else:
        conclusion = f"Conclusion: {hi_name} shows the highest {metric}"
    conclusion += f", leading {worst_name} by ~{pct:.0f}%." if pct is not None else "."

    return {"headline": headline, "points": points, "conclusion": conclusion}


def _tail_insight(table):
    """If P50 and P90 of the same metric are present, describe the tail spread."""
    headers = table["columns"]
    rows = table["rows"]
    n_dims = table["value_start"]

    def find(tag):
        for ci in range(n_dims, len(headers)):
            if str(headers[ci]).lower().startswith(tag.lower() + " "):
                return ci
        return None

    p50i, p90i = find("P50"), find("P90")
    if p50i is None or p90i is None:
        return None
    spreads = []
    for r in rows:
        a, b = r[p50i], r[p90i]
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a) > 1e-9:
            spreads.append((b - a) / a * 100)
    if not spreads:
        return None
    avg_spread = sum(spreads) / len(spreads)
    return f"Tail: P90 runs ~{avg_spread:.0f}% above P50 on average (higher = less consistent)."


def _summary_for_listing(table):
    """Overview summary for a wide per-iteration listing of many metrics."""
    headers = table["columns"]
    rows = table["rows"]
    n_dims = table["value_start"]
    value_cols = list(range(n_dims, len(headers)))

    n_runs = len(rows)
    n_devices = len({r[0] for r in rows}) if n_dims else 0

    varying, constant, allzero = [], [], []
    for ci in value_cols:
        nums = [r[ci] for r in rows if isinstance(r[ci], (int, float))]
        if not nums:
            continue
        mn, mx = min(nums), max(nums)
        name = _pretty_metric(headers[ci])
        if mn == 0 and mx == 0:
            allzero.append(name)
        elif mn == mx:
            constant.append(name)
        else:
            varying.append((name, mx - mn))
    varying.sort(key=lambda x: -x[1])

    headline = (
        f"{len(value_cols)} metrics listed for {n_runs} run(s) "
        f"across {n_devices} device(s)."
    )
    points = []
    if varying:
        names = ", ".join(n for n, _ in varying[:5]) + ("…" if len(varying) > 5 else "")
        points.append(f"Metrics that vary run-to-run: {names}.")
    if allzero:
        names = ", ".join(allzero[:5]) + ("…" if len(allzero) > 5 else "")
        points.append(f"Always zero (inactive in this scenario): {names}.")
    if constant:
        names = ", ".join(constant[:5]) + ("…" if len(constant) > 5 else "")
        points.append(f"Constant across runs: {names}.")

    conclusion = (
        "Conclusion: this is a full per-iteration listing — use the table for the "
        "detail, or ask about a single metric (e.g. \"cpu power over iterations\") "
        "to see its trend as a chart."
    )
    return {"headline": headline, "points": points, "conclusion": conclusion}


def _polish_summary(question, base, table):
    """Rephrase the computed summary into friendlier prose via the LLM.

    The model may only reword the supplied facts — it must not change any number.
    Returns a {headline, points} dict, or None on any failure (caller falls back).
    """
    facts = {
        "question": question,
        "headline": base["headline"],
        "points": base["points"],
        "conclusion": base.get("conclusion", ""),
        "columns": table["columns"],
        "rows": table["rows"][:25],
    }
    system = (
        "You write clear, helpful data insights for a power/performance dashboard. "
        "You are given facts already computed from real data. Rewrite them as plain, "
        "friendly English a non-expert understands. CRITICAL: do not change, add, or "
        "invent any numbers — only reuse the figures provided. Provide one headline "
        "sentence, 3-6 short insight bullet points, and a one-sentence conclusion that "
        "interprets what the result means (a takeaway or recommendation). Return ONLY "
        'JSON of the form {"headline": "...", "points": ["...", "..."], "conclusion": "..."}.'
    )
    try:
        raw = _call_llm(system, json.dumps(facts))
        data = json.loads(raw)
        headline = str(data.get("headline") or "").strip()
        points = [str(p).strip() for p in (data.get("points") or []) if str(p).strip()]
        conclusion = str(data.get("conclusion") or "").strip()
        if headline:
            return {
                "headline": headline,
                "points": points[:6],
                "conclusion": conclusion or base.get("conclusion", ""),
            }
    except Exception:
        pass
    return None


def _summary_to_text(summary):
    """Flatten a {headline, points, conclusion} summary into a single string."""
    parts = [summary.get("headline", "")]
    parts += [f"• {p}" for p in summary.get("points", [])]
    if summary.get("conclusion"):
        parts.append(summary["conclusion"])
    return "  ".join(p for p in parts if p)


# ── Chart selection (LLM-chosen, server-validated, heuristic fallback) ─────────

def _build_chart(table, spec):
    """Decide how to visualize the result table.

    The LLM's chart_type is honored when valid and compatible; otherwise a
    heuristic picks a sensible chart. The server always chooses the actual axes
    /columns from the real table, so the chart can never show invented data.
    Returns a render-ready descriptor consumed by the frontend.
    """
    headers = table["columns"]
    rows = table["rows"]
    n_dims = table["value_start"]
    pref = spec.get("chart") or {}
    reason = pref.get("reason") or ""

    def _none(msg):
        return {"type": "none", "reason": reason or msg, "x_index": None,
                "y_indices": [], "series_index": None, "orientation": "v",
                "title": spec.get("title", "")}

    numeric_cols = [
        i for i in range(n_dims, len(headers))
        if any(isinstance(r[i], (int, float)) for r in rows)
    ]
    if not rows or not numeric_cols or len(rows) == 1:
        return _none("Single value — shown as a statistic rather than a chart.")

    # A wide raw listing of many different metrics can't be shown well in one
    # chart (each metric has its own scale/unit) — the table is the right view.
    is_listing = "first" in spec.get("aggregations", [])
    if is_listing and len(numeric_cols) > 1:
        return _none(
            "This result lists several metrics — the table shows them all. "
            "Ask about a single metric (e.g. \"cpu power over iterations\") to chart it."
        )

    # Never mix different units/scales on one chart: keep only the columns that
    # share the most common unit (e.g. all Watts, not Watts + hours together).
    if len(numeric_cols) > 1:
        unit_counts = {}
        for i in numeric_cols:
            u = _unit_of(headers[i])
            unit_counts[u] = unit_counts.get(u, 0) + 1
        common_unit = max(unit_counts, key=unit_counts.get)
        same_unit = [i for i in numeric_cols if _unit_of(headers[i]) == common_unit]
        if same_unit:
            numeric_cols = same_unit

    dims = list(range(n_dims))
    # A Date/Iteration dimension makes a trend (line) meaningful. When both are
    # present, use whichever actually varies more (e.g. many iterations, one date).
    seq_candidates = [i for i in dims if headers[i] in ("Date", "Iteration")]
    seq_index = None
    if seq_candidates:
        seq_index = max(seq_candidates, key=lambda i: len({str(r[i]) for r in rows}))
    # Category axis = first non-sequence dimension (e.g. Device).
    cat_index = next((i for i in dims if i != seq_index), dims[0] if dims else None)

    # Heuristic default.
    if seq_index is not None and len(rows) >= 3:
        heuristic = "line"
    elif len(numeric_cols) >= 2:
        heuristic = "grouped_bar"
    else:
        heuristic = "bar"

    ctype = pref.get("chart_type") if pref.get("chart_type") in _CHART_TYPES else None
    if ctype in (None, "none"):
        ctype = heuristic

    # Compatibility guards — fall back when the chosen type can't be drawn.
    if ctype == "line" and seq_index is None:
        ctype = "grouped_bar" if len(numeric_cols) >= 2 else "bar"
    if ctype == "box" and (cat_index is None or len(numeric_cols) != 1):
        ctype = "bar"

    if ctype == "line":
        x_index, y_indices = seq_index, numeric_cols
    elif ctype == "box":
        x_index, y_indices = cat_index, numeric_cols[:1]
    else:  # bar / grouped_bar
        x_index = cat_index if cat_index is not None else 0
        y_indices = numeric_cols

    # A category dimension (e.g. Device) used to color/separate series. For line
    # charts this draws one colored line per device; bar/box are colored by their
    # x category directly in the frontend.
    series_index = None
    if ctype == "line" and cat_index is not None and cat_index != seq_index:
        if len({str(r[cat_index]) for r in rows}) > 1:
            series_index = cat_index
            y_indices = numeric_cols[:1]

    n_cat = len({str(r[x_index]) for r in rows}) if x_index is not None else len(rows)
    orientation = "h" if (ctype in ("bar", "grouped_bar") and n_cat > 6) else "v"

    if not reason:
        reason = {
            "line": "Values move over a sequence, so a line chart shows the trend.",
            "grouped_bar": "Several statistics per group — shown as grouped bars.",
            "bar": "Comparing a value across groups — shown as a bar chart.",
            "box": "Distribution across iterations — shown as a box plot.",
        }.get(ctype, "")

    return {
        "type": ctype,
        "reason": reason,
        "x_index": x_index,
        "y_indices": y_indices,
        "series_index": series_index,
        "orientation": orientation,
        "title": spec.get("title", ""),
    }


# ── Public entry point ────────────────────────────────────────────────────────

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


def _build_refine_prompt(ctx: dict, prev_spec: dict) -> str:
    """Prompt for a follow-up that adjusts an existing analysis in place.

    Reuses the full base prompt (data schema + spec format) and adds the previous
    query plan so the model edits it rather than starting over.
    """
    plan = json.dumps(_spec_for_context(prev_spec), indent=2, default=str)
    return (
        _build_system_prompt(ctx)
        + "\n\nREFINEMENT MODE\n"
        "The user already has the analysis described by the query plan below. Treat the "
        "user's message as an ADJUSTMENT to this plan, not a brand-new question: start "
        "from it and change only what the user asks, keeping everything else the same. "
        "Return the COMPLETE updated spec in the same JSON schema.\n\n"
        "CURRENT QUERY PLAN:\n" + plan
    )


def _extract_spec(raw: str) -> dict:
    """Parse the model output into a spec dict, tolerating extra prose."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise RuntimeError("The model did not return a valid analysis spec.")
        return json.loads(match.group(0))


def analyze(question: str, context: dict | None = None,
            clarification: str | None = None) -> dict:
    """Run a natural-language analysis and return a structured result.

    Args:
        question: The user's natural-language request.
        context: Optional follow-up context. When it contains a previous
            ``spec`` (the validated query plan of an existing view), the question
            is treated as a refinement of that plan instead of a new analysis.
        clarification: Optional answer to a previous clarifying question. When
            present, the model is told to answer (not ask again), so the
            clarify loop can run at most once.

    Returns:
        Either an analysis result (title, summary, columns, rows, chart, spec,
        ...) or, when the request is ambiguous, ``{"clarify": {"question",
        "options"}}`` asking the user to disambiguate.

    Raises:
        ValueError: If the question is empty.
        RuntimeError: On auth/network failure, empty data source, or an
            unparseable model response.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("Please enter a question.")

    ctx = _data_context()
    if not ctx["rows"]:
        raise RuntimeError("No data is available in the current data source.")

    prev_spec = context.get("spec") if isinstance(context, dict) else None
    if isinstance(prev_spec, dict) and prev_spec:
        system_prompt = _build_refine_prompt(ctx, prev_spec)
    else:
        system_prompt = _build_system_prompt(ctx)

    clarification = (clarification or "").strip()
    if clarification:
        user_message = (
            f"Original request: {question}\n"
            f"User clarification: {clarification}\n"
            "Now produce MODE A (answer). Do not ask again."
        )
    else:
        user_message = question

    parsed = _extract_spec(_call_llm(system_prompt, user_message))

    # The model may ask to clarify instead of answering. This is honored only on
    # the first pass (never once the user has already clarified), so the loop can
    # run at most once and can never get stuck.
    if str(parsed.get("action", "")).lower() == "clarify" and not clarification:
        clarify_question = str(parsed.get("question") or "").strip()
        if clarify_question:
            options = [str(o).strip() for o in (parsed.get("options") or []) if str(o).strip()]
            return {"clarify": {"question": clarify_question, "options": options[:4]}}

    spec = _validate_spec(parsed)
    filtered = _filter_rows(ctx["rows"], spec["filters"])

    if not filtered:
        return {
            "title": spec["title"],
            "explanation": spec["explanation"],
            "summary": {
                "headline": "No rows matched the requested filters.",
                "points": ["Try rephrasing or broadening the question."],
                "conclusion": "",
            },
            "insight": "No rows matched the requested filters. Try rephrasing or broadening the question.",
            "columns": [],
            "rows": [],
            "chart": {"type": "none", "reason": "No rows to chart.", "x_index": None,
                      "y_indices": [], "series_index": None, "orientation": "v", "title": spec["title"]},
            "spec": spec,
            "kql": spec["kql"],
            "matched_rows": 0,
        }

    table = _build_table(filtered, spec)

    # Domain-aware template summary first (always available, never hallucinated),
    # then optionally polish the wording with the model (Option C / hybrid).
    summary = _build_summary(table, spec)
    if config.AI_SUMMARY_POLISH:
        polished = _polish_summary(question, summary, table)
        if polished:
            summary = polished

    chart = _build_chart(table, spec)

    return {
        "title": spec["title"],
        "explanation": spec["explanation"],
        "summary": summary,
        "insight": _summary_to_text(summary),
        "columns": table["columns"],
        "rows": table["rows"],
        "chart": chart,
        "spec": spec,
        "kql": spec["kql"],
        "matched_rows": len(filtered),
    }

