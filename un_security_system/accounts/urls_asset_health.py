from django.urls import path
from . import views_asset_health

urlpatterns = [
    path("assets/<int:asset_id>/health/", views_asset_health.asset_health_detail, name="asset_health_detail"),
    path("assets/<int:asset_id>/health/configure/", views_asset_health.asset_health_configure, name="asset_health_configure"),
    path("assets/<int:asset_id>/health/agent-token/", views_asset_health.issue_agent_token, name="asset_health_agent_token"),
    path("asset-health/", views_asset_health.asset_health_dashboard, name="asset_health_dashboard"),
    path("asset-health/printers/", views_asset_health.printer_management, name="printer_management"),
    path("asset-health/printers/<int:asset_id>/", views_asset_health.printer_detail, name="printer_health_detail"),
    path("asset-health/printers/<int:asset_id>/action/", views_asset_health.printer_action, name="printer_action"),
    path("api/asset-health/v1/heartbeat/", views_asset_health.agent_heartbeat, name="asset_health_agent_heartbeat"),
]
