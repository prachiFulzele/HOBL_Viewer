"""Temporary JSON-file data source for the HOBL Dashboard.

Reads hobl_result*.json files (the exact files HOBL uploads) and exposes the
same query surface the dashboard needs. It mirrors the FUTURE Kusto schema for
``Hobl_RawMetrics`` so the frontend behaves identically whether data comes from
these files or from Kusto:

    * device attributes live in a single ``DeviceConfig`` object (dynamic column)
    * ``Pt`` and ``HostName`` are their own fields
    * tier name = ``<device_name>_<usable_ram_config_gb>_<test_name>``

Columns that are not present in the JSON (BuildLab, BuildDate, etc.) are left
empty. ``RunDate``/``BuildDate`` are treated as today, since all uploads happened
today.

This module is only used when ``config.DATA_SOURCE == "json"``; it is a
stand-in until the new Kusto columns are deployed. The dashboard features built
against it carry over unchanged once the source switches back to Kusto.
"""

import json
import re
from datetime import date
from pathlib import Path

import config


def _json_dir() -> str:
    """Active folder to read JSON files from in JSON mode.

    This is exclusively the folder of files uploaded through the dashboard
    (config.UPLOAD_DIR). When nothing has been uploaded (or after the user clears
    the uploads) the folder is empty, so the dashboard shows no data until the
    user uploads result files — it never reads from any other location.
    """
    return getattr(config, "UPLOAD_DIR", "") or ""

# All provided JSON files were uploaded today.
_TODAY = date.today().isoformat()


def _as_set(value):
    """Normalize a filter value (None / "" / str / iterable) to a set or None.

    Returns None when no filtering should be applied (empty/falsy), so callers
    can use ``not sel or r["X"] in sel``.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return {value}
    s = {str(v) for v in value if str(v) != ""}
    return s or None


def _normalize_name(name: str) -> str:
    """Collapse whitespace runs to a single underscore, matching the metric
    name normalization done by HoblMetricExtractor before ingestion."""
    return re.sub(r"\s+", "_", (name or "").strip())


def _to_number(raw):
    """Parse a metric value to int/float; return None if not numeric."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return raw
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def _load_records() -> list[dict]:
    """Load every JSON file in the active JSON folder into a flat list of metric
    rows, one row per metric, shaped like a Hobl_RawMetrics record."""
    records: list[dict] = []
    folder = Path(_json_dir())
    if not folder.is_dir():
        return records

    for path in sorted(folder.glob("*.json")):
        try:
            with open(path, encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue

        run_info = data.get("run_info") or {}
        device_config = data.get("device_config") or {}

        device_name = str(device_config.get("device_name", ""))
        usable_ram = device_config.get("usable_ram_config_gb", "")
        test_name = run_info.get("test_name") or run_info.get("scenario") or ""

        tier_name = f"{device_name}_{usable_ram}_{test_name}"
        scenario = run_info.get("scenario") or test_name or ""
        study_type = (
            run_info.get("run_type")
            or run_info.get("scenario")
            or run_info.get("test_name")
            or "Unknown"
        )
        host_name = run_info.get("HostName") or run_info.get("hostname") or ""
        iteration = run_info.get("run_number")
        device_config_json = json.dumps(device_config, separators=(",", ":"))

        # Mirror the extractor's HashSet de-duplication: identical metric rows
        # within one test result collapse to one.
        seen: set = set()

        def add(name: str, metric_type: str, raw_value, unit: str, pt: str):
            normalized = _normalize_name(name)
            value = _to_number(raw_value)
            if not normalized or value is None:
                return
            key = (normalized, metric_type, value, unit, pt)
            if key in seen:
                return
            seen.add(key)
            records.append(
                {
                    "TierName": tier_name,
                    "Device": device_name,
                    "Ram": str(usable_ram) if usable_ram != "" else "",
                    "TestName": test_name,
                    "Scenario": scenario,
                    "RunDate": _TODAY,
                    "Iteration": iteration,
                    "Name": normalized,
                    "Value": value,
                    "Unit": unit,
                    "MetricType": metric_type,
                    "StudyType": study_type,
                    "Pt": pt,
                    "HostName": host_name,
                    "DeviceConfig": device_config_json,
                }
            )

        for metric in data.get("power_metrics") or []:
            add(
                metric.get("name"),
                "PowerMetrics",
                metric.get("value"),
                metric.get("unit") or "N/A",
                "N/A",
            )

        for metric in data.get("power_calculation") or []:
            add(
                metric.get("name"),
                "PowerCalculation",
                metric.get("value"),
                metric.get("unit") or "N/A",
                "N/A",
            )

        for metric in data.get("perf_metrics") or []:
            add(
                metric.get("metric") or metric.get("name"),
                "PerfMetrics",
                metric.get("duration_ms") if metric.get("duration_ms") is not None else metric.get("value"),
                metric.get("unit") or "ms",
                str(metric.get("pt")) if metric.get("pt") is not None else "N/A",
            )

    return records


# Display/sort order for metric types.
_METRIC_TYPE_ORDER = {"PowerMetrics": 0, "PowerCalculation": 1, "PerfMetrics": 2}


def _metric_type_rank(metric_type: str) -> int:
    return _METRIC_TYPE_ORDER.get(metric_type, 99)


def _ram_sort_key(ram: str):
    try:
        return (0, float(ram))
    except (TypeError, ValueError):
        return (1, ram)


def get_filter_options() -> dict:
    """Distinct values for each independent filter, across all data."""
    records = _load_records()
    return {
        "devices": sorted({r["Device"] for r in records if r["Device"]}),
        "rams": sorted({r["Ram"] for r in records if r["Ram"]}, key=_ram_sort_key),
        "scenarios": sorted({r["Scenario"] for r in records if r["Scenario"]}),
        "hostnames": sorted({r["HostName"] for r in records if r.get("HostName")}),
        "dates": sorted({r["RunDate"] for r in records if r["RunDate"]}, reverse=True),
    }


def _apply_last_n(rows: list[dict], last_n) -> list[dict]:
    """Keep only the most recent ``last_n`` iterations per Device+Ram+Scenario.

    Operates on already-projected rows (with Device, Ram, Scenario, Date,
    Iteration). Recency is ordered by (Date, Iteration) descending.
    """
    if not last_n or last_n <= 0:
        return rows

    from collections import defaultdict

    iterations = defaultdict(set)
    for r in rows:
        iterations[(r["Device"], r["Ram"], r["Scenario"])].add((r["Date"], r["Iteration"]))

    keep = {}
    for key, values in iterations.items():
        ordered = sorted(
            values,
            key=lambda x: (x[0], x[1] if x[1] is not None else 0),
            reverse=True,
        )[:last_n]
        keep[key] = set(ordered)

    return [
        r
        for r in rows
        if (r["Date"], r["Iteration"]) in keep[(r["Device"], r["Ram"], r["Scenario"])]
    ]


def _apply_iter_range(rows: list[dict], start_iter, end_iter) -> list[dict]:
    """Keep iterations whose chronological rank falls in [start_iter, end_iter].

    Within each Device+Ram+Scenario group, the distinct (Date, Iteration) pairs
    are ranked ascending (earliest run = rank 1, latest run = rank N). Rows whose
    rank is outside the inclusive [start_iter, end_iter] window are dropped.
    ``start_iter`` defaults to 1 and ``end_iter`` defaults to N (all) when None.
    """
    if (start_iter is None or start_iter <= 1) and end_iter is None:
        return rows

    from collections import defaultdict

    iterations = defaultdict(set)
    for r in rows:
        iterations[(r["Device"], r["Ram"], r["Scenario"])].add((r["Date"], r["Iteration"]))

    keep = {}
    for key, values in iterations.items():
        ordered = sorted(values, key=lambda x: (x[0], x[1] if x[1] is not None else 0))
        lo = start_iter if start_iter and start_iter > 1 else 1
        hi = end_iter if end_iter is not None else len(ordered)
        # Ranks are 1-based; slice the inclusive [lo, hi] window.
        keep[key] = set(ordered[lo - 1:hi])

    return [
        r
        for r in rows
        if (r["Date"], r["Iteration"]) in keep[(r["Device"], r["Ram"], r["Scenario"])]
    ]


def get_metrics(
    device: str = "",
    ram: str = "",
    scenario: str = "",
    host: str = "",
    start_date: str = "",
    end_date: str = "",
    last_n=None,
    start_iter=None,
    end_iter=None,
) -> list[dict]:
    """Metrics matching the provided filters. Any empty filter means "all".

    Dates are filtered as an inclusive [start_date, end_date] range (ISO
    yyyy-MM-dd strings compare correctly). ``start_iter``/``end_iter`` keep only
    iterations whose chronological rank (earliest = 1) is in that inclusive
    window, per Device+Ram+Scenario. ``last_n`` then keeps only the most recent N
    of the remaining iterations. Returns rows enriched with their grouping context
    (device, ram, scenario, date, iteration, pt) so results stay distinguishable
    when several configs match.
    """
    dev_set = _as_set(device)
    ram_set = _as_set(ram)
    scn_set = _as_set(scenario)
    host_set = _as_set(host)
    rows = [
        r
        for r in _load_records()
        if (not dev_set or r["Device"] in dev_set)
        and (not ram_set or r["Ram"] in ram_set)
        and (not scn_set or r["Scenario"] in scn_set)
        and (not host_set or r.get("HostName") in host_set)
        and (not start_date or r["RunDate"] >= start_date)
        and (not end_date or r["RunDate"] <= end_date)
    ]
    rows.sort(
        key=lambda r: (
            r["Device"],
            _ram_sort_key(r["Ram"]),
            r["Scenario"],
            # Latest first: negate date ordinal and iteration.
            tuple(-int(p) for p in r["RunDate"].split("-")) if r["RunDate"] else (0,),
            -(r["Iteration"]) if isinstance(r["Iteration"], int) else 0,
            _metric_type_rank(r["MetricType"]),
            r["Name"],
        )
    )
    projected = [
        {
            "Device": r["Device"],
            "Ram": r["Ram"],
            "Scenario": r["Scenario"],
            "Date": r["RunDate"],
            "Iteration": r["Iteration"],
            "MetricName": r["Name"],
            "Value": r["Value"],
            "Unit": r["Unit"],
            "MetricType": r["MetricType"],
            "Pt": r["Pt"],
        }
        for r in rows
    ]
    return _apply_last_n(_apply_iter_range(projected, start_iter, end_iter), last_n)


# ── Transposed table view (Excel-style: iterations as columns) ────────────────
#
# Each "column" is one iteration (one JSON file / run). The frontend lays metrics
# out as rows and these columns left-to-right, grouped by date. We keep the raw
# perf samples (no de-dup, original order) so the UI can compute "first value" and
# per-iteration P50/P70/P90.

# DUT header fields: display label -> device_config key.
_HEADER_FIELDS = [
    ("IHV", "cpu_mfg"),
    ("OEM", "OEM"),
    ("Device", "dut_type"),
    ("Device name", "device_name"),
    ("LKG", "LKG"),
    ("OS Build", "os_build"),
    ("HW Version (EV/DV)", "HWVersion"),
    ("Battery Capacity", "battery_capacity_wh"),
    ("Default RAM", "memory_size_gb"),
    ("Usable RAM", "usable_ram_config_gb"),
]

# Power Metrics section: display label -> power_calculation name.
_POWER_FIELDS = [
    ("SoC Power (W)", "soc_power"),
    ("Memory Power (W)", "memory_power"),
    ("Display Power (W)", "display_power"),
    ("System Power (W)", "system_power"),
    ("Battery Life (hrs)", "battery_life"),
]


def _load_columns() -> list[dict]:
    """Load each JSON file as one iteration "column" for the transposed table."""
    columns: list[dict] = []
    folder = Path(_json_dir())
    if not folder.is_dir():
        return columns

    for path in sorted(folder.glob("*.json")):
        try:
            with open(path, encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue

        run_info = data.get("run_info") or {}
        device_config = data.get("device_config") or {}

        device_name = str(device_config.get("device_name", ""))
        usable_ram = device_config.get("usable_ram_config_gb", "")
        test_name = run_info.get("test_name") or run_info.get("scenario") or ""
        scenario = run_info.get("scenario") or test_name or ""

        header = {label: device_config.get(key, "") for label, key in _HEADER_FIELDS}
        # Host Name is per-run (varies across iterations), sourced from run_info.
        header["Host Name"] = run_info.get("HostName") or run_info.get("hostname") or ""

        # Power Metrics (from power_calculation), plus the full calculation/metric
        # maps for the "all metrics" view.
        calc_map, pm_map = {}, {}
        for m in data.get("power_calculation") or []:
            calc_map[_normalize_name(m.get("name"))] = {
                "value": _to_number(m.get("value")),
                "unit": m.get("unit") or "",
            }
        for m in data.get("power_metrics") or []:
            pm_map[_normalize_name(m.get("name"))] = {
                "value": _to_number(m.get("value")),
                "unit": m.get("unit") or "",
            }
        power = {}
        for label, name in _POWER_FIELDS:
            entry = calc_map.get(name)
            power[label] = entry["value"] if entry else None

        # Perf samples: keep all of them, in order. Shape A has pt+metric+duration_ms;
        # Shape B has name+value+unit (e.g. teams_fps, no pt).
        perf_by_pt: dict = {}
        perf_by_name: dict = {}
        perf_all: list = []
        for m in data.get("perf_metrics") or []:
            if "pt" in m:
                pt = m.get("pt")
                value = _to_number(m.get("duration_ms"))
                if value is None:
                    value = _to_number(m.get("value"))
                if value is None:
                    continue
                perf_by_pt.setdefault(int(pt) if pt is not None else None, []).append(value)
                perf_all.append({
                    "pt": pt,
                    "name": m.get("metric") or "",
                    "value": value,
                    "unit": m.get("unit") or "ms",
                })
            else:
                value = _to_number(m.get("value"))
                if value is None:
                    continue
                name = _normalize_name(m.get("name"))
                perf_by_name[name] = value
                perf_all.append({
                    "pt": None,
                    "name": name,
                    "value": value,
                    "unit": m.get("unit") or "",
                })

        columns.append({
            "Device": device_name,
            "Ram": str(usable_ram) if usable_ram != "" else "",
            "Scenario": scenario,
            "Date": _TODAY,
            "Iteration": run_info.get("run_number"),
            "Header": header,
            "Power": power,
            "PowerCalculation": calc_map,
            "PowerMetrics": pm_map,
            "PerfByPt": perf_by_pt,
            "PerfByName": perf_by_name,
            "PerfAll": perf_all,
        })

    return columns


def get_table_data(
    device: str = "",
    ram: str = "",
    scenario: str = "",
    host: str = "",
    start_date: str = "",
    end_date: str = "",
    last_n=None,
    start_iter=None,
    end_iter=None,
) -> list[dict]:
    """Iteration columns matching the filters, for the transposed table view.

    Applies the same Device/Ram/Scenario/date filters, iteration range, and
    last-N rules as ``get_metrics``, then orders columns by Date then Iteration
    ascending (left-to-right, oldest first) so the table reads like the source
    spreadsheet.
    """
    dev_set = _as_set(device)
    ram_set = _as_set(ram)
    scn_set = _as_set(scenario)
    host_set = _as_set(host)
    cols = [
        c
        for c in _load_columns()
        if (not dev_set or c["Device"] in dev_set)
        and (not ram_set or c["Ram"] in ram_set)
        and (not scn_set or c["Scenario"] in scn_set)
        and (not host_set or c["Header"].get("Host Name") in host_set)
        and (not start_date or c["Date"] >= start_date)
        and (not end_date or c["Date"] <= end_date)
    ]
    cols = _apply_iter_range(cols, start_iter, end_iter)
    cols = _apply_last_n(cols, last_n)
    cols.sort(
        key=lambda c: (
            c["Device"],
            _ram_sort_key(c["Ram"]),
            c["Scenario"],
            tuple(int(p) for p in c["Date"].split("-")) if c["Date"] else (0,),
            c["Iteration"] if isinstance(c["Iteration"], int) else 0,
        )
    )
    return cols
