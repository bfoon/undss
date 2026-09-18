import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .asset_health import (
    STALE_AFTER_MINUTES,
    get_health_context,
    humanize_uptime,
    iso,
    issue_token,
    save_snapshot,
    serialize_health,
    token_hash,
)
from .cups_service import cancel_job, pause_printer, printer_jobs, resume_printer, server_status
from .models import Asset


def _same_agency(user, asset):
    if user.is_superuser:
        return True
    return bool(
        getattr(user, "agency_id", None)
        and getattr(asset, "agency_id", None) == user.agency_id
    )


def _is_ict(user):
    return bool(
        user.is_superuser
        or getattr(user, "role", "") == "ict_focal"
    )


def _can_view_health(user, asset):
    if not _same_agency(user, asset):
        return False
    if _is_ict(user):
        return True
    return getattr(asset, "current_holder_id", None) == user.id


def _get_asset_for_health(user, asset_id):
    asset = get_object_or_404(
        Asset.objects.select_related(
            "agency", "category", "unit", "current_holder"
        ),
        pk=asset_id,
    )
    return asset if _can_view_health(user, asset) else None


# ---------------------------------------------------------------------------
# Fleet data (shared by the dashboard page and its live JSON endpoint)
# ---------------------------------------------------------------------------
_FLEET_SQL = """
    SELECT d.asset_id, d.monitor_type, d.hostname, host(d.ip_address),
           d.status, d.last_seen, s.health_score, s.recorded_at,
           d.vendor, d.model_name, d.serial_number, d.printer_queue_name,
           s.cpu_percent, s.memory_percent, s.disk_free_percent,
           s.battery_health_percent, s.temperature_c, s.uptime_seconds,
           s.os_name, s.os_version, s.firewall_status, s.antivirus_status,
           s.page_count_total, s.toner_black_percent,
           s.ports_up, s.ports_down, s.port_errors
    FROM accounts_asset_health_device d
    LEFT JOIN LATERAL (
        SELECT health_score, recorded_at, cpu_percent, memory_percent,
               disk_free_percent, battery_health_percent, temperature_c,
               uptime_seconds, os_name, os_version, firewall_status,
               antivirus_status, page_count_total, toner_black_percent,
               ports_up, ports_down, port_errors
        FROM accounts_asset_health_snapshot
        WHERE asset_id = d.asset_id
        ORDER BY recorded_at DESC LIMIT 1
    ) s ON TRUE
    WHERE d.enabled = TRUE
      {scope}
    ORDER BY
      CASE d.status
        WHEN 'critical' THEN 1
        WHEN 'warning' THEN 2
        WHEN 'offline' THEN 3
        ELSE 4
      END,
      d.asset_id
"""

_FLEET_COLUMNS = [
    "asset_id", "monitor_type", "hostname", "ip_address", "status", "last_seen",
    "health_score", "recorded_at", "vendor", "model_name", "serial_number",
    "printer_queue_name", "cpu_percent", "memory_percent", "disk_free_percent",
    "battery_health_percent", "temperature_c", "uptime_seconds", "os_name",
    "os_version", "firewall_status", "antivirus_status", "page_count_total",
    "toner_black_percent", "ports_up", "ports_down", "port_errors",
]


def _allowed_asset_ids(user):
    return list(
        Asset.objects.filter(
            agency_id=getattr(user, "agency_id", None)
        ).values_list("id", flat=True)
    )


def _fleet_rows(user, monitor_type=None):
    params = []
    scope = ""

    if not user.is_superuser:
        allowed = _allowed_asset_ids(user)
        if not allowed:
            return []
        scope += " AND d.asset_id = ANY(%s)"
        params.append(allowed)

    if monitor_type:
        scope += " AND d.monitor_type = %s"
        params.append(monitor_type)

    with connection.cursor() as cur:
        cur.execute(_FLEET_SQL.format(scope=scope), params)
        return [dict(zip(_FLEET_COLUMNS, r)) for r in cur.fetchall()]


def _sparklines(asset_ids, points=24, hours=24):
    """Last N health scores per asset, oldest first, for inline sparklines."""
    if not asset_ids:
        return {}

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, health_score, recorded_at
            FROM (
                SELECT asset_id, health_score, recorded_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY asset_id ORDER BY recorded_at DESC
                       ) AS rn
                FROM accounts_asset_health_snapshot
                WHERE asset_id = ANY(%s)
                  AND recorded_at >= NOW() - make_interval(hours => %s)
            ) t
            WHERE rn <= %s
            ORDER BY asset_id, recorded_at ASC
            """,
            [list(asset_ids), hours, points],
        )
        series = {}
        for asset_id, score, _recorded_at in cur.fetchall():
            series.setdefault(asset_id, []).append(score)
    return series


def _open_alert_counts(asset_ids):
    if not asset_ids:
        return {}

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, severity, COUNT(*)
            FROM accounts_asset_health_alert
            WHERE is_open = TRUE AND asset_id = ANY(%s)
            GROUP BY asset_id, severity
            """,
            [list(asset_ids)],
        )
        counts = {}
        for asset_id, severity, total in cur.fetchall():
            bucket = counts.setdefault(
                asset_id, {"critical": 0, "warning": 0, "info": 0, "total": 0}
            )
            bucket[severity] = total
            bucket["total"] += total
    return counts


def _fleet_payload(user, monitor_type=None):
    """Everything the dashboard needs, JSON-safe."""
    rows = _fleet_rows(user, monitor_type=monitor_type)
    asset_ids = [r["asset_id"] for r in rows]

    assets = {
        a.id: a
        for a in Asset.objects.filter(id__in=asset_ids).select_related(
            "category", "unit"
        )
    }
    sparks = _sparklines(asset_ids)
    alerts = _open_alert_counts(asset_ids)

    now = timezone.now()
    stale_cutoff = now - timedelta(minutes=STALE_AFTER_MINUTES)

    devices = []
    summary = {
        "total": 0,
        "healthy": 0,
        "warning": 0,
        "critical": 0,
        "offline": 0,
        "unknown": 0,
        "stale": 0,
        "alerts_critical": 0,
        "alerts_warning": 0,
        "score_sum": 0,
        "score_count": 0,
        "by_type": {},
    }

    for row in rows:
        asset = assets.get(row["asset_id"])
        alert_bucket = alerts.get(row["asset_id"], {"critical": 0, "warning": 0, "info": 0, "total": 0})
        last_seen = row["last_seen"]
        is_stale = bool(last_seen and last_seen < stale_cutoff)
        status = row["status"] or "unknown"

        devices.append(
            {
                "asset_id": row["asset_id"],
                "name": (asset.name if asset else None) or row["hostname"] or f"Asset {row['asset_id']}",
                "asset_tag": getattr(asset, "asset_tag", None) if asset else None,
                "category": getattr(getattr(asset, "category", None), "name", None),
                "unit": getattr(getattr(asset, "unit", None), "name", None),
                "monitor_type": row["monitor_type"],
                "hostname": row["hostname"],
                "ip_address": row["ip_address"],
                "vendor": row["vendor"],
                "model_name": row["model_name"],
                "serial_number": row["serial_number"],
                "queue": row["printer_queue_name"],
                "status": status,
                "health_score": row["health_score"],
                "last_seen": iso(last_seen),
                "recorded_at": iso(row["recorded_at"]),
                "seconds_since_seen": int((now - last_seen).total_seconds()) if last_seen else None,
                "is_stale": is_stale,
                "cpu_percent": row["cpu_percent"],
                "memory_percent": row["memory_percent"],
                "disk_free_percent": row["disk_free_percent"],
                "battery_health_percent": row["battery_health_percent"],
                "temperature_c": row["temperature_c"],
                "uptime_human": humanize_uptime(row["uptime_seconds"]),
                "os": " ".join(x for x in [row["os_name"], row["os_version"]] if x) or None,
                "firewall_status": row["firewall_status"],
                "antivirus_status": row["antivirus_status"],
                "page_count_total": row["page_count_total"],
                "toner_black_percent": row["toner_black_percent"],
                "ports_up": row["ports_up"],
                "ports_down": row["ports_down"],
                "port_errors": row["port_errors"],
                "alerts": alert_bucket,
                "spark": sparks.get(row["asset_id"], []),
                "url": reverse("accounts:asset_health_detail", args=[row["asset_id"]]) if asset else None,
                "printer_url": (
                    reverse("accounts:printer_health_detail", args=[row["asset_id"]])
                    if row["monitor_type"] == "printer" else None
                ),
            }
        )

        summary["total"] += 1
        summary[status if status in summary else "unknown"] += 1
        if is_stale:
            summary["stale"] += 1
        summary["alerts_critical"] += alert_bucket.get("critical", 0)
        summary["alerts_warning"] += alert_bucket.get("warning", 0)
        if row["health_score"] is not None:
            summary["score_sum"] += row["health_score"]
            summary["score_count"] += 1
        bucket = summary["by_type"].setdefault(
            row["monitor_type"], {"total": 0, "healthy": 0, "problem": 0}
        )
        bucket["total"] += 1
        if status == "healthy":
            bucket["healthy"] += 1
        elif status in {"warning", "critical", "offline"}:
            bucket["problem"] += 1

    summary["avg_score"] = (
        round(summary["score_sum"] / summary["score_count"])
        if summary["score_count"] else None
    )
    summary.pop("score_sum")
    summary.pop("score_count")

    return {
        "ok": True,
        "server_time": iso(now),
        "stale_after_minutes": STALE_AFTER_MINUTES,
        "summary": summary,
        "devices": devices,
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@login_required
def asset_health_dashboard(request):
    if not _is_ict(request.user):
        messages.error(request, "ICT access is required.")
        return redirect("accounts:asset_management")

    payload = _fleet_payload(request.user)

    return render(
        request,
        "accounts/assets/asset_health_dashboard.html",
        {
            "initial_payload": payload,
            "devices": payload["devices"],
            "summary": payload["summary"],
            "live_url": reverse("accounts:asset_health_dashboard_live"),
        },
    )


@login_required
def asset_health_dashboard_live(request):
    if not _is_ict(request.user):
        return JsonResponse({"ok": False, "error": "ICT access is required."}, status=403)

    monitor_type = request.GET.get("type") or None
    if monitor_type not in {None, "endpoint", "printer", "switch", "other"}:
        monitor_type = None

    return JsonResponse(_fleet_payload(request.user, monitor_type=monitor_type))


def _printer_counter_context(asset_id, context):
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT recorded_at, total_pages, supplies
            FROM accounts_printer_counter
            WHERE asset_id=%s
            ORDER BY recorded_at DESC
            LIMIT 300
            """,
            [asset_id],
        )
        counters = [
            {"recorded_at": r[0], "total_pages": r[1], "supplies": r[2]}
            for r in cur.fetchall()
        ]

    context["printer_counters"] = counters

    if counters and counters[0]["total_pages"] is not None:
        latest = counters[0]["total_pages"]
        for days, key in ((1, "pages_last_24h"), (7, "pages_last_7_days"), (30, "pages_last_30_days")):
            cutoff = timezone.now() - timedelta(days=days)
            older = next(
                (
                    x["total_pages"] for x in counters
                    if x["recorded_at"] <= cutoff and x["total_pages"] is not None
                ),
                None,
            )
            if older is not None:
                context[key] = max(0, latest - older)

    return context


@login_required
def asset_health_detail(request, asset_id):
    asset = _get_asset_for_health(request.user, asset_id)

    if asset is None:
        messages.error(
            request,
            "You do not have access to this asset health record.",
        )
        return redirect("accounts:asset_management")

    try:
        hours = int(request.GET.get("hours") or 168)
    except (TypeError, ValueError):
        hours = 168

    context = get_health_context(asset.id, hours=hours)
    context["asset"] = asset
    context["can_manage_health"] = _is_ict(request.user)
    context["live_url"] = reverse("accounts:asset_health_live", args=[asset.id])

    if context.get("device") and context["device"]["monitor_type"] == "printer":
        _printer_counter_context(asset.id, context)

    context["initial_payload"] = serialize_health(asset.id, hours=hours, context=context)

    return render(request, "accounts/assets/asset_health_detail.html", context)


@login_required
def asset_health_live(request, asset_id):
    """JSON feed powering the auto-refreshing detail page."""
    asset = _get_asset_for_health(request.user, asset_id)

    if asset is None:
        return JsonResponse({"ok": False, "error": "Access denied."}, status=403)

    try:
        hours = int(request.GET.get("hours") or 168)
    except (TypeError, ValueError):
        hours = 168

    return JsonResponse(serialize_health(asset.id, hours=hours))


@login_required
@require_POST
def asset_health_configure(request, asset_id):
    asset = get_object_or_404(Asset, pk=asset_id)

    if not _same_agency(request.user, asset) or not _is_ict(request.user):
        messages.error(request, "ICT access is required.")
        return redirect("accounts:asset_management")

    monitor_type = (request.POST.get("monitor_type") or "endpoint").strip()

    if monitor_type not in {"endpoint", "printer", "switch", "other"}:
        monitor_type = "other"

    ip_address = (request.POST.get("ip_address") or "").strip() or None
    hostname = (request.POST.get("hostname") or "").strip()[:255]
    queue = (request.POST.get("printer_queue_name") or "").strip()[:160]
    enabled = request.POST.get("enabled") == "1"
    now = timezone.now()

    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts_asset_health_device
            (
              asset_id,monitor_type,hostname,ip_address,
              printer_queue_name,enabled,status,
              created_at,updated_at
            )
            VALUES
            (%s,%s,%s,%s::inet,%s,%s,'unknown',%s,%s)
            ON CONFLICT (asset_id)
            DO UPDATE SET
              monitor_type=EXCLUDED.monitor_type,
              hostname=EXCLUDED.hostname,
              ip_address=EXCLUDED.ip_address,
              printer_queue_name=EXCLUDED.printer_queue_name,
              enabled=EXCLUDED.enabled,
              updated_at=EXCLUDED.updated_at
            """,
            [asset.id, monitor_type, hostname, ip_address, queue, enabled, now, now],
        )

    messages.success(request, "Asset Health monitoring settings updated.")
    return redirect("accounts:asset_health_detail", asset_id=asset.id)


@login_required
@require_POST
def issue_agent_token(request, asset_id):
    asset = get_object_or_404(Asset, pk=asset_id)

    if not _same_agency(request.user, asset) or not _is_ict(request.user):
        return JsonResponse({"ok": False, "error": "Access denied."}, status=403)

    token, digest = issue_token()
    now = timezone.now()

    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts_asset_health_device
            (
              asset_id,monitor_type,token_hash,enabled,
              status,created_at,updated_at
            )
            VALUES
            (%s,'endpoint',%s,TRUE,'unknown',%s,%s)
            ON CONFLICT (asset_id)
            DO UPDATE SET
              monitor_type='endpoint',
              token_hash=EXCLUDED.token_hash,
              enabled=TRUE,
              updated_at=EXCLUDED.updated_at
            """,
            [asset.id, digest, now, now],
        )

    return JsonResponse(
        {
            "ok": True,
            "asset_id": asset.id,
            "asset": str(asset),
            "token": token,
            "endpoint": request.build_absolute_uri(
                "/accounts/api/asset-health/v1/heartbeat/"
            ),
            "warning": "Copy this token now. It will not be shown again.",
        }
    )


@csrf_exempt
@require_POST
def agent_heartbeat(request):
    auth = request.META.get("HTTP_AUTHORIZATION", "")

    if not auth.lower().startswith("bearer "):
        return JsonResponse({"ok": False, "error": "Missing bearer token."}, status=401)

    digest = token_hash(auth.split(" ", 1)[1].strip())

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id,enabled
            FROM accounts_asset_health_device
            WHERE token_hash=%s
            LIMIT 1
            """,
            [digest],
        )
        row = cur.fetchone()

    if not row or not row[1]:
        return JsonResponse({"ok": False, "error": "Invalid or disabled token."}, status=401)

    # Keep heartbeats compact. This also prevents oversized telemetry posts.
    if len(request.body) > 131072:
        return JsonResponse({"ok": False, "error": "Heartbeat payload too large."}, status=413)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "JSON object required."}, status=400)

    score, status, findings = save_snapshot(row[0], payload, source="agent")

    return JsonResponse(
        {
            "ok": True,
            "asset_id": row[0],
            "health_score": score,
            "status": status,
            "alerts": len(findings),
            "findings": [
                {"severity": s, "code": c, "message": m} for s, c, m in findings
            ],
            # The agent can honour this to align with the dashboard refresh.
            "next_heartbeat_seconds": 300,
            "server_time": timezone.now().isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Printers
# ---------------------------------------------------------------------------
@login_required
def printer_management(request):
    if not _is_ict(request.user):
        messages.error(request, "ICT access is required.")
        return redirect("accounts:asset_management")

    payload = _fleet_payload(request.user, monitor_type="printer")

    printers = [
        {
            "asset_id": d["asset_id"],
            "asset": None,
            "name": d["name"],
            "hostname": d["hostname"],
            "ip_address": d["ip_address"],
            "status": d["status"],
            "last_seen": d["last_seen"],
            "queue": d["queue"],
            "page_count_total": d["page_count_total"],
            "toner_black_percent": d["toner_black_percent"],
            "health_score": d["health_score"],
        }
        for d in payload["devices"]
    ]

    assets = {
        a.id: a
        for a in Asset.objects.filter(
            id__in=[p["asset_id"] for p in printers]
        ).select_related("category", "unit")
    }
    for printer in printers:
        printer["asset"] = assets.get(printer["asset_id"])

    return render(
        request,
        "accounts/assets/printer_management.html",
        {
            "printers": printers,
            "cups": server_status(),
            "summary": payload["summary"],
            "initial_payload": payload,
            "live_url": reverse("accounts:asset_health_dashboard_live") + "?type=printer",
        },
    )


@login_required
def printer_detail(request, asset_id):
    asset = get_object_or_404(Asset, pk=asset_id)

    if not _same_agency(request.user, asset) or not _is_ict(request.user):
        messages.error(request, "ICT access is required.")
        return redirect("accounts:asset_management")

    context = get_health_context(asset.id)
    context["asset"] = asset
    context["cups"] = server_status()
    context["live_url"] = reverse("accounts:asset_health_live", args=[asset.id])

    queue_name = (context.get("device") or {}).get("printer_queue_name")
    context["print_jobs"] = []

    if queue_name:
        try:
            raw_jobs = printer_jobs(queue_name)
            context["print_jobs"] = [
                {
                    "id": job_id,
                    "user": job.get("job-originating-user-name") or "—",
                    # Keep document title only when CUPS exposes it. Sites that
                    # do not want titles retained can disable job-name logging
                    # at the CUPS layer.
                    "name": job.get("job-name") or "Hidden / unavailable",
                    "state": job.get("job-state") or "—",
                }
                for job_id, job in raw_jobs.items()
            ]
        except Exception as exc:
            context["cups_jobs_error"] = str(exc)

    _printer_counter_context(asset.id, context)
    context["initial_payload"] = serialize_health(asset.id, context=context)

    return render(request, "accounts/assets/printer_detail.html", context)


@login_required
@require_POST
def printer_action(request, asset_id):
    asset = get_object_or_404(Asset, pk=asset_id)

    if not _same_agency(request.user, asset) or not _is_ict(request.user):
        messages.error(request, "ICT access is required.")
        return redirect("accounts:asset_management")

    context = get_health_context(asset.id)
    queue = (context.get("device") or {}).get("printer_queue_name")

    if not queue:
        messages.error(request, "No CUPS queue is configured for this printer.")
        return redirect("accounts:printer_health_detail", asset_id=asset.id)

    action = request.POST.get("action")

    try:
        if action == "pause":
            pause_printer(queue)
            messages.success(request, f"Printer queue {queue} paused.")
        elif action == "resume":
            resume_printer(queue)
            messages.success(request, f"Printer queue {queue} resumed.")
        elif action == "cancel_job":
            cancel_job(request.POST.get("job_id"))
            messages.success(request, "Print job cancelled.")
        else:
            messages.error(request, "Unknown printer action.")
    except Exception as exc:
        messages.error(request, f"Print server action failed: {exc}")

    return redirect("accounts:printer_health_detail", asset_id=asset.id)
