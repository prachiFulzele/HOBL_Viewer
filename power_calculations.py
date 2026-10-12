"""Device-specific corrections for derived power metrics."""

import re


_ROP_KEYS = {"rop", "roppower"}
_DISPLAY_POWER_KEYS = {"displaypower", "totaldisplaypower"}
_DISPLAY_RAIL_KEYS = (
    {"pmemidisplaybklt", "pmemibkltin", "pmemidisplaybacklight"},
    {"pmemidisplaypanel3p3v", "pmemi3p3vpanelin", "pmemidisplaypanel"},
)
_TOTAL_POWER_KEYS = {"totalpower"}
_SOC_POWER_KEYS = {"socpower"}
_SYSTEM_POWER_KEYS = {"systempower"}
_BATTERY_LIFE_KEYS = {"batterylife"}
_MEMORY_POWER_KEYS = {"memorypower", "drampower"}
_VSYS_CORE_KEYS = {"vsyscore", "pmemivsyscore"}
_VSYS_ROP_KEYS = {"vsysrop", "pmemivsysrop"}
_MINOS_SOC_RAIL_KEYS = (
    {"pmicmt6363", "pmemipmicmt6363"},
    {"pmicmt6373", "pmemipmicmt6373"},
    {"cpup", "pmemicpup"},
    {"cpue", "pmemicpue"},
    {"gpum", "pmemigpum", "pmemigpumisc"},
    {"gpuc", "pmemigpuc", "pmemigpucompute"},
    {"core", "pmemicore", "pmemicpucore"},
    {"dla", "dlanpu", "pmemidla", "pmemidlanpu", "pmeminpu"},
)
_MINOS_GROUPED_RAILS = (
    (_MEMORY_POWER_KEYS, "memory_power", "pmemimemory"),
    ({"cpupower"}, "cpu_power", "pmemicpu"),
    ({"ainpupower", "npupower"}, "npu_power", "pmeminpu"),
    ({"gpupower"}, "gpu_power", "pmemigpu"),
    ({"storagepower", "ssdpower"}, "storage_power", "pmemistorage"),
    ({"wifibtpower", "wifipower"}, "wifi_power", "pmemiwifi"),
    ({"camerapower"}, "camera_power", "pmemicamera"),
    (
        {"keyboardpower", "keyboardtouchpad", "keyboardtouchpadpower"},
        "keyboard_power",
        "pmemikeyboard",
    ),
    ({"sampower"}, "sam_power", "pmemisam"),
    ({"touchpower"}, "touch_power", "pmemitouch"),
)
_MINOS_DISPLAY_RAIL_KEYS = (
    {"pmemidisplaypanel"},
    {"pmemidisplaybacklight"},
)
_ROP_COMPONENT_KEYS = (
    _SOC_POWER_KEYS,
    {"ainpupower", "npupower"},
    _MEMORY_POWER_KEYS,
    {"ssdpower"},
    {"wifipower"},
    {"displaypower"},
    {"touchpower"},
    {"audiopower"},
    {"camerapower"},
    {"sampower"},
    {"keyboardtouchpad", "keyboardtouchpadpower"},
)


def _metric_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return key[:-1] if key.endswith("w") else key


def _is_heracles_device(device_identifiers) -> bool:
    keys = [_metric_key(str(value or "")) for value in device_identifiers]
    return any(
        "heracles" in key or "hesrot" in key or "herrot" in key
        for key in keys
    )


def _is_minos_device(device_identifiers) -> bool:
    keys = [_metric_key(str(value or "")) for value in device_identifiers]
    return any("minos" in key or "diomin" in key for key in keys)


def is_rop_metric(name: str) -> bool:
    return _metric_key(name) in _ROP_KEYS


def is_display_power_metric(name: str) -> bool:
    return _metric_key(name) in _DISPLAY_POWER_KEYS


def is_soc_power_metric(name: str) -> bool:
    return _metric_key(name) in _SOC_POWER_KEYS


def is_system_power_metric(name: str) -> bool:
    return _metric_key(name) in _SYSTEM_POWER_KEYS


def is_battery_life_metric(name: str) -> bool:
    return _metric_key(name) in _BATTERY_LIFE_KEYS


def _upsert_calculation(
    calculations: dict,
    aliases: set[str],
    default_name: str,
    value: float,
    unit: str,
) -> str:
    target_name = next(
        (name for name in calculations if _metric_key(name) in aliases),
        default_name,
    )
    calculations[target_name] = {"value": value, "unit": unit or "W"}
    return target_name


def apply_heracles_power(
    calculations: dict, power_metrics: dict, *device_identifiers
) -> tuple[bool, bool]:
    """Apply Heracles SoC and system power formulas."""
    if not _is_heracles_device(device_identifiers):
        return False, False

    calculations_by_key = {
        _metric_key(name): entry for name, entry in calculations.items()
    }
    rails_by_key = {_metric_key(name): entry for name, entry in power_metrics.items()}
    core_entry = next(
        (rails_by_key[key] for key in _VSYS_CORE_KEYS if key in rails_by_key),
        None,
    )
    memory_entry = next(
        (
            calculations_by_key[key]
            for key in _MEMORY_POWER_KEYS
            if key in calculations_by_key
        ),
        None,
    )
    rop_entry = next(
        (rails_by_key[key] for key in _VSYS_ROP_KEYS if key in rails_by_key),
        None,
    )

    soc_corrected = False
    if core_entry is not None and memory_entry is not None:
        try:
            soc_value = (float(core_entry["value"]) * 0.94) - float(
                memory_entry["value"]
            )
        except (KeyError, TypeError, ValueError):
            pass
        else:
            target_name = next(
                (name for name in calculations if is_soc_power_metric(name)),
                "soc_power",
            )
            calculations[target_name] = {
                "value": soc_value,
                "unit": core_entry.get("unit") or "W",
            }
            soc_corrected = True

    system_corrected = False
    if core_entry is not None and rop_entry is not None:
        try:
            system_value = float(core_entry["value"]) + float(rop_entry["value"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            target_name = next(
                (name for name in calculations if is_system_power_metric(name)),
                "system_power",
            )
            calculations[target_name] = {
                "value": system_value,
                "unit": core_entry.get("unit") or rop_entry.get("unit") or "W",
            }
            system_corrected = True

    return soc_corrected, system_corrected


def apply_heracles_component_power(
    calculations: dict, power_metrics: dict, *device_identifiers
) -> set[str]:
    """Build Heracles memory and display calculations from raw rails."""
    if not _is_heracles_device(device_identifiers):
        return set()

    rails_by_key = {_metric_key(name): entry for name, entry in power_metrics.items()}
    corrected: set[str] = set()

    memory_entries = [
        entry
        for key, entry in rails_by_key.items()
        if key.startswith("pmemimemory")
    ]
    if memory_entries:
        try:
            memory_value = sum(float(entry["value"]) for entry in memory_entries)
        except (KeyError, TypeError, ValueError):
            pass
        else:
            corrected.add(
                _upsert_calculation(
                    calculations,
                    _MEMORY_POWER_KEYS,
                    "memory_power",
                    memory_value,
                    memory_entries[0].get("unit") or "W",
                )
            )

    display_entries = [
        next(
            (rails_by_key[key] for key in aliases if key in rails_by_key),
            None,
        )
        for aliases in _DISPLAY_RAIL_KEYS
    ]
    if all(entry is not None for entry in display_entries):
        try:
            display_value = sum(float(entry["value"]) for entry in display_entries)
        except (KeyError, TypeError, ValueError):
            pass
        else:
            corrected.add(
                _upsert_calculation(
                    calculations,
                    _DISPLAY_POWER_KEYS,
                    "display_power",
                    display_value,
                    display_entries[0].get("unit") or "W",
                )
            )

    return corrected


def apply_minos_power(
    calculations: dict, power_metrics: dict, *device_identifiers
) -> set[str]:
    """Build Minos power calculations from its raw power rails."""
    if not _is_minos_device(device_identifiers):
        return set()

    rails_by_key = {_metric_key(name): entry for name, entry in power_metrics.items()}
    corrected: set[str] = set()

    system_entry = next(
        (
            rails_by_key[key]
            for key in ("pmemisystem", "pmemisys")
            if key in rails_by_key
        ),
        None,
    )
    if system_entry is not None:
        try:
            system_value = float(system_entry["value"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            unit = system_entry.get("unit") or "W"
            corrected.add(
                _upsert_calculation(
                    calculations, _TOTAL_POWER_KEYS, "total_power", system_value, unit
                )
            )
            corrected.add(
                _upsert_calculation(
                    calculations,
                    _SYSTEM_POWER_KEYS,
                    "system_power",
                    system_value,
                    unit,
                )
            )

    soc_entries = [
        next(
            (rails_by_key[key] for key in aliases if key in rails_by_key),
            None,
        )
        for aliases in _MINOS_SOC_RAIL_KEYS
    ]
    if all(entry is not None for entry in soc_entries):
        try:
            soc_value = 0.85 * sum(float(entry["value"]) for entry in soc_entries)
        except (KeyError, TypeError, ValueError):
            pass
        else:
            corrected.add(
                _upsert_calculation(
                    calculations,
                    _SOC_POWER_KEYS,
                    "soc_power",
                    soc_value,
                    soc_entries[0].get("unit") or "W",
                )
            )

    display_entries = [
        next(
            (rails_by_key[key] for key in aliases if key in rails_by_key),
            None,
        )
        for aliases in _MINOS_DISPLAY_RAIL_KEYS
    ]
    if all(entry is not None for entry in display_entries):
        try:
            display_value = sum(float(entry["value"]) for entry in display_entries)
        except (KeyError, TypeError, ValueError):
            pass
        else:
            corrected.add(
                _upsert_calculation(
                    calculations,
                    _DISPLAY_POWER_KEYS,
                    "display_power",
                    display_value,
                    display_entries[0].get("unit") or "W",
                )
            )

    for aliases, default_name, rail_prefix in _MINOS_GROUPED_RAILS:
        entries = [
            entry
            for key, entry in rails_by_key.items()
            if key.startswith(rail_prefix)
        ]
        if not entries:
            continue
        try:
            value = sum(float(entry["value"]) for entry in entries)
        except (KeyError, TypeError, ValueError):
            continue
        corrected.add(
            _upsert_calculation(
                calculations,
                aliases,
                default_name,
                value,
                entries[0].get("unit") or "W",
            )
        )

    return corrected


def apply_battery_life(calculations: dict, battery_capacity) -> str | None:
    """Calculate battery life in hours from capacity in Wh and system power."""
    system_entry = next(
        (
            entry
            for name, entry in calculations.items()
            if is_system_power_metric(name)
        ),
        None,
    )
    if system_entry is None:
        return None
    try:
        capacity_wh = float(battery_capacity)
        system_power = float(system_entry["value"])
    except (KeyError, TypeError, ValueError):
        return None
    if capacity_wh <= 0 or system_power <= 0:
        return None
    return _upsert_calculation(
        calculations,
        _BATTERY_LIFE_KEYS,
        "battery_life",
        capacity_wh / system_power,
        "hrs",
    )


def apply_purwa_display_power(
    calculations: dict, power_metrics: dict, *device_identifiers
) -> bool:
    """Set Purwa display power to the backlight plus 3.3 V panel rails."""
    if not any("purwa" in str(value or "").lower() for value in device_identifiers):
        return False

    rails_by_key = {_metric_key(name): entry for name, entry in power_metrics.items()}
    rail_entries = [
        next((rails_by_key[key] for key in aliases if key in rails_by_key), None)
        for aliases in _DISPLAY_RAIL_KEYS
    ]
    if any(entry is None for entry in rail_entries):
        return False
    try:
        display_value = sum(float(entry["value"]) for entry in rail_entries)
    except (KeyError, TypeError, ValueError):
        return False

    target_name = next(
        (name for name in calculations if is_display_power_metric(name)),
        "display_power",
    )
    calculations[target_name] = {
        "value": display_value,
        "unit": rail_entries[0].get("unit") or "W",
    }
    return True


def apply_purwa_rop(calculations: dict, *device_identifiers) -> bool:
    """Replace a Purwa run's ROP with total power minus component powers.

    Returns ``True`` when the correction was applied. Incomplete runs are left
    unchanged rather than publishing a partial residual.
    """
    if not any("purwa" in str(value or "").lower() for value in device_identifiers):
        return False

    entries_by_key = {_metric_key(name): entry for name, entry in calculations.items()}
    total_entry = next(
        (entries_by_key[key] for key in _TOTAL_POWER_KEYS if key in entries_by_key),
        None,
    )
    component_entries = [
        next((entries_by_key[key] for key in aliases if key in entries_by_key), None)
        for aliases in _ROP_COMPONENT_KEYS
    ]
    if total_entry is None or any(entry is None for entry in component_entries):
        return False

    try:
        rop_value = float(total_entry["value"]) - sum(
            float(entry["value"]) for entry in component_entries
        )
    except (KeyError, TypeError, ValueError):
        return False

    target_name = next(
        (name for name in calculations if is_rop_metric(name)),
        "rop",
    )
    calculations[target_name] = {
        "value": rop_value,
        "unit": total_entry.get("unit") or "W",
    }
    return True