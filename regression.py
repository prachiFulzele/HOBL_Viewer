"""Adjacent-build comparisons over the shared per-iteration table data."""

import json
import math
import re
from statistics import median


def _build(column):
    value = column.get("Header", {}).get("OS Build")
    return "" if value is None else str(value).strip()


def _build_key(build):
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", build)
    ), build


def build_options(columns):
    """Distinct non-empty build numbers in numeric-aware display order."""
    return sorted({_build(column) for column in columns if _build(column)}, key=_build_key)


def _entries(column):
    for source in ("PowerCalculation", "PowerMetrics"):
        for name, entry in column.get(source, {}).items():
            yield source, name, None, entry.get("unit") or "", entry.get("value")
    for entry in column.get("PerfAll", []):
        pt = entry.get("pt")
        yield (
            "PerfMetrics", entry["name"], str(pt) if pt is not None else None,
            entry.get("unit") or "", entry.get("value"),
        )


def _metric_key(source, name, pt, unit):
    return json.dumps([source, name, pt, unit], separators=(",", ":"))


def metric_options(columns):
    options = {}
    for column in columns:
        for source, name, pt, unit, _value in _entries(column):
            key = _metric_key(source, name, pt, unit)
            label = f"{source} / {name}"
            if pt is not None:
                label += f" / PT {pt}"
            if unit:
                label += f" ({unit})"
            options[key] = {"value": key, "label": label, "unit": unit}
    return sorted(options.values(), key=lambda option: option["label"].casefold())


def compare_builds(
    columns, metric, direction, exclude_under_8=False, included_builds=None
):
    """Pool finite samples per build, then compare consecutive build medians."""
    if direction not in ("lower", "higher"):
        raise ValueError("direction must be 'lower' or 'higher'.")
    if metric not in {option["value"] for option in metric_options(columns)}:
        raise ValueError("Select a metric available for this device, RAM, and scenario.")

    included = set(included_builds) if included_builds is not None else None
    groups = {}
    missing_build_iterations = 0
    invalid_samples = 0
    for column in columns:
        build = _build(column)
        if not build:
            missing_build_iterations += 1
            continue
        if included is not None and build not in included:
            continue
        group = groups.setdefault(build, {"samples": [], "iterations": 0})
        samples = []
        for source, name, pt, unit, value in _entries(column):
            if _metric_key(source, name, pt, unit) != metric:
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                samples.append(value)
            else:
                invalid_samples += 1
        group["samples"].extend(samples)
        group["iterations"] += bool(samples)

    rows = []
    excluded_builds = 0
    for build in sorted(groups, key=_build_key):
        group = groups[build]
        if exclude_under_8 and group["iterations"] < 8:
            excluded_builds += 1
            continue
        p50 = median(group["samples"]) if group["samples"] else None
        previous = rows[-1] if rows else None
        delta = percent = None
        status = "Baseline" if previous is None else "Not comparable"
        if previous is not None and p50 is not None and previous["p50"] is not None:
            delta = p50 - previous["p50"]
            if previous["p50"] != 0:
                percent = delta / abs(previous["p50"]) * 100
            worsening = delta if direction == "lower" else -delta
            status = "Regression" if worsening > 0 else "Improvement" if worsening < 0 else "Unchanged"
        if p50 is None:
            status = "No samples"
        rows.append({
            "build": build,
            "previous_build": previous["build"] if previous else None,
            "p50": p50,
            "samples": len(group["samples"]),
            "iterations": group["iterations"],
            "delta": delta,
            "percent": percent,
            "status": status,
        })
    return {
        "rows": rows,
        "missing_build_iterations": missing_build_iterations,
        "invalid_samples": invalid_samples,
        "excluded_builds": excluded_builds,
    }
