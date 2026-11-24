# accounts/models_esign_agents.py
"""
UN PASS — NES Studio: Agentic Flows.

A Flow Studio flow moves a document between *people*. An agentic flow moves
it between *AI steps*: read it, summarise it, check it against a policy page,
convert it, decide where it goes, and tell the right people. People can still
step in — a Human check card pauses the run until someone approves the AI's
work.

Add ONE line at the bottom of accounts/models.py, under the identity import:

    from .models_esign_agents import *    # noqa: F401,F403

then:  python manage.py makemigrations accounts && python manage.py migrate

The parts
---------
AgentFlow          the drawing: cards and connections, kept as a JSON graph,
                   with its trigger and who it is shared with.
AgentFlowShare     one person the flow is shared with, as a user or an editor.
AgentSubscription  "use this shared agent on my documents too" — so one
                   person can build an agent and the whole country office can
                   switch it on for themselves.
AgentRun           one document (or one pasted text) going through a flow. It
                   keeps its own copy of the graph, so editing a flow never
                   changes a run already under way.
AgentStep          what one card did on one run: its output, the model used,
                   the files it made, how long it took — or what went wrong.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

AGENCY_MODEL = "accounts.Agency"

__all__ = [
    "AgentFlow",
    "AgentFlowShare",
    "AgentSubscription",
    "AgentRun",
    "AgentStep",
    "agent_upload_to",
]


def _token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def _run_reference() -> str:
    return "AG-" + uuid.uuid4().hex[:8].upper()


def agent_upload_to(instance, filename):
    run = getattr(instance, "run", None) or instance
    ref = getattr(run, "reference", "x")
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in (filename or "file"))[-90:]
    return f"esign/agents/{timezone.now():%Y/%m}/{ref}/{uuid.uuid4().hex[:8]}-{safe}"


class AgentFlow(models.Model):
    SHARE_PRIVATE = "private"
    SHARE_OFFICE = "office"
    SHARE_AGENCY = "agency"
    SHARE_CHOICES = [
        (SHARE_PRIVATE, "Only me and the people I add"),
        (SHARE_OFFICE, "My whole country office"),
        (SHARE_AGENCY, "My whole agency"),
    ]

    TRIGGER_MANUAL = "manual"
    TRIGGER_RECEIVED = "envelope_received"
    TRIGGER_SENT = "envelope_sent"
    TRIGGER_COMPLETED = "envelope_completed"
    TRIGGER_CHOICES = [
        (TRIGGER_MANUAL, "When I run it"),
        (TRIGGER_RECEIVED, "A document comes to me for signature"),
        (TRIGGER_SENT, "I send a document for signature"),
        (TRIGGER_COMPLETED, "A document I sent is fully signed"),
    ]
    AUTOMATIC = (TRIGGER_RECEIVED, TRIGGER_SENT, TRIGGER_COMPLETED)

    agency = models.ForeignKey(AGENCY_MODEL, on_delete=models.CASCADE, related_name="esign_agent_flows")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="esign_agent_flows",
    )
    #: Snapshot of the creator's country office, so CO sharing keeps working
    #: if the creator's account is later removed.
    office_id = models.PositiveIntegerField(null=True, blank=True)

    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default="")
    graph = models.JSONField(default=dict, blank=True)
    #: Copied from the Trigger card on save, so the envelope signal can find
    #: the flows that care with one indexed query.
    trigger = models.CharField(max_length=24, choices=TRIGGER_CHOICES, default=TRIGGER_MANUAL, db_index=True)
    #: Automatic triggers only fire while this is on. Manual runs ignore it.
    is_enabled = models.BooleanField(default=False)

    share_scope = models.CharField(max_length=10, choices=SHARE_CHOICES, default=SHARE_PRIVATE)
    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    imported_from = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Agentic flow"
        indexes = [models.Index(fields=["trigger", "is_enabled", "is_active"])]

    def __str__(self):
        return self.name

    @property
    def is_automatic(self) -> bool:
        return self.trigger in self.AUTOMATIC

    @property
    def card_count(self) -> int:
        return len([n for n in (self.graph or {}).get("nodes") or [] if n.get("type") not in ("start", "note")])

    @property
    def trigger_label(self) -> str:
        return dict(self.TRIGGER_CHOICES).get(self.trigger, self.trigger)

    @property
    def trigger_icon(self) -> str:
        return {
            self.TRIGGER_MANUAL: "bi-hand-index-thumb",
            self.TRIGGER_RECEIVED: "bi-inbox",
            self.TRIGGER_SENT: "bi-send",
            self.TRIGGER_COMPLETED: "bi-patch-check",
        }.get(self.trigger, "bi-lightning")


class AgentFlowShare(models.Model):
    """One person a flow is shared with. Email-only rows bind on first sign-in."""

    ROLE_USE = "use"
    ROLE_EDIT = "edit"
    ROLE_CHOICES = [(ROLE_USE, "Can use"), (ROLE_EDIT, "Can edit")]

    flow = models.ForeignKey(AgentFlow, on_delete=models.CASCADE, related_name="shares")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="esign_agent_shares",
    )
    email = models.EmailField(blank=True, default="")
    role = models.CharField(max_length=6, choices=ROLE_CHOICES, default=ROLE_USE)
    shared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Agentic flow share"

    def __str__(self):
        who = (self.user.get_full_name() or self.user.username) if self.user_id else self.email
        return f"{self.flow} → {who} ({self.role})"


class AgentSubscription(models.Model):
    """'Run this automatic agent on my documents too.'"""

    flow = models.ForeignKey(AgentFlow, on_delete=models.CASCADE, related_name="subscriptions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="esign_agent_subscriptions")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("flow", "user")]
        verbose_name = "Agentic flow subscription"

    def __str__(self):
        return f"{self.user} uses {self.flow}"


class AgentRun(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_WAITING = "waiting"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_STOPPED = "stopped"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_WAITING, "Waiting for a person"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_STOPPED, "Stopped by a policy"),
    ]
    OPEN_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_WAITING)

    flow = models.ForeignKey(AgentFlow, on_delete=models.SET_NULL, null=True, blank=True, related_name="runs")
    flow_name = models.CharField(max_length=150, blank=True, default="")
    flow_version = models.PositiveIntegerField(default=1)
    graph = models.JSONField(default=dict, blank=True)

    agency = models.ForeignKey(AGENCY_MODEL, on_delete=models.CASCADE, related_name="esign_agent_runs")
    reference = models.CharField(max_length=32, unique=True, default=_run_reference, editable=False)
    #: Who it ran for. Emails to "me" go here; files land in their workbench.
    run_as = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="esign_agent_runs",
    )
    trigger = models.CharField(max_length=24, default=AgentFlow.TRIGGER_MANUAL)
    is_test = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_QUEUED, db_index=True)

    envelope = models.ForeignKey(
        "accounts.Envelope", on_delete=models.SET_NULL, null=True, blank=True, related_name="agent_runs",
    )
    #: Stops the same envelope revision starting the same agent twice.
    dedupe_key = models.CharField(max_length=120, blank=True, default="", db_index=True)

    document = models.FileField(upload_to=agent_upload_to, blank=True, null=True)
    document_name = models.CharField(max_length=200, blank=True, default="")
    input_text = models.TextField(blank=True, default="")
    note = models.CharField(max_length=300, blank=True, default="")

    #: Engine state: outputs by card, knowledge read, which cards are active.
    context = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    provider = models.CharField(max_length=80, blank=True, default="")
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Agentic flow run"

    def __str__(self):
        return f"{self.flow_name} — {self.reference}"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_QUEUED: "secondary",
            self.STATUS_RUNNING: "info",
            self.STATUS_WAITING: "warning",
            self.STATUS_COMPLETED: "success",
            self.STATUS_FAILED: "danger",
            self.STATUS_CANCELLED: "dark",
            self.STATUS_STOPPED: "danger",
        }.get(self.status, "secondary")

    @property
    def subject(self) -> str:
        if self.envelope_id and self.envelope:
            return self.envelope.subject
        if self.document_name:
            return self.document_name
        text = " ".join((self.input_text or "").split())
        return (text[:80] + ("…" if len(text) > 80 else "")) or self.flow_name

    @property
    def duration_seconds(self):
        if self.started_at and self.finished_at:
            return max(0, int((self.finished_at - self.started_at).total_seconds()))
        return None

    def node(self, node_id):
        for n in (self.graph or {}).get("nodes") or []:
            if n.get("id") == node_id:
                return n
        return None


class AgentStep(models.Model):
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_WAITING = "waiting"
    STATUS_SKIPPED = "skipped"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "Working"),
        (STATUS_DONE, "Done"),
        (STATUS_WAITING, "Waiting for a person"),
        (STATUS_SKIPPED, "Skipped"),
        (STATUS_FAILED, "Failed"),
    ]

    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="steps")
    node_id = models.CharField(max_length=40)
    node_label = models.CharField(max_length=150, blank=True, default="")
    kind = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    port = models.CharField(max_length=40, blank=True, default="")

    output_text = models.TextField(blank=True, default="")
    output_data = models.JSONField(default=dict, blank=True)
    output_file = models.FileField(upload_to=agent_upload_to, blank=True, null=True)
    output_file_name = models.CharField(max_length=200, blank=True, default="")
    message = models.CharField(max_length=400, blank=True, default="")

    model = models.CharField(max_length=80, blank=True, default="")
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)

    #: Human check: who decides, by link, like a workflow task.
    token = models.CharField(max_length=64, unique=True, default=_token, db_index=True)
    assignees = models.JSONField(default=list, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    decided_name = models.CharField(max_length=150, blank=True, default="")
    decision_comment = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["started_at", "id"]
        verbose_name = "Agentic flow step"

    def __str__(self):
        return f"{self.node_label} ({self.run.reference})"

    @property
    def duration_ms(self):
        if self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None

    @property
    def duration_label(self) -> str:
        ms = self.duration_ms
        if ms is None:
            return ""
        return f"{ms / 1000:.1f} s" if ms < 10000 else f"{round(ms / 1000)} s"

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_RUNNING: "info",
            self.STATUS_DONE: "success",
            self.STATUS_WAITING: "warning",
            self.STATUS_SKIPPED: "secondary",
            self.STATUS_FAILED: "danger",
        }.get(self.status, "secondary")


# ─────────────────────────────────────────────────────────────────────────────
# Envelope triggers: "a document comes to me for signature", "I send one",
# "one I sent is completed". The status before the save is remembered so only
# real transitions start agents, and the work is deferred until the commit so
# an agent can never roll back — or slow down — anybody's signature.
# ─────────────────────────────────────────────────────────────────────────────

from django.db.models.signals import post_save, pre_save  # noqa: E402
from django.dispatch import receiver  # noqa: E402


@receiver(pre_save, sender="accounts.Envelope", dispatch_uid="esign_agents_envelope_before")
def _agents_envelope_before(sender, instance, update_fields=None, **kwargs):
    if not instance.pk or (update_fields is not None and "status" not in update_fields):
        instance._agent_prev_status = None
        return
    instance._agent_prev_status = (sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first())


@receiver(post_save, sender="accounts.Envelope", dispatch_uid="esign_agents_envelope_after")
def _agents_envelope_after(sender, instance, created, update_fields=None, **kwargs):
    old = getattr(instance, "_agent_prev_status", None)
    new = instance.status
    if created or old is None or old == new or new not in ("sent", "completed"):
        return
    if not getattr(settings, "AGENT_ENVELOPE_TRIGGERS", True):
        return
    if not AgentFlow.objects.filter(trigger__in=AgentFlow.AUTOMATIC, is_enabled=True, is_active=True).exists():
        return

    from django.db import transaction

    envelope_id = instance.pk

    def _go():
        from .agent_engine_esign import on_envelope_status

        on_envelope_status(envelope_id, old, new)

    transaction.on_commit(_go)
