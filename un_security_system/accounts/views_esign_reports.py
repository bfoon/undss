# accounts/views_esign_reports.py
"""Views for the UN PASS eSign BI-style Report Builder."""

from __future__ import annotations

import csv
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .models_esign_studio import DocumentWorkflow, FormTemplate
from .report_bi_esign import (
    ReportError,
    archive_report,
    create_report,
    duplicate_report,
    ensure_report_tables,
    get_report,
    list_reports,
    query_report,
    raw_export_rows,
    report_access,
    source_catalog,
    update_report,
)
from .studio_common_esign import (
    json_body,
    shared_template_q,
    studio_context,
    studio_gate,
)


def _source_name(report):
    if report["source_type"] == "form":
        obj = FormTemplate.objects.filter(pk=report["source_id"]).first()
    else:
        obj = DocumentWorkflow.objects.filter(pk=report["source_id"]).first()
    return obj.name if obj else "Deleted source"


def _report_or_404(request, pk, *, edit=False):
    report = get_report(pk)
    if not report or not report_access(request.user, report, edit=edit):
        raise Http404()
    report["source_name"] = _source_name(report)
    return report


@login_required
@require_GET
def reports_home(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce

    ensure_report_tables()
    forms = (
        FormTemplate.objects.filter(shared_template_q(request.user))
        .select_related("created_by")
        .annotate(submission_total=Count("submissions"))
        .order_by("name")
    )
    flows = (
        DocumentWorkflow.objects.filter(shared_template_q(request.user), is_active=True)
        .select_related("created_by", "form")
        .annotate(run_total=Count("runs"))
        .order_by("name")
    )

    reports = list_reports(request.user)
    for report in reports:
        report["source_name"] = _source_name(report)
        report["is_owner"] = report["created_by_id"] == request.user.id

    return render(
        request,
        "accounts/esign/studio/reports.html",
        studio_context(
            request,
            "reports",
            reports=reports,
            forms=forms,
            flows=flows,
        ),
    )


@login_required
@require_POST
def report_new(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce

    try:
        report = create_report(
            request.user,
            agency,
            source_type=request.POST.get("source_type"),
            source_id=request.POST.get("source_id"),
            name=request.POST.get("name") or "",
        )
    except (ReportError, ValueError, TypeError) as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_reports")

    messages.success(request, "Report created. Build your dashboard below.")
    return redirect("accounts:esign_report_designer", pk=report["id"])


@login_required
@require_GET
def report_designer(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce

    report = _report_or_404(request, pk, edit=True)
    catalog = source_catalog(
        request.user,
        report["source_type"],
        report["source_id"],
    )
    return render(
        request,
        "accounts/esign/studio/report_designer.html",
        studio_context(
            request,
            "reports",
            report=report,
            catalog=catalog,
            report_config=report.get("config") or {},
            save_url=reverse("accounts:esign_report_save", args=[report["id"]]),
            query_url=reverse("accounts:esign_report_query", args=[report["id"]]),
            view_url=reverse("accounts:esign_report_view", args=[report["id"]]),
            export_url=reverse("accounts:esign_report_export_csv", args=[report["id"]]),
        ),
    )


@login_required
@require_POST
def report_save(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return JsonResponse({"ok": False, "error": "eSign is not enabled."}, status=403)

    report = _report_or_404(request, pk, edit=True)
    body, problem = json_body(request, "flow")
    if problem:
        return problem

    try:
        saved = update_report(
            report["id"],
            request.user,
            name=body.get("name"),
            description=body.get("description"),
            share_scope=body.get("share_scope"),
            config=body.get("config"),
        )
    except ReportError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    return JsonResponse(
        {
            "ok": True,
            "updated_at": str(saved.get("updated_at") or ""),
            "name": saved["name"],
        }
    )


@login_required
@require_GET
def report_view(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce

    report = _report_or_404(request, pk, edit=False)
    catalog = source_catalog(
        request.user,
        report["source_type"],
        report["source_id"],
    )
    return render(
        request,
        "accounts/esign/studio/report_view.html",
        studio_context(
            request,
            "reports",
            report=report,
            can_edit=report_access(request.user, report, edit=True),
            catalog=catalog,
            report_config=report.get("config") or {},
            query_url=reverse("accounts:esign_report_query", args=[report["id"]]),
            export_url=reverse("accounts:esign_report_export_csv", args=[report["id"]]),
        ),
    )


@login_required
@require_POST
def report_query(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return JsonResponse({"ok": False, "error": "eSign is not enabled."}, status=403)

    report = _report_or_404(request, pk, edit=False)
    body, problem = json_body(request, "flow")
    if problem:
        return problem

    widgets = body.get("widgets")
    if not isinstance(widgets, list):
        widgets = (report.get("config") or {}).get("widgets") or []

    filters = body.get("filters")
    if not isinstance(filters, list):
        filters = []

    try:
        data = query_report(
            request.user,
            report,
            widgets,
            runtime_filters=filters,
        )
    except ReportError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        return JsonResponse(
            {
                "ok": False,
                "error": f"The report data could not be prepared: {exc}",
            },
            status=500,
        )

    return JsonResponse({"ok": True, **data})


@login_required
@require_POST
def report_duplicate(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce

    try:
        new_report = duplicate_report(pk, request.user, agency)
    except ReportError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_reports")

    messages.success(request, "Report copied.")
    return redirect("accounts:esign_report_designer", pk=new_report["id"])


@login_required
@require_POST
def report_delete(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce

    try:
        archive_report(pk, request.user)
    except ReportError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Report removed.")
    return redirect("accounts:esign_reports")


@login_required
@require_GET
def report_export_csv(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce

    report = _report_or_404(request, pk, edit=False)
    dataset_id = request.GET.get("dataset") or (
        "submissions" if report["source_type"] == "form" else "runs"
    )

    try:
        catalog = source_catalog(
            request.user,
            report["source_type"],
            report["source_id"],
        )
        dataset = next(
            (d for d in catalog["datasets"] if d["id"] == dataset_id),
            None,
        )
        if dataset is None:
            raise ReportError("Choose a valid dataset.")
        rows, meta = raw_export_rows(request.user, report, dataset_id)
    except ReportError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_report_view", pk=pk)

    field_ids = [f["id"] for f in dataset["fields"]]
    field_labels = [f["label"] for f in dataset["fields"]]

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    safe = "".join(
        ch if ch.isalnum() or ch in "-_" else "-"
        for ch in report["name"]
    )[:80] or "report"
    response["Content-Disposition"] = f'attachment; filename="{safe}-{dataset_id}.csv"'
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow(field_labels)
    for row in rows:
        writer.writerow([row.get(fid, "") for fid in field_ids])

    return response
