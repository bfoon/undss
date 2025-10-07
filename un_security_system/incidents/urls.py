from django.urls import path
from . import views

app_name = "incidents"

urlpatterns = [
    path("new/", views.IncidentCreateView.as_view(), name="new"),
    path("my/", views.MyIncidentListView.as_view(), name="my_incidents"),
    path("triage/", views.TeamIncidentListView.as_view(), name="triage"),  # LSA/SOC
    path("<int:pk>/", views.IncidentDetailView.as_view(), name="incident_detail"),
    path("<int:pk>/update/", views.add_update, name="add_update"),
    path("<int:pk>/status/", views.change_status, name="change_status"),  # LSA/SOC
]
