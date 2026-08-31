"""
tenancy/decorators.py
=====================

Function-view guards.

    from tenancy.decorators import feature_required, office_admin_required

    @login_required
    @feature_required("esign")
    def esign_dashboard(request):
        ...
"""

from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render

from .catalog import FEATURES_BY_CODE
from .services import has_all, has_any, is_main_admin, is_office_admin


def _deny(request, codes, mode="all"):
    """Render the friendly 'module not enabled' page instead of a bare 403."""
    names = [FEATURES_BY_CODE[c].name for c in codes if c in FEATURES_BY_CODE]
    context = {
        "feature_names": names,
        "requires_all": mode == "all",
        "office": getattr(request.user, "country_office", None),
    }
    return render(request, "tenancy/feature_disabled.html", context, status=403)


def feature_required(*codes, mode="all"):
    """
    Block the view unless the caller's office has the feature(s) switched on.

    mode="all"  (default) every code must be enabled
    mode="any"  at least one must be enabled
    """
    check = has_all if mode == "all" else has_any

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("accounts:login")
            if not check(request.user, codes):
                return _deny(request, codes, mode)
            return view_func(request, *args, **kwargs)
        _wrapped.required_features = codes
        return _wrapped
    return decorator


def office_admin_required(view_func):
    """Main or sub admin of their own country office."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        if not is_office_admin(request.user):
            messages.error(request, "You need office admin rights to open that page.")
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped


def main_admin_required(view_func):
    """Main admin only — appointing sub admins, delegable feature switches."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        if not is_main_admin(request.user):
            messages.error(request, "Only a main admin can do that.")
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped


def superuser_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        if not request.user.is_superuser:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped
