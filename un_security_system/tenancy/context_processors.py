"""
tenancy/context_processors.py
=============================

Global tenancy context used by the navigation and dashboard.

Besides the legacy ``features`` mapping, this exposes a resolved list of the
modules available to the signed-in user.  The list is built from the same
feature_map() used by the middleware, so agency grants, country-office
overrides, dependencies and defaults all stay consistent everywhere.
"""

from django.urls import NoReverseMatch, reverse

from .catalog import CATEGORY_LABELS, FEATURES
from .services import (
    admin_role,
    feature_map,
    is_main_admin,
    is_office_admin,
    scope_of,
    sso_config_for,
)


# The existing dashboard is a security-operations dashboard.  If none of these
# modules is available, dashboard.html switches to the My Modules launcher.
SECURITY_DASHBOARD_CODES = frozenset({
    "visitor_access",
    "vehicle_access",
    "parking_cards",
    "asset_exit",
    "security_incidents",
    "incident_reporting",
})


# Primary landing pages.  Child capabilities deliberately land on their parent
# module when they do not have a useful standalone entry page.
LANDING_CANDIDATES = {
    "visitor_access": ("visitors:visitor_list",),
    "visitor_cards": ("visitors:visitor_card_list", "visitors:visitor_list"),
    "visitor_meeting_link": ("visitors:visitor_list",),
    "vehicle_access": ("vehicles:vehicle_list",),
    "parking_cards": ("vehicles:my_pc_requests", "vehicles:parking_card_list"),
    "key_control": ("vehicles:key_list", "vehicles:quick_key"),
    "asset_exit": ("vehicles:my_asset_exits", "vehicles:asset_exit_new"),
    "security_incidents": ("incidents:triage", "incidents:my_incidents"),
    "incident_reporting": ("incidents:my_incidents", "incidents:new"),
    "comms_devices": ("comms:my_devices",),
    "common_services": ("incidents:my_csr", "incidents:cs_support"),
    "room_booking": ("accounts:room_list", "accounts:my_bookings"),
    "room_attendance": ("accounts:room_list",),
    "mailroom": ("vehicles:package_list",),
    "mailroom_flow": ("vehicles:package_list",),
    "mailroom_signing": ("vehicles:package_list",),
    "ict_console": ("accounts:ict_user_list",),
    "asset_mgmt": ("accounts:asset_management",),
    "consumables": ("accounts:consumables_dashboard", "accounts:asset_management"),
    "exit_clearance": ("accounts:exit_organization", "accounts:asset_management"),
    "mobile_lines": ("accounts:mobile_lines", "accounts:cell_lines", "accounts:asset_management"),
    "esign": ("accounts:esign_dashboard",),
    "esign_markup": ("accounts:esign_dashboard",),
    "id_cards": ("accounts:my_idcard_requests",),
    "analytics": ("dashboard:analytics", "dashboard:reports"),
    "activity_log": ("accounts:activity_log", "accounts:profile"),
    "sso_microsoft": ("tenancy:sso_start",),
}


MODULE_ICONS = {
    "visitor_access": "bi-people",
    "vehicle_access": "bi-car-front",
    "parking_cards": "bi-credit-card",
    "key_control": "bi-key",
    "asset_exit": "bi-box-arrow-right",
    "security_incidents": "bi-shield-exclamation",
    "incident_reporting": "bi-exclamation-triangle",
    "comms_devices": "bi-broadcast",
    "common_services": "bi-tools",
    "room_booking": "bi-door-open",
    "mailroom": "bi-box-seam",
    "ict_console": "bi-person-gear",
    "asset_mgmt": "bi-laptop",
    "mobile_lines": "bi-phone",
    "esign": "bi-pen-fill",
    "id_cards": "bi-person-badge",
    "analytics": "bi-graph-up",
    "activity_log": "bi-clock-history",
    "sso_microsoft": "bi-microsoft",
}


def _first_url(candidates):
    """Return the first route that exists in this deployment."""
    for name in candidates:
        try:
            return reverse(name)
        except NoReverseMatch:
            continue
    return ""


def _available_modules(flags, user):
    """
    Build one card/menu row per enabled root module.

    Enabled child features are shown under their first parent as included
    capabilities.  This prevents items such as eSign markup or room attendance
    from disappearing while also avoiding duplicate top-level modules.
    """
    children_by_parent = {}
    for feat in FEATURES:
        if feat.requires and flags.get(feat.code, False):
            children_by_parent.setdefault(feat.requires[0], []).append(feat.name)

    modules = []
    role = getattr(user, "role", "")

    for feat in FEATURES:
        if feat.requires or not flags.get(feat.code, False):
            continue

        # ICT Console is an administrative module.  An agency default may make
        # the feature technically on, but it should not be offered to ordinary
        # staff who cannot use the console.
        if feat.code == "ict_console" and not (
            getattr(user, "is_superuser", False) or role == "ict_focal"
        ):
            continue

        modules.append({
            "code": feat.code,
            "name": feat.name,
            "category": feat.category,
            "category_label": CATEGORY_LABELS.get(feat.category, feat.category.title()),
            "description": feat.description,
            "icon": MODULE_ICONS.get(feat.code, "bi-grid"),
            "url": _first_url(LANDING_CANDIDATES.get(feat.code, ())),
            "included": children_by_parent.get(feat.code, []),
        })

    return modules


def tenancy(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {
            "features": {},
            "office": None,
            "office_admin_role": None,
            "is_main_admin": False,
            "is_office_admin": False,
            "sso_config": None,
            "asset_mgmt_enabled": False,
            "esign_enabled": False,
            "available_modules": [],
            "available_menu_modules": [],
            "has_security_dashboard": False,
            "assigned_module_count": 0,
        }

    flags = feature_map(user)
    agency_id, office_id = scope_of(user)
    modules = _available_modules(flags, user)

    # eSign already has its own top-level menu, so do not duplicate it in the
    # compact My Modules dropdown.  It still appears on the My Modules dashboard.
    menu_modules = [m for m in modules if m["code"] != "esign"]

    return {
        "features": flags,
        "office": getattr(user, "country_office", None),
        "office_admin_role": admin_role(user),
        "is_main_admin": is_main_admin(user),
        "is_office_admin": is_office_admin(user),
        "sso_config": sso_config_for(agency_id, office_id),
        "available_modules": modules,
        "available_menu_modules": menu_modules,
        "has_security_dashboard": any(flags.get(code, False) for code in SECURITY_DASHBOARD_CODES),
        "assigned_module_count": len(modules),
        # Legacy names used by older templates.
        "asset_mgmt_enabled": flags.get("asset_mgmt", False),
        "esign_enabled": flags.get("esign", False),
    }
