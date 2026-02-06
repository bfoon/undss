import csv
import re
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from urllib.parse import urlparse, parse_qs

from .models import Asset, AssetVerification, AgencyServiceConfig, AgencyAssetRoles, Unit, AssetCategory
from .view_asset_management import _is_ict, _is_ops_manager, _managed_unit_ids


def _can_verify_assets(user, agency, roles, is_ict, is_ops, managed_units):
    # Keep it simple: ICT + Ops + Unit managers can verify
    if user.is_superuser or is_ict or is_ops:
        return True
    return bool(managed_units)


def extract_asset_id(raw_value: str) -> int | None:
    """
    Supports:
    - "3"
    - "/accounts/assets/asset/3/"
    - "https://domain/.../asset/3/"
    - Any URL path that ends with /<int>/
    """
    if not raw_value:
        return None

    raw_value = raw_value.strip()

    if raw_value.isdigit():
        return int(raw_value)

    try:
        path = urlparse(raw_value).path if raw_value.startswith("http") else raw_value
        nums = re.findall(r"/(\d+)/?$", path)
        if nums:
            return int(nums[0])
    except Exception:
        return None

    return None


@login_required
def asset_verify(request):
    """
    Asset verification portal:
    - enter tag or scan QR -> verify
    - record AssetVerification
    - show found asset + last verification info
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

    is_ict = user.is_superuser or _is_ict(user, agency)
    is_ops = user.is_superuser or _is_ops_manager(user, agency)
    managed_units = _managed_unit_ids(user, agency)

    if not _can_verify_assets(user, agency, roles, is_ict, is_ops, managed_units):
        messages.error(request, "You are not allowed to verify assets.")
        return redirect("accounts:asset_management")

    found_asset = None
    last_verifications = []

    if request.method == "POST":
        raw = (request.POST.get("tag") or "").strip()
        method = (request.POST.get("method") or "manual").strip()
        note = (request.POST.get("note") or "").strip()
        location = (request.POST.get("location") or "").strip()

        if not raw:
            messages.error(request, "Please enter or scan an asset tag.")
            return redirect("accounts:asset_verify")

        # Lookup asset by tag within agency
        asset = None
        tag_entered = ""

        # 1) If scan gives a URL or an ID -> resolve by ID
        asset_id = extract_asset_id(raw)
        if asset_id:
            asset = Asset.objects.filter(
                agency=agency, id=asset_id
            ).select_related("category", "unit", "current_holder").first()
            tag_entered = str(asset_id)  # what was scanned (id/url), for audit trail
            method = "scan"

        # 2) Otherwise treat as a tag
        if not asset:
            tag = raw
            if not tag:
                messages.error(request, "Please enter or scan an asset tag.")
                return redirect("accounts:asset_verify")

            asset = Asset.objects.filter(
                agency=agency, asset_tag__iexact=tag
            ).select_related("category", "unit", "current_holder").first()
            tag_entered = tag

        if not asset:
            messages.error(request, f"No asset found for tag: {tag}")
            return redirect("accounts:asset_verify")

        # visibility rules (same as management registry)
        allowed = False
        if is_ict:
            allowed = True
        elif is_ops and (asset.unit_id is None or getattr(asset.unit, "is_core_unit", False)):
            allowed = True
        elif asset.unit_id and asset.unit_id in managed_units:
            allowed = True

        if not allowed:
            messages.error(request, "You can’t verify this asset (not in your managed scope).")
            return redirect("accounts:asset_verify")

        # Create verification record
        AssetVerification.objects.create(
            agency=agency,
            asset=asset,
            verified_by=user,
            verified_at=timezone.now(),
            method=method if method in ("manual", "scan") else "manual",
            tag_entered=tag_entered,
            note=note,
            location=location,
        )

        messages.success(request, f"Asset verified: {asset.name} ({asset.asset_tag})")
        found_asset = asset

    # Show recent verifications by this user
    recent = AssetVerification.objects.filter(agency=agency, verified_by=user).select_related(
        "asset", "asset__category", "asset__unit"
    )[:25]

    return render(request, "accounts/assets/asset_verify.html", {
        "is_ict": is_ict,
        "is_ops": is_ops,
        "found_asset": found_asset,
        "recent": recent,
    })


@login_required
def asset_verification_history(request):
    """
    Filterable verification history + export CSV.
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
    is_ict = user.is_superuser or _is_ict(user, agency)
    is_ops = user.is_superuser or _is_ops_manager(user, agency)
    managed_units = _managed_unit_ids(user, agency)

    if not _can_verify_assets(user, agency, roles, is_ict, is_ops, managed_units):
        messages.error(request, "You are not allowed to view verification history.")
        return redirect("accounts:asset_management")

    qs = AssetVerification.objects.filter(agency=agency).select_related(
        "asset", "asset__category", "asset__unit", "verified_by"
    )

    # filters
    tag = (request.GET.get("tag") or "").strip()
    unit = (request.GET.get("unit") or "").strip()
    category = (request.GET.get("category") or "").strip()
    verified_by = (request.GET.get("verified_by") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    if tag:
        qs = qs.filter(Q(asset__asset_tag__icontains=tag) | Q(tag_entered__icontains=tag))
    if unit:
        qs = qs.filter(asset__unit_id=unit)
    if category:
        qs = qs.filter(asset__category_id=category)
    if verified_by:
        qs = qs.filter(verified_by_id=verified_by)
    if date_from:
        qs = qs.filter(verified_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(verified_at__date__lte=date_to)

    # scope restriction (managers should only see their scope, ICT sees all)
    if not is_ict:
        if is_ops:
            qs = qs.filter(Q(asset__unit__isnull=True) | Q(asset__unit__is_core_unit=True))
        elif managed_units:
            qs = qs.filter(asset__unit_id__in=managed_units)

    qs = qs.order_by("-verified_at")

    # export CSV
    if request.GET.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="asset-verification-history.csv"'
        w = csv.writer(resp)
        w.writerow(["Verified At", "Tag", "Asset", "Category", "Unit", "Status", "Verified By", "Method", "Location", "Note"])
        for v in qs[:5000]:
            a = v.asset
            w.writerow([
                v.verified_at.strftime("%Y-%m-%d %H:%M"),
                a.asset_tag or v.tag_entered,
                a.name,
                getattr(a.category, "name", ""),
                getattr(a.unit, "name", "Unallocated/Core") if a.unit_id else "Unallocated/Core",
                getattr(a, "status", ""),
                v.verified_by.get_full_name() if v.verified_by else "System",
                v.method,
                v.location,
                (v.note or "")[:200],
            ])
        return resp

    units = Unit.objects.filter(agency=agency).order_by("name")
    categories = AssetCategory.objects.filter(agency=agency).order_by("name")

    return render(request, "accounts/assets/asset_verification_history.html", {
        "verifications": qs[:500],
        "units": units,
        "categories": categories,
    })
