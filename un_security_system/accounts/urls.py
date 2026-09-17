"""
UN PASS accounts URL wrapper.

Deployment:
1. Copy current accounts/urls.py to accounts/urls_legacy.py.
2. Replace accounts/urls.py with this file.

All existing routes stay available; Asset Health routes are appended.
"""
from .urls_legacy import *  # noqa: F401,F403
from django.urls import include, path

urlpatterns = list(urlpatterns) + [
    path("", include("accounts.urls_asset_health")),
]
