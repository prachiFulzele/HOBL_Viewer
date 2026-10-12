"""Canonical device names shared by all dashboard data sources."""


_RAW_RAILS_ONLY_DEVICES = {"gpt-len-idl-01"}


def uses_raw_rails_only(device_name) -> bool:
    return str(device_name or "").strip().casefold() in _RAW_RAILS_ONLY_DEVICES


def canonical_device_name(device_name, ihv="") -> str:
    """Return the dashboard name used to group equivalent physical devices."""
    name = str(device_name or "").strip()
    normalized_name = name.casefold()
    normalized_ihv = str(ihv or "").strip().casefold()

    if normalized_name in {"wssi", "wssiidc"}:
        if normalized_ihv == "int":
            return "FUN-LNL-IDL-02"
        if normalized_ihv == "qc":
            return "QC-HES-ROT-IDL-"

    aliases = {
        "fun-lnl-idl-02": "FUN-LNL-IDL-02",
        "qc-hes-rot-idl-": "QC-HES-ROT-IDL-",
        "qc-hes-rotidl-1": "QC-HES-ROT-IDL-",
        "rom-ms-idclab-2": "ROM-MS-IDCLAB-2",
        "rom-msidclab-25": "ROM-MS-IDCLAB-2",
        "ws-lab-x26203x7": "PTL-PUE-IDL",
        "ptl-pue-idl": "PTL-PUE-IDL",
        "len-wcl-fun-idl": "WCL",
        "wcl": "WCL",
        "pantherlake-7": "Pantherlake-7",
        "itl-ptl-maa-idl": "Pantherlake-7",
    }
    if normalized_name == "wssiidc" and normalized_ihv == "nvidia":
        return "Diomedes minos"
    return aliases.get(normalized_name, name)


def canonical_device_kql(device_expr="M_Device", ihv_expr="M_IHV") -> str:
    """Return a KQL expression equivalent to ``canonical_device_name``."""
    name = f"tolower(trim(@'\\s+', {device_expr}))"
    ihv = f"tolower(trim(@'\\s+', {ihv_expr}))"
    return (
        "case("
        f"{name} in ('wssi', 'wssiidc') and {ihv} == 'int', 'FUN-LNL-IDL-02', "
        f"{name} in ('wssi', 'wssiidc') and {ihv} == 'qc', 'QC-HES-ROT-IDL-', "
        f"{name} == 'fun-lnl-idl-02', 'FUN-LNL-IDL-02', "
        f"{name} in ('qc-hes-rot-idl-', 'qc-hes-rotidl-1'), 'QC-HES-ROT-IDL-', "
        f"{name} in ('rom-ms-idclab-2', 'rom-msidclab-25'), 'ROM-MS-IDCLAB-2', "
        f"{name} == 'wssiidc' and {ihv} == 'nvidia', 'Diomedes minos', "
        f"{name} in ('ws-lab-x26203x7', 'ptl-pue-idl'), 'PTL-PUE-IDL', "
        f"{name} in ('len-wcl-fun-idl', 'wcl'), 'WCL', "
        f"{name} in ('pantherlake-7', 'itl-ptl-maa-idl'), 'Pantherlake-7', "
        f"trim(@'\\s+', {device_expr}))"
    )
