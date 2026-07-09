"""Kusto data source for the HOBL Dashboard (the final, permanent source).

Exposes the SAME query surface and record shapes as ``json_data`` so the routes
and the frontend are completely source-agnostic. Used only when the active data
source is ``"kusto"``.

Schema (confirmed against cluster ``fungateprd.centralus`` / db
``FungatesDataStore``):

* ``Hobl_RawMetrics`` — one row per metric. Columns used here: ``Name``,
  ``MetricType`` (``PowerMetrics`` / ``PowerCalculation`` / ``PerfMetrics``),
  ``Value`` (real), ``Unit``, ``RunDate`` (datetime, the test run date),
  ``TestName`` (now just the scenario, e.g. ``youtube``), ``TierDisplayName``
  (``<device>_<usable_ram>_<scenario>``) and ``TestResultId``. There is NO ``Pt``
  column: for PerfMetrics the pt + ordering are encoded in ``Name`` as
  ``<metricname>_<ptnumber>_<orderingnumber>``.
* ``Hobl_TestResultMetadata`` — one row per result. ``TestResultId`` (guid) plus a
  ``Metadata`` dynamic column holding the DUT attributes and run context
  (``DeviceName``, ``UsableRam``, ``DefaultRam``, ``hostname``, ``IHV``,
  ``DUTType``, ``OSBuild``, ``HWVersion``, ``BatteryCapacity_wh``, ``LKG`` and
  ``IterationNumber`` …). There is no dedicated ``OEM`` field, so the OEM column
  mirrors ``DUTType`` (the same value shown in the Device column).

The two tables are joined on ``TestResultId``. Device / Ram / the DUT header and
the displayed iteration number (``IterationNumber``) come from the metadata;
Scenario is the ``TestName`` column (falling back to ``TierDisplayName`` with the
``<device>_<usable_ram>_`` prefix stripped). The "last N iterations" and
iteration-range filters rank each run (one ``TestResultId``) by its ``RunDate``
timestamp, NOT by the iteration number, which is inconsistent across the old and
new ingestion formats.

Authentication is lazy (no sign-in happens while the JSON backend is active).
NOTE: ``InteractiveBrowserCredential`` is fine for local/dev use; an unattended
deployment (e.g. a scheduled snapshot job) must instead use a managed identity or
service principal.
"""

import re

from azure.identity import InteractiveBrowserCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

import config

_client = None

# PerfMetrics encode pt + ordering at the end of the metric name. Real ingested
# data uses ``<metricname>_<ptnumber>_<orderingnumber>`` (e.g.
# ``Edge_Navigation_To_First_Paint__Stable__9412_1``); an optional ``pt`` literal
# before the pt number is also accepted (``..._pt9412_1``).
_PERF_NAME_RE = re.compile(r"^(?P<base>.+?)_+(?:pt)?(?P<pt>\d+)_(?P<order>\d+)$")


def _parse_perf_name(name: str):
    """Split a PerfMetrics name into (base, pt:int, order:int).

    Real ingested names look like ``Edge_..._Stable__9412_1`` (pt + ordering at
    the end, no literal ``pt``; the older ``..._pt9412_1`` form is also accepted).
    Returns ``None`` when the name doesn't carry a pt/order suffix.
    """
    m = _PERF_NAME_RE.match(name or "")
    if not m:
        return None
    base = m.group("base").rstrip("_")
    return base, int(m.group("pt")), int(m.group("order"))

# Display/sort order for metric types (mirrors json_data).
_METRIC_TYPE_ORDER = {"PowerMetrics": 0, "PowerCalculation": 1, "PerfMetrics": 2}

# DUT header fields: display label -> metadata key (parity with json_data). LKG is
# absent from current metadata and resolves to empty; OEM has no dedicated field, so
# it mirrors DUTType (the same value as the Device column).
_HEADER_LABEL_TO_METAKEY = [
    ("IHV", "M_IHV"),
    ("OEM", "M_DUTType"),
    ("Device", "M_DUTType"),
    ("Device name", "M_Device"),
    ("LKG", "M_LKG"),
    ("OS Build", "M_OSBuild"),
    ("HW Version (EV/DV)", "M_HWVersion"),
    ("Battery Capacity", "M_Battery"),
    ("Default RAM", "M_DefaultRam"),
    # "Usable RAM" and "Host Name" are filled in separately below.
]

# Power Metrics section: display label -> power_calculation name (parity with json).
_POWER_FIELDS = [
    ("SoC Power (W)", "soc_power"),
    ("Memory Power (W)", "memory_power"),
    ("Display Power (W)", "display_power"),
    ("System Power (W)", "system_power"),
    ("Battery Life (hrs)", "battery_life"),
]


# ── Client / query plumbing ──────────────────────────────────────────────────

def _get_client() -> KustoClient:
    global _client
    if _client is None:
        credential = InteractiveBrowserCredential()
        kcs = KustoConnectionStringBuilder.with_azure_token_credential(
            config.KUSTO_CLUSTER, credential
        )
        _client = KustoClient(kcs)
    return _client


def _run_query(kql: str) -> list[dict]:
    response = _get_client().execute(config.KUSTO_DATABASE, kql)
    primary = response.primary_results[0]
    columns = [col.column_name for col in primary.columns]
    rows = []
    for row in primary:
        record = {}
        for col, val in zip(columns, row):
            if val is not None and not isinstance(val, (str, int, float, bool)):
                val = str(val)
            record[col] = val
        rows.append(record)
    return rows


def _sanitize(value: str) -> str:
    """Basic KQL injection prevention – strip single quotes and control chars."""
    return re.sub(r"['\\\x00-\x1f]", "", value)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _safe_date(value) -> str | None:
    """Return ``value`` only if it is a strict ``yyyy-MM-dd`` date, else ``None``.

    The result is interpolated into a KQL ``datetime(...)`` literal, so it must be
    validated (not merely sanitized) to prevent injection.
    """
    value = (value or "").strip()
    return value if _DATE_RE.match(value) else None


def _in_clause(column: str, value) -> str | None:
    """KQL filter for a single value or a list. None when no filtering applies."""
    if value is None or value == "":
        return None
    values = [value] if isinstance(value, str) else [str(v) for v in value if str(v) != ""]
    if not values:
        return None
    if len(values) == 1:
        return f"| where {column} == '{_sanitize(values[0])}'"
    joined = ", ".join(f"'{_sanitize(v)}'" for v in values)
    return f"| where {column} in ({joined})"


# ── Shared join expression ───────────────────────────────────────────────────

def _meta_let() -> str:
    """A ``let`` defining ``_meta``: metadata flattened from the dynamic column.

    ``parse_json(tostring(Metadata))`` handles both a true dynamic object and a
    dynamic that wraps a JSON string.
    """
    return (
        "let _meta = Hobl_TestResultMetadata\n"
        "| extend _md = parse_json(tostring(Metadata))\n"
        "| extend TRID = tostring(TestResultId),\n"
        "    M_Device = tostring(_md.DeviceName),\n"
        "    M_Ram = tostring(_md.UsableRam),\n"
        "    M_Host = tostring(_md.hostname),\n"
        "    M_DUTType = tostring(_md.DUTType),\n"
        "    M_IHV = tostring(_md.IHV),\n"
        "    M_LKG = tostring(_md.LKG),\n"
        "    M_OSBuild = tostring(_md.OSBuild),\n"
        "    M_HWVersion = tostring(_md.HWVersion),\n"
        "    M_Battery = tostring(_md.BatteryCapacity_wh),\n"
        "    M_DefaultRam = tostring(_md.DefaultRam),\n"
        "    M_IterNum = tostring(_md.IterationNumber),\n"
        "    M_Tier = tostring(_md.TierDefinitionKey)\n"
        "| project TRID, M_Device, M_Ram, M_Host, M_DUTType, M_IHV, M_LKG,\n"
        "    M_OSBuild, M_HWVersion, M_Battery, M_DefaultRam, M_IterNum, M_Tier;\n"
    )


def _joined(filter_clauses: list[str]) -> str:
    """Base query: metrics joined to metadata, with Device/Ram/Scenario/Date
    derived and the given filter clauses applied."""
    base = (
        _meta_let()
        + f"{config.KUSTO_TABLE}\n"
        "| extend TRID = tostring(TestResultId)\n"
        "| join kind=inner (_meta) on TRID\n"
        "| extend Device = M_Device, Ram = M_Ram, Host = M_Host\n"
        "| extend Tier = iif(isnotempty(M_Tier), M_Tier, TierDisplayName)\n"
        "| extend _prefix = strcat(Device, '_', Ram, '_')\n"
        "| extend _scnFromTier = iif(isnotempty(Device) and isnotempty(Ram) and "
        "(Tier startswith_cs _prefix), substring(Tier, strlen(_prefix)), Tier)\n"
        "| extend _scnRaw = iif(isnotempty(TestName), TestName, _scnFromTier)\n"
        "| extend SuffixIter = extract(@'_([0-9]+)$', 1, _scnRaw)\n"
        "| extend Scenario = replace_regex(_scnRaw, @'_[0-9]+$', '')\n"
        "| extend _dt = coalesce(RunDate, BuildDate)\n"
        "| extend Date = format_datetime(_dt, 'yyyy-MM-dd')\n"
        "| extend RunTs = format_datetime(_dt, 'yyyy-MM-dd HH:mm:ss.fffffff')\n"
    )
    if filter_clauses:
        base += "\n".join(filter_clauses) + "\n"
    return base


def _build_filters(device, ram, scenario, host, start_date, end_date) -> list[str]:
    clauses = []
    for col, val in (("Device", device), ("Ram", ram), ("Scenario", scenario), ("Host", host)):
        clause = _in_clause(col, val)
        if clause:
            clauses.append(clause)
    # NOTE: KQL cannot order-compare two strings (SEM0064), so date filtering is
    # done on the real datetime (_dt), not the formatted Date string. The end date
    # is made inclusive of the whole day via "< end + 1d".
    start = _safe_date(start_date)
    if start:
        clauses.append(f"| where isnotnull(_dt) and _dt >= datetime({start})")
    end = _safe_date(end_date)
    if end:
        clauses.append(f"| where isnotnull(_dt) and _dt < datetime({end}) + 1d")
    return clauses


# ── Small helpers (parity with json_data) ────────────────────────────────────

def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", "_", (name or "").strip())


def _to_number(raw):
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return raw
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def _resolve_iteration(raw_iter, meta_iter_num, suffix_iter=None):
    """Effective iteration number for grouping/sorting/display.

    The per-result iteration lives in the metadata as ``IterationNumber``. Some
    (older) rows instead carry it as a trailing ``_<n>`` on ``TestName`` (e.g.
    ``youtube_097``); we strip that into ``Scenario`` and keep the number as a
    fallback. Order of preference: metadata ``IterationNumber`` -> name suffix ->
    the ``Hobl_RawMetrics.Iteration`` column.
    """
    for candidate in (meta_iter_num, suffix_iter, raw_iter):
        n = _to_number(candidate)
        if n is not None:
            return int(n)
    return raw_iter


def _metric_type_rank(metric_type: str) -> int:
    return _METRIC_TYPE_ORDER.get(metric_type, 99)


def _ram_sort_key(ram: str):
    try:
        return (0, float(ram))
    except (TypeError, ValueError):
        return (1, ram)


def _is(metric_type, expected) -> bool:
    return (metric_type or "").strip().lower() == expected.lower()


# ── last-N / iteration-range (rank runs by RunDate, per user request) ─────────
#
# Each distinct TestResultId is one run (one iteration / one table column). The
# iteration NUMBER is unreliable here (the old and new ingestion formats number
# runs differently), so recency is the run's RunDate timestamp, never the
# iteration number.

def _ts_key(ts) -> int:
    """Sortable integer from a ``yyyy-MM-dd HH:mm:ss.fffffff`` RunTs string."""
    digits = re.sub(r"\D", "", ts or "")
    return int(digits) if digits else 0


def _rank_units(rows: list[dict]) -> dict:
    """Map ``(Device, Ram, Scenario)`` -> the distinct ``TestResultId``s in that
    group ordered oldest -> newest by their RunDate timestamp."""
    from collections import defaultdict

    ts_by_trid: dict = defaultdict(dict)
    for r in rows:
        key = (r["Device"], r["Ram"], r["Scenario"])
        ts_by_trid[key][r["_TRID"]] = r.get("_RunTs") or ""
    ranked = {}
    for key, mapping in ts_by_trid.items():
        ranked[key] = [
            trid for trid, _ts in sorted(mapping.items(), key=lambda kv: (kv[1], kv[0]))
        ]
    return ranked


def _apply_last_n(rows: list[dict], last_n) -> list[dict]:
    """Keep only the most recent ``last_n`` runs per Device+Ram+Scenario, ranked
    by RunDate (latest runs win), not by the inconsistent iteration number."""
    if not last_n or last_n <= 0:
        return rows
    keep = {k: set(trids[-last_n:]) for k, trids in _rank_units(rows).items()}
    return [
        r for r in rows
        if r["_TRID"] in keep[(r["Device"], r["Ram"], r["Scenario"])]
    ]


def _apply_iter_range(rows: list[dict], start_iter, end_iter) -> list[dict]:
    """Keep runs whose chronological rank (by RunDate, earliest = 1) is in the
    inclusive ``[start_iter, end_iter]`` window, per Device+Ram+Scenario."""
    if (start_iter is None or start_iter <= 1) and end_iter is None:
        return rows
    keep = {}
    for key, trids in _rank_units(rows).items():
        lo = start_iter if start_iter and start_iter > 1 else 1
        hi = end_iter if end_iter is not None else len(trids)
        keep[key] = set(trids[lo - 1:hi])
    return [
        r for r in rows
        if r["_TRID"] in keep[(r["Device"], r["Ram"], r["Scenario"])]
    ]


# ── Public query surface ─────────────────────────────────────────────────────

def get_filter_options() -> dict:
    """Distinct values for each independent filter, across all joined data."""
    kql = _joined([]) + "| summarize by Device, Ram, Scenario, Host, Date"
    rows = _run_query(kql)
    rams = sorted({r["Ram"] for r in rows if r.get("Ram")}, key=_ram_sort_key)
    return {
        "devices": sorted({r["Device"] for r in rows if r.get("Device")}),
        "rams": rams,
        "scenarios": sorted({r["Scenario"] for r in rows if r.get("Scenario")}),
        "hostnames": sorted({r["Host"] for r in rows if r.get("Host")}),
        "dates": sorted({r["Date"] for r in rows if r.get("Date")}, reverse=True),
    }


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
    """Metrics matching the filters, one row per metric value. PerfMetrics names
    are split into ``MetricName`` (base) + ``Pt``; power names pass through."""
    clauses = _build_filters(device, ram, scenario, host, start_date, end_date)
    kql = (
        _joined(clauses)
        + "| project Device, Ram, Scenario, Date, RunTs, TRID, Iteration, M_IterNum,\n"
        "    SuffixIter, Name, Value, Unit, MetricType"
    )
    projected = []
    for r in _run_query(kql):
        value = _to_number(r.get("Value"))
        if value is None:
            continue
        mtype = r.get("MetricType")
        name = _normalize_name(r.get("Name"))
        metric_name, pt = name, "N/A"
        if _is(mtype, "PerfMetrics"):
            parsed = _parse_perf_name(name)
            if parsed:
                metric_name, pt = parsed[0], str(parsed[1])
        projected.append(
            {
                "Device": r.get("Device") or "",
                "Ram": r.get("Ram") or "",
                "Scenario": r.get("Scenario") or "",
                "Date": r.get("Date") or "",
                "_RunTs": r.get("RunTs"),
                "_TRID": r.get("TRID"),
                "Iteration": _resolve_iteration(
                    r.get("Iteration"), r.get("M_IterNum"), r.get("SuffixIter")
                ),
                "MetricName": metric_name,
                "Value": value,
                "Unit": r.get("Unit") or "",
                "MetricType": mtype,
                "Pt": pt,
            }
        )
    projected.sort(
        key=lambda r: (
            r["Device"],
            _ram_sort_key(r["Ram"]),
            r["Scenario"],
            -_ts_key(r["_RunTs"]),
            _metric_type_rank(r["MetricType"]),
            r["MetricName"],
        )
    )
    result = _apply_last_n(_apply_iter_range(projected, start_iter, end_iter), last_n)
    for row in result:
        row.pop("_RunTs", None)
        row.pop("_TRID", None)
    return result


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
    """Per-iteration columns for the transposed table view (parity with
    ``json_data.get_table_data``). One column per ``TestResultId``."""
    clauses = _build_filters(device, ram, scenario, host, start_date, end_date)
    kql = (
        _joined(clauses)
        + "| project Device, Ram, Scenario, Date, RunTs, Iteration, M_IterNum, SuffixIter,\n"
        "    Name, Value, Unit, MetricType, TRID, M_Host, M_DUTType, M_IHV,\n"
        "    M_LKG, M_OSBuild, M_HWVersion, M_Battery, M_DefaultRam, M_Device"
    )

    # Group metric rows by result (TestResultId); each group = one column.
    groups: dict = {}
    for r in _run_query(kql):
        groups.setdefault(r.get("TRID"), []).append(r)

    columns = []
    for rows in groups.values():
        meta = rows[0]
        ram_val = meta.get("Ram") or ""

        header = {label: (meta.get(key) or "") for label, key in _HEADER_LABEL_TO_METAKEY}
        header["Usable RAM"] = ram_val
        header["Host Name"] = meta.get("M_Host") or ""

        calc_map, pm_map = {}, {}
        perf_by_name, perf_all = {}, []
        # pt -> list of (order, value, base, unit) so we can keep original order.
        perf_pt_samples: dict = {}

        for r in rows:
            value = _to_number(r.get("Value"))
            if value is None:
                continue
            mtype = r.get("MetricType")
            name = _normalize_name(r.get("Name"))
            unit = r.get("Unit") or ""
            if _is(mtype, "PowerCalculation"):
                calc_map[name] = {"value": value, "unit": unit}
            elif _is(mtype, "PowerMetrics"):
                pm_map[name] = {"value": value, "unit": unit}
            elif _is(mtype, "PerfMetrics"):
                parsed = _parse_perf_name(name)
                if parsed:
                    base, pt, order = parsed
                    perf_pt_samples.setdefault(pt, []).append((order, value, base, unit))
                else:
                    perf_by_name[name] = value
                    perf_all.append({"pt": None, "name": name, "value": value, "unit": unit})

        perf_by_pt: dict = {}
        for pt, samples in perf_pt_samples.items():
            samples.sort(key=lambda x: x[0])
            perf_by_pt[pt] = [s[1] for s in samples]
            for _order, value, base, unit in samples:
                perf_all.append({"pt": pt, "name": base, "value": value, "unit": unit})

        power = {label: (calc_map[name]["value"] if name in calc_map else None)
                 for label, name in _POWER_FIELDS}

        columns.append(
            {
                "Device": meta.get("M_Device") or meta.get("Device") or "",
                "Ram": ram_val,
                "Scenario": meta.get("Scenario") or "",
                "Date": meta.get("Date") or "",
                "_RunTs": meta.get("RunTs"),
                "_TRID": meta.get("TRID"),
                "Iteration": _resolve_iteration(
                    meta.get("Iteration"), meta.get("M_IterNum"), meta.get("SuffixIter")
                ),
                "Header": header,
                "Power": power,
                "PowerCalculation": calc_map,
                "PowerMetrics": pm_map,
                "PerfByPt": perf_by_pt,
                "PerfByName": perf_by_name,
                "PerfAll": perf_all,
            }
        )

    columns = _apply_iter_range(columns, start_iter, end_iter)
    columns = _apply_last_n(columns, last_n)
    columns.sort(
        key=lambda c: (
            c["Device"],
            _ram_sort_key(c["Ram"]),
            c["Scenario"],
            _ts_key(c["_RunTs"]),
        )
    )
    for col in columns:
        col.pop("_RunTs", None)
        col.pop("_TRID", None)
    return columns
