# accounts/urls_esign_triggers.py
"""Starting a form or a flow when a module finishes its process."""
from django.urls import include, path

from . import views_esign_triggers as V

urlpatterns = [
    path("esign/automation/triggers/", V.esign_trigger_list, name="esign_trigger_list"),
    path("esign/automation/triggers/new/", V.esign_trigger_edit, name="esign_trigger_new"),
    path("esign/automation/triggers/<int:pk>/", V.esign_trigger_edit, name="esign_trigger_edit"),
    path("esign/automation/triggers/<int:pk>/delete/", V.esign_trigger_delete, name="esign_trigger_delete"),
    path("esign/automation/triggers/<int:pk>/preview/", V.esign_trigger_preview, name="esign_trigger_preview"),
    path("esign/automation/event-tokens/", V.esign_trigger_event_tokens, name="esign_trigger_event_tokens"),
    path("esign/automation/form-fields/<int:pk>/", V.esign_form_fields, name="esign_trigger_form_fields"),

    # BI-style reporting for Forms and Workflows. accounts/urls.py already
    # includes this module, so this keeps the production URL file untouched.
    path("", include("accounts.urls_esign_reports")),
]
