"""
tenancy/middleware.py
=====================

Blanket feature enforcement for UNPASS.

The gate matches on "<namespace>:<url_name>" using tenancy/catalog.py.  It also
contains explicit mappings for the mobile API routes, because those route names
do not use the normal web-module naming pattern.

Global master switches are resolved by tenancy.services.enabled_features().
Therefore a globally disabled module is blocked here for every authenticated
user, including superusers.  The tenancy/platform console remains exempt so a
superuser can always reactivate a module.
"""

from fnmatch import fnmatch

from django.http import JsonResponse
from django.shortcuts import render

from .catalog import FEATURES_BY_CODE, url_rule_index
from .global_control import is_globally_enabled
from .services import enabled_features, feature_map
from .context_processors import SECURITY_DASHBOARD_CODES, _available_modules


#: Never gated, whatever the feature rules say.
#: All tenancy console routes remain available so the global master switch can
#: never lock the administrator out of the recovery/control screen.
EXEMPT_PREFIXES = (
    "admin:",
    "accounts:login",
    "accounts:logout",
    "accounts:otp_verify",
    "accounts:password_",
    "accounts:profile",
    "accounts:register_with_invite",
    "dashboard:dashboard",
)

# Tenancy administration must remain reachable even when modules are globally
# stopped. Runtime SSO routes are intentionally NOT in this set.
EXEMPT_ROUTES = {
    "tenancy:overview",
    "tenancy:global_modules",
    "tenancy:feature_console",
    "tenancy:feature_explain",
    "tenancy:office_list",
    "tenancy:office_admins",
    "tenancy:office_users",
    "tenancy:sharing_links",
    "tenancy:sso_settings",
    "tenancy:audit_log",
}


# Mobile API routes do not all match the catalogue's normal web URL patterns.
# Mapping them here guarantees that directly calling the API cannot bypass a
# disabled module.
ROUTE_FEATURE_OVERRIDES = {
    # Runtime SSO endpoints. The catalogue already covers start/callback;
    # metadata is included explicitly so a global SSO stop is complete.
    "tenancy:sso_start": "sso_microsoft",
    "tenancy:sso_callback": "sso_microsoft",
    "tenancy:sso_metadata": "sso_microsoft",

    # eSign / forms / flows use the shared eSign engine.
    "accounts:api_m_inbox": "esign",
    "accounts:api_m_task": "esign",
    "accounts:api_m_task_decide": "esign",
    "accounts:api_m_envelopes": "esign",
    "accounts:api_m_envelope": "esign",
    "accounts:api_m_sign_sheet": "esign",
    "accounts:api_m_sign": "esign",
    "accounts:api_m_decline": "esign",
    "accounts:api_m_forms": "esign",
    "accounts:api_m_form": "esign",
    "accounts:api_m_form_submit": "esign",
    "accounts:api_m_submission": "esign",
    "accounts:api_m_flows": "esign",
    "accounts:api_m_runs": "esign",
    "accounts:api_m_run": "esign",
    "accounts:api_m_run_cancel": "esign",

    # Asset app.
    "accounts:api_m_asset_lookup": "asset_mgmt",
    "accounts:api_m_asset_search": "asset_mgmt",
    "accounts:api_m_my_assets": "asset_mgmt",

    # Native room booking app.
    "accounts:api_m_rooms": "room_booking",
    "accounts:api_m_room_bookings": "room_booking",
    "accounts:api_m_room_book": "room_booking",
    "accounts:api_m_room_booking_cancel": "room_booking",
}


class FeatureGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.rules = url_rule_index()

    def __call__(self, request):
        # Expose the resolved tenancy flags directly on the request. This makes
        # base.html/navigation reliable even if the tenancy context processor
        # was accidentally omitted from TEMPLATES.
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            flags = feature_map(user)
            modules = _available_modules(flags, user)
            request.tenancy_features = flags
            request.available_modules = modules
            request.available_menu_modules = [
                module for module in modules if module["code"] != "esign"
            ]
            request.has_security_dashboard = any(
                flags.get(code, False) for code in SECURITY_DASHBOARD_CODES
            )
            request.assigned_module_count = len(modules)
        else:
            request.tenancy_features = {}
            request.available_modules = []
            request.available_menu_modules = []
            request.has_security_dashboard = False
            request.assigned_module_count = 0

        return self.get_response(request)

    def _feature_for(self, route: str):
        # Explicit mobile mappings take priority.
        if route in ROUTE_FEATURE_OVERRIDES:
            return ROUTE_FEATURE_OVERRIDES[route]

        # self.rules is sorted longest-pattern-first by url_rule_index(), so the
        # most specific rule wins.
        for pattern, code in self.rules:
            if fnmatch(route, pattern):
                return code
        return None

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = getattr(request, "resolver_match", None)
        if match is None or not match.url_name:
            return None

        namespace = match.namespace or ""
        route = f"{namespace}:{match.url_name}" if namespace else match.url_name

        if route in EXEMPT_ROUTES or any(
            route.startswith(p) for p in EXEMPT_PREFIXES
        ):
            return None

        code = self._feature_for(route)
        if not code:
            return None

        feat = FEATURES_BY_CODE.get(code)
        feature_name = feat.name if feat else code
        globally_disabled = not is_globally_enabled(code)

        # GLOBAL OFF is absolute. Check it before authentication so tokenized
        # recipient links (for example /accounts/esign/s/<token>/) are stopped
        # as well. This is what makes the switch a real platform kill switch.
        if globally_disabled:
            if route.startswith("accounts:api_m_"):
                return JsonResponse(
                    {
                        "ok": False,
                        "error": (
                            f"{feature_name} has been disabled globally by "
                            "the UNPASS administrator."
                        ),
                        "feature": code,
                        "globally_disabled": True,
                    },
                    status=403,
                )

            user = getattr(request, "user", None)
            return render(
                request,
                "tenancy/feature_disabled.html",
                {
                    "feature_names": [feature_name],
                    "requires_all": True,
                    "office": (
                        getattr(user, "country_office", None)
                        if user is not None and getattr(user, "is_authenticated", False)
                        else None
                    ),
                    "globally_disabled": True,
                },
                status=403,
            )

        # Office/agency feature gating still applies only to authenticated
        # users, preserving existing behaviour for public/tokenized views when
        # the module is globally active.
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None

        if code in enabled_features(user):
            return None

        # The phone app expects JSON. Never return an HTML "feature disabled"
        # page to /api/m/* calls.
        if route.startswith("accounts:api_m_"):
            return JsonResponse(
                {
                    "ok": False,
                    "error": (
                        f"{feature_name} is not enabled for your UNPASS office."
                    ),
                    "feature": code,
                    "globally_disabled": False,
                },
                status=403,
            )

        return render(
            request,
            "tenancy/feature_disabled.html",
            {
                "feature_names": [feature_name],
                "requires_all": True,
                "office": getattr(user, "country_office", None),
                "globally_disabled": False,
            },
            status=403,
        )
