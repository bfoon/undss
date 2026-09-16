from django.urls import path
from . import views
from . import csr_type_views

app_name = "incidents"

urlpatterns = [
    path("new/", views.IncidentCreateView.as_view(), name="new"),
    path("my/", views.MyIncidentListView.as_view(), name="my_incidents"),
    path("triage/", views.TeamIncidentListView.as_view(), name="triage"),
    path("<int:pk>/", views.IncidentDetailView.as_view(), name="incident_detail"),
    path("<int:pk>/update/", views.add_update, name="add_update"),
    path("<int:pk>/status/", views.change_status, name="change_status"),

    path("common-services/dashboard/", views.csr_dashboard, name="csr_dashboard"),

    # Dynamic CSR request types - superuser only.
    path(
        "common-services/request-types/",
        csr_type_views.csr_request_type_list,
        name="csr_type_list",
    ),
    path(
        "common-services/request-types/<int:pk>/edit/",
        csr_type_views.csr_request_type_edit,
        name="csr_type_edit",
    ),
    path(
        "common-services/request-types/<int:pk>/toggle/",
        csr_type_views.csr_request_type_toggle,
        name="csr_type_toggle",
    ),

    path("common-services/request/", views.view_cs_support, name="cs_support"),
    path(
        "incidents/<int:incident_pk>/common-services/request/",
        views.view_cs_support,
        name="incident_cs_support",
    ),
    path("<int:pk>/assign/", views.csr_assign_view, name="cs_assign"),
    path("queue/", views.csr_fulfiller_queue, name="csr_queue"),
    path("mine/", views.my_csr_requests, name="my_csr"),
    path("common-services/<int:pk>/", views.cs_detail, name="cs_detail"),
    path("common-services/<int:pk>/status/", views.cs_update_status, name="cs_update_status"),
    path("common-services/<int:pk>/escalate/", views.cs_escalate, name="cs_escalate"),
]
