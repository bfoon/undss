# accounts/urls_esign_docgen.py
"""
UN PASS — eSign Studio: signed-copy settings and follow-on documents.

Included from accounts/urls.py next to the other studio URL modules:

    path("", include("accounts.urls_esign_docgen")),
"""
from django.urls import path

from . import views_esign_docgen as V

urlpatterns = [
    path("esign/forms/<int:pk>/automation/",
         V.esign_form_automation, name="esign_form_automation"),
    path("esign/forms/<int:pk>/automation/rules/new/",
         V.esign_document_rule_edit, name="esign_document_rule_new"),
    path("esign/forms/<int:pk>/automation/rules/<int:rule_id>/",
         V.esign_document_rule_edit, name="esign_document_rule_edit"),
    path("esign/forms/<int:pk>/automation/rules/<int:rule_id>/delete/",
         V.esign_document_rule_delete, name="esign_document_rule_delete"),
    path("esign/forms/<int:pk>/automation/rules/<int:rule_id>/preview/",
         V.esign_document_rule_preview, name="esign_document_rule_preview"),
    path("esign/forms/<int:pk>/automation/target-fields/",
         V.esign_document_rule_fields, name="esign_document_rule_fields"),
    path("esign/submissions/<int:submission_pk>/documents/generate/",
         V.esign_document_generate, name="esign_document_generate"),
    path("esign/documents/<int:document_id>/",
         V.esign_document_download, name="esign_document_download"),
    path("esign/documents/<int:document_id>/send/",
         V.esign_document_send, name="esign_document_send"),
]
