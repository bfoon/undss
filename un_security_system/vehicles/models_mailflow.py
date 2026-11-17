# vehicles/models_mailflow.py
"""
UN PASS — Packages & Mailroom on Flow Studio.

The mailroom can now run a package through any flow drawn in eSign Flow Studio
instead of (or alongside) the classic step-by-step PackageFlowTemplate.

How the pieces fit
------------------
MailDocket          Every agency gets a "Mail docket" — a real Flow Studio form,
                    one for incoming and one for outgoing items. Each package
                    becomes one filled-in docket (a FormSubmission), so a Studio
                    flow can read the package like any form: conditions and
                    routes on item type, sender type or destination, "assign
                    from a form field" on the focal-point email, fill-in steps
                    that record courier details, and signature boxes printed on
                    the docket itself.

MailRoute           A mail rule. "Government letters go through the Registry
                    legal flow", "parcels for UNICEF go through Parcel intake".
                    Rules are checked in priority order when Reception chooses
                    "Let the mail rules decide"; a fallback rule catches the rest.

PackageStudioLink   Ties one Package to its Flow Studio run, the docket it
                    carries and the rule that picked the flow. Package status,
                    completion and in-app notifications are kept in step with
                    the run by vehicles/package_studio.py.

Add this line at the very bottom of vehicles/models.py (already done in the
supplied file):

    from .models_mailflow import *  # noqa: E402,F401,F403
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

__all__ = ["MailDocket", "MailRoute", "PackageStudioLink", "mail_scan_upload_to"]

DIRECTIONS = [
    ("incoming", "Incoming"),
    ("outgoing", "Outgoing"),
]


def mail_scan_upload_to(instance, filename):
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "bin").lower()[:8]
    code = getattr(instance.package, "tracking_code", "pkg")
    return f"package_scans/studio/{timezone.now():%Y/%m}/{code}.{ext}"


class MailDocket(models.Model):
    """The Flow Studio form that carries an agency's packages for one direction."""

    agency = models.ForeignKey(
        "accounts.Agency", on_delete=models.CASCADE, related_name="mail_dockets",
    )
    direction = models.CharField(max_length=10, choices=DIRECTIONS)
    form = models.ForeignKey(
        "accounts.FormTemplate", on_delete=models.CASCADE, related_name="mail_dockets",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="mail_dockets_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("agency", "direction")]
        verbose_name = "Mail docket"

    def __str__(self):
        return f"{self.agency} — {self.get_direction_display()} docket"


class MailRoute(models.Model):
    """
    A mail rule: which Flow Studio flow a package takes.

    Every filled-in criterion must match (AND). Inside one criterion, a comma
    separated list matches if any entry does (OR). Text matches ignore case and
    look for the entry anywhere in the field, so "parcel" matches "Small parcel".
    A rule with no criteria matches everything — that is what a fallback is.
    """

    agency = models.ForeignKey(
        "accounts.Agency", on_delete=models.CASCADE, related_name="mail_routes",
    )
    direction = models.CharField(max_length=10, choices=DIRECTIONS, default="incoming")
    name = models.CharField(max_length=120)
    workflow = models.ForeignKey(
        "accounts.DocumentWorkflow", on_delete=models.CASCADE, related_name="mail_routes",
        help_text="The Flow Studio flow packages matching this rule travel through.",
    )

    priority = models.PositiveIntegerField(
        default=100, help_text="Lower numbers are checked first.",
    )
    match_item_types = models.CharField(
        max_length=300, blank=True,
        help_text="Comma-separated, e.g. parcel, box. Leave blank for any item.",
    )
    match_sender_types = models.CharField(
        max_length=200, blank=True,
        help_text="Comma-separated sender type codes (gov, private, ngo, nonprofit, individual, other).",
    )
    match_destinations = models.CharField(
        max_length=300, blank=True,
        help_text="Comma-separated destination / recipient organisations, e.g. UNICEF, WFP.",
    )
    match_keywords = models.CharField(
        max_length=300, blank=True,
        help_text="Comma-separated words looked for in the description, sender and notes.",
    )
    is_fallback = models.BooleanField(
        default=False,
        help_text="Use this flow when no other rule matches.",
    )
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="mail_routes_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["agency", "direction", "is_fallback", "priority", "name"]
        verbose_name = "Mail rule"

    def __str__(self):
        return f"[{self.get_direction_display()}] {self.name} → {self.workflow}"

    # ── Matching ─────────────────────────────────────────────────────────────
    @staticmethod
    def _items(raw):
        return [p.strip().lower() for p in (raw or "").split(",") if p.strip()]

    @property
    def has_criteria(self) -> bool:
        return any([self.match_item_types, self.match_sender_types,
                    self.match_destinations, self.match_keywords])

    def matches(self, data: dict) -> bool:
        """`data` is the plain dict produced by package_studio.package_facts()."""
        if self.is_fallback:
            return True

        def text(*keys):
            return " ".join(str(data.get(k) or "") for k in keys).lower()

        items = self._items(self.match_item_types)
        if items and not any(i in text("item_type") for i in items):
            return False

        senders = self._items(self.match_sender_types)
        if senders and (data.get("sender_type") or "").lower() not in senders:
            return False

        dests = self._items(self.match_destinations)
        if dests and not any(d in text("destination_agency", "recipient_org") for d in dests):
            return False

        words = self._items(self.match_keywords)
        if words and not any(
            w in text("description", "sender_name", "sender_org", "notes", "for_recipient")
            for w in words
        ):
            return False

        return True

    def criteria_summary(self):
        """Readable chips for the rules table."""
        if self.is_fallback:
            return ["Everything else"]
        out = []
        if self.match_item_types:
            out.append(f"Item: {self.match_item_types}")
        if self.match_sender_types:
            out.append(f"Sender: {self.match_sender_types}")
        if self.match_destinations:
            out.append(f"For: {self.match_destinations}")
        if self.match_keywords:
            out.append(f"Mentions: {self.match_keywords}")
        return out or ["Any item"]


class PackageStudioLink(models.Model):
    """One package travelling through a Flow Studio flow."""

    package = models.OneToOneField(
        "vehicles.Package", on_delete=models.CASCADE, related_name="studio_link",
    )
    workflow = models.ForeignKey(
        "accounts.DocumentWorkflow", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="package_links",
    )
    run = models.OneToOneField(
        "accounts.WorkflowRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="package_link",
    )
    docket = models.ForeignKey(
        "accounts.FormSubmission", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="package_links",
    )
    route = models.ForeignKey(
        MailRoute, on_delete=models.SET_NULL, null=True, blank=True, related_name="packages",
        help_text="The mail rule that picked the flow, if one did.",
    )
    picked_by = models.CharField(
        max_length=10, default="manual",
        choices=[("manual", "Chosen by hand"), ("rule", "Mail rule"), ("fallback", "Fallback rule")],
    )
    scan = models.FileField(upload_to=mail_scan_upload_to, blank=True, null=True)
    stage = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Human-readable step(s) the run is waiting on, kept in sync.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Package on Flow Studio"

    def __str__(self):
        ref = self.run.reference if self.run_id else "no run"
        return f"{self.package.tracking_code} ↔ {ref}"
