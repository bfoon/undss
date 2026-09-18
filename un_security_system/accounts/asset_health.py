"""
Asset Health domain logic.

Responsibilities
----------------
* Agent token issue / verification helpers.
* Scoring a raw telemetry payload into a health score + findings.
* Persisting snapshots and syncing open alerts.
* Building a rich, JSON-safe context for the UI (dashboard, detail page,
  inclusion tag) and for the live auto-refresh API.

Nothing here requires a migration: the extra device intelligence is derived
from the JSONB ``payload`` column that agents and SNMP pollers already send.
"""
import hashlib
import json
import math
import secrets
from datetime import timedelta

from django.db import connection
from django.utils import timezone

# A device that has not checked in for this long is considered stale/offline.
# Keep in sync with accounts.tasks_asset_health.mark_stale_devices.
STALE_AFTER_MINUTES = 15

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def token_hash(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def issue_token():
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def as_float(value):
    if value in ("", None):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def as_int(value):
    value = as_float(value)
    return None if value is None else int(round(value))


def _truthy_off(value):
    return str(value or "").strip().lower() in {
        "off",
        "disabled",
        "false",
        "no",
        "0",
        "not_running",
        "stopped",
        "inactive",
    }


def _truthy_on(value):
    return str(value or "").strip().lower() in {
        "on",
        "enabled",
        "true",
        "yes",
        "1",
        "running",
        "active",
        "healthy",
        "ok",
    }


def humanize_uptime(seconds):
    seconds = as_int(seconds)
    if seconds is None or seconds < 0:
        return None
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def humanize_bytes(value):
    value = as_float(value)
    if value is None:
        return None
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.1f} {units[idx]}"


def iso(value):
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def health_from_payload(payload, source="agent"):
    """Return (score, status, findings). findings = [(severity, code, msg)]."""
    score = 100
    findings = []

    def number(key):
        return as_float(payload.get(key))

    cpu = number("cpu_percent")
    disk = number("disk_free_percent")
    memory = number("memory_percent")
    battery = number("battery_health_percent")
    temp = number("temperature_c")
    toner = number("toner_black_percent")
    errors = number("port_errors")
    uptime = as_int(payload.get("uptime_seconds"))
    pending_updates = as_int(
        _first(payload, ["pending_updates", "updates_pending", "missing_patches"])
    )

    if disk is not None:
        if disk < 10:
            score -= 25
            findings.append(
                ("critical", "disk_low", f"Disk free space is critically low ({disk:.0f}%).")
            )
        elif disk < 20:
            score -= 12
            findings.append(
                ("warning", "disk_low", f"Disk free space is low ({disk:.0f}%).")
            )

    if memory is not None and memory >= 95:
        score -= 10
        findings.append(
            ("warning", "memory_high", f"Memory usage is very high ({memory:.0f}%).")
        )

    if cpu is not None and cpu >= 95:
        score -= 8
        findings.append(
            ("warning", "cpu_high", f"CPU load is saturated ({cpu:.0f}%).")
        )

    if battery is not None:
        if battery < 50:
            score -= 20
            findings.append(
                ("critical", "battery_health", f"Battery health is low ({battery:.0f}%).")
            )
        elif battery < 70:
            score -= 10
            findings.append(
                ("warning", "battery_health", f"Battery health has fallen to {battery:.0f}%.")
            )

    if temp is not None and temp >= 85:
        score -= 15
        findings.append(
            ("warning", "temperature", f"Device temperature is high ({temp:.0f}°C).")
        )

    fw = payload.get("firewall_status")
    if _truthy_off(fw):
        score -= 20
        findings.append(("critical", "firewall", "Host firewall is disabled."))

    av = payload.get("antivirus_status")
    if _truthy_off(av):
        score -= 20
        findings.append(("critical", "antivirus", "Antivirus protection is not running."))

    encryption = _first(payload, ["disk_encryption", "bitlocker", "filevault", "encryption_status"])
    if encryption is not None and _truthy_off(encryption):
        score -= 10
        findings.append(("warning", "disk_encryption", "Disk encryption is not enabled."))

    secure_boot = _first(payload, ["secure_boot", "secureboot"])
    if secure_boot is not None and _truthy_off(secure_boot):
        score -= 4
        findings.append(("info", "secure_boot", "Secure Boot is disabled."))

    if pending_updates is not None and pending_updates >= 20:
        score -= 6
        findings.append(
            ("warning", "updates_pending", f"{pending_updates} operating system updates are pending.")
        )

    if _truthy_on(_first(payload, ["reboot_required", "restart_required"])):
        score -= 3
        findings.append(("info", "reboot_required", "A restart is required to finish updates."))

    if uptime is not None and uptime > 60 * 60 * 24 * 45:
        score -= 3
        findings.append(
            ("info", "uptime_long", f"Device has not restarted in {uptime // 86400} days.")
        )

    for disk_row in _disk_rows(payload):
        smart = str(disk_row.get("smart") or "").lower()
        if smart and smart not in {"ok", "passed", "good", "healthy"}:
            score -= 25
            findings.append(
                (
                    "critical",
                    "disk_smart",
                    f"Storage device {disk_row.get('name') or ''} reports SMART status '{smart}'.".strip(),
                )
            )
            break

    if source == "printer":
        if toner is not None:
            if toner <= 5:
                score -= 20
                findings.append(
                    ("critical", "toner_low", f"Black toner is critically low ({toner:.0f}%).")
                )
            elif toner <= 15:
                score -= 10
                findings.append(
                    ("warning", "toner_low", f"Black toner is low ({toner:.0f}%).")
                )

        for supply in _supply_rows(payload):
            level = supply.get("percent")
            if level is None or supply.get("name", "").lower().startswith("black"):
                continue
            if level <= 10:
                score -= 5
                findings.append(
                    ("warning", "supply_low", f"{supply['name']} is low ({level:.0f}%).")
                )

        printer_state = str(payload.get("printer_state") or payload.get("device_status") or "").lower()
        if printer_state in {"jam", "paper_jam", "door_open", "no_paper", "out_of_paper"}:
            score -= 15
            findings.append(
                ("warning", "printer_state", f"Printer reports state '{printer_state.replace('_', ' ')}'.")
            )

    if source == "switch" and errors is not None and errors > 1000:
        score -= 15
        findings.append(
            ("warning", "port_errors", f"High interface error count detected ({int(errors)}).")
        )

    score = max(0, min(100, int(round(score))))
    status = "healthy" if score >= 80 else ("warning" if score >= 50 else "critical")
    return score, status, findings


def sync_alerts(asset_id, findings):
    now = timezone.now()
    current_codes = {code for _, code, _ in findings}

    with connection.cursor() as cur:
        for severity, code, message in findings:
            cur.execute(
                """
                SELECT id FROM accounts_asset_health_alert
                WHERE asset_id=%s AND code=%s AND is_open=TRUE
                ORDER BY id DESC LIMIT 1
                """,
                [asset_id, code],
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE accounts_asset_health_alert
                    SET severity=%s, message=%s, last_seen_at=%s
                    WHERE id=%s
                    """,
                    [severity, message[:500], now, row[0]],
                )
            else:
                cur.execute(
                    """
                    INSERT INTO accounts_asset_health_alert
                    (asset_id,severity,code,message,is_open,opened_at,last_seen_at,meta)
                    VALUES (%s,%s,%s,%s,TRUE,%s,%s,'{}'::jsonb)
                    """,
                    [asset_id, severity, code, message[:500], now, now],
                )

        if current_codes:
            cur.execute(
                """
                UPDATE accounts_asset_health_alert
                SET is_open=FALSE, resolved_at=%s
                WHERE asset_id=%s AND is_open=TRUE
                  AND NOT (code = ANY(%s))
                """,
                [now, asset_id, list(current_codes)],
            )
        else:
            cur.execute(
                """
                UPDATE accounts_asset_health_alert
                SET is_open=FALSE, resolved_at=%s
                WHERE asset_id=%s AND is_open=TRUE
                """,
                [now, asset_id],
            )


def save_snapshot(asset_id, payload, source="agent"):
    now = timezone.now()
    score, status, findings = health_from_payload(payload, source=source)

    def v(name):
        return payload.get(name)

    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts_asset_health_snapshot
            (
              asset_id,source,recorded_at,health_score,status,
              cpu_percent,memory_percent,disk_free_percent,battery_health_percent,
              temperature_c,uptime_seconds,os_name,os_version,firewall_status,
              antivirus_status,page_count_total,toner_black_percent,
              ports_up,ports_down,port_errors,payload
            )
            VALUES
            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            [
                asset_id, source, now, score, status,
                v("cpu_percent"), v("memory_percent"), v("disk_free_percent"),
                v("battery_health_percent"), v("temperature_c"), v("uptime_seconds"),
                str(v("os_name") or "")[:160], str(v("os_version") or "")[:160],
                str(v("firewall_status") or "")[:32], str(v("antivirus_status") or "")[:32],
                v("page_count_total"), v("toner_black_percent"), v("ports_up"),
                v("ports_down"), v("port_errors"), json.dumps(payload, default=str),
            ],
        )

        cur.execute(
            """
            UPDATE accounts_asset_health_device
            SET status=%s, last_seen=%s,
                hostname=COALESCE(NULLIF(%s,''), hostname),
                ip_address=COALESCE(NULLIF(%s,'')::inet, ip_address),
                mac_address=COALESCE(NULLIF(%s,''), mac_address),
                vendor=COALESCE(NULLIF(%s,''), vendor),
                model_name=COALESCE(NULLIF(%s,''), model_name),
                serial_number=COALESCE(NULLIF(%s,''), serial_number),
                updated_at=%s
            WHERE asset_id=%s
            """,
            [
                status, now,
                str(v("hostname") or "")[:255],
                str(v("ip_address") or "")[:64],
                str(v("mac_address") or "")[:64],
                str(v("vendor") or "")[:120],
                str(v("model") or v("model_name") or "")[:160],
                str(v("serial") or v("serial_number") or "")[:160],
                now, asset_id,
            ],
        )

    sync_alerts(asset_id, findings)
    return score, status, findings


# ---------------------------------------------------------------------------
# Payload introspection -> rich device profile
# ---------------------------------------------------------------------------
NESTED_CONTAINERS = (
    "system",
    "os",
    "host",
    "hardware",
    "device",
    "inventory",
    "security",
    "power",
    "battery",
    "network",
    "printer",
    "summary",
    "facts",
)

FIELD_ALIASES = {
    # System
    "os_name": ["os_name", "os", "platform", "operating_system", "name"],
    "os_version": ["os_version", "version", "release", "build"],
    "os_build": ["os_build", "build_number", "kernel_version", "kernel"],
    "architecture": ["architecture", "arch", "cpu_arch", "machine"],
    "hostname": ["hostname", "host_name", "computer_name", "sys_name"],
    "domain": ["domain", "workgroup", "ad_domain", "realm"],
    "logged_in_user": ["logged_in_user", "current_user", "username", "user", "console_user"],
    "device_timezone": ["timezone", "tz", "time_zone"],
    "last_boot": ["last_boot", "boot_time", "booted_at", "last_boot_time"],
    "agent_version": ["agent_version", "client_version", "unpass_agent_version"],
    # Hardware
    "vendor": ["vendor", "manufacturer", "make", "brand"],
    "model": ["model", "model_name", "product_name", "product"],
    "serial": ["serial", "serial_number", "service_tag", "sn"],
    "cpu_model": ["cpu_model", "processor", "cpu", "cpu_name"],
    "cpu_cores": ["cpu_cores", "cores", "physical_cores"],
    "cpu_threads": ["cpu_threads", "threads", "logical_cores"],
    "cpu_speed_ghz": ["cpu_speed_ghz", "cpu_ghz", "clock_ghz"],
    "memory_total_gb": ["memory_total_gb", "ram_gb", "total_memory_gb"],
    "memory_total_bytes": ["memory_total_bytes", "total_memory", "ram_bytes", "memory_total"],
    "gpu": ["gpu", "graphics", "video_card"],
    "bios_version": ["bios_version", "firmware_version", "bios"],
    "chassis": ["chassis", "form_factor", "device_type", "chassis_type"],
    "warranty_expiry": ["warranty_expiry", "warranty_end", "warranty"],
    # Power
    "battery_charge_percent": ["battery_charge_percent", "battery_percent", "charge_percent", "battery_level"],
    "battery_cycles": ["battery_cycles", "cycle_count", "battery_cycle_count"],
    "power_source": ["power_source", "ac_connected", "on_ac_power", "charging"],
    # Security
    "firewall_status": ["firewall_status", "firewall"],
    "antivirus_status": ["antivirus_status", "antivirus", "av_status"],
    "antivirus_product": ["antivirus_product", "av_product", "av_name"],
    "antivirus_updated": ["antivirus_updated", "av_definitions_date", "definitions_updated"],
    "disk_encryption": ["disk_encryption", "bitlocker", "filevault", "encryption_status", "encryption"],
    "secure_boot": ["secure_boot", "secureboot"],
    "tpm": ["tpm", "tpm_present", "tpm_version"],
    "screen_lock": ["screen_lock", "screensaver_lock", "auto_lock"],
    "pending_updates": ["pending_updates", "updates_pending", "missing_patches"],
    "last_patched": ["last_patched", "last_update", "last_patch_date"],
    "local_admins": ["local_admins", "admin_users", "administrators"],
    "reboot_required": ["reboot_required", "restart_required"],
    "usb_storage_policy": ["usb_storage_policy", "usb_blocked", "removable_media_policy"],
    # Network
    "ip_address": ["ip_address", "ipv4", "ip", "primary_ip"],
    "mac_address": ["mac_address", "mac", "primary_mac"],
    "gateway": ["gateway", "default_gateway"],
    "dns_servers": ["dns_servers", "dns", "nameservers"],
    "ssid": ["ssid", "wifi_ssid", "wireless_network"],
    "wifi_signal": ["wifi_signal", "signal_strength", "rssi"],
    "link_speed_mbps": ["link_speed_mbps", "link_speed", "speed_mbps"],
    "vpn_status": ["vpn_status", "vpn"],
    # Printer / switch
    "printer_state": ["printer_state", "device_status", "printer_status"],
    "page_count_total": ["page_count_total", "total_pages", "lifetime_pages"],
    "toner_black_percent": ["toner_black_percent", "black_toner"],
    "duplex": ["duplex", "duplex_supported"],
    "tray_status": ["tray_status", "trays", "input_trays"],
    "ports_up": ["ports_up"],
    "ports_down": ["ports_down"],
    "port_errors": ["port_errors"],
    "firmware": ["firmware", "firmware_version"],
}

LIST_KEYS = {
    "disks": ["disks", "volumes", "drives", "filesystems", "storage"],
    "interfaces": ["network_interfaces", "interfaces", "nics", "adapters"],
    "processes": ["top_processes", "processes", "heaviest_processes"],
    "supplies": ["supplies", "supplies_percent", "consumables"],
    "software": ["software", "installed_software", "applications"],
    "services": ["services", "critical_services"],
}


def _first(payload, names):
    """Look a value up by alias, top level first, then known nested dicts."""
    if not isinstance(payload, dict):
        return None
    for name in names:
        value = payload.get(name)
        if value not in (None, "", [], {}):
            return value
    for container in NESTED_CONTAINERS:
        sub = payload.get(container)
        if isinstance(sub, dict):
            for name in names:
                value = sub.get(name)
                if value not in (None, "", [], {}):
                    return value
    return None


def field(payload, key):
    return _first(payload, FIELD_ALIASES.get(key, [key]))


def _list_for(payload, kind):
    for name in LIST_KEYS[kind]:
        value = payload.get(name)
        if isinstance(value, list) and value:
            return value
        if isinstance(value, dict) and value:
            return [dict(v, name=v.get("name", k)) if isinstance(v, dict) else {"name": k, "value": v}
                    for k, v in value.items()]
    for container in NESTED_CONTAINERS:
        sub = payload.get(container)
        if isinstance(sub, dict):
            for name in LIST_KEYS[kind]:
                value = sub.get(name)
                if isinstance(value, list) and value:
                    return value
    return []


def _gb(value):
    """Accept GB or raw bytes and always return GB."""
    value = as_float(value)
    if value is None:
        return None
    return round(value / (1024 ** 3), 1) if value > 10 ** 6 else round(value, 1)


def _disk_rows(payload):
    rows = []
    for raw in _list_for(payload, "disks"):
        if not isinstance(raw, dict):
            continue
        total = _gb(raw.get("total_gb") or raw.get("total") or raw.get("size") or raw.get("capacity"))
        free = _gb(raw.get("free_gb") or raw.get("free") or raw.get("available"))
        used_percent = as_float(raw.get("used_percent") or raw.get("percent_used") or raw.get("usage"))
        if used_percent is None and total and free is not None and total > 0:
            used_percent = round(100.0 * (total - free) / total, 1)
        rows.append(
            {
                "name": str(raw.get("name") or raw.get("mount") or raw.get("mountpoint")
                             or raw.get("device") or raw.get("drive") or "Volume")[:80],
                "fstype": str(raw.get("fstype") or raw.get("filesystem") or raw.get("type") or "")[:24],
                "total_gb": total,
                "free_gb": free,
                "used_percent": None if used_percent is None else round(used_percent, 1),
                "smart": str(raw.get("smart") or raw.get("smart_status") or raw.get("health") or "")[:40],
            }
        )
    return rows


def _interface_rows(payload):
    rows = []
    for raw in _list_for(payload, "interfaces"):
        if not isinstance(raw, dict):
            continue
        rows.append(
            {
                "name": str(raw.get("name") or raw.get("interface") or raw.get("if_name") or "Interface")[:60],
                "mac": str(raw.get("mac") or raw.get("mac_address") or "")[:32],
                "ipv4": str(raw.get("ipv4") or raw.get("ip") or raw.get("address") or "")[:64],
                "ipv6": str(raw.get("ipv6") or "")[:64],
                "speed_mbps": as_int(raw.get("speed_mbps") or raw.get("speed") or raw.get("link_speed")),
                "state": str(raw.get("state") or raw.get("status") or raw.get("oper_status") or "")[:24],
                "type": str(raw.get("type") or raw.get("media") or "")[:24],
                "rx_bytes": as_int(raw.get("rx_bytes") or raw.get("bytes_in")),
                "tx_bytes": as_int(raw.get("tx_bytes") or raw.get("bytes_out")),
            }
        )
    return rows


def _process_rows(payload):
    rows = []
    for raw in _list_for(payload, "processes"):
        if not isinstance(raw, dict):
            continue
        rows.append(
            {
                "name": str(raw.get("name") or raw.get("process") or raw.get("command") or "process")[:80],
                "pid": as_int(raw.get("pid")),
                "cpu_percent": as_float(raw.get("cpu_percent") or raw.get("cpu")),
                "memory_percent": as_float(raw.get("memory_percent") or raw.get("memory") or raw.get("mem")),
                "user": str(raw.get("user") or raw.get("owner") or "")[:60],
            }
        )
    rows.sort(key=lambda r: (r["cpu_percent"] or 0), reverse=True)
    return rows[:10]


def _supply_rows(payload):
    raw_supplies = _list_for(payload, "supplies")
    rows = []
    for idx, raw in enumerate(raw_supplies):
        if isinstance(raw, (int, float)):
            label = ["Black", "Cyan", "Magenta", "Yellow"][idx] if idx < 4 else f"Supply {idx + 1}"
            rows.append({"name": f"{label} toner", "percent": as_float(raw)})
        elif isinstance(raw, dict):
            rows.append(
                {
                    "name": str(raw.get("name") or raw.get("label") or raw.get("colour")
                                or raw.get("color") or "Supply")[:60],
                    "percent": as_float(raw.get("percent") or raw.get("level") or raw.get("value")),
                }
            )
    return rows


def _consumed_keys():
    keys = set(NESTED_CONTAINERS)
    for names in FIELD_ALIASES.values():
        keys.update(names)
    for names in LIST_KEYS.values():
        keys.update(names)
    keys.update(
        {
            "cpu_percent", "memory_percent", "disk_free_percent", "battery_health_percent",
            "temperature_c", "uptime_seconds", "health_score", "status", "supplies_percent",
        }
    )
    return keys


CONSUMED_KEYS = _consumed_keys()


def _pretty(key):
    return key.replace("_", " ").strip().title()


def device_profile(payload, device=None):
    """Turn an arbitrary telemetry payload into labelled UI sections."""
    payload = payload if isinstance(payload, dict) else {}
    device = device or {}

    def f(key):
        return field(payload, key)

    memory_total = f("memory_total_gb")
    if memory_total is None:
        memory_total = _gb(f("memory_total_bytes"))

    system = {
        "hostname": f("hostname") or device.get("hostname") or None,
        "os_name": f("os_name"),
        "os_version": f("os_version"),
        "os_build": f("os_build"),
        "architecture": f("architecture"),
        "domain": f("domain"),
        "logged_in_user": f("logged_in_user"),
        "timezone": f("device_timezone"),
        "last_boot": f("last_boot"),
        "uptime": humanize_uptime(payload.get("uptime_seconds")),
        "agent_version": f("agent_version"),
    }

    hardware = {
        "vendor": f("vendor") or device.get("vendor") or None,
        "model": f("model") or device.get("model_name") or None,
        "serial": f("serial") or device.get("serial_number") or None,
        "cpu_model": f("cpu_model"),
        "cpu_cores": as_int(f("cpu_cores")),
        "cpu_threads": as_int(f("cpu_threads")),
        "cpu_speed_ghz": as_float(f("cpu_speed_ghz")),
        "memory_total_gb": memory_total,
        "gpu": f("gpu"),
        "bios_version": f("bios_version"),
        "chassis": f("chassis"),
        "warranty_expiry": f("warranty_expiry"),
        "battery_charge_percent": as_float(f("battery_charge_percent")),
        "battery_cycles": as_int(f("battery_cycles")),
        "power_source": f("power_source"),
    }

    security = {
        "firewall": f("firewall_status"),
        "antivirus": f("antivirus_status"),
        "antivirus_product": f("antivirus_product"),
        "antivirus_updated": f("antivirus_updated"),
        "disk_encryption": f("disk_encryption"),
        "secure_boot": f("secure_boot"),
        "tpm": f("tpm"),
        "screen_lock": f("screen_lock"),
        "pending_updates": as_int(f("pending_updates")),
        "last_patched": f("last_patched"),
        "local_admins": f("local_admins"),
        "reboot_required": f("reboot_required"),
        "usb_storage_policy": f("usb_storage_policy"),
    }

    # A compact posture list the UI can render as pass/fail pills.
    posture = []
    posture_checks = [
        ("Firewall", security["firewall"], True),
        ("Antivirus", security["antivirus"], True),
        ("Disk encryption", security["disk_encryption"], True),
        ("Secure Boot", security["secure_boot"], False),
        ("TPM", security["tpm"], False),
        ("Screen lock", security["screen_lock"], False),
    ]
    for label, value, critical in posture_checks:
        if value in (None, ""):
            continue
        ok = _truthy_on(value) or (not _truthy_off(value) and str(value).strip() != "")
        if _truthy_off(value):
            ok = False
        posture.append(
            {
                "label": label,
                "value": str(value),
                "ok": bool(ok),
                "critical": critical,
            }
        )
    if security["pending_updates"] is not None:
        posture.append(
            {
                "label": "Pending updates",
                "value": str(security["pending_updates"]),
                "ok": security["pending_updates"] < 20,
                "critical": False,
            }
        )

    network = {
        "ip_address": f("ip_address") or device.get("ip_address") or None,
        "mac_address": f("mac_address") or device.get("mac_address") or None,
        "gateway": f("gateway"),
        "dns_servers": f("dns_servers"),
        "ssid": f("ssid"),
        "wifi_signal": f("wifi_signal"),
        "link_speed_mbps": as_int(f("link_speed_mbps")),
        "vpn_status": f("vpn_status"),
        "interfaces": _interface_rows(payload),
    }

    printer = {
        "state": f("printer_state"),
        "page_count_total": as_int(f("page_count_total")),
        "toner_black_percent": as_float(f("toner_black_percent")),
        "duplex": f("duplex"),
        "tray_status": f("tray_status"),
        "firmware": f("firmware"),
        "supplies": _supply_rows(payload),
    }

    switch = {
        "ports_up": as_int(f("ports_up")),
        "ports_down": as_int(f("ports_down")),
        "port_errors": as_int(f("port_errors")),
        "firmware": f("firmware"),
    }

    extras = []
    for key, value in sorted(payload.items()):
        if key in CONSUMED_KEYS:
            continue
        if isinstance(value, (dict, list)):
            continue
        if value in (None, ""):
            continue
        extras.append({"label": _pretty(key), "value": str(value)[:200]})

    return {
        "system": system,
        "hardware": hardware,
        "security": security,
        "posture": posture,
        "network": network,
        "storage": _disk_rows(payload),
        "processes": _process_rows(payload),
        "printer": printer,
        "switch": switch,
        "extras": extras[:40],
    }


# ---------------------------------------------------------------------------
# History statistics
# ---------------------------------------------------------------------------
def _avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def downsample(rows, max_points=220):
    if len(rows) <= max_points:
        return rows
    step = len(rows) / float(max_points)
    return [rows[int(i * step)] for i in range(max_points)]


def history_stats(history, device, latest):
    now = timezone.now()
    scores = [r["health_score"] for r in history if r["health_score"] is not None]
    day_ago = now - timedelta(hours=24)
    scores_24h = [
        r["health_score"] for r in history
        if r["health_score"] is not None and r["recorded_at"] >= day_ago
    ]

    gaps = []
    for prev, current in zip(history, history[1:]):
        gap = (current["recorded_at"] - prev["recorded_at"]).total_seconds()
        if 0 < gap < 86400:
            gaps.append(gap)
    cadence = _median(gaps)

    healthy = sum(1 for r in history if r.get("status") == "healthy")
    last_seen = device.get("last_seen") if device else None
    seconds_since = (now - last_seen).total_seconds() if last_seen else None

    previous_score = scores[-2] if len(scores) >= 2 else None
    current_score = (latest or {}).get("health_score")

    return {
        "samples": len(history),
        "score_current": current_score,
        "score_previous": previous_score,
        "score_delta": (
            None if current_score is None or previous_score is None
            else current_score - previous_score
        ),
        "score_avg_24h": _avg(scores_24h),
        "score_avg_window": _avg(scores),
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "healthy_ratio": round(100.0 * healthy / len(history), 1) if history else None,
        "cadence_seconds": int(cadence) if cadence else None,
        "next_expected_in": (
            int(cadence - seconds_since)
            if cadence and seconds_since is not None
            else None
        ),
        "seconds_since_seen": int(seconds_since) if seconds_since is not None else None,
        "is_stale": bool(
            seconds_since is not None and seconds_since > STALE_AFTER_MINUTES * 60
        ),
        "uptime_human": humanize_uptime((latest or {}).get("uptime_seconds")),
        "window_start": iso(history[0]["recorded_at"]) if history else None,
        "window_end": iso(history[-1]["recorded_at"]) if history else None,
    }


def metric_cards(monitor_type, latest, previous, profile):
    """Ordered metric tiles for the detail page, per monitor type."""
    latest = latest or {}
    previous = previous or {}

    def delta(key):
        a, b = as_float(latest.get(key)), as_float(previous.get(key))
        return None if a is None or b is None else round(a - b, 1)

    def card(key, label, value, unit="%", *, invert=False, maximum=100, icon="activity", hint=None):
        return {
            "key": key,
            "label": label,
            "value": value,
            "unit": unit,
            "max": maximum,
            "delta": delta(key) if key in latest else None,
            # invert=True means "higher is better" (disk free, battery, ports up)
            "invert": invert,
            "icon": icon,
            "hint": hint,
        }

    if monitor_type == "printer":
        printer = profile.get("printer", {})
        return [
            card("page_count_total", "Lifetime pages", as_int(latest.get("page_count_total")),
                 unit="", maximum=None, icon="file-earmark-text"),
            card("toner_black_percent", "Black toner", as_float(latest.get("toner_black_percent")),
                 invert=True, icon="droplet"),
            card("uptime_seconds", "Uptime", humanize_uptime(latest.get("uptime_seconds")),
                 unit="", maximum=None, icon="clock-history"),
            card("printer_state", "Device state", printer.get("state") or "—",
                 unit="", maximum=None, icon="printer"),
        ]

    if monitor_type == "switch":
        return [
            card("ports_up", "Ports up", as_int(latest.get("ports_up")), unit="",
                 maximum=None, invert=True, icon="ethernet"),
            card("ports_down", "Ports down", as_int(latest.get("ports_down")), unit="",
                 maximum=None, icon="plug"),
            card("port_errors", "Interface errors", as_int(latest.get("port_errors")), unit="",
                 maximum=None, icon="exclamation-diamond"),
            card("uptime_seconds", "Uptime", humanize_uptime(latest.get("uptime_seconds")),
                 unit="", maximum=None, icon="clock-history"),
        ]

    return [
        card("cpu_percent", "CPU load", as_float(latest.get("cpu_percent")), icon="cpu"),
        card("memory_percent", "Memory used", as_float(latest.get("memory_percent")), icon="memory"),
        card("disk_free_percent", "Disk free", as_float(latest.get("disk_free_percent")),
             invert=True, icon="hdd"),
        card("battery_health_percent", "Battery health",
             as_float(latest.get("battery_health_percent")), invert=True, icon="battery-half"),
        card("temperature_c", "Temperature", as_float(latest.get("temperature_c")),
             unit="°C", maximum=100, icon="thermometer-half"),
        card("uptime_seconds", "Uptime", humanize_uptime(latest.get("uptime_seconds")),
             unit="", maximum=None, icon="clock-history"),
    ]


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------
SNAPSHOT_COLUMNS = [
    "recorded_at", "health_score", "status", "cpu_percent", "memory_percent",
    "disk_free_percent", "battery_health_percent", "temperature_c",
    "uptime_seconds", "os_name", "os_version", "firewall_status",
    "antivirus_status", "page_count_total", "toner_black_percent",
    "ports_up", "ports_down", "port_errors", "payload",
]

HISTORY_COLUMNS = [
    "recorded_at", "health_score", "status", "cpu_percent", "memory_percent",
    "disk_free_percent", "battery_health_percent", "temperature_c",
    "page_count_total", "toner_black_percent", "ports_up", "ports_down",
    "port_errors",
]


def get_health_context(asset_id, hours=168):
    """Full health context for one asset.

    Returns the original keys (device, latest, health_history, health_alerts)
    plus: previous, profile, stats, metrics, chart_series, alert_counts.
    """
    hours = max(1, min(int(hours or 168), 24 * 90))

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id,monitor_type,hostname,host(ip_address),mac_address,vendor,
                   model_name,serial_number,printer_queue_name,enabled,status,last_seen,
                   created_at,updated_at
            FROM accounts_asset_health_device WHERE asset_id=%s
            """,
            [asset_id],
        )
        row = cur.fetchone()
        device = None
        if row:
            keys = [
                "asset_id", "monitor_type", "hostname", "ip_address", "mac_address", "vendor",
                "model_name", "serial_number", "printer_queue_name", "enabled", "status",
                "last_seen", "created_at", "updated_at",
            ]
            device = dict(zip(keys, row))

        cur.execute(
            f"""
            SELECT {','.join(SNAPSHOT_COLUMNS)}
            FROM accounts_asset_health_snapshot
            WHERE asset_id=%s ORDER BY recorded_at DESC LIMIT 2
            """,
            [asset_id],
        )
        snapshot_rows = cur.fetchall()
        latest = dict(zip(SNAPSHOT_COLUMNS, snapshot_rows[0])) if snapshot_rows else None
        previous = dict(zip(SNAPSHOT_COLUMNS, snapshot_rows[1])) if len(snapshot_rows) > 1 else None

        cur.execute(
            f"""
            SELECT {','.join(HISTORY_COLUMNS)}
            FROM accounts_asset_health_snapshot
            WHERE asset_id=%s AND recorded_at >= NOW() - make_interval(hours => %s)
            ORDER BY recorded_at ASC LIMIT 5000
            """,
            [asset_id, hours],
        )
        history = [dict(zip(HISTORY_COLUMNS, r)) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT id,severity,code,message,opened_at,last_seen_at
            FROM accounts_asset_health_alert
            WHERE asset_id=%s AND is_open=TRUE
            ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
                     last_seen_at DESC
            """,
            [asset_id],
        )
        alerts = [
            {
                "id": r[0], "severity": r[1], "code": r[2], "message": r[3],
                "opened_at": r[4], "last_seen_at": r[5],
            }
            for r in cur.fetchall()
        ]

    payload = (latest or {}).get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    monitor_type = (device or {}).get("monitor_type", "endpoint")
    profile = device_profile(payload, device)
    stats = history_stats(history, device or {}, latest)

    alert_counts = {"critical": 0, "warning": 0, "info": 0, "total": len(alerts)}
    for alert in alerts:
        alert_counts[alert["severity"]] = alert_counts.get(alert["severity"], 0) + 1

    chart_series = [
        {
            "t": iso(r["recorded_at"]),
            "score": r["health_score"],
            "cpu": as_float(r["cpu_percent"]),
            "memory": as_float(r["memory_percent"]),
            "disk": as_float(r["disk_free_percent"]),
            "temperature": as_float(r["temperature_c"]),
            "toner": as_float(r["toner_black_percent"]),
            "pages": as_int(r["page_count_total"]),
            "errors": as_int(r["port_errors"]),
            "status": r["status"],
        }
        for r in downsample(history)
    ]

    return {
        "device": device,
        "latest": latest,
        "previous": previous,
        "health_history": history,
        "health_alerts": alerts,
        "alert_counts": alert_counts,
        "profile": profile,
        "stats": stats,
        "metrics": metric_cards(monitor_type, latest, previous, profile),
        "chart_series": chart_series,
        "hours": hours,
        "schema_missing": False,
    }


def serialize_health(asset_id, hours=168, context=None):
    """JSON-safe payload used by the live auto-refresh API."""
    context = context or get_health_context(asset_id, hours=hours)
    device = context.get("device")
    latest = context.get("latest")

    device_json = None
    if device:
        device_json = {
            key: (iso(value) if key in {"last_seen", "created_at", "updated_at"} else value)
            for key, value in device.items()
        }

    latest_json = None
    if latest:
        latest_json = {
            key: value for key, value in latest.items()
            if key not in {"recorded_at", "payload"}
        }
        latest_json["recorded_at"] = iso(latest.get("recorded_at"))
        latest_json["uptime_human"] = humanize_uptime(latest.get("uptime_seconds"))

    return {
        "ok": True,
        "asset_id": asset_id,
        "server_time": iso(timezone.now()),
        "hours": context.get("hours", hours),
        "device": device_json,
        "latest": latest_json,
        "stats": context.get("stats"),
        "metrics": context.get("metrics"),
        "profile": context.get("profile"),
        "alerts": [
            {
                "id": a["id"],
                "severity": a["severity"],
                "code": a["code"],
                "message": a["message"],
                "opened_at": iso(a["opened_at"]),
                "last_seen_at": iso(a["last_seen_at"]),
            }
            for a in context.get("health_alerts", [])
        ],
        "alert_counts": context.get("alert_counts"),
        "series": context.get("chart_series", []),
    }
