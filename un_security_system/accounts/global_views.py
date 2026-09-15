"""
tenancy/global_views.py
=======================

Platform-wide module master switches plus a guarded feature console that
preserves agency/office settings while a module is globally stopped.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .catalog import (
    DELEGABLE_CODES,
    FEATURES,
    FEATURES_BY_CODE,
    features_by_category,
)
from .decorators import superuser_required
from .global_control import (
    global_enabled_codes,
    global_switch_detail,
    global_switch_details,
    is_globally_enabled,
    set_global_feature,
)
from .models import CountryOffice, FeatureAuditLog, FeatureGrant
from .scope_control import (
    agency_is_active,
    scope_status_for_ids,
    set_agency_active,
    set_office_active,
)
from .services import (
    admin_role,
    can_toggle,
    copy_features,
    parse_scope_key,
    scope_feature_map,
    scope_feature_map_configured,
    set_feature,
)


def _agency_model():
    from django.apps import apps
    return apps.get_model("accounts", "Agency")


@login_required
@superuser_required
def platform_overview(request):
    """
    Platform overview with effective Agency/CO status and module configuration.

    Module counts remain visible while a scope is suspended so the superuser
    can see the preserved configuration that will return on reactivation.
    """
    Agency = _agency_model()
    agencies = Agency.objects.prefetch_related("country_offices").order_by("code")

    rows = []
    for agency in agencies:
        agency_active = agency_is_active(agency)
        agency_flags = scope_feature_map_configured(agency.pk, None)

        offices = []
        for office in agency.country_offices.all().order_by("name"):
            flags = scope_feature_map_configured(agency.pk, office.pk)
            own_active = bool(office.is_active)
            effective_active = bool(agency_active and own_active)

            offices.append({
                "office": office,
                "own_active": own_active,
                "effective_active": effective_active,
                "suspended_by_agency": bool(not agency_active),
                "on_count": sum(1 for value in flags.values() if value),
                "on_codes": [code for code, value in flags.items() if value],
                "user_count": (
                    office.users.count()
                    if hasattr(office, "users")
                    else 0
                ),
                "admin_count": office.admins.filter(is_active=True).count(),
                "scope_key": office.scope_key,
            })

        rows.append({
            "agency": agency,
            "agency_active": agency_active,
            "scope_key": f"agency:{agency.pk}",
            "on_count": sum(1 for value in agency_flags.values() if value),
            "offices": offices,
            "office_count": len(offices),
            "active_office_count": sum(
                1 for item in offices if item["effective_active"]
            ),
        })

    return render(
        request,
        "tenancy/overview.html",
        {
            "rows": rows,
            "total_features": len(FEATURES),
            "recent_changes": FeatureAuditLog.objects.all()[:15],
        },
    )


@login_required
@superuser_required
@require_http_methods(["POST"])
def organization_status(request):
    """
    Activate/suspend an Agency or one Country Office.

    No users, grants, trusted devices or module data are deleted.
    """
    scope_type = (request.POST.get("scope_type") or "").strip().lower()
    action = (request.POST.get("action") or "").strip().lower()
    raw_id = (request.POST.get("scope_id") or "").strip()
    reason = (request.POST.get("reason") or "").strip()

    if action not in {"enable", "disable"}:
        messages.error(request, "Choose Activate or Suspend.")
        return redirect("tenancy:overview")

    try:
        scope_id = int(raw_id)
    except (TypeError, ValueError):
        messages.error(request, "Invalid Agency/CO identifier.")
        return redirect("tenancy:overview")

    enabled = action == "enable"

    if scope_type == "agency":
        Agency = _agency_model()
        agency = Agency.objects.filter(pk=scope_id).first()
        if agency is None:
            messages.error(request, "That agency no longer exists.")
            return redirect("tenancy:overview")

        current = agency_is_active(agency)
        if current == enabled:
            messages.info(
                request,
                f"{agency} is already "
                f"{'active' if enabled else 'suspended'}.",
            )
            return redirect("tenancy:overview")

        set_agency_active(
            agency=agency,
            enabled=enabled,
            actor=request.user,
            reason=reason,
        )

        if enabled:
            messages.success(
                request,
                f"{agency} has been activated. Its active country offices and "
                "existing module settings are available again.",
            )
        else:
            messages.warning(
                request,
                f"{agency} has been suspended across UNPASS. All ordinary users "
                "under the agency are blocked, but data and settings are preserved.",
            )
        return redirect("tenancy:overview")

    if scope_type == "office":
        office = (
            CountryOffice.objects
            .filter(pk=scope_id)
            .select_related("agency")
            .first()
        )
        if office is None:
            messages.error(request, "That country office no longer exists.")
            return redirect("tenancy:overview")

        if bool(office.is_active) == enabled:
            messages.info(
                request,
                f"{office} is already "
                f"{'active' if enabled else 'suspended'}.",
            )
            return redirect("tenancy:overview")

        set_office_active(
            office=office,
            enabled=enabled,
            actor=request.user,
            reason=reason,
        )

        if enabled:
            if agency_is_active(office.agency):
                messages.success(
                    request,
                    f"{office} has been activated. Existing users and module "
                    "settings are available again.",
                )
            else:
                messages.warning(
                    request,
                    f"{office} is marked active, but {office.agency} is still "
                    "suspended, so the office remains unavailable until the "
                    "agency is activated.",
                )
        else:
            messages.warning(
                request,
                f"{office} has been suspended. Users assigned to this office "
                "are blocked, while other offices remain unchanged.",
            )
        return redirect("tenancy:overview")

    messages.error(request, "Unknown organization type.")
    return redirect("tenancy:overview")


@login_required
@superuser_required
@require_http_methods(["GET", "POST"])
def global_modules(request):
    """
    Platform-wide kill switch.

    Turning a module off here masks all agency and country-office grants without
    deleting or modifying those grants. Re-enabling restores normal tenancy
    resolution.
    """
    if request.method == "POST":
        code = (request.POST.get("feature_code") or "").strip()
        action = (request.POST.get("action") or "").strip().lower()
        reason = (request.POST.get("reason") or "").strip()

        if code not in FEATURES_BY_CODE:
            messages.error(request, "Unknown UNPASS module.")
            return redirect("tenancy:global_modules")

        if action not in {"enable", "disable"}:
            messages.error(request, "Choose Activate or Disable.")
            return redirect("tenancy:global_modules")

        enabled = action == "enable"
        current = global_switch_detail(code)

        if bool(current["enabled"]) == enabled:
            messages.info(
                request,
                f"{current['name']} is already globally "
                f"{'active' if enabled else 'disabled'}.",
            )
            return redirect("tenancy:global_modules")

        set_global_feature(
            code=code,
            enabled=enabled,
            actor=request.user,
            reason=reason,
        )

        if enabled:
            messages.success(
                request,
                f"{FEATURES_BY_CODE[code].name} is globally active again. "
                "Agency and country-office settings now apply normally.",
            )
        else:
            messages.warning(
                request,
                f"{FEATURES_BY_CODE[code].name} is now disabled across UNPASS. "
                "Existing data and local module settings were preserved.",
            )
        return redirect("tenancy:global_modules")

    effective = global_enabled_codes()
    details = global_switch_details()
    groups = []

    for category_code, category_label, features in features_by_category():
        items = []
        for feat in features:
            detail = dict(details[feat.code])
            detail["feature"] = feat
            detail["blocked_parent_names"] = [
                FEATURES_BY_CODE[parent].name
                for parent in detail["blocked_by"]
                if parent in FEATURES_BY_CODE
            ]
            items.append(detail)

        groups.append({
            "code": category_code,
            "label": category_label,
            "items": items,
        })

    return render(
        request,
        "tenancy/global_modules.html",
        {
            "groups": groups,
            "effective_count": len(effective),
            "total_count": len(FEATURES_BY_CODE),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def feature_console(request, scope_key):
    """
    Agency/office switch panel with global-master-switch awareness.

    A globally unavailable feature is read-only here. Its underlying grant is
    preserved exactly as it was, so reactivating the platform module restores
    the previous local configuration.
    """
    office, agency = parse_scope_key(scope_key)
    if not office and not agency:
        raise Http404("Unknown scope.")

    is_super = request.user.is_superuser
    if not is_super:
        role = admin_role(request.user)
        if (
            not role
            or not office
            or role.country_office_id != office.pk
            or role.level != "main"
        ):
            messages.error(request, "You can only manage your own office.")
            return redirect("dashboard:dashboard")

    agency_id = office.agency_id if office else agency.pk
    office_id = office.pk if office else None

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "copy" and is_super:
            source = request.POST.get("source_scope", "")
            n = copy_features(
                source_key=source,
                target_key=scope_key,
                actor=request.user,
            )
            messages.success(
                request,
                f"Copied {n} active global module setting(s) across. "
                "Globally stopped modules were left untouched.",
            )
            return redirect("tenancy:feature_console", scope_key=scope_key)

        submitted = set(request.POST.getlist("features"))
        changed = 0
        globally_locked = 0
        globally_available_codes = global_enabled_codes()

        for feat in FEATURES:
            # Global OFF (including dependency blocking) freezes the underlying
            # agency/office choice instead of rewriting it.
            if feat.code not in globally_available_codes:
                globally_locked += 1
                continue

            if not (
                is_super
                or (
                    feat.code in DELEGABLE_CODES
                    and can_toggle(request.user, feat.code, office)
                )
            ):
                continue

            wanted = feat.code in submitted
            current = FeatureGrant.objects.filter(
                agency=None if office else agency,
                country_office=office,
                feature_code=feat.code,
            ).first()

            if current and current.enabled == wanted:
                continue

            set_feature(
                code=feat.code,
                enabled=wanted,
                actor=request.user,
                agency=None if office else agency,
                country_office=office,
            )
            changed += 1

        if changed:
            messages.success(request, f"Saved. {changed} module(s) changed.")
        else:
            messages.info(request, "No local module changes to save.")

        if globally_locked:
            messages.info(
                request,
                f"{globally_locked} globally unavailable feature(s) were "
                "left unchanged.",
            )

        return redirect("tenancy:feature_console", scope_key=scope_key)

    # Effective runtime state (includes Global Module Control).
    effective = scope_feature_map(agency_id, office_id)

    # Configuration state underneath the global master switch.
    configured = scope_feature_map_configured(agency_id, office_id)

    explicit = {}
    grants = (
        FeatureGrant.objects.for_office(office_id)
        if office_id
        else FeatureGrant.objects.for_agency(agency_id)
    )
    for grant in grants:
        explicit[grant.feature_code] = grant

    inherited_configured = (
        scope_feature_map_configured(agency_id, None)
        if office_id
        else {}
    )

    global_details = global_switch_details()

    groups = []
    for category_code, category_label, features in features_by_category():
        items = []
        for feat in features:
            grant = explicit.get(feat.code)
            master = global_details[feat.code]
            global_available = bool(master["effective"])

            items.append({
                "feature": feat,

                # Checkbox represents preserved local configuration, not the
                # globally masked effective result.
                "enabled": configured.get(feat.code, False),
                "effective_enabled": effective.get(feat.code, False),

                "grant": grant,
                "is_explicit": grant is not None,
                "inherited_value": (
                    inherited_configured.get(feat.code)
                    if office_id
                    else None
                ),

                "global_enabled": bool(master["enabled"]),
                "global_available": global_available,
                "global_reason": master["reason"],
                "global_blocked_by": [
                    FEATURES_BY_CODE[parent].name
                    for parent in master["blocked_by"]
                    if parent in FEATURES_BY_CODE
                ],

                "editable": (
                    global_available
                    and (
                        is_super
                        or can_toggle(request.user, feat.code, office)
                    )
                ),
                "parent_names": [
                    FEATURES_BY_CODE[parent].name
                    for parent in feat.requires
                    if parent in FEATURES_BY_CODE
                ],
                "parents_ok": all(
                    configured.get(parent, False)
                    for parent in feat.requires
                ),
            })

        groups.append({
            "code": category_code,
            "label": category_label,
            "items": items,
        })

    other_scopes = []
    if is_super:
        Agency = _agency_model()
        for other_agency in Agency.objects.order_by("code"):
            key = f"agency:{other_agency.pk}"
            if key != scope_key:
                other_scopes.append(
                    (key, f"{other_agency.code} (agency-wide)")
                )

        for other_office in (
            CountryOffice.objects
            .select_related("agency")
            .order_by("agency__code", "name")
        ):
            if other_office.scope_key != scope_key:
                other_scopes.append(
                    (other_office.scope_key, str(other_office))
                )

    scope_status = scope_status_for_ids(agency_id, office_id)

    return render(
        request,
        "tenancy/feature_console.html",
        {
            "scope_key": scope_key,
            "scope_status": scope_status,
            "office": office,
            "agency": agency or (office.agency if office else None),
            "groups": groups,
            "enabled_count": sum(1 for value in effective.values() if value),
            "configured_count": sum(1 for value in configured.values() if value),
            "total_count": len(FEATURES),
            "other_scopes": other_scopes,
            "is_super": is_super,
            "delegable_codes": sorted(DELEGABLE_CODES),
        },
    )
