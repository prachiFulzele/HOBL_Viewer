# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for details.

"""Presentation: turn the computed table into a human summary and a chart.

``_build_summary`` produces a domain-aware, never-hallucinated summary straight
from the already-computed table; ``_polish_summary`` optionally rewords it via
the LLM (numbers are never changed). ``_build_chart`` picks a sensible
visualization — validating the LLM's suggestion and choosing the axes from the
real table — so a chart can never show invented data.
"""

import json

from .execute import _direction, _fmt, _pretty_metric, _unit_of
from .llm import _call_llm
from .spec import _CHART_TYPES


# ── Summary (domain-aware, human-readable) ────────────────────────────────────

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
