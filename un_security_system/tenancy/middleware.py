"""
tenancy/middleware.py
=====================

Blanket enforcement, so you do not have to decorate several hundred existing
views one at a time.

Add to settings.MIDDLEWARE after AuthenticationMiddleware:

    "tenancy.middleware.FeatureGateMiddleware",

The gate matches on "<namespace>:<url_name>" using the url_rules declared in
tenancy/catalog.py, so it does not care where each app is mounted. Anything
that matches no rule is allowed through — the gate never blocks by accident.

Decorators still work and are still worth adding to sensitive views; this is a
safety net for URLs nobody remembered to annotate.
"""

from fnmatch import fnmatch

from django.shortcuts import render

from .catalog import FEATURES_BY_CODE, url_rule_index
from .services import enabled_features, feature_map
from .context_processors import SECURITY_DASHBOARD_CODES, _available_modules

#: Never gated, whatever the rules say.
EXEMPT_PREFIXES = (
    "admin:",
    "tenancy:",
    "accounts:login",
    "accounts:logout",
    "accounts:otp_verify",
    "accounts:password_",
    "accounts:profile",
    "accounts:register_with_invite",
    "dashboard:dashboard",
)


class FeatureGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.rules = url_rule_index()

    def __call__(self, request):
        # Expose the resolved tenancy flags directly on the request. This makes
        # base.html/navigation reliable even if the tenancy context processor
        # was accidentally omitted from TEMPLATES. AuthenticationMiddleware
        # must run before this middleware (as documented above).
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
        # self.rules is sorted longest-pattern-first by url_rule_index(), so the
        # most specific rule wins: "vehicles:package_flow_*" is checked before
        # "vehicles:package_*".
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

        if any(route.startswith(p) for p in EXEMPT_PREFIXES):
            return None

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None

        code = self._feature_for(route)
        if not code:
            return None

        if code in enabled_features(user):
            return None

        feat = FEATURES_BY_CODE.get(code)
        return render(
            request,
            "tenancy/feature_disabled.html",
            {
                "feature_names": [feat.name if feat else code],
                "requires_all": True,
                "office": getattr(user, "country_office", None),
            },
            status=403,
        )
