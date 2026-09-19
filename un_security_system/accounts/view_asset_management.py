# view_asset_management.py
"""
Fast Asset Management dashboard wrapper for UN PASS.

IMPORTANT DEPLOYMENT NOTE
-------------------------
Before replacing the existing file, rename the current production file:

    view_asset_management.py -> view_asset_management_legacy.py

This module re-exports every existing route/helper from the legacy module and
only replaces the main Asset Management GET dashboard with a faster version.
All existing POST actions continue to run through the original production code.

Performance changes:
- PostgreSQL/server-side asset pagination (25/50/100 rows).
- PostgreSQL asset search/filtering.
- Aggregate SQL for asset KPIs/charts instead of serialising every asset.
- No per-row manager approval N+1 checks on dashboard GET.
- EOL calculation only for the current registry page.
- Available-asset assignment payload includes all available agency assets;
  the assignment modal filters them for the selected request.
- Lighter asset list used by consumable linking.

No migrations or schema changes.
"""

from . import view_asset_management_legacy as _legacy

# Re-export ALL existing functions/classes/helpers so the rest of accounts.urls
# and any internal imports continue to work exactly as before.
for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render


@login_required
def view_asset_management(request):
    """
    Faster GET dashboard.

    POST requests are deliberately delegated to the original production
    implementation so request/approval/assignment/return/consumable workflows
    are unchanged.
    """
    if request.method == "POST":
        return _legacy.view_asset_management(request)

    user = request.user
    agency = getattr(user, "agency", None)

    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return redirect("accounts:profile")

    svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
    if not svc.asset_mgmt_enabled and not user.is_superuser:
        messages.warning(
            request,
            "Asset Management is not enabled for your agency.",
        )
        return redirect("accounts:profile")

    roles, _ = AgencyAssetRoles.objects.get_or_create(agency=agency)

    is_ict = _legacy._is_ict(user, agency)
    is_ops = _legacy._is_ops_manager(user, agency)
    managed_units = _legacy._managed_unit_ids(user, agency)
    is_manager = user.is_superuser or bool(managed_units) or is_ops

    # ------------------------------------------------------------------
    # Live assignment options endpoint
    #
    # The assignment modal calls this every time it opens. This avoids stale
    # browser/template data after assigning one asset, navigating back to the
    # ICT queue, or restoring the page from the browser back/forward cache.
    # No new URL is required; it uses the existing /accounts/assets/ route.
    # ------------------------------------------------------------------
    if request.GET.get("assignment_options") == "1":
        if not is_ict:
            response = JsonResponse(
                {
                    "ok": False,
                    "error": "Only ICT custodians can load assignment options.",
                },
                status=403,
            )
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            return response

        request_id = (request.GET.get("request_id") or "").strip()
        if not request_id.isdigit():
            response = JsonResponse(
                {
                    "ok": False,
                    "error": "A valid asset request ID is required.",
                },
                status=400,
            )
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            return response

        asset_request = (
            AssetRequest.objects
            .filter(
                pk=int(request_id),
                agency=agency,
                status="pending_ict",
            )
            .select_related(
                "category",
                "unit",
                "requester",
            )
            .first()
        )

        if asset_request is None:
            response = JsonResponse(
                {
                    "ok": False,
                    "error": (
                        "This request is no longer waiting for ICT assignment. "
                        "Refresh the queue and try again."
                    ),
                },
                status=404,
            )
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            return response

        assets = list(
            Asset.objects
            .filter(
                agency=agency,
                status="available",
                category_id=asset_request.category_id,
            )
            .values(
                "id",
                "name",
                "category_id",
                "category__name",
                "unit_id",
                "unit__name",
                "serial_number",
                "asset_tag",
            )
            .order_by(
                "category__name",
                "name",
            )
        )

        for row in assets:
            row["category_name"] = row.pop("category__name") or ""
            row["unit_name"] = row.pop("unit__name") or ""
            row["serial_number"] = row["serial_number"] or ""
            row["asset_tag"] = row["asset_tag"] or ""

        requester_name = (
            asset_request.requester.get_full_name()
            or asset_request.requester.username
        )

        response = JsonResponse(
            {
                "ok": True,
                "request": {
                    "id": asset_request.id,
                    "category_id": asset_request.category_id,
                    "category_name": asset_request.category.name,
                    "unit_id": asset_request.unit_id,
                    "unit_name": (
                        asset_request.unit.name
                        if asset_request.unit_id
                        else "Unallocated/Core"
                    ),
                    "requester": requester_name,
                },
                "assets": assets,
            }
        )
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    # ------------------------------------------------------------------
    # Small shared lists used by forms / filters
    # ------------------------------------------------------------------
    units = list(
        Unit.objects.filter(agency=agency)
        .select_related("unit_head")
        .prefetch_related("asset_managers")
        .order_by("name")
    )
    categories = list(
        AssetCategory.objects.filter(agency=agency).order_by("name")
    )

    # ------------------------------------------------------------------
    # Asset registry scope + server-side filtering/pagination
    # ------------------------------------------------------------------
    assets_all = (
        Asset.objects
        .filter(agency=agency)
        .select_related("category", "unit", "current_holder")
    )

    if is_ict:
        asset_base_qs = assets_all
    elif is_ops:
        asset_base_qs = assets_all.filter(
            Q(unit__isnull=True) | Q(unit__is_core_unit=True)
        )
    elif managed_units:
        asset_base_qs = assets_all.filter(unit_id__in=managed_units)
    else:
        asset_base_qs = assets_all.filter(current_holder=user)

    asset_base_qs = asset_base_qs.order_by("-created_at")

    # SQL count — does not hydrate every asset object.
    asset_total_count = asset_base_qs.count()

    asset_q = (request.GET.get("asset_q") or "").strip()
    asset_category = (request.GET.get("asset_category") or "").strip()
    asset_unit = (request.GET.get("asset_unit") or "").strip()
    asset_status = (request.GET.get("asset_status") or "").strip()

    assets_filtered = asset_base_qs

    if asset_q:
        assets_filtered = assets_filtered.filter(
            Q(name__icontains=asset_q)
            | Q(serial_number__icontains=asset_q)
            | Q(asset_tag__icontains=asset_q)
            | Q(category__name__icontains=asset_q)
            | Q(current_holder__first_name__icontains=asset_q)
            | Q(current_holder__last_name__icontains=asset_q)
            | Q(current_holder__username__icontains=asset_q)
        )

    if asset_category.isdigit():
        assets_filtered = assets_filtered.filter(
            category_id=int(asset_category)
        )

    if asset_unit == "unallocated":
        assets_filtered = assets_filtered.filter(unit__isnull=True)
    elif asset_unit.isdigit():
        assets_filtered = assets_filtered.filter(unit_id=int(asset_unit))

    if asset_status in {
        "available",
        "assigned",
        "maintenance",
        "retired",
    }:
        assets_filtered = assets_filtered.filter(status=asset_status)

    asset_filtered_count = assets_filtered.count()

    try:
        asset_page_size = int(
            request.GET.get("asset_page_size") or 25
        )
    except (TypeError, ValueError):
        asset_page_size = 25

    if asset_page_size not in {25, 50, 100}:
        asset_page_size = 25

    asset_paginator = Paginator(
        assets_filtered,
        asset_page_size,
    )
    asset_page = asset_paginator.get_page(
        request.GET.get("asset_page") or 1
    )

    # Force only this page into memory. The old dashboard rendered the entire
    # visible registry before JavaScript "pagination" hid most rows.
    assets_visible = list(asset_page.object_list)

    asset_filters = {
        "q": asset_q,
        "category": asset_category,
        "unit": asset_unit,
        "status": asset_status,
        "page_size": asset_page_size,
    }

    asset_tab_active = (
        request.GET.get("asset_tab") == "1"
        or bool(
            asset_q
            or asset_category
            or asset_unit
            or asset_status
            or request.GET.get("asset_page")
        )
    )

    asset_page_range = list(
        asset_page.paginator.get_elided_page_range(
            asset_page.number,
            on_each_side=2,
            on_ends=1,
        )
    )

    # ------------------------------------------------------------------
    # Asset analytics using aggregate SQL
    # ------------------------------------------------------------------
    status_counts = {
        row["status"]: row["total"]
        for row in (
            asset_base_qs
            .order_by()
            .values("status")
            .annotate(total=Count("id"))
        )
    }

    asset_category_chart = [
        {
            "label": row["category__name"] or "Uncategorised",
            "value": row["total"],
        }
        for row in (
            asset_base_qs
            .order_by()
            .values("category__name")
            .annotate(total=Count("id"))
            .order_by("-total", "category__name")[:20]
        )
    ]

    asset_unit_chart = [
        {
            "label": row["unit__name"] or "Unallocated/Core",
            "value": row["total"],
        }
        for row in (
            asset_base_qs
            .order_by()
            .values("unit__name")
            .annotate(total=Count("id"))
            .order_by("-total", "unit__name")[:20]
        )
    ]

    # EOL property uses Python date logic, so calculate only for the current
    # registry page instead of iterating over every visible asset.
    eol_assets = []
    if is_ict or is_manager:
        eol_assets = [
            a
            for a in assets_visible
            if getattr(a, "is_eol_due", False)
            and a.status != "retired"
        ]

    # ------------------------------------------------------------------
    # Requests / approvals / returns
    # ------------------------------------------------------------------
    my_requests = (
        AssetRequest.objects
        .filter(agency=agency, requester=user)
        .select_related("unit", "category", "assigned_asset")
        .order_by("-created_at")
    )

    pending_approvals = []
    if is_manager:
        pending_qs = (
            AssetRequest.objects
            .filter(
                agency=agency,
                status="pending_manager",
            )
            .select_related(
                "unit",
                "requester",
                "category",
            )
        )

        if user.is_superuser:
            pending_approvals = list(pending_qs)
        else:
            approval_scope = Q(pk__in=[])

            if is_ops:
                approval_scope |= (
                    Q(unit__isnull=True)
                    | Q(unit__is_core_unit=True)
                )

            if managed_units:
                approval_scope |= Q(
                    unit_id__in=managed_units
                )

            pending_approvals = list(
                pending_qs
                .filter(approval_scope)
                .distinct()
            )

    pending_ict = (
        AssetRequest.objects
        .filter(
            agency=agency,
            status="pending_ict",
        )
        .select_related(
            "unit",
            "requester",
            "category",
        )
        .order_by("-created_at")
    )

    my_returns = (
        AssetReturnRequest.objects
        .filter(
            agency=agency,
            requested_by=user,
        )
        .select_related("asset")
        .order_by("-created_at")
    )

    pending_returns = (
        AssetReturnRequest.objects
        .filter(
            agency=agency,
            status="pending_ict",
        )
        .select_related(
            "asset",
            "requested_by",
        )
        .order_by("-created_at")
    )

    returning_asset_ids = set(
        pending_returns.values_list(
            "asset_id",
            flat=True,
        )
    )

    # ------------------------------------------------------------------
    # Mobile lines
    # ------------------------------------------------------------------
    if is_ict:
        mobile_lines_visible = (
            MobileLine.objects
            .filter(agency=agency)
            .select_related(
                "custodian",
                "assigned_to",
            )
            .order_by("-created_at")
        )
    else:
        mobile_lines_visible = (
            MobileLine.objects
            .filter(
                agency=agency,
                assigned_to=user,
            )
            .select_related(
                "custodian",
                "assigned_to",
            )
            .order_by("-created_at")
        )

    pending_line_reactivations = (
        MobileLineReactivationRequest.objects
        .filter(
            agency=agency,
            status="pending_ops",
        )
        .select_related(
            "line",
            "requested_by",
        )
    )

    # ------------------------------------------------------------------
    # Pending asset-change approvals — filtered in SQL instead of invoking
    # a permission helper once for every row.
    # ------------------------------------------------------------------
    pending_change_approvals = []

    if is_manager:
        cr_qs = (
            AssetChangeRequest.objects
            .filter(
                agency=agency,
                status="pending_manager",
            )
            .select_related(
                "asset",
                "requested_by",
                "asset__unit",
                "asset__category",
            )
            .order_by("-created_at")
        )

        if user.is_superuser:
            pending_change_approvals = list(cr_qs)
        else:
            change_scope = Q(pk__in=[])

            if is_ops:
                change_scope |= (
                    Q(asset__unit__isnull=True)
                    | Q(asset__unit__is_core_unit=True)
                )

            if managed_units:
                change_scope |= Q(
                    asset__unit_id__in=managed_units
                )

            pending_change_approvals = list(
                cr_qs
                .filter(change_scope)
                .distinct()
            )

    # ------------------------------------------------------------------
    # Users used by mobile-line / asset forms
    # ------------------------------------------------------------------
    agency_users = (
        User.objects
        .filter(
            agency=agency,
            is_active=True,
        )
        .order_by(
            "first_name",
            "last_name",
            "username",
        )
    )

    # ------------------------------------------------------------------
    # Consumables / supplies
    # ------------------------------------------------------------------
    consumable_categories = (
        ConsumableCategory.objects
        .filter(
            agency=agency,
            is_active=True,
        )
        .order_by(
            "category_type",
            "name",
        )
    )

    # Evaluate once; the same collection is used by the chart, low-stock
    # badges, request modal and supplies panel.
    consumable_items = list(
        ConsumableItem.objects
        .filter(
            agency=agency,
            is_active=True,
        )
        .select_related("category")
        .prefetch_related(
            "asset_links__asset",
            "asset_links__asset_category",
        )
        .order_by(
            "category__name",
            "name",
        )
    )

    my_consumable_requests = (
        ConsumableRequest.objects
        .filter(
            agency=agency,
            requester=user,
        )
        .select_related(
            "approved_by",
            "linked_asset",
            "unit",
        )
        .prefetch_related("line_items__item")
        .order_by("-created_at")
    )

    pending_consumable_approvals = []
    if is_manager:
        pending_consumable_approvals = list(
            ConsumableRequest.objects
            .filter(
                agency=agency,
                status="pending",
            )
            .select_related(
                "requester",
                "unit",
                "linked_asset",
            )
            .prefetch_related(
                "line_items__item"
            )
            .order_by("-created_at")
        )

    approved_consumable_requests = []
    if is_ict or is_ops:
        approved_consumable_requests = list(
            ConsumableRequest.objects
            .filter(
                agency=agency,
                status__in=(
                    "approved",
                    "partially_fulfilled",
                ),
            )
            .select_related(
                "requester",
                "unit",
                "approved_by",
                "linked_asset",
            )
            .prefetch_related(
                "line_items__item"
            )
            .order_by("-created_at")
        )

    low_stock_items = []
    if is_ict or is_ops:
        low_stock_items = [
            item
            for item in consumable_items
            if item.is_low_stock
        ]

    consumable_chart_data = (
        _legacy._build_consumable_chart_data(
            consumable_items
        )
        if (is_ict or is_ops)
        else "null"
    )

    consumable_asset_links = []
    if is_ict or is_ops:
        consumable_asset_links = list(
            ConsumableAssetLink.objects
            .filter(agency=agency)
            .select_related(
                "consumable_item",
                "asset",
                "asset_category",
            )
            .order_by(
                "consumable_item__name"
            )
        )

    # ------------------------------------------------------------------
    # Assignment modal payload
    #
    # Send every AVAILABLE asset for this agency to the assignment modal.
    # The modal performs the request-category filtering when the ICT
    # custodian opens a specific request.
    #
    # Do not pre-filter this list using pending ICT categories. That can
    # leave the modal with an empty payload when requests/categories change.
    # ------------------------------------------------------------------
    available_assets = []

    if is_ict:
        available_assets = [
            {
                "id": row["id"],
                "name": row["name"],
                "category_id": row["category_id"],
                "category_name": row["category__name"] or "",
                "unit_id": row["unit_id"],
                "unit_name": row["unit__name"] or "",
                "serial_number": row["serial_number"] or "",
                "asset_tag": row["asset_tag"] or "",
            }
            for row in (
                Asset.objects
                .filter(
                    agency=agency,
                    status="available",
                )
                .values(
                    "id",
                    "name",
                    "category_id",
                    "category__name",
                    "unit_id",
                    "unit__name",
                    "serial_number",
                    "asset_tag",
                )
                .order_by(
                    "category__name",
                    "name",
                )
            )
        ]

    # Lightweight rows for the consumable-link dropdown. Django templates can
    # access dictionary keys with dot notation (asset.id / asset.name).
    assets_for_link = []

    if is_ict or is_ops:
        assets_for_link = list(
            Asset.objects
            .filter(agency=agency)
            .values(
                "id",
                "name",
                "asset_tag",
            )
            .order_by("name")
        )

    return render(
        request,
        "accounts/assets/asset_management.html",
        {
            "svc": svc,
            "roles": roles,
            "is_ict": is_ict,
            "is_manager": is_manager,
            "is_ops": is_ops,

            "agency_users": agency_users,
            "pending_change_approvals": (
                pending_change_approvals
            ),

            "units": units,
            "categories": categories,

            "assets": assets_visible,
            "asset_page": asset_page,
            "asset_page_range": asset_page_range,
            "asset_total_count": asset_total_count,
            "asset_filtered_count": (
                asset_filtered_count
            ),
            "asset_filters": asset_filters,
            "asset_tab_active": asset_tab_active,

            "asset_available_count": (
                status_counts.get(
                    "available",
                    0,
                )
            ),
            "asset_assigned_count": (
                status_counts.get(
                    "assigned",
                    0,
                )
            ),
            "asset_maintenance_count": (
                status_counts.get(
                    "maintenance",
                    0,
                )
            ),
            "asset_retired_count": (
                status_counts.get(
                    "retired",
                    0,
                )
            ),

            "asset_category_chart": (
                asset_category_chart
            ),
            "asset_unit_chart": asset_unit_chart,

            "eol_assets": eol_assets,
            "mobile_lines_visible": (
                mobile_lines_visible
            ),

            "my_requests": my_requests,
            "pending_line_reactivations": (
                pending_line_reactivations
            ),
            "pending_approvals": (
                pending_approvals
            ),
            "pending_ict": pending_ict,

            "my_returns": my_returns,
            "pending_returns": pending_returns,
            "returning_asset_ids": (
                returning_asset_ids
            ),

            "consumable_categories": (
                consumable_categories
            ),
            "consumable_items": consumable_items,
            "my_consumable_requests": (
                my_consumable_requests
            ),
            "pending_consumable_approvals": (
                pending_consumable_approvals
            ),
            "approved_consumable_requests": (
                approved_consumable_requests
            ),
            "low_stock_items": low_stock_items,
            "consumable_chart_data": (
                consumable_chart_data
            ),
            "consumable_asset_links": (
                consumable_asset_links
            ),
            "assets_for_link": assets_for_link,
            "available_assets": available_assets,
        },
    )
