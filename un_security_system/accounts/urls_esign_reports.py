# accounts/urls_esign_reports.py
from django.urls import path

from . import views_esign_reports as V

urlpatterns = [
    path("esign/reports/", V.reports_home, name="esign_reports"),
    path("esign/reports/new/", V.report_new, name="esign_report_new"),
    path("esign/reports/<int:pk>/", V.report_view, name="esign_report_view"),
    path("esign/reports/<int:pk>/design/", V.report_designer, name="esign_report_designer"),
    path("esign/reports/<int:pk>/save/", V.report_save, name="esign_report_save"),
    path("esign/reports/<int:pk>/query/", V.report_query, name="esign_report_query"),
    path("esign/reports/<int:pk>/duplicate/", V.report_duplicate, name="esign_report_duplicate"),
    path("esign/reports/<int:pk>/delete/", V.report_delete, name="esign_report_delete"),
    path("esign/reports/<int:pk>/export.csv", V.report_export_csv, name="esign_report_export_csv"),
]
