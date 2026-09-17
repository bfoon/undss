import hashlib
import json
import secrets

from django.db import connection
from django.utils import timezone


def token_hash(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def issue_token():
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def health_from_payload(payload, source="agent"):
    score = 100
    findings = []

    def number(key):
        try:
            value = payload.get(key)
            return None if value in ("", None) else float(value)
        except Exception:
            return None

    disk = number("disk_free_percent")
    memory = number("memory_percent")
    battery = number("battery_health_percent")
    temp = number("temperature_c")
    toner = number("toner_black_percent")
    errors = number("port_errors")

    if disk is not None:
        if disk < 10:
            score -= 25
            findings.append(("critical", "disk_low", f"Disk free space is critically low ({disk:.0f}%)."))
        elif disk < 20:
            score -= 12
            findings.append(("warning", "disk_low", f"Disk free space is low ({disk:.0f}%)."))

    if memory is not None and memory >= 95:
        score -= 10
        findings.append(("warning", "memory_high", f"Memory usage is very high ({memory:.0f}%)."))

    if battery is not None:
        if battery < 50:
            score -= 20
            findings.append(("critical", "battery_health", f"Battery health is low ({battery:.0f}%)."))
        elif battery < 70:
            score -= 10
            findings.append(("warning", "battery_health", f"Battery health has fallen to {battery:.0f}%."))

    if temp is not None and temp >= 85:
        score -= 15
        findings.append(("warning", "temperature", f"Device temperature is high ({temp:.0f}°C)."))

    fw = str(payload.get("firewall_status") or "").lower()
    if fw in {"off", "disabled", "false"}:
        score -= 20
        findings.append(("critical", "firewall", "Host firewall is disabled."))

    av = str(payload.get("antivirus_status") or "").lower()
    if av in {"off", "disabled", "false", "not_running"}:
        score -= 20
        findings.append(("critical", "antivirus", "Antivirus protection is not running."))

    if source == "printer" and toner is not None:
        if toner <= 5:
            score -= 20
            findings.append(("critical", "toner_low", f"Black toner is critically low ({toner:.0f}%)."))
        elif toner <= 15:
            score -= 10
            findings.append(("warning", "toner_low", f"Black toner is low ({toner:.0f}%)."))

    if source == "switch" and errors is not None and errors > 1000:
        score -= 15
        findings.append(("warning", "port_errors", f"High interface error count detected ({int(errors)})."))

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


def get_health_context(asset_id):
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
                "asset_id","monitor_type","hostname","ip_address","mac_address","vendor",
                "model_name","serial_number","printer_queue_name","enabled","status",
                "last_seen","created_at","updated_at",
            ]
            device = dict(zip(keys, row))

        cur.execute(
            """
            SELECT recorded_at,health_score,status,cpu_percent,memory_percent,
                   disk_free_percent,battery_health_percent,temperature_c,
                   uptime_seconds,os_name,os_version,firewall_status,
                   antivirus_status,page_count_total,toner_black_percent,
                   ports_up,ports_down,port_errors,payload
            FROM accounts_asset_health_snapshot
            WHERE asset_id=%s ORDER BY recorded_at DESC LIMIT 1
            """,
            [asset_id],
        )
        row = cur.fetchone()
        latest = None
        if row:
            keys = [
                "recorded_at","health_score","status","cpu_percent","memory_percent",
                "disk_free_percent","battery_health_percent","temperature_c",
                "uptime_seconds","os_name","os_version","firewall_status",
                "antivirus_status","page_count_total","toner_black_percent",
                "ports_up","ports_down","port_errors","payload",
            ]
            latest = dict(zip(keys, row))

        cur.execute(
            """
            SELECT recorded_at,health_score,status,cpu_percent,memory_percent,
                   disk_free_percent,battery_health_percent,page_count_total,
                   toner_black_percent,ports_up,ports_down,port_errors
            FROM accounts_asset_health_snapshot
            WHERE asset_id=%s AND recorded_at >= NOW() - INTERVAL '7 days'
            ORDER BY recorded_at ASC LIMIT 1000
            """,
            [asset_id],
        )
        history = [
            {
                "recorded_at": r[0], "health_score": r[1], "status": r[2],
                "cpu_percent": r[3], "memory_percent": r[4],
                "disk_free_percent": r[5], "battery_health_percent": r[6],
                "page_count_total": r[7], "toner_black_percent": r[8],
                "ports_up": r[9], "ports_down": r[10], "port_errors": r[11],
            }
            for r in cur.fetchall()
        ]

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

    return {
        "device": device,
        "latest": latest,
        "health_history": history,
        "health_alerts": alerts,
    }
