from django.urls import path

from . import dashboard_esign_views as views

app_name = "esign_analytics"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("drill/<str:dataset>/", views.drilldown, name="drilldown"),
    path("access/", views.manage_access, name="manage_access"),
    path("access/<int:pk>/revoke/", views.revoke_access, name="revoke_access"),
]
