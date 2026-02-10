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

    # Create CS request (general)
    path("common-services/request/", views.view_cs_support, name="cs_support"),

    # Create CS request from an incident
    path("incidents/<int:incident_pk>/common-services/request/", views.view_cs_support, name="incident_cs_support"),
    path("<int:pk>/assign/", views.csr_assign_view, name="cs_assign"),

    path("queue/", views.csr_fulfiller_queue, name="csr_queue"),
    path("mine/", views.my_csr_requests, name="my_csr"),

]
