from django.urls import path

from . import security_console_views as views

app_name = "security_console"

urlpatterns = [
    path("", views.security_dashboard, name="dashboard"),
    path("events/", views.security_events, name="events"),
    path("export/events.csv", views.security_export_csv, name="export_csv"),
    path("download/report.html", views.security_download_report, name="download_report"),
    path("password-policy/", views.password_policy, name="password_policy"),
]
