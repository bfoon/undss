# accounts/admin_esign_studio.py
"""
Optional. Add to the bottom of accounts/admin.py, after the eSign admin:

    from .admin_esign_studio import *  # noqa: F401,F403

Read-mostly by design. Runs, tasks and events are an audit record: change a
run through the run page (cancel, reassign, retry) so every change is logged,
not by editing rows here.
"""

from django.contrib import admin
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from .models_esign_studio import (
    DocumentWorkflow,
    FormSubmission,
    FormTemplate,
    StudioFile,
    StudioFileVersion,
    WorkflowEvent,
    WorkflowRun,
    WorkflowTask,
)

__all__ = [
    "StudioFileAdmin",
    "DocumentWorkflowAdmin",
    "WorkflowRunAdmin",
    "FormTemplateAdmin",
    "FormSubmissionAdmin",
]


def _link(url_name, pk, label):
    try:
        return format_html('<a href="{}" target="_blank">{}</a>', reverse(url_name, args=[pk]), label)
    except NoReverseMatch:
        return label


# ─────────────────────────────────────────────────────────────────────────────
# PDF workbench
# ─────────────────────────────────────────────────────────────────────────────

class VersionInline(admin.TabularInline):
    model = StudioFileVersion
    extra = 0
    can_delete = False
    fields = ("number", "operation", "page_count", "size", "created_at")
    readonly_fields = fields
    ordering = ("-number",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(StudioFile)
class StudioFileAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "agency", "origin", "pages", "updated_at")
    list_filter = ("origin", "agency")
    search_fields = ("name", "owner__username", "owner__email")
    readonly_fields = ("owner", "agency", "current", "origin", "created_at", "updated_at")
    inlines = [VersionInline]

    @admin.display(description="Pages")
    def pages(self, obj):
        return obj.page_count


# ─────────────────────────────────────────────────────────────────────────────
# Workflows
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(DocumentWorkflow)
class DocumentWorkflowAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "agency", "share_scope", "steps", "version", "is_active", "updated_at")
    list_filter = ("share_scope", "is_active", "agency")
    search_fields = ("name", "description", "created_by__username")
    readonly_fields = ("graph", "version", "created_at", "updated_at", "designer")
    fields = ("name", "description", "created_by", "agency", "office_id", "form", "share_scope",
              "monitor_runs", "is_active", "version", "designer", "graph", "created_at", "updated_at")

    @admin.display(description="Steps")
    def steps(self, obj):
        return obj.step_count

    @admin.display(description="Designer")
    def designer(self, obj):
        return _link("accounts:esign_workflow_designer", obj.pk, "Open in the designer") if obj.pk else "—"


class TaskInline(admin.TabularInline):
    model = WorkflowTask
    extra = 0
    can_delete = False
    fields = ("node_label", "kind", "round", "name", "email", "status", "decided_at", "comment", "envelope")
    readonly_fields = fields
    ordering = ("created_at",)

    def has_add_permission(self, request, obj=None):
        return False


class RunEventInline(admin.TabularInline):
    model = WorkflowEvent
    extra = 0
    can_delete = False
    fields = ("at", "event", "actor", "actor_name", "node_id", "ip", "note")
    readonly_fields = fields
    ordering = ("at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WorkflowRun)
class WorkflowRunAdmin(admin.ModelAdmin):
    list_display = ("reference", "subject", "workflow_name", "initiator", "status", "started_at", "completed_at", "page")
    list_filter = ("status", "agency")
    search_fields = ("reference", "subject", "workflow_name", "initiator__username", "tasks__email")
    date_hierarchy = "started_at"
    inlines = [TaskInline, RunEventInline]
    readonly_fields = [f.name for f in WorkflowRun._meta.fields]

    @admin.display(description="")
    def page(self, obj):
        return _link("accounts:esign_run_detail", obj.pk, "Open")

    def has_add_permission(self, request):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Forms
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(FormTemplate)
class FormTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "created_by", "agency", "access", "is_published", "workflow", "updated_at")
    list_filter = ("is_published", "access", "share_scope", "agency")
    search_fields = ("name", "description", "category", "created_by__username")
    readonly_fields = ("public_token", "schema", "version", "created_at", "updated_at", "designer")

    @admin.display(description="Designer")
    def designer(self, obj):
        return _link("accounts:esign_form_designer", obj.pk, "Open in the designer") if obj.pk else "—"


@admin.register(FormSubmission)
class FormSubmissionAdmin(admin.ModelAdmin):
    list_display = ("reference", "form_name", "submitter_name", "submitter_email", "status", "created_at", "page")
    list_filter = ("status", "agency")
    search_fields = ("reference", "form_name", "submitter_name", "submitter_email")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in FormSubmission._meta.fields]

    @admin.display(description="")
    def page(self, obj):
        return _link("accounts:esign_submission_detail", obj.pk, "Open")

    def has_add_permission(self, request):
        return False
