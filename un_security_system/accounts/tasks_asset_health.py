import json
from datetime import timedelta

from celery import current_app, shared_task
from django.db import connection
from django.utils import timezone

from .asset_health import save_snapshot
from .snmp_asset_health import poll_printer, poll_switch


def register_asset_health_schedule():
    """Register Asset Health schedules without editing settings.py."""
    schedule = dict(current_app.conf.beat_schedule or {})

    schedule.setdefault(
        "unpass-asset-health-network-poll",
        {
            "task": "accounts.asset_health.poll_network_devices",
            "schedule": 300.0,
        },
    )

    schedule.setdefault(
        "unpass-asset-health-stale-device-check",
        {
            "task": "accounts.asset_health.mark_stale_devices",
            "schedule": 300.0,
        },
    )

    schedule.setdefault(
        "unpass-asset-health-retention",
        {
            "task": "accounts.asset_health.cleanup_history",
            "schedule": 86400.0,
        },
    )

    current_app.conf.beat_schedule = schedule


@shared_task(name="accounts.asset_health.poll_network_devices")
def poll_network_devices():
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, monitor_type, host(ip_address)
            FROM accounts_asset_health_device
            WHERE enabled=TRUE
              AND monitor_type IN ('printer','switch')
              AND ip_address IS NOT NULL
            ORDER BY asset_id
            """
        )
        rows = cur.fetchall()

    results = []

    for asset_id, monitor_type, host in rows:
        try:
            payload = (
                poll_printer(host)
                if monitor_type == "printer"
                else poll_switch(host)
            )

            score, status, _ = save_snapshot(
                asset_id,
                payload,
                source=monitor_type,
            )

            if (
                monitor_type == "printer"
                and payload.get("page_count_total") is not None
            ):
                with connection.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO accounts_printer_counter
                        (asset_id,recorded_at,total_pages,supplies,raw)
                        VALUES (%s,%s,%s,%s::jsonb,%s::jsonb)
                        """,
                        [
                            asset_id,
                            timezone.now(),
                            payload.get("page_count_total"),
                            json.dumps(
                                payload.get("supplies_percent", [])
                            ),
                            json.dumps(payload),
                        ],
                    )

            results.append(
                {
                    "asset_id": asset_id,
                    "status": status,
                    "score": score,
                }
            )

        except Exception as exc:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE accounts_asset_health_device
                    SET status='offline', updated_at=%s
                    WHERE asset_id=%s
                    """,
                    [timezone.now(), asset_id],
                )

            results.append(
                {
                    "asset_id": asset_id,
                    "status": "error",
                    "error": str(exc)[:300],
                }
            )

    return results


@shared_task(name="accounts.asset_health.mark_stale_devices")
def mark_stale_devices():
    cutoff = timezone.now() - timedelta(minutes=15)

    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts_asset_health_device
            SET status='offline', updated_at=%s
            WHERE enabled=TRUE
              AND last_seen IS NOT NULL
              AND last_seen < %s
              AND status <> 'offline'
            """,
            [timezone.now(), cutoff],
        )
        return cur.rowcount


@shared_task(name="accounts.asset_health.cleanup_history")
def cleanup_history():
    with connection.cursor() as cur:
        cur.execute(
            """
            DELETE FROM accounts_asset_health_snapshot
            WHERE recorded_at < NOW() - INTERVAL '90 days'
            """
        )
        snapshots = cur.rowcount

        cur.execute(
            """
            DELETE FROM accounts_printer_counter
            WHERE recorded_at < NOW() - INTERVAL '365 days'
            """
        )
        printer_rows = cur.rowcount

    return {
        "snapshots": snapshots,
        "printer_counters": printer_rows,
    }
