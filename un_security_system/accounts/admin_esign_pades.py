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


# ─────────────────────────────────────────────────────────────────────────────
# Signing identities and sole control
# ─────────────────────────────────────────────────────────────────────────────

from .models_esign_identity import (  # noqa: E402
    SignatureAttestation, SignatureIdentity, SignatureKey, SignatureMark,
)

__all__ += [
    "SignatureIdentityAdmin", "SignatureKeyAdmin",
    "SignatureMarkAdmin", "SignatureAttestationAdmin",
]


class _ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False


class SignatureKeyInline(_ReadOnlyInline):
    model = SignatureKey
    fields = ("revision", "algorithm", "key_fingerprint", "is_current",
              "created_at", "retired_at")
    readonly_fields = fields
    ordering = ("-revision",)
    verbose_name_plural = "Keys (read-only — only the owner can rotate these)"


class SignatureMarkInline(_ReadOnlyInline):
    model = SignatureMark
    fields = ("revision", "kind", "is_current", "created_at", "replaced_at")
    readonly_fields = fields
    ordering = ("-revision",)
    verbose_name_plural = "Marks (read-only — only the owner can change these)"


@admin.register(SignatureIdentity)
class SignatureIdentityAdmin(admin.ModelAdmin):
    """
    Visible to administrators, changeable only in one respect: status.

    Suspending an identity is a legitimate administrative act — offboarding,
    a suspected compromise — and it stops the person signing. It does not let
    anyone sign *as* them, which is the line that matters.

    Everything else is read-only here, and no amount of Django permission
    grants will change that: the key material is useless without a passphrase
    this server has never held, and the identifier is additionally protected
    by a database trigger.
    """

    list_display = (
        "signature_id", "enrolled_name", "owner", "status_badge",
        "key_revision", "mark_revision", "use_count", "last_used_at",
    )
    list_filter = ("status", "office_code")
    search_fields = (
        "signature_id", "enrolled_name", "enrolled_email",
        "user__username", "user__email",
    )
    date_hierarchy = "issued_at"
    ordering = ("-issued_at",)
    list_select_related = ("user",)
    inlines = [SignatureKeyInline, SignatureMarkInline]

    readonly_fields = (
        "signature_id", "user", "enrollment_nonce", "office_code",
        "issued_at", "enrolled_name", "enrolled_email", "key_revision",
        "mark_revision", "use_count", "last_used_at", "created_at",
        "updated_at", "sole_control_note",
    )
    fieldsets = (
        ("Identity", {
            "description": (
                "The signature ID is issued once and is immutable. It does "
                "not change when the signatory redraws their signature or "
                "rotates their key — that is the point of it."
            ),
            "fields": ("signature_id", "user", "enrolled_name",
                       "enrolled_email", "office_code", "issued_at"),
        }),
        ("Sole control", {"fields": ("sole_control_note",)}),
        ("Status", {
            "description": (
                "Suspending stops this person signing. It does not let anyone "
                "else sign as them, and it does not release their key."
            ),
            "fields": ("status", "suspended_reason", "status_changed_at"),
        }),
        ("Use", {
            "classes": ("collapse",),
            "fields": ("key_revision", "mark_revision", "use_count",
                       "last_used_at", "enrollment_nonce", "created_at",
                       "updated_at"),
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        from django.utils import timezone

        if change and "status" in (form.changed_data or []):
            obj.status_changed_at = timezone.now()
        super().save_model(request, obj, form, change)

    @admin.display(description="Owner", ordering="user__username")
    def owner(self, obj):
        return obj.user

    @admin.display(description="Status")
    def status_badge(self, obj):
        colour = {"active": "#198754", "suspended": "#fd7e14",
                  "retired": "#6c757d"}.get(obj.status, "#6c757d")
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            colour, obj.get_status_display(),
        )

    @admin.display(description="Who can use this signature")
    def sole_control_note(self, obj):
        key = obj.current_key
        return format_html(
            "<div style='max-width:46rem'>"
            "<p><strong>Only {}.</strong> The private key for this identity "
            "was generated in their browser and encrypted there under a "
            "passphrase this server has never seen and cannot derive. What is "
            "stored is a public key and an opaque blob.</p>"
            "<p>No administrator, superuser or database operator can sign as "
            "this person, and a forgotten passphrase cannot be recovered by "
            "anyone — the signatory rotates to a new key and keeps the same "
            "signature ID.</p>"
            "<p style='color:#6c757d'>Current key: r{} &middot; {} &middot; "
            "fingerprint {}</p></div>",
            obj.enrolled_name or str(obj.user),
            key.revision if key else "—",
            key.algorithm if key else "none on file",
            key.short_fingerprint if key else "—",
        )


@admin.register(SignatureKey)
class SignatureKeyAdmin(admin.ModelAdmin):
    """Read-only. Old keys are retained so past signatures still verify."""

    list_display = ("identity", "revision", "algorithm", "is_current",
                    "created_at", "retired_at")
    list_filter = ("is_current", "algorithm", "kdf", "cipher")
    search_fields = ("identity__signature_id", "key_fingerprint")
    readonly_fields = [f.name for f in SignatureKey._meta.fields] + ["note"]
    list_select_related = ("identity",)
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="About the stored material")
    def note(self, obj):
        return format_html(
            "<div style='max-width:46rem'>The encrypted private key below is "
            "AES-GCM ciphertext. The key that would decrypt it is derived "
            "from the signatory's passphrase, which is not stored anywhere "
            "in this system. Copying this field gives you nothing usable."
            "</div>"
        )


@admin.register(SignatureMark)
class SignatureMarkAdmin(admin.ModelAdmin):
    list_display = ("identity", "revision", "kind", "is_current",
                    "created_at", "replaced_at")
    list_filter = ("kind", "is_current")
    search_fields = ("identity__signature_id",)
    readonly_fields = [f.name for f in SignatureMark._meta.fields]
    list_select_related = ("identity",)
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SignatureAttestation)
class SignatureAttestationAdmin(admin.ModelAdmin):
    """
    Each row is one person's cryptographic act of signing one envelope.
    Evidence: read-only, never deleted.
    """

    list_display = ("created_at", "signature_id", "signer_name",
                    "envelope_link", "verified_badge")
    list_filter = ("verified", "algorithm")
    search_fields = ("signature_id", "signer_name", "signer_email",
                     "envelope__envelope_id", "nonce", "document_sha256")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in SignatureAttestation._meta.fields]
    list_select_related = ("envelope", "identity", "key")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Envelope")
    def envelope_link(self, obj):
        return format_html(
            '<a href="/accounts/esign/{}/">{}</a>',
            obj.envelope_id, obj.envelope.short_id,
        )

    @admin.display(description="Verified")
    def verified_badge(self, obj):
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            "#198754" if obj.verified else "#dc3545",
            "yes" if obj.verified else "NO",
        )
