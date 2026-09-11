# accounts/models_esign_studio.py
"""
UN PASS — eSign Studio: PDF workbench, document workflows and form builder.

Add ONE line at the very bottom of accounts/models.py, after the eSign import:

    from .models_esign import *          # noqa: F401,F403  (already there)
    from .models_esign_studio import *   # noqa: F401,F403

Then:  python manage.py makemigrations accounts && python manage.py migrate

How the three parts fit together
--------------------------------
StudioFile          a PDF in your personal workbench. Every tool (convert, merge,
                    remove pages, rotate, edit, watermark...) writes a new
                    StudioFileVersion, so any operation can be undone.

DocumentWorkflow    a flow you draw: approvals, reviews, form-filling steps,
                    signatures, conditions, notifications. The drawing is kept
                    as a JSON graph.

WorkflowRun         one document travelling through a flow. It keeps its own
                    copy of the graph, so editing a flow never changes a run
                    that is already under way.

WorkflowTask        one person's piece of work on a run: approve this, fill
                    that in, acknowledge the other. A signature step does not
                    build its own signing screen — it creates a normal eSign
                    Envelope and waits for it, so signatures carry the same
                    stamping, certificate and audit trail as everything else.

FormTemplate        a form designed with drag and drop.
FormSubmission      one filled-in copy, rendered to PDF on demand.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

AGENCY_MODEL = "accounts.Agency"

__all__ = [
    "StudioFile",
    "StudioFileVersion",
    "DocumentWorkflow",
    "WorkflowRun",
    "WorkflowTask",
    "WorkflowEvent",
    "FormTemplate",
    "FormSubmission",
    "studio_upload_to",
    "run_upload_to",
    "submission_upload_to",
]


def _token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def _run_reference() -> str:
    return "WF-" + uuid.uuid4().hex[:8].upper()


def studio_upload_to(instance, filename):
    owner = getattr(getattr(instance, "studio_file", None), "owner_id", "x")
    return f"esign/studio/{owner}/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.pdf"


def run_upload_to(instance, filename):
    return f"esign/workflows/{instance.reference}/{uuid.uuid4().hex[:8]}-{filename}"


def submission_upload_to(instance, filename):
    return f"esign/forms/{timezone.now():%Y/%m}/{instance.reference}-{uuid.uuid4().hex[:6]}.pdf"


SHARE_PRIVATE = "private"
SHARE_OFFICE = "office"
SHARE_AGENCY = "agency"
SHARE_CHOICES = [
    (SHARE_PRIVATE, "Only me"),
    (SHARE_OFFICE, "People in my country office"),
    (SHARE_AGENCY, "People in my agency"),
]


# ─────────────────────────────────────────────────────────────────────────────
# PDF workbench
# ─────────────────────────────────────────────────────────────────────────────

class StudioFile(models.Model):
    """A PDF in someone's workbench. Private to its owner."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="esign_studio_files"
    )
    agency = models.ForeignKey(
        AGENCY_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="esign_studio_files",
    )
    name = models.CharField(max_length=200)
    current = models.ForeignKey(
        "accounts.StudioFileVersion", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    origin = models.CharField(max_length=40, blank=True, default="upload")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Studio file"

    def __str__(self):
        return self.name

    @property
    def page_count(self) -> int:
        return self.current.page_count if self.current else 0

    @property
    def size(self) -> int:
        return self.current.size if self.current else 0

    @property
    def last_operation(self) -> str:
        return self.current.operation if self.current else ""

    @property
    def download_name(self) -> str:
        stem = self.name.rsplit(".", 1)[0] if self.name.lower().endswith(".pdf") else self.name
        return f"{stem[:120] or 'document'}.pdf"

    def read_bytes(self) -> bytes:
        if not self.current or not self.current.file:
            return b""
        handle = self.current.file
        handle.open("rb")
        try:
            return handle.read()
        finally:
            handle.close()


class StudioFileVersion(models.Model):
    """One saved state of a StudioFile. Reverting writes a new version."""

    studio_file = models.ForeignKey(StudioFile, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField(default=1)
    file = models.FileField(upload_to=studio_upload_to)
    operation = models.CharField(max_length=160, blank=True, default="")
    page_count = models.PositiveIntegerField(default=0)
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        unique_together = [("studio_file", "number")]

    def __str__(self):
        return f"{self.studio_file} v{self.number}"


# ─────────────────────────────────────────────────────────────────────────────
# Workflows
# ─────────────────────────────────────────────────────────────────────────────

class DocumentWorkflow(models.Model):
    """A flow drawn in the designer. `graph` holds nodes and edges."""

    agency = models.ForeignKey(
        AGENCY_MODEL, on_delete=models.CASCADE, related_name="esign_workflows"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="esign_workflows_created",
    )
    #: Snapshot of the creator's country office, so sharing keeps working if
    #: the creator's account is later removed.
    office_id = models.PositiveIntegerField(null=True, blank=True)

    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default="")
    graph = models.JSONField(default=dict, blank=True)

    form = models.ForeignKey(
        "accounts.FormTemplate", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="flows_designed",
        help_text="The form this flow was designed around; its fields drive conditions.",
    )
    share_scope = models.CharField(max_length=10, choices=SHARE_CHOICES, default=SHARE_PRIVATE)
    monitor_runs = models.BooleanField(
        default=True,
        help_text="The owner can open every run of this flow, not only the ones they take part in.",
    )
    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Document workflow"

    def __str__(self):
        return self.name

    @property
    def node_count(self) -> int:
        return len((self.graph or {}).get("nodes") or [])

    @property
    def step_count(self) -> int:
        """Steps a person acts on — what 'a 3-step flow' means to the reader."""
        return len(self.step_summary())

    def step_summary(self):
        """Human steps in drawing order, for list cards."""
        human = ("approval", "review", "fill", "signature")
        nodes = sorted((self.graph or {}).get("nodes") or [], key=lambda n: (n.get("x", 0), n.get("y", 0)))
        return [n for n in nodes if n.get("type") in human]


class WorkflowRun(models.Model):
    STATUS_RUNNING = "running"
    STATUS_RETURNED = "returned"
    STATUS_BLOCKED = "blocked"
    STATUS_COMPLETED = "completed"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "In progress"),
        (STATUS_RETURNED, "Returned for changes"),
        (STATUS_BLOCKED, "Needs attention"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
    ]
    OPEN_STATUSES = (STATUS_RUNNING, STATUS_RETURNED, STATUS_BLOCKED)

    workflow = models.ForeignKey(
        DocumentWorkflow, on_delete=models.SET_NULL, null=True, blank=True, related_name="runs"
    )
    workflow_name = models.CharField(max_length=150, blank=True, default="")
    workflow_version = models.PositiveIntegerField(default=1)
    graph = models.JSONField(default=dict, blank=True)

    agency = models.ForeignKey(
        AGENCY_MODEL, on_delete=models.CASCADE, related_name="esign_workflow_runs"
    )
    reference = models.CharField(max_length=32, unique=True, default=_run_reference, editable=False)
    subject = models.CharField(max_length=200)
    message = models.TextField(blank=True, default="")

    initiator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="esign_workflow_runs",
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_RUNNING)

    #: The working PDF. Replaced by the signed PDF after each signature step.
    document = models.FileField(upload_to=run_upload_to, blank=True, null=True)
    document_name = models.CharField(max_length=200, blank=True, default="")
    submission = models.ForeignKey(
        "accounts.FormSubmission", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs",
    )

    #: Engine state: chosen people, join arrivals, per-node rounds, overrides.
    context = models.JSONField(default=dict, blank=True)
    block_reason = models.TextField(blank=True, default="")
    outcome_note = models.CharField(max_length=300, blank=True, default="")

    final_pdf = models.FileField(upload_to=run_upload_to, blank=True, null=True)

    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Workflow run"

    def __str__(self):
        return f"{self.subject} ({self.reference})"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_RUNNING: "warning",
            self.STATUS_RETURNED: "warning",
            self.STATUS_BLOCKED: "danger",
            self.STATUS_COMPLETED: "success",
            self.STATUS_REJECTED: "danger",
            self.STATUS_CANCELLED: "dark",
        }.get(self.status, "secondary")

    def node(self, node_id):
        for n in (self.graph or {}).get("nodes") or []:
            if n.get("id") == node_id:
                return n
        return None

    def progress(self):
        human = [n for n in (self.graph or {}).get("nodes") or []
                 if n.get("type") in ("approval", "review", "fill", "signature")]
        done_nodes = set(
            self.tasks.filter(status__in=WorkflowTask.CLOSED_OK).values_list("node_id", flat=True)
        )
        total = len(human)
        done = len([n for n in human if n["id"] in done_nodes])
        if self.status == self.STATUS_COMPLETED:
            done = total
        return {"total": total, "done": done,
                "percent": int(round(done * 100 / total)) if total else (100 if not self.is_open else 0)}


class WorkflowTask(models.Model):
    KIND_APPROVAL = "approval"
    KIND_REVIEW = "review"
    KIND_FILL = "fill"
    KIND_SIGNATURE = "signature"
    KIND_PREPARE = "prepare"
    KIND_RESUBMIT = "resubmit"
    KIND_CHOICES = [
        (KIND_APPROVAL, "Approve"),
        (KIND_REVIEW, "Review and acknowledge"),
        (KIND_FILL, "Complete the form"),
        (KIND_SIGNATURE, "Signature"),
        (KIND_PREPARE, "Place signature fields"),
        (KIND_RESUBMIT, "Update and resubmit"),
    ]

    STATUS_PENDING = "pending"
    STATUS_WAITING = "waiting"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_RETURNED = "returned"
    STATUS_DONE = "done"
    STATUS_SKIPPED = "skipped"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Waiting for action"),
        (STATUS_WAITING, "Out for signature"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_RETURNED, "Returned for changes"),
        (STATUS_DONE, "Done"),
        (STATUS_SKIPPED, "Not needed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]
    OPEN = (STATUS_PENDING, STATUS_WAITING)
    CLOSED_OK = (STATUS_APPROVED, STATUS_DONE)

    run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="tasks")
    node_id = models.CharField(max_length=40)
    node_label = models.CharField(max_length=150, blank=True, default="")
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    #: Increments each time the run re-enters the node, so a decision made in an
    #: earlier round (before a return for changes) never counts again.
    round = models.PositiveSmallIntegerField(default=1)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="esign_workflow_tasks",
    )
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True, default="")
    token = models.CharField(max_length=64, unique=True, default=_token, db_index=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    comment = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_ip = models.GenericIPAddressField(null=True, blank=True)

    envelope = models.ForeignKey(
        "accounts.Envelope", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="workflow_tasks",
    )
    due_at = models.DateTimeField(null=True, blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Workflow task"

    def __str__(self):
        return f"{self.get_kind_display()} — {self.name} ({self.run.reference})"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN

    @property
    def is_overdue(self) -> bool:
        return bool(self.is_open and self.due_at and self.due_at < timezone.now())

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_PENDING: "warning",
            self.STATUS_WAITING: "info",
            self.STATUS_APPROVED: "success",
            self.STATUS_DONE: "success",
            self.STATUS_REJECTED: "danger",
            self.STATUS_RETURNED: "warning",
            self.STATUS_SKIPPED: "secondary",
            self.STATUS_CANCELLED: "dark",
        }.get(self.status, "secondary")

    @property
    def icon(self) -> str:
        return {
            self.KIND_APPROVAL: "bi-patch-check",
            self.KIND_REVIEW: "bi-eye",
            self.KIND_FILL: "bi-input-cursor-text",
            self.KIND_SIGNATURE: "bi-pen",
            self.KIND_PREPARE: "bi-crosshair",
            self.KIND_RESUBMIT: "bi-arrow-repeat",
        }.get(self.kind, "bi-dot")


class WorkflowEvent(models.Model):
    """Immutable audit trail for a run. Rendered into the workflow record PDF."""

    EVENTS = [
        ("started", "Started"),
        ("step", "Step reached"),
        ("assigned", "Assigned"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("returned", "Returned for changes"),
        ("resubmitted", "Resubmitted"),
        ("acknowledged", "Acknowledged"),
        ("filled", "Form completed"),
        ("sent_for_signature", "Sent for signature"),
        ("signed", "Signatures completed"),
        ("declined", "Signature declined"),
        ("condition", "Condition evaluated"),
        ("notified", "Notification sent"),
        ("reassigned", "Reassigned"),
        ("blocked", "Needs attention"),
        ("retried", "Step retried"),
        ("viewed", "Viewed"),
        ("commented", "Comment"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
        ("downloaded", "Downloaded"),
    ]

    run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="events")
    task = models.ForeignKey(
        WorkflowTask, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    actor_name = models.CharField(max_length=150, blank=True, default="")
    event = models.CharField(max_length=24, choices=EVENTS)
    node_id = models.CharField(max_length=40, blank=True, default="")
    note = models.CharField(max_length=300, blank=True, default="")
    ip = models.GenericIPAddressField(null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} — {self.get_event_display()}"

    @property
    def icon(self) -> str:
        return {
            "started": "bi-play-circle",
            "step": "bi-signpost-split",
            "assigned": "bi-person-plus",
            "approved": "bi-patch-check",
            "rejected": "bi-x-octagon",
            "returned": "bi-arrow-return-left",
            "resubmitted": "bi-arrow-repeat",
            "acknowledged": "bi-eye",
            "filled": "bi-input-cursor-text",
            "sent_for_signature": "bi-send",
            "signed": "bi-pen",
            "declined": "bi-x-circle",
            "condition": "bi-diamond",
            "notified": "bi-bell",
            "reassigned": "bi-person-gear",
            "blocked": "bi-exclamation-triangle",
            "retried": "bi-arrow-clockwise",
            "viewed": "bi-eye",
            "commented": "bi-chat-left-text",
            "completed": "bi-check2-circle",
            "cancelled": "bi-slash-circle",
            "downloaded": "bi-download",
        }.get(self.event, "bi-dot")


# ─────────────────────────────────────────────────────────────────────────────
# Forms
# ─────────────────────────────────────────────────────────────────────────────

class FormTemplate(models.Model):
    ACCESS_INTERNAL = "internal"
    ACCESS_LINK = "link"
    ACCESS_CHOICES = [
        (ACCESS_INTERNAL, "Signed-in colleagues it is shared with"),
        (ACCESS_LINK, "Anyone with the link"),
    ]

    agency = models.ForeignKey(AGENCY_MODEL, on_delete=models.CASCADE, related_name="esign_forms")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="esign_forms_created",
    )
    office_id = models.PositiveIntegerField(null=True, blank=True)

    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=60, blank=True, default="")
    schema = models.JSONField(default=dict, blank=True)

    workflow = models.ForeignKey(
        DocumentWorkflow, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="forms",
        help_text="Every submission starts this flow automatically.",
    )
    share_scope = models.CharField(max_length=10, choices=SHARE_CHOICES, default=SHARE_OFFICE)
    access = models.CharField(max_length=10, choices=ACCESS_CHOICES, default=ACCESS_INTERNAL)
    public_token = models.CharField(max_length=64, unique=True, default=_token, editable=False)
    is_published = models.BooleanField(default=False)
    owner_sees_submissions = models.BooleanField(default=True)
    reference_prefix = models.CharField(max_length=16, blank=True, default="FRM")
    submit_message = models.CharField(max_length=300, blank=True, default="")
    version = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Form template"

    def __str__(self):
        return self.name

    @property
    def theme(self) -> dict:
        return (self.schema or {}).get("theme") or {}

    @property
    def accent(self) -> str:
        return self.theme.get("accent") or "#009EDB"

    @property
    def field_count(self) -> int:
        return len(self.input_elements())

    def input_elements(self):
        from .form_pdf_esign import INPUT_TYPES

        return [e for e in (self.schema or {}).get("elements") or [] if e.get("type") in INPUT_TYPES]


class FormSubmission(models.Model):
    STATUS_SUBMITTED = "submitted"
    STATUS_IN_FLOW = "in_flow"
    STATUS_COMPLETED = "completed"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_IN_FLOW, "In workflow"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    form = models.ForeignKey(
        FormTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="submissions"
    )
    form_name = models.CharField(max_length=150, blank=True, default="")
    schema = models.JSONField(default=dict, blank=True)
    values = models.JSONField(default=dict, blank=True)
    reference = models.CharField(max_length=40, unique=True)

    agency = models.ForeignKey(
        AGENCY_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="esign_form_submissions",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="esign_form_submissions",
    )
    submitter_name = models.CharField(max_length=150, blank=True, default="")
    submitter_email = models.EmailField(blank=True, default="")

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_SUBMITTED)
    pdf = models.FileField(upload_to=submission_upload_to, blank=True, null=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Form submission"

    def __str__(self):
        return f"{self.form_name} — {self.reference}"

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_SUBMITTED: "primary",
            self.STATUS_IN_FLOW: "warning",
            self.STATUS_COMPLETED: "success",
            self.STATUS_REJECTED: "danger",
            self.STATUS_CANCELLED: "dark",
        }.get(self.status, "secondary")

    @property
    def title(self) -> str:
        header = (self.schema or {}).get("header") or {}
        return header.get("title") or self.form_name


# ─────────────────────────────────────────────────────────────────────────────
# Signature steps wait on ordinary envelopes
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender="accounts.Envelope", dispatch_uid="esign_studio_envelope_status")
def _envelope_status_changed(sender, instance, created, update_fields=None, **kwargs):
    """
    Advance a workflow when an envelope it is waiting on moves.

    Deferred to on_commit for two reasons: signing happens inside an atomic
    block and the completed PDF is saved a moment after the status, and a
    workflow problem must never roll back somebody's signature.
    """
    if created:
        return
    if update_fields is not None and "status" not in update_fields:
        return
    if not instance.workflow_tasks.exists():
        return

    from django.db import transaction

    envelope_id = instance.pk
    status = instance.status

    def _advance():
        from .workflow_engine_esign import on_envelope_status

        on_envelope_status(envelope_id, status)

    transaction.on_commit(_advance)
