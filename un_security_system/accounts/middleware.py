"""
tenancy/middleware.py
=====================

Two platform gates live here:

1. Agency / Country Office status
   A suspended tenant is blocked before normal module resolution.

2. Feature status
   Global Module Control -> office/agency feature grants -> catalogue defaults.

The Platform console remains recoverable by a superuser at all times.
"""

import json
from fnmatch import fnmatch

from django.contrib.auth import authenticate, get_user_model
from django.http import JsonResponse
from django.shortcuts import render

from .catalog import FEATURES_BY_CODE, url_rule_index
from .context_processors import SECURITY_DASHBOARD_CODES, _available_modules
from .global_control import is_globally_enabled
from .scope_control import user_scope_status
from .services import enabled_features, feature_map


# Feature-gate exemptions only. Agency/CO suspension is checked BEFORE these,
# so a suspended user cannot use profile/dashboard simply because those routes
# are not feature-specific.
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

# Tenancy administration routes stay feature-exempt. Organization suspension
# still blocks ordinary users; superusers bypass tenant suspension so they can
# always recover a disabled scope.
EXEMPT_ROUTES = {
    "tenancy:overview",
    "tenancy:global_modules",
    "tenancy:organization_status",
    "tenancy:feature_console",
    "tenancy:feature_explain",
    "tenancy:office_list",
    "tenancy:office_admins",
    "tenancy:office_users",
    "tenancy:sharing_links",
    "tenancy:sso_settings",
    "tenancy:audit_log",
}

SCOPE_LOGOUT_ROUTES = {
    "accounts:logout",
    "accounts:api_m_logout",
}

MOBILE_AUTH_ROUTES = {
    "accounts:api_m_login",
    "accounts:api_m_verify",
    "accounts:api_m_resend",
}


# Mobile APIs do not all match the catalogue's normal web URL patterns.
ROUTE_FEATURE_OVERRIDES = {
    # Runtime SSO endpoints. The configuration page itself remains available.
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

    # Assets.
    "accounts:api_m_asset_lookup": "asset_mgmt",
    "accounts:api_m_asset_search": "asset_mgmt",
    "accounts:api_m_my_assets": "asset_mgmt",

    # Native room booking.
    "accounts:api_m_rooms": "room_booking",
    "accounts:api_m_room_bookings": "room_booking",
    "accounts:api_m_room_book": "room_booking",
    "accounts:api_m_room_booking_cancel": "room_booking",
}


def _route_name(request):
    match = getattr(request, "resolver_match", None)
    if match is None or not match.url_name:
        return ""
    namespace = match.namespace or ""
    return f"{namespace}:{match.url_name}" if namespace else match.url_name


def _json_body(request):
    try:
        data = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _authenticate_identifier(request, identifier, password, *, strict_email=False):
    """
    Authenticate only for pre-login suspension checking.

    The real login view/API still performs its normal authentication. This
    helper has no side effects and is used only to avoid sending OTPs to a
    tenant that is already suspended.
    """
    identifier = (identifier or "").strip()
    if not identifier or not password:
        return None

    user = authenticate(request, username=identifier, password=password)
    if user is not None:
        return user

    if "@" not in identifier:
        return None

    User = get_user_model()
    matches = User._default_manager.filter(email__iexact=identifier)

    if strict_email and matches.count() != 1:
        return None

    candidate = matches.first()
    if candidate is None:
        return None

    return authenticate(
        request,
        username=candidate.get_username(),
        password=password,
    )


def _preauth_user(request, route):
    """
    Return the credentialed user before the normal login view executes.

    This lets a suspended scope fail before an OTP email is generated.
    """
    if request.method != "POST":
        return None

    if route == "accounts:login":
        return _authenticate_identifier(
            request,
            request.POST.get("login"),
            request.POST.get("password"),
            strict_email=False,
        )

    # The browser OTP page already has the pending user ID in its session.
    # Check the scope before consuming the code or calling login().
    if route == "accounts:otp_verify":
        user_id = request.session.get("otp_user_id")
        if user_id:
            User = get_user_model()
            return User._default_manager.filter(pk=user_id).first()
        return None

    if route in MOBILE_AUTH_ROUTES:
        data = _json_body(request)
        identifier = data.get("identifier") or data.get("username")
        return _authenticate_identifier(
            request,
            identifier,
            data.get("password"),
            strict_email=True,
        )

    return None


def _scope_response(request, route, status):
    """
    Return HTML for web and JSON for the phone app.
    """
    agency = status.get("agency")
    office = status.get("office")

    if route.startswith("accounts:api_m_"):
        return JsonResponse(
            {
                "ok": False,
                "error": status.get("message") or (
                    "Your UNPASS organization is currently suspended."
                ),
                "scope_disabled": True,
                "reason": status.get("reason") or "scope_disabled",
                "agency_disabled": not bool(status.get("agency_active", True)),
                "office_disabled": not bool(status.get("office_active", True)),
                "agency": (
                    getattr(agency, "name", "")
                    or getattr(agency, "code", "")
                    or ""
                ),
                "office": getattr(office, "name", "") or "",
            },
            status=403,
        )

    return render(
        request,
        "tenancy/scope_disabled.html",
        {
            "scope_status": status,
            "agency": agency,
            "office": office,
        },
        status=403,
    )


class FeatureGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.rules = url_rule_index()

    def __call__(self, request):
        # Expose resolved module flags to templates/navigation.
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

        response = self.get_response(request)

        # Some authentication flows (OTP verification and SSO callback) log the
        # user in inside the view. Catch that newly authenticated user before a
        # successful redirect/API response escapes the suspension gate.
        route = _route_name(request)
        user = getattr(request, "user", None)
        if (
            route not in SCOPE_LOGOUT_ROUTES
            and user is not None
            and getattr(user, "is_authenticated", False)
            and not getattr(user, "is_superuser", False)
        ):
            status = user_scope_status(user)
            if not status["active"]:
                return _scope_response(request, route, status)

        return response

    def _feature_for(self, route: str):
        if route in ROUTE_FEATURE_OVERRIDES:
            return ROUTE_FEATURE_OVERRIDES[route]

        # Longest matching catalogue rule wins.
        for pattern, code in self.rules:
            if fnmatch(route, pattern):
                return code
        return None

    def process_view(self, request, view_func, view_args, view_kwargs):
        route = _route_name(request)
        if not route:
            return None

        # ------------------------------------------------------------------
        # 1. Agency / Country Office suspension gate
        # ------------------------------------------------------------------

        # Allow a suspended user to explicitly sign out.
        if route not in SCOPE_LOGOUT_ROUTES:
            user = getattr(request, "user", None)

            if (
                user is not None
                and getattr(user, "is_authenticated", False)
                and not getattr(user, "is_superuser", False)
            ):
                status = user_scope_status(user)
                request.scope_status = status
                if not status["active"]:
                    return _scope_response(request, route, status)

            # Before a username/password login generates an OTP, authenticate
            # only far enough to see whether its tenant is suspended.
            if user is None or not getattr(user, "is_authenticated", False):
                candidate = _preauth_user(request, route)
                if candidate is not None and not getattr(
                    candidate, "is_superuser", False
                ):
                    status = user_scope_status(candidate)
                    if not status["active"]:
                        return _scope_response(request, route, status)

        # ------------------------------------------------------------------
        # 2. Feature gate
        # ------------------------------------------------------------------
        if route in EXEMPT_ROUTES or any(
            route.startswith(prefix) for prefix in EXEMPT_PREFIXES
        ):
            return None

        code = self._feature_for(route)
        if not code:
            return None

        feat = FEATURES_BY_CODE.get(code)
        feature_name = feat.name if feat else code
        globally_disabled = not is_globally_enabled(code)

        # Global OFF is absolute and also blocks anonymous/tokenized module URLs.
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
                        if user is not None
                        and getattr(user, "is_authenticated", False)
                        else None
                    ),
                    "globally_disabled": True,
                },
                status=403,
            )

        # Agency/office module grants apply to authenticated users.
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None

        if code in enabled_features(user):
            return None

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
