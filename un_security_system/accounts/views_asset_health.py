import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .asset_health import get_health_context, issue_token, save_snapshot, token_hash
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


@login_required
def asset_health_dashboard(request):
    if not _is_ict(request.user):
        messages.error(request, "ICT access is required.")
        return redirect("accounts:asset_management")

    agency_id = getattr(request.user, "agency_id", None)

    with connection.cursor() as cur:
        if request.user.is_superuser:
            cur.execute(
                """
                SELECT d.asset_id,d.monitor_type,d.hostname,host(d.ip_address),
                       d.status,d.last_seen,s.health_score,s.recorded_at
                FROM accounts_asset_health_device d
                LEFT JOIN LATERAL (
                    SELECT health_score,recorded_at
                    FROM accounts_asset_health_snapshot
                    WHERE asset_id=d.asset_id
                    ORDER BY recorded_at DESC LIMIT 1
                ) s ON TRUE
                WHERE d.enabled=TRUE
                ORDER BY
                  CASE d.status
                    WHEN 'critical' THEN 1
                    WHEN 'warning' THEN 2
                    WHEN 'offline' THEN 3
                    ELSE 4
                  END,
                  d.asset_id
                """
            )
        else:
            allowed_asset_ids = list(
                Asset.objects.filter(
                    agency_id=agency_id
                ).values_list("id", flat=True)
            )
            if not allowed_asset_ids:
                rows = []
                cur.execute("SELECT 1 WHERE FALSE")
            else:
                cur.execute(
                    """
                    SELECT d.asset_id,d.monitor_type,d.hostname,host(d.ip_address),
                           d.status,d.last_seen,s.health_score,s.recorded_at
                    FROM accounts_asset_health_device d
                    LEFT JOIN LATERAL (
                        SELECT health_score,recorded_at
                        FROM accounts_asset_health_snapshot
                        WHERE asset_id=d.asset_id
                        ORDER BY recorded_at DESC LIMIT 1
                    ) s ON TRUE
                    WHERE d.enabled=TRUE
                      AND d.asset_id = ANY(%s)
                    ORDER BY
                      CASE d.status
                        WHEN 'critical' THEN 1
                        WHEN 'warning' THEN 2
                        WHEN 'offline' THEN 3
                        ELSE 4
                      END,
                      d.asset_id
                    """,
                    [allowed_asset_ids],
                )
        rows = cur.fetchall()

    asset_ids = [r[0] for r in rows]
    assets = {
        a.id: a
        for a in Asset.objects.filter(id__in=asset_ids).select_related(
            "category", "unit"
        )
    }

    devices = [
        {
            "asset": assets.get(r[0]),
            "asset_id": r[0],
            "monitor_type": r[1],
            "hostname": r[2],
            "ip_address": r[3],
            "status": r[4],
            "last_seen": r[5],
            "health_score": r[6],
            "recorded_at": r[7],
        }
        for r in rows
    ]

    return render(
        request,
        "accounts/assets/asset_health_dashboard.html",
        {"devices": devices},
    )


@login_required
def asset_health_detail(request, asset_id):
    asset = _get_asset_for_health(request.user, asset_id)

    if asset is None:
        messages.error(
            request,
            "You do not have access to this asset health record.",
        )
        return redirect("accounts:asset_management")

    context = get_health_context(asset.id)
    context["asset"] = asset
    context["can_manage_health"] = _is_ict(request.user)

    if (
        context.get("device")
        and context["device"]["monitor_type"] == "printer"
    ):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT recorded_at,total_pages,supplies
                FROM accounts_printer_counter
                WHERE asset_id=%s
                ORDER BY recorded_at DESC
                LIMIT 300
                """,
                [asset.id],
            )
            counters = [
                {
                    "recorded_at": r[0],
                    "total_pages": r[1],
                    "supplies": r[2],
                }
                for r in cur.fetchall()
            ]

        context["printer_counters"] = counters

        if counters and counters[0]["total_pages"] is not None:
            latest = counters[0]["total_pages"]
            cutoff = timezone.now() - timedelta(days=30)
            older = next(
                (
                    x["total_pages"]
                    for x in counters
                    if x["recorded_at"] <= cutoff
                    and x["total_pages"] is not None
                ),
                None,
            )
            if older is not None:
                context["pages_last_30_days"] = max(
                    0,
                    latest - older,
                )

    return render(
        request,
        "accounts/assets/asset_health_detail.html",
        context,
    )


@login_required
@require_POST
def asset_health_configure(request, asset_id):
    asset = get_object_or_404(Asset, pk=asset_id)

    if (
        not _same_agency(request.user, asset)
        or not _is_ict(request.user)
    ):
        messages.error(request, "ICT access is required.")
        return redirect("accounts:asset_management")

    monitor_type = (
        request.POST.get("monitor_type") or "endpoint"
    ).strip()

    if monitor_type not in {
        "endpoint",
        "printer",
        "switch",
        "other",
    }:
        monitor_type = "other"

    ip_address = (
        request.POST.get("ip_address") or ""
    ).strip() or None

    hostname = (
        request.POST.get("hostname") or ""
    ).strip()[:255]

    queue = (
        request.POST.get("printer_queue_name") or ""
    ).strip()[:160]

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
            [
                asset.id,
                monitor_type,
                hostname,
                ip_address,
                queue,
                enabled,
                now,
                now,
            ],
        )

    messages.success(
        request,
        "Asset Health monitoring settings updated.",
    )
    return redirect(
        "accounts:asset_health_detail",
        asset_id=asset.id,
    )


@login_required
@require_POST
def issue_agent_token(request, asset_id):
    asset = get_object_or_404(Asset, pk=asset_id)

    if (
        not _same_agency(request.user, asset)
        or not _is_ict(request.user)
    ):
        return JsonResponse(
            {"ok": False, "error": "Access denied."},
            status=403,
        )

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
            [
                asset.id,
                digest,
                now,
                now,
            ],
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
            "warning": (
                "Copy this token now. "
                "It will not be shown again."
            ),
        }
    )


@csrf_exempt
@require_POST
def agent_heartbeat(request):
    auth = request.META.get(
        "HTTP_AUTHORIZATION",
        "",
    )

    if not auth.lower().startswith("bearer "):
        return JsonResponse(
            {
                "ok": False,
                "error": "Missing bearer token.",
            },
            status=401,
        )

    digest = token_hash(
        auth.split(" ", 1)[1].strip()
    )

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
        return JsonResponse(
            {
                "ok": False,
                "error": "Invalid or disabled token.",
            },
            status=401,
        )

    # Keep heartbeats compact. This also prevents oversized telemetry posts.
    if len(request.body) > 65536:
        return JsonResponse(
            {
                "ok": False,
                "error": "Heartbeat payload too large.",
            },
            status=413,
        )

    try:
        payload = json.loads(
            request.body.decode("utf-8")
        )
    except Exception:
        return JsonResponse(
            {
                "ok": False,
                "error": "Invalid JSON.",
            },
            status=400,
        )

    if not isinstance(payload, dict):
        return JsonResponse(
            {
                "ok": False,
                "error": "JSON object required.",
            },
            status=400,
        )

    score, status, findings = save_snapshot(
        row[0],
        payload,
        source="agent",
    )

    return JsonResponse(
        {
            "ok": True,
            "asset_id": row[0],
            "health_score": score,
            "status": status,
            "alerts": len(findings),
            "server_time": timezone.now().isoformat(),
        }
    )


@login_required
def printer_management(request):
    if not _is_ict(request.user):
        messages.error(
            request,
            "ICT access is required.",
        )
        return redirect(
            "accounts:asset_management"
        )

    agency_id = getattr(
        request.user,
        "agency_id",
        None,
    )

    with connection.cursor() as cur:
        if request.user.is_superuser:
            cur.execute(
                """
                SELECT
                  d.asset_id,d.hostname,host(d.ip_address),
                  d.status,d.last_seen,d.printer_queue_name,
                  s.page_count_total,s.toner_black_percent,
                  s.health_score
                FROM accounts_asset_health_device d
                LEFT JOIN LATERAL (
                    SELECT
                      page_count_total,
                      toner_black_percent,
                      health_score
                    FROM accounts_asset_health_snapshot
                    WHERE asset_id=d.asset_id
                    ORDER BY recorded_at DESC
                    LIMIT 1
                ) s ON TRUE
                WHERE d.monitor_type='printer'
                  AND d.enabled=TRUE
                ORDER BY d.asset_id
                """
            )
        else:
            allowed_asset_ids = list(
                Asset.objects.filter(
                    agency_id=agency_id
                ).values_list("id", flat=True)
            )
            if not allowed_asset_ids:
                cur.execute("SELECT 1 WHERE FALSE")
            else:
                cur.execute(
                    """
                    SELECT
                      d.asset_id,d.hostname,host(d.ip_address),
                      d.status,d.last_seen,d.printer_queue_name,
                      s.page_count_total,s.toner_black_percent,
                      s.health_score
                    FROM accounts_asset_health_device d
                    LEFT JOIN LATERAL (
                        SELECT
                          page_count_total,
                          toner_black_percent,
                          health_score
                        FROM accounts_asset_health_snapshot
                        WHERE asset_id=d.asset_id
                        ORDER BY recorded_at DESC
                        LIMIT 1
                    ) s ON TRUE
                    WHERE d.monitor_type='printer'
                      AND d.enabled=TRUE
                      AND d.asset_id = ANY(%s)
                    ORDER BY d.asset_id
                    """,
                    [allowed_asset_ids],
                )
        rows = cur.fetchall()

    asset_ids = [r[0] for r in rows]
    assets = {
        a.id: a
        for a in Asset.objects.filter(
            id__in=asset_ids
        ).select_related(
            "category",
            "unit",
        )
    }

    printers = [
        {
            "asset": assets.get(r[0]),
            "asset_id": r[0],
            "hostname": r[1],
            "ip_address": r[2],
            "status": r[3],
            "last_seen": r[4],
            "queue": r[5],
            "page_count_total": r[6],
            "toner_black_percent": r[7],
            "health_score": r[8],
        }
        for r in rows
    ]

    return render(
        request,
        "accounts/assets/printer_management.html",
        {
            "printers": printers,
            "cups": server_status(),
        },
    )


@login_required
def printer_detail(request, asset_id):
    asset = get_object_or_404(
        Asset,
        pk=asset_id,
    )

    if (
        not _same_agency(request.user, asset)
        or not _is_ict(request.user)
    ):
        messages.error(
            request,
            "ICT access is required.",
        )
        return redirect(
            "accounts:asset_management"
        )

    context = get_health_context(
        asset.id
    )
    context["asset"] = asset
    context["cups"] = server_status()

    queue_name = (
        context.get("device") or {}
    ).get("printer_queue_name")

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

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT
              recorded_at,total_pages,supplies
            FROM accounts_printer_counter
            WHERE asset_id=%s
            ORDER BY recorded_at DESC
            LIMIT 300
            """,
            [asset.id],
        )

        context["printer_counters"] = [
            {
                "recorded_at": r[0],
                "total_pages": r[1],
                "supplies": r[2],
            }
            for r in cur.fetchall()
        ]

    return render(
        request,
        "accounts/assets/printer_detail.html",
        context,
    )


@login_required
@require_POST
def printer_action(request, asset_id):
    asset = get_object_or_404(
        Asset,
        pk=asset_id,
    )

    if (
        not _same_agency(request.user, asset)
        or not _is_ict(request.user)
    ):
        messages.error(
            request,
            "ICT access is required.",
        )
        return redirect(
            "accounts:asset_management"
        )

    context = get_health_context(
        asset.id
    )
    queue = (
        context.get("device") or {}
    ).get("printer_queue_name")

    if not queue:
        messages.error(
            request,
            "No CUPS queue is configured for this printer.",
        )
        return redirect(
            "accounts:printer_health_detail",
            asset_id=asset.id,
        )

    action = request.POST.get(
        "action"
    )

    try:
        if action == "pause":
            pause_printer(queue)
            messages.success(
                request,
                f"Printer queue {queue} paused.",
            )
        elif action == "resume":
            resume_printer(queue)
            messages.success(
                request,
                f"Printer queue {queue} resumed.",
            )
        elif action == "cancel_job":
            cancel_job(
                request.POST.get("job_id")
            )
            messages.success(
                request,
                "Print job cancelled.",
            )
        else:
            messages.error(
                request,
                "Unknown printer action.",
            )
    except Exception as exc:
        messages.error(
            request,
            f"Print server action failed: {exc}",
        )

    return redirect(
        "accounts:printer_health_detail",
        asset_id=asset.id,
    )
