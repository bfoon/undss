# accounts/urls_esign_sentinel.py
"""NES Sentinel routes. The api/ endpoints are for installed Sentinels only."""
from django.urls import path

from . import views_esign_sentinel as V

urlpatterns = [
    path("esign/sentinel/api/enroll/", V.api_enroll, name="esign_sentinel_enroll"),
    path("esign/sentinel/api/feed/", V.api_feed, name="esign_sentinel_feed"),
    path("esign/sentinel/api/state/", V.api_state, name="esign_sentinel_state"),
    path("esign/sentinel/api/alert/", V.api_alert, name="esign_sentinel_alert"),
    path("esign/sentinel/api/heartbeat/", V.api_heartbeat, name="esign_sentinel_heartbeat"),
    path("esign/sentinel/", V.sentinel_home, name="esign_sentinel_home"),
    path("esign/<int:pk>/watchers/", V.envelope_watchers, name="esign_envelope_watchers"),
]
