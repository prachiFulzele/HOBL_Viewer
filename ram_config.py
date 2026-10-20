"""Read-time repairs for missing usable RAM in the affected build."""

from collections import Counter

from device_names import canonical_device_name


RAM_REPAIR_BUILD = "26708.1000"
RAM_OVERRIDES = {
    "ROM-MS-IDC-25": "16",
    "QC-LAM-PURWA-01": "16",
    "QC-LAM-PURWA-03": "8",
    "ITL-PTL-MAA-07": "32",
    "LEN-WCL-FUN-01": "8",
}


def repair_json_ram(documents):
    """Use original earlier run numbers only; repairs never become evidence."""
    history = {}
    targets = []
    for document in documents:
        config = document.get("device_config") or {}
        run = document.get("run_info") or {}
        if str(config.get("os_build") or "").strip() != RAM_REPAIR_BUILD:
            continue
        device = canonical_device_name(config.get("device_name"), config.get("cpu_mfg"))
        ram = str(config.get("usable_ram_config_gb") or "").strip()
        try:
            order = int(run["run_number"])
        except (KeyError, TypeError, ValueError):
            order = None
        if ram:
            if order is not None:
                history.setdefault(device, []).append((order, ram))
        else:
            targets.append((document, device, order))
    for document, device, order in targets:
        ram = RAM_OVERRIDES.get(device, "")
        if not ram and order is not None:
            earlier = [(timestamp, value) for timestamp, value in history.get(device, [])
                       if timestamp < order]
            if earlier:
                counts = Counter(value for _, value in earlier)
                latest = {}
                for timestamp, value in earlier:
                    latest[value] = max(timestamp, latest.get(value, timestamp))
                ram = min(counts, key=lambda value: (-counts[value], -latest[value], value))
        if ram:
            document.setdefault("device_config", {})["usable_ram_config_gb"] = ram
    return documents


def ram_override_kql(device_expr, ram_expr, build_expr):
    cases = " ".join(
        f"{device_expr} == '{device}', '{ram}',"
        for device, ram in RAM_OVERRIDES.items()
    )
    return (
        f"iff({build_expr} == '{RAM_REPAIR_BUILD}' and isempty(trim(@'\\s+', {ram_expr})), "
        f"case({cases} {ram_expr}), {ram_expr})"
    )
