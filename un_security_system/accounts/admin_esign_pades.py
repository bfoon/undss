# accounts/admin_esign_pades.py
"""
Read-only admin for NES PAdES seal records.

Add to the bottom of accounts/admin.py, beside the other NES admin imports:

    from .admin_esign_pades import *  # noqa: F401,F403

Seals are produced by the signing pipeline, never typed in, so every field is
read-only and there is no add form. Deleting a row would only lose the record —
the signature lives in the PDF — so deletion is off too. What this view is for
is the estate question: "is everything completed this quarter sealed, and at
what level?" The `status` and `level_achieved` filters answer it.
"""

from django.contrib import admin
from django.utils.html import format_html

from .models_esign_pades import EnvelopeSeal

__all__ = ["EnvelopeSealAdmin"]


@admin.register(EnvelopeSeal)
class EnvelopeSealAdmin(admin.ModelAdmin):
    list_display = (
        "envelope_link",
        "status_badge",
        "level_achieved",
        "timestamped",
        "signer_common_name",
        "applied_at",
    )
    list_filter = (
        "status", "level_achieved", "timestamped", "certificate_sealed",
        "audit_anchor_timestamped",
    )
    search_fields = (
        "envelope__envelope_id",
        "envelope__subject",
        "document_sha256",
        "signer_common_name",
        "signer_serial",
    )
    date_hierarchy = "applied_at"
    ordering = ("-applied_at",)
    list_select_related = ("envelope",)

    readonly_fields = (
        "envelope",
        "status",
        "level_requested",
        "level_achieved",
        "applied_at",
        "field_name",
        "digest_algorithm",
        "document_sha256",
        "signer_common_name",
        "signer_organization",
        "signer_serial",
        "signer_issuer",
        "certificate_expires_at",
        "timestamped",
        "timestamp_authority",
        "detail",
        "certificate_sealed",
        "certificate_sha256",
        "policy_oid",
        "policy_uri",
        "commitment_type",
        "last_refreshed_at",
        "refresh_count",
        "next_refresh_due",
        "refresh_detail",
        "audit_chain_head",
        "audit_chain_length",
        "audit_anchored_at",
        "audit_anchor_timestamped",
        "audit_detail",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Envelope", {"fields": ("envelope", "status", "detail")}),
        ("Conformance", {
            "fields": (
                "level_requested",
                "level_achieved",
                "applied_at",
                "timestamped",
                "timestamp_authority",
            ),
        }),
        ("Long-term validity", {
            "description": (
                "A signature stays verifiable only while its validation "
                "material can still be assembled. Each refresh adds a "
                "document timestamp over the whole file, extending the "
                "window. Run <code>manage.py esign_pades --refresh-ltv</code> "
                "on a schedule."
            ),
            "fields": (
                "last_refreshed_at",
                "refresh_count",
                "next_refresh_due",
                "refresh_detail",
            ),
        }),
        ("Declared policy", {
            "classes": ("collapse",),
            "fields": ("policy_oid", "policy_uri", "commitment_type"),
        }),
        ("Audit-trail anchor", {
            "description": (
                "The head and length of the hash chain at completion, signed. "
                "This is what makes entries removed from the END of the trail "
                "detectable — the hash links alone cannot catch that."
            ),
            "fields": (
                "audit_chain_head",
                "audit_chain_length",
                "audit_anchored_at",
                "audit_anchor_timestamped",
                "audit_detail",
            ),
        }),
        ("Signature", {
            "fields": (
                "field_name", "digest_algorithm", "document_sha256",
                "certificate_sealed", "certificate_sha256",
            ),
        }),
        ("Certificate", {
            "fields": (
                "signer_common_name",
                "signer_organization",
                "signer_serial",
                "signer_issuer",
                "certificate_expires_at",
            ),
        }),
        ("Record", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Envelope", ordering="envelope__envelope_id")
    def envelope_link(self, obj):
        env = obj.envelope
        return format_html(
            '<a href="/accounts/esign/{}/">{}</a>',
            env.pk,
            env.short_id,
        )

    @admin.display(description="Seal")
    def status_badge(self, obj):
        colour = {
            "sealed": "#198754",
            "failed": "#dc3545",
            "skipped": "#6c757d",
        }.get(obj.status, "#6c757d")
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            colour,
            obj.get_status_display(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Cross-office witnessing
# ─────────────────────────────────────────────────────────────────────────────

from .models_esign_witness import (  # noqa: E402
    WitnessAlert, WitnessDelivery, WitnessPeer, WitnessRecord,
)

__all__ += [
    "WitnessPeerAdmin", "WitnessRecordAdmin",
    "WitnessDeliveryAdmin", "WitnessAlertAdmin",
]


@admin.register(WitnessPeer)
class WitnessPeerAdmin(admin.ModelAdmin):
    """
    Peers are editable here, unlike the rest of this module — registering an
    office is an administrative act, not a produced record. Tokens are the
    exception noted below.
    """
    list_display = ("code", "name", "active", "ready", "last_contact_at")
    list_filter = ("active", "accepts_push", "answers_lookup")
    search_fields = ("code", "name", "base_url")
    readonly_fields = ("last_contact_at", "last_error", "created_at")
    fieldsets = (
        ("Office", {"fields": ("code", "name", "base_url", "active")}),
        ("Direction", {"fields": ("accepts_push", "answers_lookup")}),
        ("Credentials", {
            "description": (
                "The tokens authenticate the connection. What authenticates a "
                "statement is the certificate — a statement signed by any "
                "other key is rejected, so a leaked token alone cannot forge "
                "anything. Rotate tokens with "
                "<code>manage.py esign_witness --new-token</code>."
            ),
            "fields": ("outbound_token", "inbound_token", "certificate_pem"),
        }),
        ("Contact history", {
            "classes": ("collapse",),
            "fields": ("last_contact_at", "last_error", "created_at"),
        }),
    )

    @admin.display(boolean=True, description="Ready")
    def ready(self, obj):
        return obj.is_configured


@admin.register(WitnessRecord)
class WitnessRecordAdmin(admin.ModelAdmin):
    """Attestations are produced, never typed — read-only throughout."""
    list_display = (
        "envelope_uid", "origin_code", "kind", "short_hash",
        "source", "signature_verified", "issued_at",
    )
    list_filter = ("kind", "origin_code", "signature_verified", "received_from")
    search_fields = ("envelope_uid", "document_sha256", "reference", "chain_head")
    date_hierarchy = "issued_at"
    readonly_fields = [f.name for f in WitnessRecord._meta.fields]
    list_select_related = ("received_from", "local_envelope")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Document SHA-256")
    def short_hash(self, obj):
        return (obj.document_sha256 or "")[:20] + "…"

    @admin.display(description="Source")
    def source(self, obj):
        return obj.received_from.code if obj.received_from_id else "ours"


@admin.register(WitnessDelivery)
class WitnessDeliveryAdmin(admin.ModelAdmin):
    list_display = ("peer", "record", "status", "attempts", "last_attempt_at")
    list_filter = ("status", "peer")
    search_fields = ("record__envelope_uid",)
    readonly_fields = [f.name for f in WitnessDelivery._meta.fields]
    list_select_related = ("peer", "record")

    def has_add_permission(self, request):
        return False


@admin.register(WitnessAlert)
class WitnessAlertAdmin(admin.ModelAdmin):
    """
    Alerts can be resolved here with a note, but not deleted: the fact that an
    alarm once fired is part of the record, and a resolved alert with an
    explanation is more useful to an auditor than a clean empty list.
    """
    list_display = (
        "raised_at", "severity", "kind", "envelope_uid", "peer", "resolved",
    )
    list_filter = ("severity", "kind", "resolved", "peer")
    search_fields = ("envelope_uid", "detail", "origin_code")
    date_hierarchy = "raised_at"
    list_select_related = ("peer", "local_envelope")
    readonly_fields = (
        "kind", "severity", "origin_code", "envelope_uid", "local_envelope",
        "peer", "detail", "evidence", "raised_at", "notified_at",
    )
    fields = readonly_fields + ("seen", "resolved", "resolved_note")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        from django.utils import timezone

        if obj.resolved and obj.resolved_at is None:
            obj.resolved_at = timezone.now()
        if not obj.resolved:
            obj.resolved_at = None
        super().save_model(request, obj, form, change)
