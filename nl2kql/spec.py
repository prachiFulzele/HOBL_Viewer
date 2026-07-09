# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for details.

"""Analysis spec: the vocabulary, validation, and displayed-query rendering.

The LLM never executes anything — it only emits a small JSON *spec* describing
the filters / grouping / aggregation it wants. This module owns:

    * the vocabulary a spec may use (``_AGGREGATIONS``, ``_DIMENSIONS``,
      ``_VALUE_OPS``, ``_CHART_TYPES``);
    * ``_extract_spec`` / ``_validate_spec`` — turn raw model output into a
      strict, trusted spec dict; and
    * ``_spec_to_kql`` — render a read-only, KQL-style query that mirrors the
      analysis for the "Show generated query" panel. It is a transparency
      artifact only; the statistics themselves are computed in Python (see
      execute.py) for cross-source consistency.
"""

import json
import re

import config


# Aggregations the executor supports. Percentiles use linear interpolation,
# matching the dashboard's existing percentile math.
_AGGREGATIONS = {"mean", "median", "min", "max", "count", "first", "p50", "p70", "p90"}

# Fields a row can be grouped / pivoted on.
_DIMENSIONS = {"Device", "Ram", "Scenario", "Date", "Iteration", "MetricName", "MetricType"}

# Comparison operators allowed in value_filters.
_VALUE_OPS = {"<", "<=", ">", ">=", "==", "!="}

# Chart types the AI may choose; the server validates and falls back as needed.
_CHART_TYPES = {"bar", "grouped_bar", "line", "box", "none"}


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


def _source_name(backend) -> str:
    """'kusto' when the backend is the Kusto module, else 'json'."""
    return "kusto" if getattr(backend, "__name__", "") == "kusto_data" else "json"


def _spec_to_kql(spec: dict, source: str = "logical") -> str:
    """Render a read-only, KQL-style query that mirrors what the analysis does.

    For Kusto it simulates the real two-table shape the dashboard uses
    (``Hobl_RawMetrics`` joined to ``Hobl_TestResultMetadata`` on
    ``TestResultId``, with Device/Ram/Scenario/Date derived); for other sources
    it shows a single logical-table form. This is a transparency artifact shown
    in the UI — not the exact statement executed (statistics are computed in
    Python for cross-source consistency).
    """
    f = spec["filters"]

    def _inlist(field, values):
        items = ", ".join(f'"{v}"' for v in values)
        return f"| where {field} in ({items})"

    where = []
    if f["devices"]:
        where.append(_inlist("Device", f["devices"]))
    if f["rams"]:
        where.append(_inlist("Ram", f["rams"]))
    if f["scenarios"]:
        where.append(_inlist("Scenario", f["scenarios"]))
    if f["metric_types"]:
        where.append(_inlist("MetricType", f["metric_types"]))
    if f["metric_name_contains"]:
        terms = ", ".join(f'"{s}"' for s in f["metric_name_contains"])
        where.append(f"| where MetricName has_any ({terms})")
    for vf in f["value_filters"]:
        where.append(f"| where Value {vf['op']} {vf['value']:g}")

    if source == "kusto":
        lines = [
            "// DUT metadata (device, RAM, iteration) joined to each metric row",
            "let runs = Hobl_TestResultMetadata",
            "    | project TestResultId,",
            "        Device = tostring(Metadata.DeviceName),",
            "        Ram = tostring(Metadata.UsableRam),",
            "        IterationNumber = toint(Metadata.IterationNumber);",
            f"{config.KUSTO_TABLE}",
            "| join kind=inner runs on TestResultId",
            "| extend Scenario = TestName,",
            "         Date = format_datetime(RunDate, 'yyyy-MM-dd'),",
            "         MetricName = Name",
        ]
    else:
        lines = ["Hobl_RawMetrics"]
    lines += where

    if f["last_n"]:
        suffix = " (runs ranked by RunDate)" if source == "kusto" else ""
        lines.append(
            f"| // keep the {f['last_n']} most recent run(s) per Device, Ram, Scenario{suffix}"
        )

    by = ", ".join(spec["group_by"])
    aggs = spec["aggregations"]
    is_listing = aggs == ["first"] or aggs == ["count"]
    if is_listing:
        lines.append("| project Device, Ram, Scenario, Date, Iteration, MetricName, Value, Unit")
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

    if source == "kusto" and not is_listing:
        lines.append("// statistics shown are computed in Python from the fetched rows")
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


def _extract_spec(raw: str) -> dict:
    """Parse the model output into a spec dict, tolerating extra prose."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise RuntimeError("The model did not return a valid analysis spec.")
        return json.loads(match.group(0))
