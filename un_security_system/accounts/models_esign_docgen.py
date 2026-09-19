# accounts/models_esign_docgen.py
"""
UN PASS — eSign Studio: follow-on documents.

One form produces another. A purchase request produces an invoice; a payment
form produces a receipt; a clearance form produces a certificate. The second
document is an ordinary FormTemplate, filled in automatically from the first
one's answers and rendered to PDF.

Add ONE line at the very bottom of accounts/models.py, after the studio import:

    from .models_esign import *           # noqa: F401,F403  (already there)
    from .models_esign_studio import *    # noqa: F401,F403  (already there)
    from .models_esign_docgen import *    # noqa: F401,F403

Then:  python manage.py makemigrations accounts && python manage.py migrate

FormDocumentRule   "when this form is submitted / when its flow completes,
                    build <other form> like this, number it like this, and send
                    it to these people."
GeneratedDocument  one document actually produced, with its PDF and the child
                    submission it was written into.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

__all__ = [
    "FormDocumentRule",
    "GeneratedDocument",
    "generated_upload_to",
]


def generated_upload_to(instance, filename):
    return f"esign/documents/{timezone.now():%Y/%m}/{instance.number or uuid.uuid4().hex[:8]}.pdf"


class FormDocumentRule(models.Model):
    TRIGGER_SUBMIT = "on_submit"
    TRIGGER_COMPLETE = "on_complete"
    TRIGGER_STEP = "on_step"
    TRIGGER_MANUAL = "manual"
    TRIGGER_CHOICES = [
        (TRIGGER_SUBMIT, "As soon as the form is submitted"),
        (TRIGGER_COMPLETE, "When the workflow finishes successfully"),
        (TRIGGER_STEP, "When a particular workflow step is done"),
        (TRIGGER_MANUAL, "Only when someone asks for it"),
    ]

    SEND_IMMEDIATELY = "immediately"
    SEND_WITH_FINAL = "with_final"
    SEND_NEVER = "never"
    SEND_CHOICES = [
        (SEND_IMMEDIATELY, "Email it as soon as it is made"),
        (SEND_WITH_FINAL, "Attach it to the final signed copy"),
        (SEND_NEVER, "Do not email it — keep it on the record"),
    ]

    form = models.ForeignKey(
        "accounts.FormTemplate", on_delete=models.CASCADE, related_name="document_rules",
        help_text="The form whose answers feed the document.",
    )
    target_form = models.ForeignKey(
        "accounts.FormTemplate", on_delete=models.PROTECT, related_name="document_rules_as_target",
        help_text="The form used as the template for the document (invoice, receipt, …).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="esign_document_rules",
    )

    name = models.CharField(max_length=150, help_text="What this produces, e.g. “Invoice”.")
    description = models.CharField(max_length=300, blank=True, default="")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)

    trigger = models.CharField(max_length=16, choices=TRIGGER_CHOICES, default=TRIGGER_COMPLETE)
    node_id = models.CharField(
        max_length=40, blank=True, default="",
        help_text="With “when a step is done”, the workflow step to wait for.",
    )
    condition = models.JSONField(
        default=dict, blank=True,
        help_text="Only produce it when this is true. Same rule builder as the rest of the studio.",
    )

    #: [{"target": "<key on the target form>", "expr": "<formula over this form's answers>"}]
    field_map = models.JSONField(default=list, blank=True)
    #: [{"target": "<table key>", "from": "<table key here>",
    #:   "columns": {"<target column>": "<row formula>"}}]
    table_map = models.JSONField(default=list, blank=True)

    number_prefix = models.CharField(max_length=16, blank=True, default="INV")
    number_format = models.CharField(
        max_length=60, blank=True, default="{prefix}-{year}-{sequence:04d}",
        help_text="Placeholders: {prefix} {year} {month} {sequence} {reference}",
    )

    send_when = models.CharField(max_length=16, choices=SEND_CHOICES, default=SEND_WITH_FINAL)
    send_to_initiator = models.BooleanField(default=True)
    send_to_submitter = models.BooleanField(default=True)
    send_to_signers = models.BooleanField(default=False)
    #: Extra fixed addresses, and keys on the source form that hold an address.
    send_to_emails = models.JSONField(default=list, blank=True)
    send_to_email_fields = models.JSONField(default=list, blank=True)
    email_subject = models.CharField(max_length=200, blank=True, default="")
    email_message = models.TextField(blank=True, default="")

    #: Also start the target form's own workflow (so an invoice can be approved).
    start_target_workflow = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Follow-on document rule"

    def __str__(self):
        return f"{self.name} ({self.form_id} → {self.target_form_id})"

    @property
    def trigger_label(self) -> str:
        return dict(self.TRIGGER_CHOICES).get(self.trigger, self.trigger)

    @property
    def mapped_count(self) -> int:
        return len(self.field_map or []) + len(self.table_map or [])


class GeneratedDocument(models.Model):
    STATUS_READY = "ready"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_READY, "Ready"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Could not be made"),
    ]

    rule = models.ForeignKey(
        FormDocumentRule, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="documents",
    )
    source_submission = models.ForeignKey(
        "accounts.FormSubmission", on_delete=models.CASCADE, related_name="generated_documents",
    )
    submission = models.ForeignKey(
        "accounts.FormSubmission", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="generated_from", help_text="The child submission holding the document's answers.",
    )
    run = models.ForeignKey(
        "accounts.WorkflowRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="generated_documents",
    )
    target_form = models.ForeignKey(
        "accounts.FormTemplate", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="documents_generated",
    )

    title = models.CharField(max_length=200, blank=True, default="")
    number = models.CharField(max_length=60, blank=True, default="", db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_READY)
    error = models.CharField(max_length=300, blank=True, default="")
    pdf = models.FileField(upload_to=generated_upload_to, blank=True, null=True)
    sent_to = models.JSONField(default=list, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Generated document"

    def __str__(self):
        return self.number or self.title or f"Document {self.pk}"

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_READY: "primary",
            self.STATUS_SENT: "success",
            self.STATUS_FAILED: "danger",
        }.get(self.status, "secondary")
