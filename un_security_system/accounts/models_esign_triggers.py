# accounts/models_esign_triggers.py
"""
UN PASS — starting a form or a flow when a module finishes.

A ModuleTrigger says: "when <this moment> happens, and <this is true>, start
<that form or flow>, fill these answers in from the record, and hand it to
<this person>."

Add ONE line at the bottom of accounts/models.py, under the docgen import:

    from .models_esign_triggers import *   # noqa: F401,F403

then:  python manage.py makemigrations accounts && python manage.py migrate
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

__all__ = ["ModuleTrigger", "ModuleTriggerLog"]


class ModuleTrigger(models.Model):
    ACTION_DRAFT = "draft"
    ACTION_SUBMIT = "submit"
    ACTION_FLOW = "flow"
    ACTION_CHOICES = [
        (ACTION_DRAFT, "Open a form for someone to fill in"),
        (ACTION_SUBMIT, "Fill the form in and submit it automatically"),
        (ACTION_FLOW, "Start a workflow straight away"),
    ]

    WHO_REQUESTER = "requester"
    WHO_ISSUER = "issuer"
    WHO_MANAGER = "manager"
    WHO_ACTOR = "actor"
    WHO_FIXED = "fixed"
    WHO_CHOICES = [
        (WHO_REQUESTER, "The person the record is about"),
        (WHO_ISSUER, "Whoever issued or handled it"),
        (WHO_MANAGER, "The approving manager"),
        (WHO_ACTOR, "Whoever completed the step"),
        (WHO_FIXED, "A named person"),
    ]

    name = models.CharField(max_length=150, help_text="What this is for, e.g. “Asset handover undertaking”.")
    description = models.CharField(max_length=300, blank=True, default="")
    event_code = models.CharField(
        max_length=80, db_index=True,
        help_text="The moment this waits for. See accounts/module_events.py.",
    )
    agency = models.ForeignKey(
        "accounts.Agency", on_delete=models.CASCADE, null=True, blank=True,
        related_name="module_triggers",
        help_text="Leave empty to apply to every agency.",
    )
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)

    #: [{"token": "asset_category", "op": "eq", "value": "Laptop"}, …] — all must hold.
    conditions = models.JSONField(default=list, blank=True)

    action = models.CharField(max_length=10, choices=ACTION_CHOICES, default=ACTION_DRAFT)
    target_form = models.ForeignKey(
        "accounts.FormTemplate", on_delete=models.PROTECT, null=True, blank=True,
        related_name="module_triggers",
    )
    target_workflow = models.ForeignKey(
        "accounts.DocumentWorkflow", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="module_triggers",
        help_text="Leave empty to use the form's own workflow.",
    )

    #: [{"target": "<question on the form>", "expr": "<formula over the record>"}]
    field_map = models.JSONField(default=list, blank=True)

    assign_to = models.CharField(max_length=12, choices=WHO_CHOICES, default=WHO_REQUESTER)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="module_trigger_assignments",
    )
    notify = models.BooleanField(default=True, help_text="Email them a link to it.")
    email_subject = models.CharField(max_length=200, blank=True, default="")
    email_message = models.TextField(blank=True, default="")

    once_per_record = models.BooleanField(
        default=True,
        help_text="Never start this twice for the same record, however often the event fires.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="module_triggers_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [models.Index(fields=["event_code", "is_active"])]
        verbose_name = "Module trigger"

    def __str__(self):
        return f"{self.name} ({self.event_code})"

    @property
    def action_label(self) -> str:
        return dict(self.ACTION_CHOICES).get(self.action, self.action)

    @property
    def event(self):
        from . import module_events

        return module_events.get(self.event_code)

    @property
    def event_label(self) -> str:
        event = self.event
        return f"{event.module} — {event.label}" if event else self.event_code


class ModuleTriggerLog(models.Model):
    STATUS_STARTED = "started"
    STATUS_SKIPPED = "skipped"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_STARTED, "Started"),
        (STATUS_SKIPPED, "Skipped"),
        (STATUS_FAILED, "Failed"),
    ]

    trigger = models.ForeignKey(
        ModuleTrigger, on_delete=models.CASCADE, null=True, blank=True, related_name="logs"
    )
    event_code = models.CharField(max_length=80, db_index=True)
    record_model = models.CharField(max_length=80, blank=True, default="")
    record_id = models.CharField(max_length=40, blank=True, default="", db_index=True)
    record_label = models.CharField(max_length=200, blank=True, default="")

    submission = models.ForeignKey(
        "accounts.FormSubmission", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="triggered_by",
    )
    run = models.ForeignKey(
        "accounts.WorkflowRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="triggered_by",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="module_trigger_logs",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_STARTED)
    note = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["record_model", "record_id"])]
        verbose_name = "Module trigger log"

    def __str__(self):
        return f"{self.event_code} → {self.status}"

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_STARTED: "success",
            self.STATUS_SKIPPED: "secondary",
            self.STATUS_FAILED: "danger",
        }.get(self.status, "secondary")
