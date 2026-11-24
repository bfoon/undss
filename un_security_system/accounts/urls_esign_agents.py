# accounts/urls_esign_agents.py
"""
NES Studio — Agentic Flows. Included from accounts/urls.py:

    path("", include("accounts.urls_esign_agents")),

Every name starts with esign_agent, so the tenancy gate for NES
("accounts:esign_*") and for the module itself ("accounts:esign_agent*")
both apply without any change to the middleware.
"""
from django.urls import path

from . import views_esign_agents as V

urlpatterns = [
    path("esign/agents/", V.esign_agents, name="esign_agents"),
    path("esign/agents/new/", V.esign_agent_new, name="esign_agent_new"),
    path("esign/agents/compose/", V.esign_agent_compose, name="esign_agent_compose"),
    path("esign/agents/import/", V.esign_agent_import, name="esign_agent_import"),
    path("esign/agents/policy/build/", V.esign_agent_policy_build, name="esign_agent_policy_build"),
    path("esign/agents/policy/library/", V.esign_agent_policy_library, name="esign_agent_policy_library"),
    path("esign/agents/extract/", V.esign_agent_extract, name="esign_agent_extract"),
    path("esign/agents/<int:pk>/design/", V.esign_agent_design, name="esign_agent_design"),
    path("esign/agents/<int:pk>/save/", V.esign_agent_save, name="esign_agent_save"),
    path("esign/agents/<int:pk>/share/", V.esign_agent_share, name="esign_agent_share"),
    path("esign/agents/<int:pk>/toggle/", V.esign_agent_toggle, name="esign_agent_toggle"),
    path("esign/agents/<int:pk>/subscribe/", V.esign_agent_subscribe, name="esign_agent_subscribe"),
    path("esign/agents/<int:pk>/duplicate/", V.esign_agent_duplicate, name="esign_agent_duplicate"),
    path("esign/agents/<int:pk>/delete/", V.esign_agent_delete, name="esign_agent_delete"),
    path("esign/agents/<int:pk>/export/", V.esign_agent_export, name="esign_agent_export"),
    path("esign/agents/<int:pk>/run/", V.esign_agent_run_new, name="esign_agent_run_new"),
    path("esign/agent-runs/<int:pk>/", V.esign_agent_run, name="esign_agent_run"),
    path("esign/agent-runs/<int:pk>/status/", V.esign_agent_run_status, name="esign_agent_run_status"),
    path("esign/agent-runs/<int:pk>/cancel/", V.esign_agent_run_cancel, name="esign_agent_run_cancel"),
    path("esign/agent-runs/<int:pk>/again/", V.esign_agent_run_rerun, name="esign_agent_run_rerun"),
    path("esign/agent-runs/<int:pk>/document/", V.esign_agent_run_document, name="esign_agent_run_document"),
    path("esign/agent-runs/<int:pk>/files/<int:step_id>/", V.esign_agent_step_file, name="esign_agent_step_file"),
    path("esign/agent-check/<str:token>/", V.esign_agent_check, name="esign_agent_check"),
]
