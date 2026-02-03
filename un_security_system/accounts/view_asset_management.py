# view_asset_management.py
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.db import transaction

import csv

from .asset_email import send_email_async
from .utils_assets import (get_manager_emails_for_request, get_ict_custodian_emails,
                           can_user_approve_asset_change,
                           can_user_approve_return,
                           generate_unique_asset_tag,
                           build_qr_payload,generate_qr_image,save_qr_to_asset
                           )

from .models import (
    AgencyServiceConfig, AgencyAssetRoles, Unit,
    AssetCategory, Asset, AssetRequest,
    AssetHistory, AssetReturnRequest, AssetChangeRequest
)
from .pdf_assets import build_asset_labels_pdf, LabelSpec


# --- helpers (keep yours if already defined elsewhere) ---
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
    # unit head OR asset_managers
    unit_ids = set(
        Unit.objects.filter(agency=agency, unit_head=user).values_list("id", flat=True)
    )
    unit_ids |= set(
        Unit.objects.filter(agency=agency, asset_managers=user).values_list("id", flat=True)
    )
    return unit_ids


def _log_event(agency, asset, actor, event, note="", meta=None):
    # If you already have a logger, keep it.
    # This version is safe and minimal.
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
    """Managers can see assets for units they manage; ops manages core/unallocated; ICT sees all; holder sees assigned."""
    is_ict = user.is_superuser or _is_ict(user, agency)
    is_ops = user.is_superuser or _is_ops_manager(user, agency)
    managed_units = _managed_unit_ids(user, agency)

    if is_ict:
        return True

    # Ops manages core unit or unallocated
    if is_ops and (asset.unit_id is None or (asset.unit and getattr(asset.unit, "is_core_unit", False))):
        return True

    # Unit manager manages assets in their units
    if asset.unit_id and asset.unit_id in managed_units:
        return True

    # Holder can view their assigned asset
    if asset.current_holder_id == user.id:
        return True

    return False


def _can_user_approve_change(user, agency, asset) -> bool:
    """Asset Manager / Unit Head approves changes. Ops for core/unallocated. Superuser allowed."""
    if user.is_superuser:
        return True

    roles, _ = AgencyAssetRoles.objects.get_or_create(agency=agency)

    # Ops for core/unallocated
    if roles.operations_manager_id and user.id == roles.operations_manager_id:
        if (asset.unit_id is None) or (asset.unit and getattr(asset.unit, "is_core_unit", False)):
            return True

    # If assigned to a unit: unit head or asset managers
    if asset.unit_id:
        if asset.unit.unit_head_id and user.id == asset.unit.unit_head_id:
            return True
        return asset.unit.asset_managers.filter(id=user.id).exists()

    return False

def _safe_email(u):
    return getattr(u, "email", None)

def _get_exit_recipients(user, agency):
    # Unit head
    unit = getattr(user, "unit", None)
    unit_head_email = _safe_email(getattr(unit, "unit_head", None)) if unit else None
    # Ops manager + ICT custodians
    roles = getattr(agency, "asset_roles", None)
    ops_email = _safe_email(getattr(roles, "operations_manager", None)) if roles else None
    ict_emails = list(roles.ict_custodian.values_list("email", flat=True)) if roles else []

    # Cell company focal points
    cell_focal_emails = list(roles.cell_service_focal_point.values_list("email", flat=True)) if roles else []

    # de-dup + remove empty
    base = [unit_head_email, ops_email] + ict_emails
    base = [e for e in dict.fromkeys(base) if e]
    cell_focal_emails = [e for e in dict.fromkeys(cell_focal_emails) if e]
    return base, cell_focal_emails


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

    # Agency service toggle
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

    # ------------------------------------------
    # Helper: safe email sending (no crashes)
    # ------------------------------------------
    def _notify(subject: str, to_emails: list[str], html_template: str, ctx: dict):
        to_emails = [e for e in (to_emails or []) if e]
        if not to_emails:
            return
        ctx = {
            **ctx,
            "subject": subject,
            "agency": agency,
            "portal_url": portal_url,
        }
        try:
            send_email_async(
                subject=subject,
                to_emails=to_emails,
                html_template=html_template,
                context=ctx,
            )
        except Exception:
            pass

    # -----------------------------
    # Shared data
    # -----------------------------
    units = Unit.objects.filter(agency=agency).select_related("unit_head").prefetch_related("asset_managers")
    categories = AssetCategory.objects.filter(agency=agency).order_by("name")

    assets_all = Asset.objects.filter(agency=agency).select_related("category", "unit", "current_holder")

    # Assets visibility:
    # - ICT: all
    # - Ops manager: core/unallocated
    # - Unit managers: assets for managed units
    # - Requesters: only assets assigned to them
    if is_ict:
        assets_visible = assets_all
    elif is_ops:
        assets_visible = assets_all.filter(Q(unit__isnull=True) | Q(unit__is_core_unit=True))
    elif managed_units:
        assets_visible = assets_all.filter(unit_id__in=managed_units)
    else:
        assets_visible = assets_all.filter(current_holder=user)

    assets_visible = assets_visible.order_by("-created_at")

    # Requests visibility (requester)
    my_requests = AssetRequest.objects.filter(
        agency=agency, requester=user
    ).select_related("unit", "category", "assigned_asset").order_by("-created_at")

    # approvals pending for me (manager)
    pending_approvals = []
    if is_manager:
        pending_qs = AssetRequest.objects.filter(
            agency=agency, status="pending_manager"
        ).select_related("unit", "requester", "category")
        pending_approvals = [r for r in pending_qs if (user.is_superuser or r.can_user_approve_as_manager(user))]

    # ICT queue
    pending_ict = AssetRequest.objects.filter(
        agency=agency, status="pending_ict"
    ).select_related("unit", "requester", "category")

    # Returns
    my_returns = AssetReturnRequest.objects.filter(
        agency=agency, requested_by=user
    ).select_related("asset").order_by("-created_at")

    pending_returns = AssetReturnRequest.objects.filter(
        agency=agency, status="pending_ict"
    ).select_related("asset", "requested_by").order_by("-created_at")

    # Quick: assets currently in return pipeline (for badges)
    returning_asset_ids = set(pending_returns.values_list("asset_id", flat=True))

    # EOL list (for ICT / managers)
    eol_assets = []
    if is_ict or is_manager:
        eol_assets = [a for a in assets_visible if getattr(a, "is_eol_due", False) and a.status != "retired"]

    # -----------------------------
    # POST actions
    # -----------------------------
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        # ============ A) Cancel request (before approval/assignment) ============
        if action == "cancel_request":
            req_id = request.POST.get("request_id")
            req_obj = get_object_or_404(AssetRequest, id=req_id, agency=agency)

            if req_obj.requester_id != user.id and not user.is_superuser:
                messages.error(request, "You can only cancel your own request.")
                return redirect("accounts:asset_management")

            # Can cancel only if not approved/assigned/received/rejected
            cancellable_statuses = {"draft", "pending_manager", "pending_ict"}
            if req_obj.status not in cancellable_statuses:
                messages.info(request, "This request can’t be cancelled at this stage.")
                return redirect("accounts:asset_management")

            req_obj.status = "cancelled"
            req_obj.save(update_fields=["status"])

            messages.success(request, f"Request #{req_obj.id} cancelled.")
            return redirect("accounts:asset_management")

        # ============ 1) Create asset request ============
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
            _notify(
                subject=f"Asset Request #{req.id} — Approval Required",
                to_emails=manager_emails,
                html_template="emails/assets/request_submitted.html",
                ctx={"req": req},
            )

            if req.status == "pending_ict":
                ict_emails = get_ict_custodian_emails(req)
                _notify(
                    subject=f"Asset Request #{req.id} — Pending ICT Assignment",
                    to_emails=ict_emails,
                    html_template="emails/assets/request_approved.html",
                    ctx={"req": req, "approved_by": "System (Auto)"},
                )

            messages.success(request, f"Asset request #{req.id} submitted.")
            return redirect("accounts:asset_management")

        # ============ 2) Manager approve/reject ============
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

                _notify(
                    subject=f"Asset Request #{req_obj.id} — Approved",
                    to_emails=[getattr(req_obj.requester, "email", None)],
                    html_template="emails/assets/request_approved.html",
                    ctx={"req": req_obj, "approved_by": user.get_full_name() or user.username},
                )
                _notify(
                    subject=f"Asset Request #{req_obj.id} — Pending ICT Assignment",
                    to_emails=get_ict_custodian_emails(req_obj),
                    html_template="emails/assets/request_approved.html",
                    ctx={"req": req_obj, "approved_by": user.get_full_name() or user.username},
                )

                messages.success(request, f"Request #{req_obj.id} approved.")
                return redirect("accounts:asset_management")

            # reject
            reason = (request.POST.get("reason") or "").strip()
            if not reason:
                messages.error(request, "Please provide a rejection reason.")
                return redirect("accounts:asset_management")

            req_obj.reject(user, reason=reason)

            _notify(
                subject=f"Asset Request #{req_obj.id} — Rejected",
                to_emails=[getattr(req_obj.requester, "email", None)],
                html_template="emails/assets/request_rejected.html",
                ctx={"req": req_obj, "rejected_by": user.get_full_name() or user.username},
            )

            messages.warning(request, f"Request #{req_obj.id} rejected.")
            return redirect("accounts:asset_management")

        # ============ 3) ICT register asset ============
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

            # service settings
            svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)

            # auto-generate tag if enabled and user asked OR blank tag
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

            # generate QR code
            payload = build_qr_payload(request, asset, include_url=svc.asset_qr_include_url)
            logo_path = getattr(getattr(agency, "logo", None), "path", None) if getattr(agency, "logo", None) else None
            qr_img = generate_qr_image(payload, agency_logo_path=logo_path)
            asset.qr_payload = payload
            save_qr_to_asset(asset, qr_img, filename_prefix="assetqr")
            asset.save(update_fields=["qr_code", "qr_payload"])

            _log_event(agency, asset, user, "registered", note="Asset registered into pool (tag + QR created).")

            messages.success(request, "Asset registered successfully (tag + QR generated).")
            return redirect("accounts:asset_management")

        # ============ 4) ICT assign asset ============
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

            _notify(
                subject=f"Asset Request #{req_obj.id} — Asset Assigned",
                to_emails=[getattr(req_obj.requester, "email", None)],
                html_template="emails/assets/asset_assigned.html",
                ctx={"req": req_obj, "asset": asset, "assigned_by": user.get_full_name() or user.username},
            )

            messages.success(request, f"Asset assigned for request #{req_obj.id}.")
            return redirect("accounts:asset_management")

        # ============ 5) Requester verifies receipt ============
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
                _log_event(agency, req_obj.assigned_asset, user, "receipt_verified", note="Requester verified receipt.", meta={"request_id": req_obj.id})

            _notify(
                subject=f"Asset Request #{req_obj.id} — Receipt Verified",
                to_emails=get_ict_custodian_emails(req_obj),
                html_template="emails/assets/receipt_verified.html",
                ctx={"req": req_obj, "asset": req_obj.assigned_asset},
            )

            messages.success(request, f"Receipt verified for request #{req_obj.id}.")
            return redirect("accounts:asset_management")

        # ============ B) Initiate return ============
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

            # prevent duplicate pending return for same asset
            if AssetReturnRequest.objects.filter(agency=agency, asset=asset, status="pending_ict").exists():
                messages.info(request, "Return already submitted and pending ICT verification.")
                return redirect("accounts:asset_management")

            rr = AssetReturnRequest.objects.create(
                agency=agency,
                asset=asset,
                requested_by=user,
                reason=reason,
                status="pending_ict",
            )
            _log_event(agency, asset, user, "return_initiated", note=reason or "Return initiated.", meta={"return_id": rr.id})

            ict_emails = get_ict_custodian_emails(None)
            if not ict_emails:
                ict_emails = list(roles.ict_custodian.values_list("email", flat=True))

            _notify(
                subject=f"Asset Return #{rr.id} — Pending ICT Verification",
                to_emails=ict_emails,
                html_template="emails/assets/return_initiated.html",
                ctx={"rr": rr, "asset": asset},
            )

            messages.success(request, f"Return request #{rr.id} submitted to ICT.")
            return redirect("accounts:asset_management")

        # ============ C) Cancel return (before ICT verifies) ============
        if action == "cancel_return":
            rr_id = request.POST.get("return_id")
            rr = get_object_or_404(AssetReturnRequest, id=rr_id, agency=agency)

            if rr.requested_by_id != user.id and not user.is_superuser:
                messages.error(request, "You can only cancel your own return request.")
                return redirect("accounts:asset_management")

            if rr.status != "pending_ict":
                messages.info(request, "This return request can’t be cancelled anymore.")
                return redirect("accounts:asset_management")

            # Use whatever status you have in choices; 'cancelled' is common
            rr.status = "cancelled"
            rr.save(update_fields=["status"])

            _log_event(agency, rr.asset, user, "return_cancelled", note="Return request cancelled.", meta={"return_id": rr.id})

            messages.success(request, f"Return request #{rr.id} cancelled.")
            return redirect("accounts:asset_management")

        # ============ 7) ICT verifies return received ============
        if action == "verify_return_received":
            rr_id = request.POST.get("return_id")
            rr = get_object_or_404(AssetReturnRequest, id=rr_id, agency=agency)

            if not is_ict:
                messages.error(request, "Only ICT can verify returns.")
                return redirect("accounts:asset_management")

            if rr.status != "pending_ict":
                messages.info(request, "This return request is already processed.")
                return redirect("accounts:asset_management")

            asset = rr.asset
            asset.status = "available"
            asset.current_holder = None
            asset.save(update_fields=["status", "current_holder"])

            rr.status = "received"
            rr.verified_by = user
            rr.verified_at = timezone.now()
            rr.save(update_fields=["status", "verified_by", "verified_at"])

            pending = AssetReturnRequest.objects.filter(
                agency=agency,
                requested_by=exit_user,
                status="pending_ict"
            ).exists()

            if not pending:
                ExitRequest.objects.filter(agency=agency, user=exit_user,
                                           status__in=["pending_returns", "pending_ict_confirmation"]) \
                    .update(status="cleared", cleared_at=timezone.now())

            _log_event(agency, asset, user, "return_received", note="ICT verified return and placed asset back to pool.", meta={"return_id": rr.id})

            _notify(
                subject=f"Asset Return #{rr.id} — Received by ICT",
                to_emails=[getattr(rr.requested_by, "email", None)],
                html_template="emails/assets/return_received.html",
                ctx={"rr": rr, "asset": asset},
            )

            messages.success(request, "Return verified. Asset is now back in the pool.")
            return redirect("accounts:asset_management")

        # ============ 8) ICT mark retired/disposed ============
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

        # ============ D) Manager approves/rejects CHANGE REQUEST (from dashboard tab) ============
        if action in ("approve_change_request", "reject_change_request"):
            cr_id = (request.POST.get("change_request_id") or "").strip()
            cr = get_object_or_404(AssetChangeRequest, id=cr_id, agency=agency)
            asset = cr.asset

            if cr.status != "pending_manager":
                messages.info(request, "This change request is already processed.")
                return redirect("accounts:asset_management")

            # permission check using your shared helper
            if not (user.is_superuser or can_user_approve_asset_change(user, asset, roles)):
                messages.error(request, "You are not allowed to approve changes for this asset.")
                return redirect("accounts:asset_management")

            manager_note = (request.POST.get("manager_note") or "").strip()

            if action == "reject_change_request":
                cr.reject(user, note=manager_note)
                _log_event(
                    agency, asset, user, "status_change",
                    note=f"Change request #{cr.id} rejected.",
                    meta={"change_request_id": cr.id}
                )
                messages.warning(request, f"Change request #{cr.id} rejected.")
                return redirect("accounts:asset_management")

            # approve -> apply proposed changes
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

                _log_event(
                    agency, asset, user, "status_change",
                    note=f"Change request #{cr.id} approved and applied.",
                    meta={"change_request_id": cr.id, "applied": proposed}
                )

            messages.success(request, f"Change request #{cr.id} approved and applied.")
            return redirect("accounts:asset_management")


        messages.error(request, "Unknown action.")
        return redirect("accounts:asset_management")

    # -----------------------------
    # Change Requests pending approvals (Manager / Ops / Superuser)
    # -----------------------------
    pending_change_approvals = []
    if is_manager:
        cr_qs = AssetChangeRequest.objects.filter(
            agency=agency,
            status="pending_manager",
        ).select_related(
            "asset", "requested_by",
            "asset__unit", "asset__category"
        ).order_by("-created_at")

        # Only show those the current user is allowed to approve
        pending_change_approvals = [
            cr for cr in cr_qs if can_user_approve_asset_change(user, cr.asset, roles)
        ]


    return render(request, "accounts/assets/asset_management.html", {
        "svc": svc,
        "roles": roles,
        "is_ict": is_ict,
        "is_manager": is_manager,
        "is_ops": is_ops,
        "pending_change_approvals": pending_change_approvals,

        "units": units,
        "categories": categories,

        "assets": assets_visible,
        "eol_assets": eol_assets,

        "my_requests": my_requests,
        "pending_approvals": pending_approvals,
        "pending_ict": pending_ict,

        "my_returns": my_returns,
        "pending_returns": pending_returns,

        # badge support
        "returning_asset_ids": returning_asset_ids,
    })

@login_required
def asset_detail(request, asset_id: int):
    user = request.user
    agency = getattr(user, "agency", None)
    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return redirect("accounts:profile")

    # service toggle
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

    # active pending return (for badges + verify action)
    active_return = AssetReturnRequest.objects.filter(
        agency=agency, asset=asset, status__in=["pending_ict", "in_transit"]
    ).select_related("requested_by", "verified_by").first()

    # change requests list
    change_requests = AssetChangeRequest.objects.filter(
        agency=agency, asset=asset
    ).select_related("requested_by", "decided_by").order_by("-created_at")[:30]
    pending_changes = [cr for cr in change_requests if cr.status == "pending_manager"]

    # -----------------------------
    # POST actions
    # -----------------------------
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        # ==========================
        # A) Requester initiates return
        # ==========================
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
                agency=agency,
                asset=asset,
                requested_by=user,
                reason=reason,
                status="pending_ict",
            )

            _log_event(
                agency, asset, user, "return_initiated",
                note=reason or "Return initiated by requester.",
                meta={"return_id": rr.id}
            )

            messages.success(request, f"Return request #{rr.id} submitted to ICT.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        # ==========================
        # B) Requester cancels return (if not yet verified)
        # ==========================
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

            _log_event(
                agency, asset, user, "status_change",
                note=f"Return #{rr.id} cancelled by requester.",
                meta={"return_id": rr.id}
            )

            messages.success(request, f"Return #{rr.id} cancelled.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        # ==========================
        # C) ICT verifies received return (asset back to pool)
        # ==========================
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

                # SAFE: only set verification_note if model actually has it
                if hasattr(rr, "verification_note"):
                    rr.verification_note = note
                    rr.save(update_fields=["status", "verified_by", "verified_at", "verification_note"])
                else:
                    rr.save(update_fields=["status", "verified_by", "verified_at"])

                # move asset back to pool
                asset.current_holder = None
                asset.status = "available"
                asset.save(update_fields=["current_holder", "status"])

                _log_event(
                    agency, asset, user, "return_received",
                    note=note or f"ICT verified receipt for Return #{rr.id}. Asset returned to pool.",
                    meta={"return_id": rr.id}
                )

            messages.success(request, f"Return #{rr.id} verified. Asset returned to pool.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        # ==========================
        # D) ICT retires/disposes asset
        # ==========================
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

                _log_event(
                    agency, asset, user, "retired",
                    note=note or "Asset retired/disposed."
                )

            messages.success(request, "Asset marked as retired/disposed.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        # ==========================
        # E) ICT proposes asset edit (Change Request) -> manager approval required
        # ==========================
        if action == "propose_asset_change":
            if not is_ict:
                messages.error(request, "Only ICT can submit asset change requests.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            proposed = {}

            # Only record values that actually changed
            name = (request.POST.get("name") or "").strip()
            if name and name != asset.name:
                proposed["name"] = name

            status = (request.POST.get("status") or "").strip()
            if status and status != asset.status:
                proposed["status"] = status

            serial_number = (request.POST.get("serial_number") or "").strip()
            serial_number = serial_number or None
            if serial_number != asset.serial_number:
                proposed["serial_number"] = serial_number

            asset_tag = (request.POST.get("asset_tag") or "").strip()
            asset_tag = asset_tag or None
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
                # store as string; apply on approval
                current = asset.acquired_at.strftime("%Y-%m-%d") if asset.acquired_at else ""
                if acquired_at != current:
                    proposed["acquired_at"] = acquired_at

            reason = (request.POST.get("reason") or "").strip()

            if not proposed:
                messages.info(request, "No changes detected to submit.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            cr = AssetChangeRequest.objects.create(
                agency=agency,
                asset=asset,
                requested_by=user,
                proposed_changes=proposed,
                reason=reason,
                status="pending_manager",
            )

            _log_event(
                agency, asset, user, "status_change",
                note=f"Change request #{cr.id} submitted for approval.",
                meta={"change_request_id": cr.id, "proposed": proposed}
            )

            messages.success(request, f"Change request #{cr.id} submitted for approval.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        # ==========================
        # F) ICT cancels change request (pending only)
        # ==========================
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

            _log_event(
                agency, asset, user, "status_change",
                note=f"Change request #{cr.id} cancelled by ICT.",
                meta={"change_request_id": cr.id}
            )

            messages.success(request, f"Change request #{cr.id} cancelled.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        # ==========================
        # G) Asset Manager approves/rejects change request
        # ==========================
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
                _log_event(
                    agency, asset, user, "status_change",
                    note=f"Change request #{cr.id} rejected.",
                    meta={"change_request_id": cr.id}
                )
                messages.warning(request, f"Change request #{cr.id} rejected.")
                return redirect("accounts:asset_detail", asset_id=asset.id)

            # approve + apply changes
            proposed = cr.proposed_changes or {}
            with transaction.atomic():
                # apply FK changes safely
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
                    # stored as "YYYY-MM-DD"
                    try:
                        asset.acquired_at = timezone.datetime.strptime(proposed["acquired_at"], "%Y-%m-%d").date()
                    except Exception:
                        pass

                asset.save()

                cr.approve(user, note=note)

                _log_event(
                    agency, asset, user, "status_change",
                    note=f"Change request #{cr.id} approved and applied.",
                    meta={"change_request_id": cr.id, "applied": proposed}
                )

            messages.success(request, f"Change request #{cr.id} approved and applied.")
            return redirect("accounts:asset_detail", asset_id=asset.id)

        messages.error(request, "Unknown action.")
        return redirect("accounts:asset_detail", asset_id=asset.id)

    # -------------------------------
    # GET context
    # -------------------------------
    history = AssetHistory.objects.filter(
        agency=agency, asset=asset
    ).select_related("actor")[:80]

    return render(request, "accounts/assets/asset_detail.html", {
        "asset": asset,
        "history": history,
        "is_ict": is_ict,
        "units": units,
        "categories": categories,

        # change flow
        "can_approve_changes": can_approve_changes,
        "pending_changes": pending_changes,
        "change_requests": change_requests,

        # return flow
        "pending_return": active_return if active_return else None,
    })


@login_required
def asset_report(request):
    """
    Report for ICT / managers:
    - assigned/unassigned
    - export CSV
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

    is_ict = user.is_superuser or _is_ict(user, agency)
    managed_units = _managed_unit_ids(user, agency)
    is_manager = user.is_superuser or bool(managed_units) or _is_ops_manager(user, agency)

    if not (is_ict or is_manager):
        messages.error(request, "Only ICT/asset managers can access reports.")
        return redirect("accounts:asset_management")

    qs = Asset.objects.filter(agency=agency).select_related("category", "unit", "current_holder")

    status = request.GET.get("status")  # available/assigned/maintenance/retired
    assigned_flag = request.GET.get("assigned")  # 1/0
    category_id = request.GET.get("category")
    unit_id = request.GET.get("unit")
    export = request.GET.get("export")  # csv

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

    # managers limited to their units (ICT sees all)
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

    # permissions: ICT can print all; managers print assets they manage; users print their assigned assets
    assets_qs = Asset.objects.filter(agency=agency).select_related("category", "unit", "current_holder")

    if is_ict:
        visible = assets_qs
    elif is_ops:
        visible = assets_qs.filter(Q(unit__isnull=True) | Q(unit__is_core_unit=True))
    elif managed_units:
        visible = assets_qs.filter(unit_id__in=managed_units)
    else:
        visible = assets_qs.filter(current_holder=user)

    # filter selection: ids=1,2,3 OR status=available/assigned/retired
    ids_str = (request.GET.get("ids") or "").strip()
    status = (request.GET.get("status") or "").strip()
    mode = (request.GET.get("mode") or "a4").strip()   # a4 or sticker
    include_url = (request.GET.get("include_url") or "1").strip() == "1"

    if ids_str:
        try:
            ids = [int(x) for x in ids_str.split(",") if x.strip().isdigit()]
            visible = visible.filter(id__in=ids)
        except Exception:
            pass
    elif status:
        visible = visible.filter(status=status)

    assets = list(visible.order_by("category__name", "name")[:500])  # safety cap

    if not assets:
        messages.info(request, "No assets found for this selection.")
        return redirect("accounts:asset_management")

    # label spec can be customized here
    spec = LabelSpec(
        w_mm=70,
        h_mm=35,
        cols=3,
        rows=8,
        margin_x_mm=8,
        margin_y_mm=10,
        gap_x_mm=2.5,
        gap_y_mm=2.5,
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

@login_required
def exit_organization(request):
    user = request.user
    agency = getattr(user, "agency", None)  # adjust if your app uses different field
    if not agency:
        messages.error(request, "No agency/organization found for your profile.")
        return redirect("accounts:profile")

    if request.method == "POST":
        reason = (request.POST.get("reason") or "").strip()
        typed = (request.POST.get("typed_confirm") or "").strip()

        if typed != "CONFIRM":
            messages.error(request, "You must type CONFIRM (in capital letters) to proceed.")
            return redirect("accounts:profile")

        if reason not in ("resigned", "reassigned"):
            messages.error(request, "Please choose a valid reason (Resigned or Reassigned).")
            return redirect("accounts:profile")

        with transaction.atomic():
            exit_req = ExitRequest.objects.create(
                agency=agency,
                user=user,
                reason=reason,
                typed_confirm=typed,
                status="pending_returns",
            )

            # 1) Create return requests for all assets assigned to this user
            assets = Asset.objects.filter(agency=agency, current_holder=user, status="assigned")
            created_rr = []
            for asset in assets:
                # avoid duplicates
                if AssetReturnRequest.objects.filter(agency=agency, asset=asset, status="pending_ict").exists():
                    continue
                rr = AssetReturnRequest.objects.create(
                    agency=agency,
                    asset=asset,
                    requested_by=user,
                    reason=f"Exit Organization ({reason})",
                    status="pending_ict",
                )
                created_rr.append(rr)

            # 2) If user has SIM/Data lines: mark suspended + notify cell focal points
            user_lines = MobileLine.objects.filter(agency=agency, assigned_to=user, status="assigned")
            for line in user_lines:
                line.suspend()

        # Emails (after transaction is OK)
        base_recipients, cell_focal_emails = _get_exit_recipients(user, agency)

        # Email unit head + ops + ict
        send_email_async(
            subject=f"Exit Notice: {user.get_full_name() or user.username} ({reason})",
            to_emails=base_recipients,
            html_template="emails/exit/exit_submitted.html",
            context={
                "user": user,
                "agency": agency,
                "reason": reason,
                "exit_req": exit_req,
                "assets": assets,
                "return_requests": created_rr,
                "lines": user_lines,
            },
        )

        # Email cell provider focal point(s) only if there are lines
        if user_lines.exists() and cell_focal_emails:
            send_email_async(
                subject=f"Action Required: Disable lines for {user.get_full_name() or user.username}",
                to_emails=cell_focal_emails,
                html_template="emails/exit/disable_lines.html",
                context={"user": user, "agency": agency, "lines": user_lines, "exit_req": exit_req},
            )

        messages.success(request, "Exit request submitted. Please return your assigned assets to ICT.")
        return redirect("accounts:profile")

    # GET
    return render(request, "accounts/assets/exit_organization.html", {})