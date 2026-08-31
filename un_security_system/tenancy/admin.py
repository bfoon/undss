"""
tenancy/admin.py
================

Django-admin access to the same data as the console. The console is the nicer
place to work; this is here for bulk edits and for support access.
"""

from django import forms
from django.apps import apps
from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html

from .catalog import FEATURES, FEATURES_BY_CODE
from .models import (
    CountryOffice,
    DirectoryShare,
    FeatureAuditLog,
    FeatureGrant,
    OfficeAdmin,
    SSOConfiguration,
)
from .services import bump_cache


class FeatureGrantInline(admin.TabularInline):
    model = FeatureGrant
    fk_name = "country_office"
    extra = 0
    fields = ("feature_code", "enabled", "is_paid", "cost_amount", "valid_until")
    ordering = ("feature_code",)


class OfficeAdminInline(admin.TabularInline):
    model = OfficeAdmin
    extra = 0
    fields = ("user", "level", "can_manage_users", "can_invite", "is_active")
    raw_id_fields = ("user",)


@admin.register(CountryOffice)
class CountryOfficeAdmin(admin.ModelAdmin):
    list_display = ("name", "agency", "code", "country", "enabled_modules",
                    "admin_count", "is_active", "is_default", "console_link")
    list_filter = ("agency", "is_active", "country")
    search_fields = ("name", "code", "country", "city", "agency__code", "agency__name")
    inlines = (FeatureGrantInline, OfficeAdminInline)
    fieldsets = (
        ("Identity", {"fields": ("agency", "name", "code", "logo")}),
        ("Location", {"fields": ("country", "city", "timezone_name")}),
        ("Contact", {"fields": ("contact_email", "contact_phone")}),
        ("Status", {"fields": ("is_active", "is_default")}),
    )

    @admin.display(description="Modules on")
    def enabled_modules(self, obj):
        from .services import scope_feature_map
        flags = scope_feature_map(obj.agency_id, obj.pk)
        return f"{sum(1 for v in flags.values() if v)} of {len(FEATURES)}"

    @admin.display(description="Admins")
    def admin_count(self, obj):
        return obj.admins.filter(is_active=True).count()

    @admin.display(description="Console")
    def console_link(self, obj):
        url = reverse("tenancy:feature_console", args=[obj.scope_key])
        return format_html('<a href="{}">Manage modules</a>', url)


class FeatureGrantAdminForm(forms.ModelForm):
    """Admin form with one unambiguous agency-or-office scope selector."""

    scope = forms.ChoiceField(
        label="Scope",
        required=True,
        help_text="Choose one scope only: an agency-wide grant OR a single country office.",
    )

    class Meta:
        model = FeatureGrant
        fields = (
            "scope",
            "feature_code",
            "enabled",
            "is_paid",
            "cost_amount",
            "cost_currency",
            "valid_until",
            "notes",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        Agency = apps.get_model("accounts", "Agency")
        agencies = Agency.objects.all().order_by("code", "name")
        offices = CountryOffice.objects.select_related("agency").order_by(
            "agency__code", "name"
        )

        agency_choices = [
            (f"agency:{agency.pk}", f"{agency.code} — {agency.name}")
            for agency in agencies
        ]
        office_choices = [
            (f"office:{office.pk}", str(office))
            for office in offices
        ]

        choices = [("", "---------")]
        if agency_choices:
            choices.append(("Agencies", agency_choices))
        if office_choices:
            choices.append(("Country offices", office_choices))
        self.fields["scope"].choices = choices

        if self.instance and self.instance.pk:
            self.fields["scope"].initial = self.instance.scope_key

    def clean(self):
        cleaned = super().clean()
        scope_key = cleaned.get("scope")
        feature_code = cleaned.get("feature_code")

        if not scope_key or ":" not in scope_key:
            self.add_error("scope", "Choose an agency or a country office.")
            return cleaned

        kind, _, raw_pk = scope_key.partition(":")
        try:
            pk = int(raw_pk)
        except (TypeError, ValueError):
            self.add_error("scope", "Invalid scope selected.")
            return cleaned

        agency = None
        office = None
        if kind == "agency":
            Agency = apps.get_model("accounts", "Agency")
            agency = Agency.objects.filter(pk=pk).first()
            if agency is None:
                self.add_error("scope", "The selected agency no longer exists.")
                return cleaned
        elif kind == "office":
            office = CountryOffice.objects.filter(pk=pk).first()
            if office is None:
                self.add_error("scope", "The selected country office no longer exists.")
                return cleaned
        else:
            self.add_error("scope", "Invalid scope selected.")
            return cleaned

        # ScopedModel requires exactly one of these to be populated.
        self.instance.agency = agency
        self.instance.country_office = office

        # Give a friendly error instead of letting the DB unique constraint fail.
        if feature_code:
            duplicate = FeatureGrant.objects.filter(feature_code=feature_code)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if agency is not None:
                duplicate = duplicate.filter(
                    agency=agency,
                    country_office__isnull=True,
                )
                scope_label = str(agency)
            else:
                duplicate = duplicate.filter(country_office=office)
                scope_label = str(office)

            if duplicate.exists():
                self.add_error(
                    "feature_code",
                    f"This feature already has a grant for {scope_label}. Edit the existing grant instead.",
                )

        return cleaned


@admin.register(FeatureGrant)
class FeatureGrantAdmin(admin.ModelAdmin):
    form = FeatureGrantAdminForm
    fields = (
        "scope", "feature_code", "enabled", "is_paid",
        "cost_amount", "cost_currency", "valid_until", "notes", "updated_by",
    )
    readonly_fields = ("updated_by",)
    list_display = ("feature_label", "scope_label", "enabled", "is_paid",
                    "valid_until", "updated_by", "updated_at")
    list_filter = ("enabled", "feature_code", "is_paid", "agency", "country_office")
    search_fields = ("feature_code", "notes", "agency__code", "country_office__name")
    actions = ("action_enable", "action_disable")
    list_select_related = ("agency", "country_office", "updated_by")

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Feature", ordering="feature_code")
    def feature_label(self, obj):
        feat = FEATURES_BY_CODE.get(obj.feature_code)
        return feat.name if feat else obj.feature_code

    @admin.display(description="Scope")
    def scope_label(self, obj):
        return obj.scope_label

    @admin.action(description="Enable selected features")
    def action_enable(self, request, queryset):
        n = queryset.update(enabled=True, updated_by=request.user)
        bump_cache()
        self.message_user(request, f"Enabled {n} grant(s).", messages.SUCCESS)

    @admin.action(description="Disable selected features")
    def action_disable(self, request, queryset):
        n = queryset.update(enabled=False, updated_by=request.user)
        bump_cache()
        self.message_user(request, f"Disabled {n} grant(s).", messages.SUCCESS)


@admin.register(OfficeAdmin)
class OfficeAdminAdmin(admin.ModelAdmin):
    list_display = ("user", "country_office", "level", "can_manage_users",
                    "can_toggle_delegable_features", "is_active", "granted_by", "granted_at")
    list_filter = ("level", "is_active", "country_office")
    search_fields = ("user__username", "user__first_name", "user__last_name",
                     "country_office__name")
    autocomplete_fields = ("country_office",)
    raw_id_fields = ("user", "granted_by")


@admin.register(DirectoryShare)
class DirectoryShareAdmin(admin.ModelAdmin):
    list_display = ("feature_code", "source_label", "direction", "target_label",
                    "is_active", "created_by", "created_at")
    list_filter = ("feature_code", "is_active", "is_mutual")
    autocomplete_fields = ("source_office", "target_office")
    raw_id_fields = ("created_by",)

    @admin.display(description="")
    def direction(self, obj):
        return "↔" if obj.is_mutual else "→"

    @admin.display(description="From")
    def source_label(self, obj):
        return obj.source_label

    @admin.display(description="To")
    def target_label(self, obj):
        return obj.target_label


@admin.register(SSOConfiguration)
class SSOConfigurationAdmin(admin.ModelAdmin):
    list_display = ("scope_label", "provider", "is_enabled", "is_ready",
                    "enforce_sso", "auto_provision_users", "updated_at")
    list_filter = ("provider", "is_enabled", "enforce_sso")
    search_fields = ("tenant_id", "client_id", "allowed_email_domains")
    autocomplete_fields = ("country_office",)
    fieldsets = (
        ("Scope", {"fields": ("agency", "country_office")}),
        ("Entra application", {
            "fields": ("provider", "display_name", "tenant_id", "client_id",
                       "client_secret", "authority", "redirect_uri", "scopes"),
        }),
        ("Account handling", {
            "fields": ("allowed_email_domains", "auto_provision_users",
                       "default_role", "group_role_map"),
        }),
        ("Enforcement", {"fields": ("is_enabled", "enforce_sso", "bypass_for_superusers")}),
        ("Diagnostics", {"fields": ("last_tested_at", "last_test_result")}),
    )
    readonly_fields = ("last_tested_at", "last_test_result")

    @admin.display(description="Scope")
    def scope_label(self, obj):
        return obj.scope_label

    @admin.display(boolean=True, description="Ready")
    def is_ready(self, obj):
        return obj.is_ready


@admin.register(FeatureAuditLog)
class FeatureAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "scope_label", "feature_code", "actor")
    list_filter = ("action", "feature_code")
    search_fields = ("scope_label", "feature_code", "detail", "actor__username")
    date_hierarchy = "created_at"
    readonly_fields = ("actor", "action", "scope_key", "scope_label",
                       "feature_code", "detail", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
