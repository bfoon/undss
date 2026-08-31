"""
tenancy/urls.py
===============

Mount in the project urls.py:

    path("platform/", include("tenancy.urls", namespace="tenancy")),
"""

from django.urls import path

from . import sso, views

app_name = "tenancy"

urlpatterns = [
    # Superuser console
    path("", views.platform_overview, name="overview"),
    path("features/<str:scope_key>/", views.feature_console, name="feature_console"),
    path("features/<str:scope_key>/explain/<str:code>/", views.feature_explain, name="feature_explain"),

    # Country offices
    path("offices/", views.office_list, name="office_list"),
    path("offices/<int:pk>/admins/", views.office_admins, name="office_admins"),
    path("offices/<int:pk>/users/", views.office_users, name="office_users"),

    # Cross-office visibility
    path("sharing/", views.sharing_links, name="sharing_links"),

    # Microsoft SSO (configuration now, sign-in flow later)
    path("sso/", views.sso_settings, name="sso_settings"),
    path("sso/start/", sso.sso_start, name="sso_start"),
    path("sso/callback/", sso.sso_callback, name="sso_callback"),
    path("sso/metadata/", sso.sso_metadata, name="sso_metadata"),

    # Audit
    path("audit/", views.audit_log, name="audit_log"),
]
