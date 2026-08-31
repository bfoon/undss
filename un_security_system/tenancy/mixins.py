"""
tenancy/mixins.py
=================

Class-based view guards, matching the decorators one for one.

    class EsignDashboardView(FeatureRequiredMixin, ListView):
        required_features = ["esign"]

Also provides OfficeScopedQuerysetMixin, which narrows any user-facing
queryset to the caller's country office plus anything linked to it.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from .catalog import FEATURES_BY_CODE
from .services import (
    has_all,
    has_any,
    is_main_admin,
    is_office_admin,
    visible_users_for,
)


class FeatureRequiredMixin(LoginRequiredMixin):
    """Set ``required_features`` on the view. Use ``feature_mode = "any"`` to OR them."""

    required_features = []
    feature_mode = "all"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and self.required_features:
            check = has_all if self.feature_mode == "all" else has_any
            if not check(request.user, self.required_features):
                names = [
                    FEATURES_BY_CODE[c].name
                    for c in self.required_features
                    if c in FEATURES_BY_CODE
                ]
                return render(
                    request,
                    "tenancy/feature_disabled.html",
                    {
                        "feature_names": names,
                        "requires_all": self.feature_mode == "all",
                        "office": getattr(request.user, "country_office", None),
                    },
                    status=403,
                )
        return super().dispatch(request, *args, **kwargs)


class OfficeAdminRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not is_office_admin(request.user):
            messages.error(request, "You need office admin rights to open that page.")
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class MainAdminRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not is_main_admin(request.user):
            messages.error(request, "Only a main admin can do that.")
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class SuperuserRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_superuser:
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class OfficeScopedQuerysetMixin:
    """
    Narrow a queryset to the caller's own office.

    Set ``office_field`` to the path from the model to CountryOffice, e.g.
    "country_office" on User, or "requested_by__country_office" on a request
    model. Superusers are not narrowed.
    """

    office_field = "country_office"

    def scope_queryset(self, qs):
        user = self.request.user
        if user.is_superuser:
            return qs
        office_id = getattr(user, "country_office_id", None)
        if not office_id:
            return qs.none()
        return qs.filter(**{f"{self.office_field}_id": office_id})

    def get_queryset(self):
        return self.scope_queryset(super().get_queryset())


class SharedDirectoryMixin:
    """
    For views that pick people — eSign recipients, asset assignees.

    Set ``directory_feature`` and call ``self.get_directory()`` to obtain the
    user queryset including any linked offices or agencies.
    """

    directory_feature = None

    def get_directory(self, base_queryset=None):
        if not self.directory_feature:
            raise ValueError("Set directory_feature on the view.")
        return visible_users_for(self.request.user, self.directory_feature, base_queryset)
