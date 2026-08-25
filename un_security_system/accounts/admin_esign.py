# accounts/admin_esign.py
"""
Optional. Add to the bottom of accounts/admin.py:

    from .admin_esign import *  # noqa: F401,F403
"""

from django.contrib import admin
from django.contrib.admin.sites import NotRegistered
from django.utils.html import format_html

from .models import AgencyServiceConfig
from .models_esign import (
    Envelope,
    EnvelopeDocument,
    EnvelopeEvent,
    EnvelopeRecipient,
    SignatureField,
    SignatureProfile,
)

__all__ = [
    "EnvelopeAdmin",
    "SignatureProfileAdmin",
    "EnvelopeEventAdmin",
    "AgencyServiceConfigAdmin",
]


class RecipientInline(admin.TabularInline):
    model = EnvelopeRecipient
    extra = 0
    readonly_fields = ("token", "status", "sent_at", "viewed_at", "signed_at", "signed_ip")
    fields = ("order", "name", "email", "role", "status", "token", "signed_at", "signed_ip")


class DocumentInline(admin.TabularInline):
    model = EnvelopeDocument
    extra = 0
    readonly_fields = ("page_count", "uploaded_at")


class FieldInline(admin.TabularInline):
    model = SignatureField
    extra = 0
    readonly_fields = ("filled_at",)
    fields = ("document", "recipient", "kind", "page", "x", "y", "w", "h", "required", "filled_at")


class EventInline(admin.TabularInline):
    model = EnvelopeEvent
    extra = 0
    can_delete = False
    readonly_fields = ("at", "event", "recipient", "actor", "ip", "user_agent", "note")
    fields = readonly_fields
    ordering = ("at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Envelope)
class EnvelopeAdmin(admin.ModelAdmin):
    list_display = ("subject", "envelope_id", "agency", "status", "created_by", "created_at", "completed_at")
    list_filter = ("status", "agency", "created_at")
    search_fields = ("subject", "envelope_id", "reference", "recipients__name", "recipients__email")
    readonly_fields = ("envelope_id", "download_token", "created_at", "sent_at", "completed_at", "voided_at")
    inlines = [DocumentInline, RecipientInline, FieldInline, EventInline]
    date_hierarchy = "created_at"


@admin.register(SignatureProfile)
class SignatureProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "label", "kind", "is_default", "preview", "updated_at")
    list_filter = ("kind", "is_default")
    search_fields = ("user__username", "user__email", "label", "typed_text")

    def preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="height:32px">', obj.image.url)
        return "—"


@admin.register(EnvelopeEvent)
class EnvelopeEventAdmin(admin.ModelAdmin):
    list_display = ("at", "envelope", "event", "recipient", "actor", "ip")
    list_filter = ("event", "at")
    search_fields = ("envelope__envelope_id", "envelope__subject", "recipient__email", "note")
    readonly_fields = [f.name for f in EnvelopeEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------
# Per-agency service switches (Asset Management / eSign)
# admin.py registers this model plainly, so replace that registration.
# ---------------------------------------------------------------------------

try:
    admin.site.unregister(AgencyServiceConfig)
except NotRegistered:
    pass


@admin.register(AgencyServiceConfig)
class AgencyServiceConfigAdmin(admin.ModelAdmin):
    list_display = ("agency", "asset_mgmt_enabled", "esign_enabled", "modules")
    list_filter = ("asset_mgmt_enabled", "esign_enabled")
    list_editable = ("asset_mgmt_enabled", "esign_enabled")
    search_fields = ("agency__code", "agency__name")
    ordering = ("agency__code",)
    fieldsets = (
        (None, {"fields": ("agency",)}),
        ("Asset Management", {
            "fields": (
                "asset_mgmt_enabled",
                "asset_mgmt_is_paid",
                "asset_mgmt_cost_amount",
                "asset_mgmt_cost_currency",
            ),
        }),
        ("eSign", {
            "description": "eSign is independent — it can be on with Asset Management off.",
            "fields": (
                "esign_enabled",
                "esign_is_paid",
                "esign_cost_amount",
                "esign_cost_currency",
            ),
        }),
        ("Asset workflow", {
            "classes": ("collapse",),
            "fields": (
                "require_manager_approval",
                "require_ict_assignment",
                "require_requester_verification",
                "asset_tag_auto_generate",
                "asset_tag_prefix",
                "asset_tag_length",
                "asset_qr_include_url",
            ),
        }),
    )

    @admin.display(description="Modules")
    def modules(self, obj):
        return ", ".join(obj.enabled_services) or "—"