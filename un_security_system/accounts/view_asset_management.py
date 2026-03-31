# view_asset_management.py
import csv
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .asset_email import send_email_async
from .utils_assets import (
    build_qr_payload,
    can_user_approve_asset_change,
    can_user_approve_return,
    generate_qr_image,
    generate_unique_asset_tag,
    get_ict_custodian_emails,
    get_manager_emails_for_request,
    save_qr_to_asset,
)
from .models import (
    AgencyAssetRoles, AgencyServiceConfig, Asset, AssetCategory,
    AssetChangeRequest, AssetHistory, AssetRequest, AssetReturnRequest,
    CellServiceFocalPoint, ConsumableAssetLink, ConsumableCategory,
    ConsumableItem, ConsumableRequest, ConsumableRequestItem,
    ConsumableStockLog, ExitRequest, MobileLine,
    MobileLineReactivationRequest, Unit, User,
)
from .pdf_assets import LabelSpec, build_asset_labels_pdf


# ─────────────────────────────────────────────────────────────────────────────
# Role helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_ict(user, agency):
    if user.is_superuser:
        return True
    roles = getattr(agency, "asset_roles", None)
    return bool(roles and roles.ict_custodian.filter(id=user.id).exists()) or getattr(user, "role", "") == "ict_focal"


def _is_ops_manager(user, agency):
    if user.is_superuser:
        return True
    roles = getattr(agency, "asset_roles", None)
    return bool(roles and roles.operations_manager_id == user.id)


def _managed_unit_ids(user, agency):
    unit_ids = set(
        Unit.objects.filter(agency=agency, unit_head=user).values_list("id", flat=True)
    )
    unit_ids |= set(
        Unit.objects.filter(agency=agency, asset_managers=user).values_list("id", flat=True)
    )
    return unit_ids


def _log_event(agency, asset, actor, event, note="", meta=None):
    try:
        AssetHistory.objects.create(
            agency=agency,
            asset=asset,
            actor=actor,
            event=event,
            note=note or "",
            meta=meta or {},
        )
    except Exception:
        pass


def _notify(subject: str, to_emails: list[str], html_template: str, ctx: dict):
    to_emails = [e for e in (to_emails or []) if e]
    if not to_emails:
        return
    brand = getattr(settings, "SITE_NAME", "UN PASS")
    try:
        send_email_async(
            subject=subject,
            to_emails=to_emails,
            html_template=html_template,
            context={**ctx, "subject": subject, "brand": brand},
        )
    except Exception:
        pass


def _can_user_manage_asset(user, agency, asset) -> bool:
    is_ict = user.is_superuser or _is_ict(user, agency)
    is_ops = user.is_superuser or _is_ops_manager(user, agency)
    managed_units = _managed_unit_ids(user, agency)

    if is_ict:
        return True
    if is_ops and (asset.unit_id is None or (asset.unit and getattr(asset.unit, "is_core_unit", False))):
        return True
    if asset.unit_id and asset.unit_id in managed_units:
        return True
    if asset.current_holder_id == user.id:
        return True
    return False


def _can_user_approve_change(user, agency, asset) -> bool:
    if user.is_superuser:
        return True
    roles, _ = AgencyAssetRoles.objects.get_or_create(agency=agency)
    if roles.operations_manager_id and user.id == roles.operations_manager_id:
        if (asset.unit_id is None) or (asset.unit and getattr(asset.unit, "is_core_unit", False)):
            return True
    if asset.unit_id:
        if asset.unit.unit_head_id and user.id == asset.unit.unit_head_id:
            return True
        return asset.unit.asset_managers.filter(id=user.id).exists()
    return False


def _safe_email(u):
    return getattr(u, "email", None)


def _get_exit_recipients(user, agency):
    unit = getattr(user, "unit", None)
    unit_head_email = _safe_email(getattr(unit, "unit_head", None)) if unit else None

    roles = getattr(agency, "asset_roles", None)
    ops_email = _safe_email(getattr(roles, "operations_manager", None)) if roles else None
    ict_emails = list(roles.ict_custodian.values_list("email", flat=True)) if roles else []

    cell_focal_emails = list(
        CellServiceFocalPoint.objects.filter(agency=agency, is_active=True)
        .values_list("email", flat=True)
    )

    base = [unit_head_email, ops_email] + ict_emails
    base = [e for e in dict.fromkeys(base) if e]
    cell_focal_emails = [e for e in dict.fromkeys(cell_focal_emails) if e]

    return base, cell_focal_emails


def _get_line_suspend_recipients(agency):
    provider_focals = list(
        CellServiceFocalPoint.objects.filter(agency=agency, is_active=True)
        .values_list("email", flat=True)
    )

    roles = getattr(agency, "asset_roles", None)
    ops_email = roles.operations_manager.email if roles and roles.operations_manager else None

    registry_emails = list(
        User.objects.filter(agency=agency, role="registry", is_active=True)
        .exclude(email__isnull=True).exclude(email__exact="")
        .values_list("email", flat=True)
    )

    def _uniq(lst):
        return list(dict.fromkeys([e for e in lst if e]))

    return _uniq(provider_focals), ops_email, _uniq(registry_emails)


# ─────────────────────────────────────────────────────────────────────────────
# Consumable chart helper (module-level so it can be reused)
# ─────────────────────────────────────────────────────────────────────────────

def _build_consumable_chart_data(items_qs):
    """
    Returns a JSON string ready for Chart.js.
    Horizontal bar per item coloured green / amber / red.
    Dashed line overlay shows the low-stock threshold.
    """
    labels, stock_vals, threshold_vals, colors = [], [], [], []
    for item in items_qs:
        labels.append(item.name)
        stock_vals.append(item.stock_qty)
        threshold_vals.append(item.low_stock_threshold)
        if item.is_out_of_stock:
            colors.append("rgba(220,53,69,0.8)")
        elif item.is_low_stock:
            colors.append("rgba(255,193,7,0.8)")
        else:
            colors.append("rgba(25,135,84,0.8)")

    return json.dumps({
        "labels": labels,
        "datasets": [
            {
                "label": "Current Stock",
                "data": stock_vals,
                "backgroundColor": colors,
                "borderRadius": 4,
                "barThickness": 18,
            },
            {
                "label": "Low-Stock Threshold",
                "data": threshold_vals,
                "type": "line",
                "borderColor": "rgba(220,53,69,0.9)",
                "borderWidth": 2,
                "borderDash": [5, 5],
                "pointRadius": 0,
                "fill": False,
                "tension": 0,
            },
        ],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Main dashboard view
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def view_asset_management(request):
    """
    Dashboard for:
    - Requesters: my requests + my assigned assets + returns + cancel before approval
    - Managers: approve requests for units they manage (and Ops for core/unallocated)
    - ICT: register + assign + verify returns + see all assets
    """
    user = request.user
    agency = getattr(user, "agency", None)

    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return redirect("accounts:profile")

    svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
    if not svc.asset_mgmt_enabled and not user.is_superuser:
        messages.warning(request, "Asset Management is not enabled for your agency.")
        return redirect("accounts:profile")

    roles, _ = AgencyAssetRoles.objects.get_or_create(agency=agency)

    is_ict = _is_ict(user, agency)
    is_ops = _is_ops_manager(user, agency)
    managed_units = _managed_unit_ids(user, agency)
    is_manager = user.is_superuser or bool(managed_units) or is_ops

    portal_url = request.build_absolute_uri(reverse("accounts:asset_management"))

    def _notify_local(subject: str, to_emails: list[str], html_template: str, ctx: dict):
        to_emails = [e for e in (to_emails or []) if e]
        if not to_emails:
            return
        ctx = {**ctx, "subject": subject, "agency": agency, "portal_url": portal_url}
        try:
            send_email_async(
                subject=subject,
                to_emails=to_emails,
                html_template=html_template,
                context=ctx,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Shared data
    # ------------------------------------------------------------------
    units = Unit.objects.filter(agency=agency).select_related("unit_head").prefetch_related("asset_managers")
    categories = AssetCategory.objects.filter(agency=agency).order_by("name")

    assets_all = Asset.objects.filter(agency=agency).select_related("category", "unit", "current_holder")

    if is_ict:
        assets_visible = assets_all
    elif is_ops:
        assets_visible = assets_all.filter(Q(unit__isnull=True) | Q(unit__is_core_unit=True))
    elif managed_units:
        assets_visible = assets_all.filter(unit_id__in=managed_units)
    else:
        assets_visible = assets_all.filter(current_holder=user)

    assets_visible = assets_visible.order_by("-created_at")

    my_requests = AssetRequest.objects.filter(
        agency=agency, requester=user
    ).select_related("unit", "category", "assigned_asset").order_by("-created_at")

    pending_approvals = []
    if is_manager:
        pending_qs = AssetRequest.objects.filter(
            agency=agency, status="pending_manager"
        ).select_related("unit", "requester", "category")
        pending_approvals = [r for r in pending_qs if (user.is_superuser or r.can_user_approve_as_manager(user))]

    pending_ict = AssetRequest.objects.filter(
        agency=agency, status="pending_ict"
    ).select_related("unit", "requester", "category")

    my_returns = AssetReturnRequest.objects.filter(
        agency=agency, requested_by=user
    ).select_related("asset").order_by("-created_at")

    pending_returns = AssetReturnRequest.objects.filter(
        agency=agency, status="pending_ict"
    ).select_related("asset", "requested_by").order_by("-created_at")

    returning_asset_ids = set(pending_returns.values_list("asset_id", flat=True))

    eol_assets = []
    if is_ict or is_manager:
        eol_assets = [a for a in assets_visible if getattr(a, "is_eol_due", False) and a.status != "retired"]

    if is_ict:
        mobile_lines_visible = MobileLine.objects.filter(agency=agency).select_related(
            "custodian", "assigned_to"
        ).order_by("-created_at")
    else:
        mobile_lines_visible = MobileLine.objects.filter(
            agency=agency, assigned_to=user
        ).select_related("custodian", "assigned_to").order_by("-created_at")

    # ------------------------------------------------------------------
    # POST actions
    # ------------------------------------------------------------------
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        # ── Cancel asset request ──────────────────────────────────────
        if action == "cancel_request":
            req_id = request.POST.get("request_id")
            req_obj = get_object_or_404(AssetRequest, id=req_id, agency=agency)

            if req_obj.requester_id != user.id and not user.is_superuser:
                messages.error(request, "You can only cancel your own request.")
                return redirect("accounts:asset_management")

            cancellable_statuses = {"draft", "pending_manager", "pending_ict"}
            if req_obj.status not in cancellable_statuses:
                messages.info(request, "This request can't be cancelled at this stage.")
                return redirect("accounts:asset_management")

            req_obj.status = "cancelled"
            req_obj.save(update_fields=["status"])
            messages.success(request, f"Request #{req_obj.id} cancelled.")
            return redirect("accounts:asset_management")

        # ── Create asset request ──────────────────────────────────────
        if action == "create_request":
            category_id = request.POST.get("category_id")
            unit_id = request.POST.get("unit_id")
            justification = (request.POST.get("justification") or "").strip()

            category = get_object_or_404(AssetCategory, id=category_id, agency=agency)

            unit = None
            if unit_id:
                unit = get_object_or_404(Unit, id=unit_id, agency=agency)
            else:
                unit = getattr(user, "unit", None)

            req = AssetRequest.objects.create(
                agency=agency,
                requester=user,
                unit=unit,
                category=category,
                justification=justification,
                status="pending_manager" if svc.require_manager_approval else "pending_ict",
            )

            manager_emails = get_manager_emails_for_request(req)
            _notify_local(
                subject=f"Asset Request #{req.id} — Approval Required",
                to_emails=manager_emails,
                html_template="emails/assets/request_submitted.html",
                ctx={"req": req},
            )

            if req.status == "pending_ict":
                ict_emails = get_ict_custodian_emails(req)
                _notify_local(
                    subject=f"Asset Request #{req.id} — Pending ICT Assignment",
                    to_emails=ict_emails,
                    html_template="emails/assets/request_approved.html",
                    ctx={"req": req, "approved_by": "System (Auto)"},
                )

            messages.success(request, f"Asset request #{req.id} submitted.")
            return redirect("accounts:asset_management")

        # ── Manager approve / reject asset request ────────────────────
        if action in ("approve_request", "reject_request"):
            req_id = request.POST.get("request_id")
            req_obj = get_object_or_404(AssetRequest, id=req_id, agency=agency)

            if req_obj.status != "pending_manager":
                messages.info(request, "This request is already processed.")
                return redirect("accounts:asset_management")

            if not (user.is_superuser or req_obj.can_user_approve_as_manager(user)):
                messages.error(request, "You are not allowed to approve this request.")
                return redirect("accounts:asset_management")

            if action == "approve_request":
                req_obj.approve(user)
                _notify_local(
                    subject=f"Asset Request #{req_obj.id} — Approved",
                    to_emails=[getattr(req_obj.requester, "email", None)],
                    html_template="emails/assets/request_approved.html",
                    ctx={"req": req_obj, "approved_by": user.get_full_name() or user.username},
                )
                _notify_local(
                    subject=f"Asset Request #{req_obj.id} — Pending ICT Assignment",
                    to_emails=get_ict_custodian_emails(req_obj),
                    html_template="emails/assets/request_approved.html",
                    ctx={"req": req_obj, "approved_by": user.get_full_name() or user.username},
                )
                messages.success(request, f"Request #{req_obj.id} approved.")
                return redirect("accounts:asset_management")

            reason = (request.POST.get("reason") or "").strip()
            if not reason:
                messages.error(request, "Please provide a rejection reason.")
                return redirect("accounts:asset_management")

            req_obj.reject(user, reason=reason)
            _notify_local(
                subject=f"Asset Request #{req_obj.id} — Rejected",
                to_emails=[getattr(req_obj.requester, "email", None)],
                html_template="emails/assets/request_rejected.html",
                ctx={"req": req_obj, "rejected_by": user.get_full_name() or user.username},
            )
            messages.warning(request, f"Request #{req_obj.id} rejected.")
            return redirect("accounts:asset_management")

        # ── ICT register asset ────────────────────────────────────────
        if action == "register_asset":
            if not is_ict:
                messages.error(request, "Only ICT custodian can register assets.")
                return redirect("accounts:asset_management")

            name = (request.POST.get("name") or "").strip()
            serial_number = (request.POST.get("serial_number") or "").strip() or None
            asset_tag = (request.POST.get("asset_tag") or "").strip() or None
            auto_tag = (request.POST.get("auto_tag") or "").strip() == "1"
            category_id = request.POST.get("category_id")
            unit_id = request.POST.get("unit_id")
            acquired_at = request.POST.get("acquired_at") or None

            if not name or not category_id:
                messages.error(request, "Name and category are required.")
                return redirect("accounts:asset_management")

            category = get_object_or_404(AssetCategory, id=category_id, agency=agency)
            unit = get_object_or_404(Unit, id=unit_id, agency=agency) if unit_id else None

            svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)

            if (not asset_tag and svc.asset_tag_auto_generate) or (auto_tag and svc.asset_tag_auto_generate):
                asset_tag = generate_unique_asset_tag(
                    agency=agency,
                    prefix=svc.asset_tag_prefix,
                    length=svc.asset_tag_length,
                    AssetModel=Asset,
                )
                tag_generated = True
            else:
                tag_generated = False

            asset = Asset.objects.create(
                agency=agency,
                category=category,
                unit=unit,
                name=name,
                serial_number=serial_number,
                asset_tag=asset_tag,
                tag_generated=tag_generated,
                status="available",
                acquired_at=acquired_at,
            )

            payload = build_qr_payload(request, asset, include_url=svc.asset_qr_include_url)
            logo_path = getattr(getattr(agency, "logo", None), "path", None) if getattr(agency, "logo", None) else None
            qr_img = generate_qr_image(payload, agency_logo_path=logo_path)
            asset.qr_payload = payload
            save_qr_to_asset(asset, qr_img, filename_prefix="assetqr")
            asset.save(update_fields=["qr_code", "qr_payload"])

            _log_event(agency, asset, user, "registered", note="Asset registered into pool (tag + QR created).")
            messages.success(request, "Asset registered successfully (tag + QR generated).")
            return redirect("accounts:asset_management")

        # ── Register Mobile Line ──────────────────────────────────────
        if action == "register_mobile_line":
            if not is_ict:
                messages.error(request, "Only ICT custodian can register mobile lines.")
                return redirect("accounts:asset_management")

            line_type = (request.POST.get("line_type") or "").strip()
            provider = (request.POST.get("provider") or "").strip()
            msisdn = (request.POST.get("msisdn") or "").strip()
            sim_serial = (request.POST.get("sim_serial") or "").strip()
            notes = (request.POST.get("notes") or "").strip()

            valid_types = {"sim", "data", "sim_data"}
            if line_type not in valid_types:
                messages.error(request, "Please select a valid line type (SIM, Data, or SIM + Data).")
                return redirect("accounts:asset_management")

            if not msisdn:
                messages.error(request, "Phone number (MSISDN) is required.")
                return redirect("accounts:asset_management")

            msisdn = msisdn.replace(" ", "")

            if MobileLine.objects.filter(msisdn=msisdn).exists():
                messages.warning(request, f"This line ({msisdn}) is already registered in the system.")
                return redirect("accounts:asset_management")

            try:
                MobileLine.objects.create(
                    agency=agency,
                    line_type=line_type,
                    provider=provider,
                    msisdn=msisdn,
                    sim_serial=sim_serial,
                    custodian=user,
                    status="available",
                    notes=notes,
                )
            except IntegrityError:
                messages.warning(request, f"This line ({msisdn}) is already registered in the system.")
                return redirect("accounts:asset_management")

            messages.success(request, f"Mobile line registered: {msisdn}")
            return redirect("accounts:asset_management")

        # ── Request mobile line reactivation ──────────────────────────
        if action == "request_reactivate_line":
            line_id = request.POST.get("line_id")
            reason = (request.POST.get("reason") or "").strip()

            if not (is_ict or is_ops or getattr(user, "role", "") == "registry"):
                messages.error(request, "You are not allowed to request reactivation.")
                return redirect("accounts:asset_management")

            line = get_object_or_404(MobileLine, agency=agency, id=line_id)

            if line.status != "suspended":
                messages.info(request, "Only suspended lines can be reactivated.")
                return redirect("accounts:asset_management")

            if MobileLineReactivationRequest.objects.filter(
                    agency=agency, line=line, status="pending_ops"
            ).exists():
                messages.warning(request, "A reactivation request is already pending approval.")
                return redirect("accounts:asset_management")

            rr = MobileLineReactivationRequest.objects.create(
                agency=agency,
                line=line,
                requested_by=user,
                reason=reason,
                status="pending_ops",
            )

            roles_obj = getattr(agency, "asset_roles", None)
            ops = roles_obj.operations_manager if roles_obj and roles_obj.operations_manager else None
            if ops and ops.email:
                send_email_async(
                    subject=f"Approval Needed: Reactivate Mobile Line {line.msisdn}",
                    to_emails=[ops.email],
                    html_template="emails/mobile_lines/reactivation_requested.html",
                    context={"agency": agency, "line": line, "req": rr, "portal_url": portal_url},
                )

            messages.success(request, f"Reactivation request submitted for {line.msisdn} (pending Ops approval).")
            return redirect("accounts:asset_management")

        # ── Assign mobile line ────────────────────────────────────────
        if action == "assign_mobile_line":
            line_id = request.POST.get("line_id")
            assignee_id = request.POST.get("assignee_id")

            if not (is_ict or is_ops):
                messages.error(request, "Only ICT or Asset Manager can assign mobile lines.")
                return redirect("accounts:asset_management")

            line = get_object_or_404(MobileLine, agency=agency, id=line_id)

            if line.status != "available":
                messages.warning(request, "This line is not available for assignment.")
                return redirect("accounts:asset_management")

            assignee = get_object_or_404(User, id=assignee_id)
            if getattr(assignee, "agency_id", None) != agency.id:
                messages.error(request, "You can only assign lines to users in your agency.")
                return redirect("accounts:asset_management")

            line.assigned_to = assignee
            line.status = "assigned"
            line.issued_at = timezone.now()
            line.save(update_fields=["assigned_to", "status", "issued_at"])

            messages.success(request, f"Line {line.msisdn} assigned to {assignee.get_full_name() or assignee.username}.")
            return redirect("accounts:asset_management")

        # ── Ops approve reactivation ──────────────────────────────────
        if action == "approve_reactivate_line":
            req_id = request.POST.get("reactivation_request_id")
            note = (request.POST.get("manager_note") or "").strip()

            roles_obj = getattr(agency, "asset_roles", None)
            ops_id = roles_obj.operations_manager_id if roles_obj else None
            if not (user.is_superuser or (ops_id and user.id == ops_id)):
                messages.error(request, "Only the Operations Manager can approve reactivations.")
                return redirect("accounts:asset_management")

            rr = get_object_or_404(MobileLineReactivationRequest, id=req_id, agency=agency)

            if rr.status != "pending_ops":
                messages.info(request, "This reactivation request is already processed.")
                return redirect("accounts:asset_management")

            line = rr.line
            if line.status != "suspended":
                messages.warning(request, "Line is no longer suspended; nothing to approve.")
                rr.status = "cancelled"
                rr.save(update_fields=["status"])
                return redirect("accounts:asset_management")

            rr.approve(user, note=note)
            line.reactivate()

            provider_focals, ops_email, registry_emails = _get_line_suspend_recipients(agency)
            to_emails = list(dict.fromkeys(
                [e for e in provider_focals + registry_emails +
                 ([rr.requested_by.email] if rr.requested_by and rr.requested_by.email else [])
                 if e]
            ))

            if to_emails:
                send_email_async(
                    subject=f"Approved: Mobile Line Reactivated {line.msisdn}",
                    to_emails=to_emails,
                    html_template="emails/mobile_lines/reactivation_approved.html",
                    context={"agency": agency, "line": line, "req": rr, "approved_by": user, "portal_url": portal_url},
                )

            messages.success(request, f"Approved and reactivated: {line.msisdn}")
            return redirect("accounts:asset_management")

        # ── ICT assign asset to request ───────────────────────────────
        if action == "assign_asset":
            req_id = request.POST.get("request_id")
            asset_id = request.POST.get("asset_id")

            req_obj = get_object_or_404(AssetRequest, id=req_id, agency=agency)
            asset = get_object_or_404(Asset, id=asset_id, agency=agency)

            if req_obj.status != "pending_ict":
                messages.info(request, "This request is not pending ICT assignment.")
                return redirect("accounts:asset_management")

            if not is_ict:
                messages.error(request, "Only ICT custodian can assign assets.")
                return redirect("accounts:asset_management")

            if asset.status != "available":
                messages.error(request, "Selected asset is not available.")
                return redirect("accounts:asset_management")

            if asset.category_id != req_obj.category_id:
                messages.error(request, "Asset category does not match the request category.")
                return redirect("accounts:asset_management")

            req_obj.assign_asset(user, asset)
            _log_event(agency, asset, user, "assigned", note=f"Assigned to {req_obj.requester}", meta={"request_id": req_obj.id})

            _notify_local(
                subject=f"Asset Request #{req_obj.id} — Asset Assigned",
                to_emails=[getattr(req_obj.requester, "email", None)],
                html_template="emails/assets/asset_assigned.html",
                ctx={"req": req_obj, "asset": asset, "assigned_by": user.get_full_name() or user.username},
            )

            messages.success(request, f"Asset assigned for request #{req_obj.id}.")
            return redirect("accounts:asset_management")

        # ── Suspend mobile line ───────────────────────────────────────
        if action == "suspend_mobile_line":
            line_id = request.POST.get("line_id")
            reason = (request.POST.get("reason") or "").strip()

            if not (is_ict or is_ops):
                messages.error(request, "Only ICT or Operations Manager can suspend mobile lines.")
                return redirect("accounts:asset_management")

            line = get_object_or_404(MobileLine, agency=agency, id=line_id)

            if line.status == "suspended":
                messages.info(request, "This line is already suspended.")
                return redirect("accounts:asset_management")

            if line.status == "retired":
                messages.warning(request, "Retired lines cannot be suspended.")
                return redirect("accounts:asset_management")

            line.suspend()

            if reason:
                line.notes = (line.notes or "").strip()
                stamp = f"\n\n[SUSPENDED {timezone.now():%Y-%m-%d %H:%M}] by {user.get_full_name() or user.username}: {reason}"
                line.notes = (line.notes + stamp).strip()
                line.save(update_fields=["notes"])

            provider_focals, ops_email, registry_emails = _get_line_suspend_recipients(agency)
            to_emails = list(dict.fromkeys(
                [e for e in provider_focals + ([ops_email] if ops_email else []) + registry_emails if e]
            ))

            if to_emails:
                send_email_async(
                    subject=f"Mobile Line Suspended: {line.msisdn}",
                    to_emails=to_emails,
                    html_template="emails/mobile_lines/line_suspended.html",
                    context={
                        "agency": agency, "line": line, "reason": reason,
                        "suspended_by": user, "portal_url": portal_url, "when": timezone.now(),
                    },
                )

            messages.success(request, f"Line suspended: {line.msisdn}")
            return redirect("accounts:asset_management")

        # ── Requester verifies receipt ─────────────────────────────────
        if action == "verify_receipt":
            req_id = request.POST.get("request_id")
            req_obj = get_object_or_404(AssetRequest, id=req_id, agency=agency)

            if req_obj.requester_id != user.id:
                messages.error(request, "You can only verify your own request.")
                return redirect("accounts:asset_management")

            if req_obj.status != "assigned":
                messages.info(request, "This request is not ready for verification.")
                return redirect("accounts:asset_management")

            req_obj.verify_receipt(user)

            if req_obj.assigned_asset:
                _log_event(agency, req_obj.assigned_asset, user, "receipt_verified",
                           note="Requester verified receipt.", meta={"request_id": req_obj.id})

            _notify_local(
                subject=f"Asset Request #{req_obj.id} — Receipt Verified",
                to_emails=get_ict_custodian_emails(req_obj),
                html_template="emails/assets/receipt_verified.html",
                ctx={"req": req_obj, "asset": req_obj.assigned_asset},
            )

            messages.success(request, f"Receipt verified for request #{req_obj.id}.")
            return redirect("accounts:asset_management")

        # ── Initiate asset return ─────────────────────────────────────
        if action == "initiate_return":
            asset_id = request.POST.get("asset_id")
            reason = (request.POST.get("reason") or "").strip()
            asset = get_object_or_404(Asset, id=asset_id, agency=agency)

            if asset.current_holder_id != user.id and not user.is_superuser:
                messages.error(request, "You can only return an asset assigned to you.")
                return redirect("accounts:asset_management")

            if asset.status != "assigned":
                messages.error(request, "Only assigned assets can be returned.")
                return redirect("accounts:asset_management")

            if AssetReturnRequest.objects.filter(agency=agency, asset=asset, status="pending_ict").exists():
                messages.info(request, "Return already submitted and pending ICT verification.")
                return redirect("accounts:asset_management")

            rr = AssetReturnRequest.objects.create(
                agency=agency, asset=asset, requested_by=user,
                reason=reason, status="pending_ict",
            )
            _log_event(agency, asset, user, "return_initiated",
                       note=reason or "Return initiated.", meta={"return_id": rr.id})

            ict_emails = get_ict_custodian_emails(None)
            if not ict_emails:
                ict_emails = list(roles.ict_custodian.values_list("email", flat=True))

            _notify_local(
                subject=f"Asset Return #{rr.id} — Pending ICT Verification",
                to_emails=ict_emails,
                html_template="emails/assets/return_initiated.html",
                ctx={"rr": rr, "asset": asset},
            )

            messages.success(request, f"Return request #{rr.id} submitted to ICT.")
            return redirect("accounts:asset_management")

        # ── Cancel return ─────────────────────────────────────────────
        if action == "cancel_return":
            rr_id = request.POST.get("return_id")
            rr = get_object_or_404(AssetReturnRequest, id=rr_id, agency=agency)

            if rr.requested_by_id != user.id and not user.is_superuser:
                messages.error(request, "You can only cancel your own return request.")
                return redirect("accounts:asset_management")

            if rr.status != "pending_ict":
                messages.info(request, "This return request can't be cancelled anymore.")
                return redirect("accounts:asset_management")

            rr.status = "cancelled"
            rr.save(update_fields=["status"])

            _log_event(agency, rr.asset, user, "return_cancelled",
                       note="Return request cancelled.", meta={"return_id": rr.id})

            messages.success(request, f"Return request #{rr.id} cancelled.")
            return redirect("accounts:asset_management")

        # ── ICT verifies return received ──────────────────────────────
        if action == "verify_return_received":
            rr_id = request.POST.get("return_id")
            rr = get_object_or_404(AssetReturnRequest, id=rr_id, agency=agency)

            if not is_ict:
                messages.error(request, "Only ICT can verify returns.")
                return redirect("accounts:asset_management")

            if rr.status != "pending_ict":
                messages.info(request, "This return request is already processed.")
                return redirect("accounts:asset_management")

            exit_user = rr.requested_by
            asset = rr.asset
            asset.status = "available"
            asset.current_holder = None
            asset.save(update_fields=["status", "current_holder"])

            rr.status = "received"
            rr.verified_by = user
            rr.verified_at = timezone.now()
            rr.save(update_fields=["status", "verified_by", "verified_at"])

            still_pending = AssetReturnRequest.objects.filter(
                agency=agency, requested_by=exit_user, status="pending_ict",
            ).exists()

            if not still_pending:
                ExitRequest.objects.filter(
                    agency=agency, user=exit_user,
                    status__in=["pending_returns", "pending_ict_confirmation", "submitted"]
                ).update(status="cleared", cleared_at=timezone.now())

            _log_event(agency, asset, user, "return_received",
                       note="ICT verified return and placed asset back to pool.",
                       meta={"return_id": rr.id})

            _notify_local(
                subject=f"Asset Return #{rr.id} — Received by ICT",
                to_emails=[getattr(exit_user, "email", None)],
                html_template="emails/assets/return_received.html",
                ctx={"rr": rr, "asset": asset},
            )

            if not still_pending:
                messages.success(request, "Return verified. All pending returns completed — exit request cleared.")
            else:
                messages.success(request, "Return verified. Asset is now back in the pool.")
            return redirect("accounts:asset_management")

        # ── ICT retire asset ──────────────────────────────────────────
        if action == "retire_asset":
            if not is_ict:
                messages.error(request, "Only ICT can retire/dispose assets.")
                return redirect("accounts:asset_management")

            asset_id = request.POST.get("asset_id")
            note = (request.POST.get("note") or "").strip()
            asset = get_object_or_404(Asset, id=asset_id, agency=agency)

            asset.status = "retired"
            asset.current_holder = None
            asset.save(update_fields=["status", "current_holder"])

            _log_event(agency, asset, user, "retired", note=note or "Asset retired/disposed.")
            messages.success(request, "Asset marked as retired/disposed.")
            return redirect("accounts:asset_management")

        # ── Manager approve / reject change request ───────────────────
        if action in ("approve_change_request", "reject_change_request"):
            cr_id = (request.POST.get("change_request_id") or "").strip()
            cr = get_object_or_404(AssetChangeRequest, id=cr_id, agency=agency)
            asset = cr.asset

            if cr.status != "pending_manager":
                messages.info(request, "This change request is already processed.")
                return redirect("accounts:asset_management")

            if not (user.is_superuser or can_user_approve_asset_change(user, asset, roles)):
                messages.error(request, "You are not allowed to approve changes for this asset.")
                return redirect("accounts:asset_management")

            manager_note = (request.POST.get("manager_note") or "").strip()

            if action == "reject_change_request":
                cr.reject(user, note=manager_note)
                _log_event(agency, asset, user, "status_change",
                           note=f"Change request #{cr.id} rejected.",
                           meta={"change_request_id": cr.id})
                messages.warning(request, f"Change request #{cr.id} rejected.")
                return redirect("accounts:asset_management")

            proposed = cr.proposed_changes or {}
            with transaction.atomic():
                if "category_id" in proposed:
                    asset.category_id = int(proposed["category_id"])
                if "unit_id" in proposed:
                    asset.unit_id = int(proposed["unit_id"])
                if "name" in proposed:
                    asset.name = proposed["name"]
                if "status" in proposed:
                    asset.status = proposed["status"]
                if "serial_number" in proposed:
                    asset.serial_number = proposed["serial_number"]
                if "asset_tag" in proposed:
                    asset.asset_tag = proposed["asset_tag"]
                if "acquired_at" in proposed:
                    try:
                        asset.acquired_at = timezone.datetime.strptime(
                            proposed["acquired_at"], "%Y-%m-%d"
                        ).date()
                    except Exception:
                        pass
                asset.save()
                cr.approve(user, note=manager_note)
                _log_event(agency, asset, user, "status_change",
                           note=f"Change request #{cr.id} approved and applied.",
                           meta={"change_request_id": cr.id, "applied": proposed})

            messages.success(request, f"Change request #{cr.id} approved and applied.")
            return redirect("accounts:asset_management")

        # ── Consumable: Request supplies ──────────────────────────────
        if action == "create_consumable_request":
            notes = (request.POST.get("notes") or "").strip()

            linked_asset_id = (request.POST.get("linked_asset_id") or "").strip()
            linked_asset = None
            if linked_asset_id:
                try:
                    linked_asset = Asset.objects.get(id=linked_asset_id, agency=agency)
                except (ValueError, Asset.DoesNotExist):
                    linked_asset = None

            item_ids = request.POST.getlist("consumable_item_id")
            quantities = request.POST.getlist("consumable_qty")

            if not item_ids:
                messages.error(request, "Please select at least one item.")
                return redirect("accounts:asset_management")

            line_data = []
            errors = []
            for item_id, qty_str in zip(item_ids, quantities):
                try:
                    qty = int(qty_str)
                    item = ConsumableItem.objects.get(id=item_id, agency=agency, is_active=True)
                except (ValueError, ConsumableItem.DoesNotExist):
                    errors.append(f"Invalid item or quantity (id={item_id}).")
                    continue
                if qty <= 0:
                    errors.append(f"Quantity for {item.name} must be positive.")
                    continue
                if item.max_per_request and qty > item.max_per_request:
                    errors.append(f"{item.name}: max {item.max_per_request} per request.")
                    continue
                if item.is_out_of_stock:
                    errors.append(f"{item.name} is currently out of stock.")
                    continue
                line_data.append((item, qty))

            if errors:
                for e in errors:
                    messages.error(request, e)
                return redirect("accounts:asset_management")

            if not line_data:
                messages.error(request, "No valid items in your request.")
                return redirect("accounts:asset_management")

            with transaction.atomic():
                creq = ConsumableRequest.objects.create(
                    agency=agency,
                    requester=user,
                    unit=getattr(user, "unit", None),
                    notes=notes,
                    linked_asset=linked_asset,
                    status="pending",
                )
                for item, qty in line_data:
                    ConsumableRequestItem.objects.create(
                        request=creq,
                        item=item,
                        quantity_requested=qty,
                    )

            approval_emails = []
            if roles:
                if roles.operations_manager and roles.operations_manager.email:
                    approval_emails.append(roles.operations_manager.email)
            requester_unit = getattr(user, "unit", None)
            if requester_unit and requester_unit.unit_head and requester_unit.unit_head.email:
                if requester_unit.unit_head.email not in approval_emails:
                    approval_emails.append(requester_unit.unit_head.email)

            _notify_local(
                subject=f"Supply Request #{creq.id} — Pending Approval",
                to_emails=approval_emails,
                html_template="emails/consumables/request_submitted.html",
                ctx={"creq": creq},
            )

            messages.success(request, f"Supply request #{creq.id} submitted for approval.")
            return redirect("accounts:asset_management")

        # ── Consumable: Approve / reject ──────────────────────────────
        if action in ("approve_consumable_request", "reject_consumable_request"):
            if not is_manager:
                messages.error(request, "Only managers can approve supply requests.")
                return redirect("accounts:asset_management")

            creq_id = (request.POST.get("consumable_request_id") or "").strip()
            creq = get_object_or_404(ConsumableRequest, id=creq_id, agency=agency)

            if creq.status != "pending":
                messages.info(request, "This supply request has already been processed.")
                return redirect("accounts:asset_management")

            if action == "reject_consumable_request":
                reason = (request.POST.get("reason") or "").strip()
                if not reason:
                    messages.error(request, "Please provide a rejection reason.")
                    return redirect("accounts:asset_management")
                creq.reject(user, reason=reason)
                _notify_local(
                    subject=f"Supply Request #{creq.id} — Rejected",
                    to_emails=[getattr(creq.requester, "email", None)],
                    html_template="emails/consumables/request_rejected.html",
                    ctx={"creq": creq, "rejected_by": user.get_full_name() or user.username},
                )
                messages.warning(request, f"Supply request #{creq.id} rejected.")
            else:
                creq.approve(user)
                _notify_local(
                    subject=f"Supply Request #{creq.id} — Approved",
                    to_emails=[getattr(creq.requester, "email", None)],
                    html_template="emails/consumables/request_approved.html",
                    ctx={"creq": creq, "approved_by": user.get_full_name() or user.username},
                )
                ict_dispatch_emails = list(roles.ict_custodian.values_list("email", flat=True)) if roles else []
                _notify_local(
                    subject=f"Supply Request #{creq.id} — Ready for Dispatch",
                    to_emails=ict_dispatch_emails,
                    html_template="emails/consumables/request_submitted.html",
                    ctx={"creq": creq},
                )
                messages.success(request, f"Supply request #{creq.id} approved — awaiting dispatch.")

            return redirect("accounts:asset_management")

        # ── Consumable: Cancel ────────────────────────────────────────
        if action == "cancel_consumable_request":
            creq_id = (request.POST.get("consumable_request_id") or "").strip()
            creq = get_object_or_404(ConsumableRequest, id=creq_id, agency=agency)

            if creq.requester_id != user.id and not user.is_superuser:
                messages.error(request, "You can only cancel your own supply requests.")
                return redirect("accounts:asset_management")

            if creq.status not in ("pending", "approved"):
                messages.info(request, "This request cannot be cancelled at this stage.")
                return redirect("accounts:asset_management")

            creq.status = "cancelled"
            creq.save(update_fields=["status"])
            messages.success(request, f"Supply request #{creq.id} cancelled.")
            return redirect("accounts:asset_management")

        # ── Consumable: Dispatch ──────────────────────────────────────
        if action == "dispatch_consumable":
            if not (is_ict or is_ops):
                messages.error(request, "Only ICT or Operations Manager can dispatch supplies.")
                return redirect("accounts:asset_management")

            creq_id = (request.POST.get("consumable_request_id") or "").strip()
            creq = get_object_or_404(ConsumableRequest, id=creq_id, agency=agency)

            if creq.status not in ("approved", "partially_fulfilled"):
                messages.info(request, "Only approved requests can be dispatched.")
                return redirect("accounts:asset_management")

            dispatch_note = (request.POST.get("dispatch_note") or "").strip()
            line_item_ids = request.POST.getlist("line_item_id")
            dispatch_qtys = request.POST.getlist("dispatch_qty")

            with transaction.atomic():
                all_fulfilled = True
                any_dispatched = False

                for li_id, qty_str in zip(line_item_ids, dispatch_qtys):
                    try:
                        qty = int(qty_str)
                        line = ConsumableRequestItem.objects.select_for_update().get(
                            id=li_id, request=creq
                        )
                    except (ValueError, ConsumableRequestItem.DoesNotExist):
                        continue

                    if qty <= 0 or line.is_fulfilled:
                        if not line.is_fulfilled:
                            all_fulfilled = False
                        continue

                    qty = min(qty, line.remaining)

                    cons_item = ConsumableItem.objects.select_for_update().get(id=line.item_id)

                    if cons_item.stock_qty < qty:
                        messages.warning(request, f"Not enough stock for {cons_item.name}. Available: {cons_item.stock_qty}")
                        qty = cons_item.stock_qty

                    if qty <= 0:
                        all_fulfilled = False
                        continue

                    before = cons_item.stock_qty
                    cons_item.stock_qty -= qty
                    cons_item.save(update_fields=["stock_qty"])

                    line.quantity_dispatched += qty
                    line.save(update_fields=["quantity_dispatched"])

                    ConsumableStockLog.objects.create(
                        agency=agency,
                        item=cons_item,
                        event="dispatched",
                        quantity_before=before,
                        quantity_change=-qty,
                        quantity_after=cons_item.stock_qty,
                        reference_request=creq,
                        note=dispatch_note or f"Dispatched for request #{creq.id}",
                        actor=user,
                    )

                    any_dispatched = True

                    if not line.is_fulfilled:
                        all_fulfilled = False

                if not any_dispatched:
                    messages.warning(request, "Nothing was dispatched. Check stock levels.")
                    return redirect("accounts:asset_management")

                lines_all = list(creq.line_items.all())
                all_done = all(l.is_fulfilled for l in lines_all)
                creq.status = "fulfilled" if all_done else "partially_fulfilled"
                creq.save(update_fields=["status"])

            _notify_local(
                subject=f"Your Supply Request #{creq.id} has been dispatched",
                to_emails=[getattr(creq.requester, "email", None)],
                html_template="emails/consumables/dispatched.html",
                ctx={
                    "creq": creq,
                    "dispatched_by": user.get_full_name() or user.username,
                    "dispatched_at": timezone.now(),
                    "dispatch_note": dispatch_note,
                },
            )

            dispatched_item_ids = [li.item_id for li in creq.line_items.all()]
            now_low = [
                i for i in ConsumableItem.objects.filter(
                    agency=agency, id__in=dispatched_item_ids, is_active=True,
                )
                if i.is_low_stock
            ]
            if now_low:
                ict_alert_emails = list(roles.ict_custodian.values_list("email", flat=True)) if roles else []
                if is_ops and user.email and user.email not in ict_alert_emails:
                    ict_alert_emails.append(user.email)
                _notify_local(
                    subject=f"[{agency}] Low Stock Alert — {len(now_low)} item(s) need restocking",
                    to_emails=ict_alert_emails,
                    html_template="emails/consumables/low_stock_alert.html",
                    ctx={"creq": creq, "low_stock_items": now_low},
                )

            messages.success(request, f"Supply request #{creq.id} dispatched successfully.")
            return redirect("accounts:asset_management")

        # ── Consumable: Restock item ──────────────────────────────────
        if action == "restock_consumable_item":
            if not (is_ict or is_ops):
                messages.error(request, "Only ICT or Operations Manager can restock supplies.")
                return redirect("accounts:asset_management")

            item_id = (request.POST.get("consumable_item_id") or "").strip()
            qty_str = (request.POST.get("restock_qty") or "0").strip()
            note = (request.POST.get("restock_note") or "").strip()

            try:
                qty = int(qty_str)
                item = ConsumableItem.objects.get(id=item_id, agency=agency)
            except (ValueError, ConsumableItem.DoesNotExist):
                messages.error(request, "Invalid item or quantity.")
                return redirect("accounts:asset_management")

            if qty <= 0:
                messages.error(request, "Restock quantity must be a positive number.")
                return redirect("accounts:asset_management")

            with transaction.atomic():
                item_locked = ConsumableItem.objects.select_for_update().get(id=item.id)
                before = item_locked.stock_qty
                item_locked.stock_qty += qty
                item_locked.save(update_fields=["stock_qty"])

                ConsumableStockLog.objects.create(
                    agency=agency, item=item_locked, event="restocked",
                    quantity_before=before, quantity_change=qty,
                    quantity_after=item_locked.stock_qty,
                    note=note or f"Restocked by {user.get_full_name() or user.username}",
                    actor=user,
                )

            messages.success(request, f"Restocked {item.name}: +{qty} {item.unit_of_measure}.")
            return redirect("accounts:asset_management")

        # ── Consumable: Register new item ─────────────────────────────
        if action == "register_consumable_item":
            if not (is_ict or is_ops):
                messages.error(request, "Only ICT or Operations Manager can register supply items.")
                return redirect("accounts:asset_management")

            cat_id = (request.POST.get("consumable_cat_id") or "").strip()
            name = (request.POST.get("consumable_item_name") or "").strip()
            unit_of_measure = (request.POST.get("unit_of_measure") or "piece").strip()
            initial_stock = int(request.POST.get("initial_stock") or 0)
            low_threshold = int(request.POST.get("low_stock_threshold") or 5)
            max_per_req = request.POST.get("max_per_request") or None
            description = (request.POST.get("item_description") or "").strip()

            if not name or not cat_id:
                messages.error(request, "Item name and category are required.")
                return redirect("accounts:asset_management")

            cat = get_object_or_404(ConsumableCategory, id=cat_id, agency=agency)

            item = ConsumableItem.objects.create(
                agency=agency,
                category=cat,
                name=name,
                description=description,
                unit_of_measure=unit_of_measure,
                stock_qty=initial_stock,
                low_stock_threshold=low_threshold,
                max_per_request=int(max_per_req) if max_per_req else None,
            )

            if initial_stock > 0:
                ConsumableStockLog.objects.create(
                    agency=agency, item=item, event="restocked",
                    quantity_before=0, quantity_change=initial_stock,
                    quantity_after=initial_stock,
                    note="Initial stock on registration.", actor=user,
                )

            messages.success(request, f"Supply item '{item.name}' registered with {initial_stock} in stock.")
            return redirect("accounts:asset_management")

        # ── Consumable: Register new category ─────────────────────────
        if action == "register_consumable_category":
            if not (is_ict or is_ops):
                messages.error(request, "Only ICT or Operations Manager can add supply categories.")
                return redirect("accounts:asset_management")

            cat_name = (request.POST.get("consumable_cat_name") or "").strip()
            cat_type = (request.POST.get("consumable_cat_type") or "other").strip()
            cat_desc = (request.POST.get("consumable_cat_desc") or "").strip()

            if not cat_name:
                messages.error(request, "Category name is required.")
                return redirect("accounts:asset_management")

            ConsumableCategory.objects.get_or_create(
                agency=agency,
                name=cat_name,
                defaults={"category_type": cat_type, "description": cat_desc},
            )
            messages.success(request, f"Category '{cat_name}' added.")
            return redirect("accounts:asset_management")

        # ── Consumable: Link item to asset ────────────────────────────
        if action == "link_consumable_to_asset":
            if not (is_ict or is_ops):
                messages.error(request, "Only ICT or Operations Manager can manage asset links.")
                return redirect("accounts:asset_management")

            item_id = (request.POST.get("consumable_item_id") or "").strip()
            asset_id = (request.POST.get("asset_id") or "").strip()
            cat_id = (request.POST.get("asset_category_id") or "").strip()
            note = (request.POST.get("link_note") or "").strip()[:255]

            try:
                cons_item = ConsumableItem.objects.get(id=item_id, agency=agency)
            except (ValueError, ConsumableItem.DoesNotExist):
                messages.error(request, "Consumable item not found.")
                return redirect("accounts:asset_management")

            linked_asset_obj = None
            linked_cat_obj = None

            if asset_id:
                try:
                    linked_asset_obj = Asset.objects.get(id=asset_id, agency=agency)
                except (ValueError, Asset.DoesNotExist):
                    messages.error(request, "Asset not found.")
                    return redirect("accounts:asset_management")
            if cat_id:
                try:
                    linked_cat_obj = AssetCategory.objects.get(id=cat_id, agency=agency)
                except (ValueError, AssetCategory.DoesNotExist):
                    pass

            if not linked_asset_obj and not linked_cat_obj:
                messages.error(request, "Select an asset or an asset category to link.")
                return redirect("accounts:asset_management")

            obj, created = ConsumableAssetLink.objects.get_or_create(
                consumable_item=cons_item,
                asset=linked_asset_obj,
                defaults={
                    "agency": agency,
                    "asset_category": linked_cat_obj,
                    "note": note,
                    "created_by": user,
                },
            )
            if not created:
                obj.note = note
                obj.save(update_fields=["note"])

            messages.success(
                request,
                f"{'Linked' if created else 'Updated:'} {cons_item.name} ↔ {linked_asset_obj or linked_cat_obj}."
            )
            return redirect("accounts:asset_management")

        # ── Consumable: Unlink from asset ─────────────────────────────
        if action == "unlink_consumable_from_asset":
            if not (is_ict or is_ops):
                messages.error(request, "Only ICT or Operations Manager can manage asset links.")
                return redirect("accounts:asset_management")

            link_id = (request.POST.get("link_id") or "").strip()
            try:
                link = ConsumableAssetLink.objects.get(id=link_id, agency=agency)
                name = str(link)
                link.delete()
                messages.success(request, f"Removed link: {name}.")
            except (ValueError, ConsumableAssetLink.DoesNotExist):
                messages.error(request, "Link not found.")
            return redirect("accounts:asset_management")

        messages.error(request, "Unknown action.")
        return redirect("accounts:asset_management")

    # ------------------------------------------------------------------
    # GET — assemble context
    # ------------------------------------------------------------------
    pending_change_approvals = []
    if is_manager:
        cr_qs = AssetChangeRequest.objects.filter(
            agency=agency, status="pending_manager",
        ).select_related("asset", "requested_by", "asset__unit", "asset__category").order_by("-created_at")
        pending_change_approvals = [
            cr for cr in cr_qs if can_user_approve_asset_change(user, cr.asset, roles)
        ]

    agency_users = User.objects.filter(
        agency=agency, is_active=True
    ).order_by("first_name", "last_name", "username")

    pending_line_reactivations = MobileLineReactivationRequest.objects.filter(
        agency=agency, status="pending_ops"
    ).select_related("line", "requested_by")

    # ── Consumables / Supplies data ───────────────────────────────────
    consumable_categories = ConsumableCategory.objects.filter(
        agency=agency, is_active=True
    ).order_by("category_type", "name")

    consumable_items_qs = ConsumableItem.objects.filter(
        agency=agency, is_active=True
    ).select_related("category").prefetch_related("asset_links__asset", "asset_links__asset_category").order_by("category__name", "name")

    my_consumable_requests = ConsumableRequest.objects.filter(
        agency=agency, requester=user
    ).select_related(
        "approved_by", "linked_asset", "unit"
    ).prefetch_related("line_items__item").order_by("-created_at")

    pending_consumable_approvals = []
    if is_manager:
        pending_consumable_approvals = list(
            ConsumableRequest.objects.filter(agency=agency, status="pending")
            .select_related("requester", "unit", "linked_asset")
            .prefetch_related("line_items__item")
            .order_by("-created_at")
        )

    approved_consumable_requests = []
    if is_ict or is_ops:
        approved_consumable_requests = list(
            ConsumableRequest.objects.filter(agency=agency, status__in=("approved", "partially_fulfilled"))
            .select_related("requester", "unit", "approved_by", "linked_asset")
            .prefetch_related("line_items__item")
            .order_by("-created_at")
        )

    low_stock_items = []
    if is_ict or is_ops:
        low_stock_items = [i for i in consumable_items_qs if i.is_low_stock]

    consumable_chart_data = (
        _build_consumable_chart_data(consumable_items_qs)
        if (is_ict or is_ops) else "null"
    )

    # All asset-consumable links for the overview modal (ICT / Ops only)
    consumable_asset_links = []
    if is_ict or is_ops:
        consumable_asset_links = list(
            ConsumableAssetLink.objects.filter(agency=agency)
            .select_related("consumable_item", "asset", "asset_category")
            .order_by("consumable_item__name")
        )

    return render(request, "accounts/assets/asset_management.html", {
        "svc": svc,
        "roles": roles,
        "is_ict": is_ict,
        "is_manager": is_manager,
        "is_ops": is_ops,
        "agency_users": agency_users,
        "pending_change_approvals": pending_change_approvals,

        "units": units,
        "categories": categories,

        "assets": assets_visible,
        "eol_assets": eol_assets,
        "mobile_lines_visible": mobile_lines_visible,

        "my_requests": my_requests,
        "pending_line_reactivations": pending_line_reactivations,
        "pending_approvals": pending_approvals,
        "pending_ict": pending_ict,

        "my_returns": my_returns,
        "pending_returns": pending_returns,
        "returning_asset_ids": returning_asset_ids,

        # Consumables / supplies
        "consumable_categories":          consumable_categories,
        "consumable_items":               consumable_items_qs,
        "my_consumable_requests":         my_consumable_requests,
        "pending_consumable_approvals":   pending_consumable_approvals,
        "approved_consumable_requests":   approved_consumable_requests,
        "low_stock_items":                low_stock_items,
        "consumable_chart_data":          consumable_chart_data,
        "consumable_asset_links":         consumable_asset_links,
        "assets_for_link": (
            Asset.objects.filter(agency=agency).order_by("name")
            if (is_ict or is_ops) else []
        ),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Consumables export (CSV or printable HTML report)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def consumables_export(request):
    """
    GET /supplies/export/?format=csv|report&scope=items|requests|all

    format=csv    → raw CSV download
    format=report → printable HTML rendered server-side (open in new tab → Ctrl+P)
    scope         → what to include (default: all)
    """
    user = request.user
    agency = getattr(user, "agency", None)
    if not agency:
        messages.error(request, "Not assigned to an agency.")
        return redirect("accounts:profile")

    fmt = request.GET.get("format", "csv")
    scope = request.GET.get("scope", "all")

    items_qs = (
        ConsumableItem.objects
        .filter(agency=agency, is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )

    from datetime import timedelta
    cutoff = timezone.now() - timedelta(days=90)
    requests_qs = (
        ConsumableRequest.objects
        .filter(agency=agency, created_at__gte=cutoff)
        .select_related("requester", "approved_by", "unit", "linked_asset")
        .prefetch_related("line_items__item")
        .order_by("-created_at")
    )

    # ── CSV ──────────────────────────────────────────────────────────
    if fmt == "csv":
        response = HttpResponse(content_type="text/csv")
        ts = timezone.now().strftime("%Y%m%d_%H%M")
        response["Content-Disposition"] = (
            f'attachment; filename="consumables_export_{ts}.csv"'
        )
        writer = csv.writer(response)

        if scope in ("items", "all"):
            writer.writerow([])
            writer.writerow(["=== STOCK INVENTORY ==="])
            writer.writerow([
                "Item", "Category", "Category Type",
                "Unit of Measure", "In Stock", "Low-Stock Threshold",
                "Status", "Max Per Request",
            ])
            for it in items_qs:
                status = (
                    "Out of Stock" if it.is_out_of_stock else
                    "Low Stock"    if it.is_low_stock    else
                    "OK"
                )
                writer.writerow([
                    it.name,
                    it.category.name,
                    it.category.get_category_type_display(),
                    it.unit_of_measure,
                    it.stock_qty,
                    it.low_stock_threshold,
                    status,
                    it.max_per_request or "No limit",
                ])

        if scope in ("requests", "all"):
            writer.writerow([])
            writer.writerow(["=== SUPPLY REQUESTS (last 90 days) ==="])
            writer.writerow([
                "Request #", "Requester", "Unit", "Status",
                "Linked Asset", "Approved By", "Approved At",
                "Date", "Items",
            ])
            for creq in requests_qs:
                items_summary = " | ".join(
                    f"{li.item.name} ×{li.quantity_requested} (sent {li.quantity_dispatched})"
                    for li in creq.line_items.all()
                )
                linked = str(creq.linked_asset) if getattr(creq, "linked_asset", None) else "—"
                approver = (
                    (creq.approved_by.get_full_name() or creq.approved_by.username)
                    if creq.approved_by else "—"
                )
                writer.writerow([
                    f"#{creq.id}",
                    creq.requester.get_full_name() or creq.requester.username,
                    creq.unit.name if creq.unit else "—",
                    creq.get_status_display(),
                    linked,
                    approver,
                    creq.approved_at.strftime("%d %b %Y %H:%M") if creq.approved_at else "—",
                    creq.created_at.strftime("%d %b %Y"),
                    items_summary,
                ])

        return response

    # ── Printable HTML report ────────────────────────────────────────
    total_items = items_qs.count()
    low_stock_count = sum(1 for i in items_qs if i.is_low_stock)
    out_stock_count = sum(1 for i in items_qs if i.is_out_of_stock)
    total_requests = requests_qs.count()
    fulfilled_reqs = requests_qs.filter(status="fulfilled").count()

    ctx = {
        "agency":          agency,
        "generated_at":    timezone.now(),
        "items":           items_qs,
        "requests":        requests_qs,
        "scope":           scope,
        "total_items":     total_items,
        "low_stock_count": low_stock_count,
        "out_stock_count": out_stock_count,
        "total_requests":  total_requests,
        "fulfilled_reqs":  fulfilled_reqs,
    }
    html = render_to_string("accounts/assets/consumables_report.html", ctx, request)
    return HttpResponse(html)


# ─────────────────────────────────────────────────────────────────────────────
# Asset detail
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def asset_detail(request, asset_id: int):
    user = request.user
    agency = getattr(user, "agency", None)
    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return redirect("accounts:profile")

    svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
    if not svc.asset_mgmt_enabled and not user.is_superuser:
        messages.warning(request, "Asset Management is not enabled for your agency.")
        return redirect("accounts:profile")

    roles, _ = AgencyAssetRoles.objects.get_or_create(agency=agency)

    asset = get_object_or_404(
        Asset.objects.select_related("category", "unit", "current_holder"),
        id=asset_id, agency=agency
    )

    if not _can_user_manage_asset(user, agency, asset):
        messages.error(request, "You are not allowed to view this asset.")
        return redirect("accounts:asset_management")

    is_ict = user.is_superuser or _is_ict(user, agency)
    can_approve_changes = _can_user_approve_change(user, agency, asset)

    units = Unit.objects.filter(agency=agency).select_related("unit_head").prefetch_related("asset_managers")
    categories = AssetCategory.objects.filter(agency=agency).order_by("name")

    active_return = AssetReturnRequest.objects.filter(
        agency=agency, asset=asset, status__in=["pending_ict", "in_transit"]
    ).select_related("requested_by", "verified_by").first()

    change_requests = AssetChangeRequest.objects.filter(
        agency=agency, asset=asset
    ).select_related("requested_by", "decided_by").order_by("-created_at")[:30]
    pending_changes = [cr for cr in change_requests if cr.status == "pending_manager"]

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "initiate_return":
            posted_asset_id = str(request.POST.get("asset_id") or "").strip()
            if posted_asset_id != str(asset.id):
                messages.error(request, "Invalid asset payload.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            if asset.current_holder_id != user.id and not user.is_superuser:
                messages.error(request, "You can only return an asset assigned to you.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            if asset.status != "assigned":
                messages.error(request, "Only assigned assets can be returned.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            if active_return:
                messages.info(request, "A return request is already pending for this asset.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            reason = (request.POST.get("reason") or "").strip()

            rr = AssetReturnRequest.objects.create(
                agency=agency, asset=asset, requested_by=user,
                reason=reason, status="pending_ict",
            )
            _log_event(agency, asset, user, "return_initiated",
                       note=reason or "Return initiated by requester.",
                       meta={"return_id": rr.id})

            messages.success(request, f"Return request #{rr.id} submitted to ICT.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        if action == "cancel_return":
            return_id = str(request.POST.get("return_id") or "").strip()
            rr = get_object_or_404(AssetReturnRequest, id=return_id, agency=agency, asset=asset)

            if rr.requested_by_id != user.id and not user.is_superuser:
                messages.error(request, "You can only cancel your own return request.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            if rr.status not in ["pending_ict", "in_transit"]:
                messages.info(request, "This return request can no longer be cancelled.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            rr.status = "cancelled"
            rr.save(update_fields=["status"])
            _log_event(agency, asset, user, "status_change",
                       note=f"Return #{rr.id} cancelled by requester.",
                       meta={"return_id": rr.id})

            messages.success(request, f"Return #{rr.id} cancelled.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        if action == "verify_return_received":
            if not is_ict:
                messages.error(request, "Only ICT can verify returns.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            return_id = (request.POST.get("return_id") or "").strip()
            rr = get_object_or_404(AssetReturnRequest, id=return_id, agency=agency, asset=asset)

            if rr.status not in ["pending_ict", "in_transit"]:
                messages.info(request, "This return request is already processed.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            note = (request.POST.get("note") or "").strip()

            with transaction.atomic():
                rr.status = "received"
                rr.verified_by = user
                rr.verified_at = timezone.now()
                if hasattr(rr, "verification_note"):
                    rr.verification_note = note
                    rr.save(update_fields=["status", "verified_by", "verified_at", "verification_note"])
                else:
                    rr.save(update_fields=["status", "verified_by", "verified_at"])

                asset.current_holder = None
                asset.status = "available"
                asset.save(update_fields=["current_holder", "status"])

                _log_event(agency, asset, user, "return_received",
                           note=note or f"ICT verified receipt for Return #{rr.id}. Asset returned to pool.",
                           meta={"return_id": rr.id})

            messages.success(request, f"Return #{rr.id} verified. Asset returned to pool.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        if action == "retire_asset":
            posted_asset_id = str(request.POST.get("asset_id") or "").strip()
            if posted_asset_id != str(asset.id):
                messages.error(request, "Invalid asset payload.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            if not is_ict:
                messages.error(request, "Only ICT can retire/dispose assets.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            note = (request.POST.get("note") or "").strip()

            with transaction.atomic():
                asset.status = "retired"
                asset.retired_at = timezone.localdate()
                asset.current_holder = None
                asset.save(update_fields=["status", "retired_at", "current_holder"])
                _log_event(agency, asset, user, "retired", note=note or "Asset retired/disposed.")

            messages.success(request, "Asset marked as retired/disposed.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        if action == "propose_asset_change":
            if not is_ict:
                messages.error(request, "Only ICT can submit asset change requests.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            proposed = {}

            name = (request.POST.get("name") or "").strip()
            if name and name != asset.name:
                proposed["name"] = name

            status = (request.POST.get("status") or "").strip()
            if status and status != asset.status:
                proposed["status"] = status

            serial_number = (request.POST.get("serial_number") or "").strip() or None
            if serial_number != asset.serial_number:
                proposed["serial_number"] = serial_number

            asset_tag = (request.POST.get("asset_tag") or "").strip() or None
            if asset_tag != asset.asset_tag:
                proposed["asset_tag"] = asset_tag

            category_id = (request.POST.get("category_id") or "").strip()
            if category_id and str(asset.category_id) != category_id:
                proposed["category_id"] = int(category_id)

            unit_id = (request.POST.get("unit_id") or "").strip()
            if unit_id:
                if (asset.unit_id is None) or (str(asset.unit_id) != unit_id):
                    proposed["unit_id"] = int(unit_id)

            acquired_at = (request.POST.get("acquired_at") or "").strip()
            if acquired_at:
                current = asset.acquired_at.strftime("%Y-%m-%d") if asset.acquired_at else ""
                if acquired_at != current:
                    proposed["acquired_at"] = acquired_at

            reason = (request.POST.get("reason") or "").strip()

            if not proposed:
                messages.info(request, "No changes detected to submit.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            cr = AssetChangeRequest.objects.create(
                agency=agency, asset=asset, requested_by=user,
                proposed_changes=proposed, reason=reason, status="pending_manager",
            )
            _log_event(agency, asset, user, "status_change",
                       note=f"Change request #{cr.id} submitted for approval.",
                       meta={"change_request_id": cr.id, "proposed": proposed})

            messages.success(request, f"Change request #{cr.id} submitted for approval.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        if action == "cancel_change_request":
            cr_id = (request.POST.get("change_request_id") or "").strip()
            cr = get_object_or_404(AssetChangeRequest, id=cr_id, agency=agency, asset=asset)

            if cr.requested_by_id != user.id and not user.is_superuser:
                messages.error(request, "You can only cancel your own change request.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            if cr.status != "pending_manager":
                messages.info(request, "This change request can no longer be cancelled.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            cr.status = "cancelled"
            cr.save(update_fields=["status"])
            _log_event(agency, asset, user, "status_change",
                       note=f"Change request #{cr.id} cancelled by ICT.",
                       meta={"change_request_id": cr.id})

            messages.success(request, f"Change request #{cr.id} cancelled.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        if action in ("approve_change_request", "reject_change_request"):
            cr_id = (request.POST.get("change_request_id") or "").strip()
            cr = get_object_or_404(AssetChangeRequest, id=cr_id, agency=agency, asset=asset)

            if cr.status != "pending_manager":
                messages.info(request, "This change request is already processed.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            if not can_approve_changes:
                messages.error(request, "You are not allowed to approve asset changes for this asset.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            note = (request.POST.get("manager_note") or "").strip()

            if action == "reject_change_request":
                cr.reject(user, note=note)
                _log_event(agency, asset, user, "status_change",
                           note=f"Change request #{cr.id} rejected.",
                           meta={"change_request_id": cr.id})
                messages.warning(request, f"Change request #{cr.id} rejected.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            proposed = cr.proposed_changes or {}
            with transaction.atomic():
                if "category_id" in proposed:
                    asset.category_id = int(proposed["category_id"])
                if "unit_id" in proposed:
                    asset.unit_id = int(proposed["unit_id"])
                if "name" in proposed:
                    asset.name = proposed["name"]
                if "status" in proposed:
                    asset.status = proposed["status"]
                if "serial_number" in proposed:
                    asset.serial_number = proposed["serial_number"]
                if "asset_tag" in proposed:
                    asset.asset_tag = proposed["asset_tag"]
                if "acquired_at" in proposed:
                    try:
                        asset.acquired_at = timezone.datetime.strptime(
                            proposed["acquired_at"], "%Y-%m-%d"
                        ).date()
                    except Exception:
                        pass
                asset.save()
                cr.approve(user, note=note)
                _log_event(agency, asset, user, "status_change",
                           note=f"Change request #{cr.id} approved and applied.",
                           meta={"change_request_id": cr.id, "applied": proposed})

            messages.success(request, f"Change request #{cr.id} approved and applied.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        messages.error(request, "Unknown action.")
        return redirect("accounts:asset_detail", asset_id=asset.id)

    # GET
    history = AssetHistory.objects.filter(
        agency=agency, asset=asset
    ).select_related("actor")[:80]

    return render(request, "accounts/assets/asset_detail.html", {
        "asset": asset,
        "history": history,
        "is_ict": is_ict,
        "units": units,
        "categories": categories,
        "can_approve_changes": can_approve_changes,
        "pending_changes": pending_changes,
        "change_requests": change_requests,
        "pending_return": active_return if active_return else None,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Asset report
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def asset_report(request):
    """Report for ICT / managers: assigned/unassigned + CSV export."""
    user = request.user
    agency = getattr(user, "agency", None)
    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return redirect("accounts:profile")

    svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
    if not svc.asset_mgmt_enabled and not user.is_superuser:
        messages.warning(request, "Asset Management is not enabled for your agency.")
        return redirect("accounts:profile")

    is_ict = user.is_superuser or _is_ict(user, agency)
    managed_units = _managed_unit_ids(user, agency)
    is_manager = user.is_superuser or bool(managed_units) or _is_ops_manager(user, agency)

    if not (is_ict or is_manager):
        messages.error(request, "Only ICT/asset managers can access reports.")
        return redirect("accounts:asset_management")

    qs = Asset.objects.filter(agency=agency).select_related("category", "unit", "current_holder")

    status = request.GET.get("status")
    assigned_flag = request.GET.get("assigned")
    category_id = request.GET.get("category")
    unit_id = request.GET.get("unit")
    export = request.GET.get("export")

    if status:
        qs = qs.filter(status=status)
    if assigned_flag == "1":
        qs = qs.filter(current_holder__isnull=False)
    if assigned_flag == "0":
        qs = qs.filter(current_holder__isnull=True)
    if category_id:
        qs = qs.filter(category_id=category_id)
    if unit_id:
        qs = qs.filter(unit_id=unit_id)
    if not is_ict and managed_units:
        qs = qs.filter(unit_id__in=managed_units)

    qs = qs.order_by("category__name", "name")

    if export == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="assets_report.csv"'
        writer = csv.writer(resp)
        writer.writerow(["Category", "Asset", "Serial", "Tag", "Status", "Unit", "Assigned To", "EOL Due"])
        for a in qs:
            writer.writerow([
                a.category.name if a.category else "",
                a.name,
                a.serial_number or "",
                a.asset_tag or "",
                a.status,
                a.unit.name if a.unit else "",
                (a.current_holder.get_full_name() or a.current_holder.username) if a.current_holder else "",
                a.eol_due_date.isoformat() if a.eol_due_date else "",
            ])
        return resp

    categories = AssetCategory.objects.filter(agency=agency).order_by("name")
    units = Unit.objects.filter(agency=agency).order_by("name")

    return render(request, "accounts/assets/asset_report.html", {
        "assets": qs,
        "categories": categories,
        "units": units,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Asset labels PDF
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def asset_labels_pdf(request):
    user = request.user
    agency = getattr(user, "agency", None)
    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return redirect("accounts:profile")

    svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
    if not svc.asset_mgmt_enabled and not user.is_superuser:
        messages.warning(request, "Asset Management is not enabled for your agency.")
        return redirect("accounts:profile")

    roles, _ = AgencyAssetRoles.objects.get_or_create(agency=agency)

    is_ict = user.is_superuser or _is_ict(user, agency)
    is_ops = user.is_superuser or _is_ops_manager(user, agency)
    managed_units = _managed_unit_ids(user, agency)

    assets_qs = Asset.objects.filter(agency=agency).select_related("category", "unit", "current_holder")

    if is_ict:
        visible = assets_qs
    elif is_ops:
        visible = assets_qs.filter(Q(unit__isnull=True) | Q(unit__is_core_unit=True))
    elif managed_units:
        visible = assets_qs.filter(unit_id__in=managed_units)
    else:
        visible = assets_qs.filter(current_holder=user)

    ids_str = (request.GET.get("ids") or "").strip()
    status = (request.GET.get("status") or "").strip()
    mode = (request.GET.get("mode") or "a4").strip()
    include_url = (request.GET.get("include_url") or "1").strip() == "1"

    if ids_str:
        try:
            ids = [int(x) for x in ids_str.split(",") if x.strip().isdigit()]
            visible = visible.filter(id__in=ids)
        except Exception:
            pass
    elif status:
        visible = visible.filter(status=status)

    assets = list(visible.order_by("category__name", "name")[:500])

    if not assets:
        messages.info(request, "No assets found for this selection.")
        return redirect("accounts:asset_management")

    spec = LabelSpec(
        w_mm=70, h_mm=35, cols=3, rows=8,
        margin_x_mm=8, margin_y_mm=10,
        gap_x_mm=2.5, gap_y_mm=2.5,
    )

    pdf_bytes = build_asset_labels_pdf(
        request=request,
        assets=assets,
        agency=agency,
        mode=mode,
        spec=spec,
        include_url_in_qr=include_url,
    )

    filename = f"asset-labels-{agency.id}-{mode}.pdf"
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Exit organization
# ─────────────────────────────────────────────────────────────────────────────

PENDING_EXIT_STATUSES = {"submitted", "pending_returns", "pending_ict_confirmation"}


@login_required
def exit_organization(request):
    user = request.user
    agency = getattr(user, "agency", None)
    if not agency:
        messages.error(request, "No agency/organization found for your profile.")
        return redirect("accounts:profile")

    if request.method != "POST":
        return redirect("accounts:asset_management")

    reason = (request.POST.get("reason") or "").strip()
    typed = (request.POST.get("typed_confirm") or "").strip()

    if typed != "CONFIRM":
        messages.error(request, "You must type CONFIRM (in capital letters) to proceed.")
        return redirect("accounts:asset_management")

    if reason not in ("resigned", "reassigned"):
        messages.error(request, "Please choose a valid reason (Resigned or Reassigned).")
        return redirect("accounts:asset_management")

    existing = ExitRequest.objects.filter(
        agency=agency, user=user, status__in=PENDING_EXIT_STATUSES
    ).first()

    if existing:
        messages.warning(
            request,
            "You already have a pending exit request on the system. "
            "Please wait for the request to complete."
        )
        return redirect("accounts:asset_management")

    portal_url = request.build_absolute_uri(reverse("accounts:asset_management"))

    try:
        with transaction.atomic():
            exit_req = ExitRequest.objects.create(
                agency=agency,
                user=user,
                reason=reason,
                typed_confirm=typed,
                status="pending_returns",
            )

            assets = list(
                Asset.objects.filter(agency=agency, current_holder=user, status="assigned")
                .select_related("category", "unit")
            )

            created_rr = []
            for asset in assets:
                if AssetReturnRequest.objects.filter(
                    agency=agency, asset=asset, status="pending_ict"
                ).exists():
                    continue
                rr = AssetReturnRequest.objects.create(
                    agency=agency, asset=asset, requested_by=user,
                    reason=f"Exit Organization ({reason})", status="pending_ict",
                )
                created_rr.append(rr)

            user_lines = MobileLine.objects.filter(
                agency=agency, assigned_to=user, status="assigned"
            )
            for line in user_lines:
                line.suspend()

        base_recipients, cell_focal_emails = _get_exit_recipients(user, agency)

        if base_recipients:
            send_email_async(
                subject=f"Exit Notice: {user.get_full_name() or user.username} ({reason})",
                to_emails=base_recipients,
                html_template="emails/exit/exit_submitted.html",
                context={
                    "user": user, "agency": agency, "reason": reason,
                    "exit_req": exit_req, "assets": assets,
                    "return_requests": created_rr, "lines": list(user_lines),
                    "submitted_at": timezone.now(),
                },
            )

        if user_lines.exists() and cell_focal_emails:
            send_email_async(
                subject=f"Action Required: Disable lines for {user.get_full_name() or user.username}",
                to_emails=cell_focal_emails,
                html_template="emails/exit/disable_lines.html",
                context={
                    "user": user, "agency": agency, "lines": list(user_lines),
                    "exit_req": exit_req, "reason": reason,
                    "submitted_at": timezone.now(),
                },
            )

        messages.success(request, "Exit request submitted. Please return your assigned assets to ICT.")
        return redirect("accounts:asset_management")

    except IntegrityError:
        messages.warning(
            request,
            "You already have a pending exit request on the system. "
            "Please wait for the request to complete."
        )
        return redirect("accounts:asset_management")


# ─────────────────────────────────────────────────────────────────────────────
# Consumable item detail page
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def consumable_item_detail(request, item_id: int):
    """
    Detail page for a single consumable item.
    Shows item info, current stock level, and full stock history.
    ICT / Ops can restock or correct stock directly from this page.
    """
    user = request.user
    agency = getattr(user, "agency", None)

    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return redirect("accounts:profile")

    svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
    if not svc.asset_mgmt_enabled and not user.is_superuser:
        messages.warning(request, "Asset Management is not enabled for your agency.")
        return redirect("accounts:profile")

    roles, _ = AgencyAssetRoles.objects.get_or_create(agency=agency)
    is_ict = _is_ict(user, agency)
    is_ops = _is_ops_manager(user, agency)
    can_manage = is_ict or is_ops or user.is_superuser

    item = get_object_or_404(
        ConsumableItem.objects.select_related("category", "agency"),
        id=item_id, agency=agency,
    )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "restock_consumable_item":
            if not can_manage:
                messages.error(request, "Only ICT or Operations Manager can restock supplies.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            qty_str = (request.POST.get("restock_qty") or "0").strip()
            note = (request.POST.get("restock_note") or "").strip()
            try:
                qty = int(qty_str)
            except ValueError:
                messages.error(request, "Invalid quantity.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            if qty <= 0:
                messages.error(request, "Restock quantity must be a positive number.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            with transaction.atomic():
                item_locked = ConsumableItem.objects.select_for_update().get(id=item.id)
                before = item_locked.stock_qty
                item_locked.stock_qty += qty
                item_locked.save(update_fields=["stock_qty"])
                ConsumableStockLog.objects.create(
                    agency=agency, item=item_locked, event="restocked",
                    quantity_before=before, quantity_change=qty,
                    quantity_after=item_locked.stock_qty,
                    note=note or f"Restocked by {user.get_full_name() or user.username}",
                    actor=user,
                )
                item.refresh_from_db()

            messages.success(request, f"Restocked {item.name}: +{qty} {item.unit_of_measure}. New stock: {item.stock_qty}.")
            return redirect("accounts:consumable_item_detail", item_id=item.id)

        if action == "correct_stock":
            if not can_manage:
                messages.error(request, "Only ICT or Operations Manager can correct stock.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            qty_str = (request.POST.get("corrected_qty") or "").strip()
            note = (request.POST.get("correction_note") or "").strip()
            try:
                new_qty = int(qty_str)
                if new_qty < 0:
                    raise ValueError
            except ValueError:
                messages.error(request, "Corrected quantity must be a non-negative number.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            with transaction.atomic():
                item_locked = ConsumableItem.objects.select_for_update().get(id=item.id)
                before = item_locked.stock_qty
                change = new_qty - before
                item_locked.stock_qty = new_qty
                item_locked.save(update_fields=["stock_qty"])
                ConsumableStockLog.objects.create(
                    agency=agency, item=item_locked, event="corrected",
                    quantity_before=before, quantity_change=change,
                    quantity_after=new_qty,
                    note=note or f"Manual correction by {user.get_full_name() or user.username}",
                    actor=user,
                )
                item.refresh_from_db()

            messages.success(request, f"Stock corrected. {item.name} is now {item.stock_qty} {item.unit_of_measure}.")
            return redirect("accounts:consumable_item_detail", item_id=item.id)

        messages.error(request, "Unknown action.")
        return redirect("accounts:consumable_item_detail", item_id=item.id)

    # GET
    stock_logs = ConsumableStockLog.objects.filter(
        agency=agency, item=item
    ).select_related("actor", "reference_request", "reference_request__requester").order_by("-created_at")

    total_dispatched = sum(abs(l.quantity_change) for l in stock_logs if l.event == "dispatched")
    total_restocked = sum(l.quantity_change for l in stock_logs if l.event == "restocked")

    recent_requests = ConsumableRequestItem.objects.filter(
        item=item
    ).select_related(
        "request", "request__requester", "request__unit"
    ).order_by("-request__created_at")[:20]

    return render(request, "accounts/assets/consumable_item_detail.html", {
        "item":             item,
        "stock_logs":       stock_logs,
        "total_dispatched": total_dispatched,
        "total_restocked":  total_restocked,
        "recent_requests":  recent_requests,
        "can_manage":       can_manage,
        "is_ict":           is_ict,
        "is_ops":           is_ops,
    })