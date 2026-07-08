# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for details.

"""Deterministic execution: filter, aggregate, and pivot real rows.

Everything here runs in pure Python over rows fetched from the active backend,
so the figures shown to the user are computed from real data and never
hallucinated. It also holds the small text/number formatting helpers shared
with present.py (``_pretty_metric``, ``_unit_of``, ``_fmt``, ``_agg_label``,
``_direction``).
"""

import operator
import re
from collections import defaultdict

from .spec import _DIMENSIONS


# ── Formatting / labelling helpers (shared with present.py) ───────────────────

# Human-readable label for an aggregation, used in column headers.
_AGG_LABELS = {
    "mean": "Mean", "median": "Median", "min": "Min", "max": "Max",
    "count": "Count", "first": "", "p50": "P50", "p70": "P70", "p90": "P90",
}


def _agg_label(agg):
    return _AGG_LABELS.get(agg, agg.capitalize())


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


# ── Aggregation math ──────────────────────────────────────────────────────────

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


# ── Row filtering ─────────────────────────────────────────────────────────────

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
    # NOTE: last-N is intentionally NOT applied here — it is pushed down to
    # ``backend.get_metrics(last_n=...)`` so the AI inherits the dashboard's exact
    # run-date-based ranking.
    return out


# ── Table build (pivot + aggregate + sort/limit) ──────────────────────────────

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
