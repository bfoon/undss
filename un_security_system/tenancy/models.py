"""
tenancy/models.py
=================

Multi-tenancy and per-module entitlements for UN PASS.

Hierarchy
---------
    Agency (accounts.Agency)          e.g. UNDP, UNICEF, WFP
      └── CountryOffice               e.g. UNDP Gambia, UNDP Senegal
            └── User                  accounts.User.country_office

Feature resolution precedence (highest first)
---------------------------------------------
    1. FeatureGrant on the user's country office
    2. FeatureGrant on the user's agency
    3. FeatureDef.default_enabled from tenancy/catalog.py

A feature also resolves OFF if any of its ``requires`` parents are off.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .catalog import (
    FEATURE_CHOICES,
    FEATURES_BY_CODE,
    SHAREABLE_CHOICES,
    is_known,
)


# ---------------------------------------------------------------------------
# Scope mixin — every scoped row hangs off exactly one Agency OR one CountryOffice
# ---------------------------------------------------------------------------

class ScopedModel(models.Model):
    """
    Abstract base for rows that apply either agency-wide or to one country
    office. Exactly one of ``agency`` / ``country_office`` must be set.
    """

    agency = models.ForeignKey(
        "accounts.Agency",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="%(class)s_set",
        help_text="Set for an agency-wide row. Leave blank if scoping to one office.",
    )
    country_office = models.ForeignKey(
        "tenancy.CountryOffice",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="%(class)s_set",
        help_text="Set to scope this row to a single country office.",
    )

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if bool(self.agency_id) == bool(self.country_office_id):
            raise ValidationError(
                "Choose exactly one scope: either an agency or a country office."
            )

    @property
    def scope_kind(self) -> str:
        return "office" if self.country_office_id else "agency"

    @property
    def scope_key(self) -> str:
        """Stable string key, e.g. 'office:12' or 'agency:3'."""
        if self.country_office_id:
            return f"office:{self.country_office_id}"
        return f"agency:{self.agency_id}"

    @property
    def scope_label(self) -> str:
        if self.country_office_id:
            return str(self.country_office)
        return str(self.agency)

    @property
    def scope_object(self):
        return self.country_office or self.agency


# ---------------------------------------------------------------------------
# Country office
# ---------------------------------------------------------------------------

class CountryOffice(models.Model):
    """
    A single agency presence in a single country. The unit that owns users and
    (usually) the unit that features are switched on for.
    """

    agency = models.ForeignKey(
        "accounts.Agency",
        on_delete=models.CASCADE,
        related_name="country_offices",
    )
    name = models.CharField(max_length=120, help_text="e.g. Gambia Country Office")
    code = models.CharField(
        max_length=20,
        help_text="Short code, unique within the agency. e.g. GMB",
    )
    country = models.CharField(max_length=80, blank=True, default="")
    city = models.CharField(max_length=80, blank=True, default="")
    timezone_name = models.CharField(
        max_length=64, default="UTC",
        help_text="IANA name, e.g. Africa/Banjul",
    )
    logo = models.ImageField(upload_to="office_logos/", null=True, blank=True)

    contact_email = models.EmailField(blank=True, default="")
    contact_phone = models.CharField(max_length=40, blank=True, default="")

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False,
        help_text="Fallback office for users of this agency who have none set.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Country office"
        verbose_name_plural = "Country offices"
        ordering = ["agency__code", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "code"], name="uniq_office_code_per_agency"
            ),
            models.UniqueConstraint(
                fields=["agency"],
                condition=models.Q(is_default=True),
                name="uniq_default_office_per_agency",
            ),
        ]
        indexes = [
            models.Index(fields=["agency", "is_active"]),
        ]

    def __str__(self):
        return f"{self.agency.code} · {self.name}"

    @property
    def scope_key(self) -> str:
        return f"office:{self.pk}"

    def user_count(self) -> int:
        return self.users.count()


# ---------------------------------------------------------------------------
# Feature grants — the actual on/off switches
# ---------------------------------------------------------------------------

class FeatureGrantQuerySet(models.QuerySet):
    def active(self):
        return self.filter(enabled=True)

    def for_agency(self, agency_id):
        return self.filter(agency_id=agency_id, country_office__isnull=True)

    def for_office(self, office_id):
        return self.filter(country_office_id=office_id)


class FeatureGrant(ScopedModel):
    """
    One row per (scope, feature). Presence of a row is an explicit decision;
    absence falls through to the next level of precedence.
    """

    feature_code = models.CharField(max_length=50, choices=FEATURE_CHOICES, db_index=True)
    enabled = models.BooleanField(default=True)

    # Optional commercial terms, mirroring the old AgencyServiceConfig fields.
    is_paid = models.BooleanField(default=False)
    cost_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cost_currency = models.CharField(max_length=10, default="USD", blank=True)
    valid_until = models.DateField(
        null=True, blank=True,
        help_text="Optional. After this date the feature resolves as off.",
    )

    notes = models.TextField(blank=True, default="")

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="feature_grants_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = FeatureGrantQuerySet.as_manager()

    class Meta:
        verbose_name = "Feature grant"
        verbose_name_plural = "Feature grants"
        ordering = ["feature_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "feature_code"],
                condition=models.Q(country_office__isnull=True),
                name="uniq_grant_per_agency_feature",
            ),
            models.UniqueConstraint(
                fields=["country_office", "feature_code"],
                condition=models.Q(country_office__isnull=False),
                name="uniq_grant_per_office_feature",
            ),
        ]
        indexes = [
            models.Index(fields=["feature_code", "enabled"]),
        ]

    def __str__(self):
        state = "on" if self.enabled else "off"
        return f"{self.scope_label} · {self.feature_name} · {state}"

    def clean(self):
        super().clean()
        if not is_known(self.feature_code):
            raise ValidationError(
                f"'{self.feature_code}' is not in the feature catalogue."
            )

    @property
    def feature(self):
        return FEATURES_BY_CODE.get(self.feature_code)

    @property
    def feature_name(self) -> str:
        f = self.feature
        return f.name if f else self.feature_code

    @property
    def is_expired(self) -> bool:
        return bool(self.valid_until and self.valid_until < timezone.localdate())

    @property
    def effective(self) -> bool:
        return self.enabled and not self.is_expired


# ---------------------------------------------------------------------------
# Admin delegation inside a country office
# ---------------------------------------------------------------------------

class OfficeAdmin(models.Model):
    """
    Who administers a country office.

    main  — appointed by the superuser. Manages users in the office AND may
            appoint or revoke sub admins. May flip features marked delegable.
    sub   — appointed by a main admin. Manages ordinary users only.
    """

    LEVEL_MAIN = "main"
    LEVEL_SUB = "sub"
    LEVEL_CHOICES = (
        (LEVEL_MAIN, "Main admin"),
        (LEVEL_SUB, "Sub admin"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="office_admin_roles",
    )
    country_office = models.ForeignKey(
        CountryOffice,
        on_delete=models.CASCADE,
        related_name="admins",
    )
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default=LEVEL_SUB)

    can_manage_users = models.BooleanField(default=True)
    can_reset_passwords = models.BooleanField(default=True)
    can_invite = models.BooleanField(
        default=True, help_text="May generate registration links and QR invites."
    )
    can_toggle_delegable_features = models.BooleanField(
        default=False,
        help_text="Main admins only: flip features the catalogue marks delegable.",
    )

    is_active = models.BooleanField(default=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="office_admin_grants_made",
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Office admin"
        verbose_name_plural = "Office admins"
        ordering = ["country_office", "level", "user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "country_office"], name="uniq_admin_per_user_office"
            ),
        ]
        indexes = [
            models.Index(fields=["country_office", "level", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user} · {self.get_level_display()} · {self.country_office}"

    @property
    def is_main(self) -> bool:
        return self.level == self.LEVEL_MAIN and self.is_active

    def revoke(self, save=True):
        self.is_active = False
        self.revoked_at = timezone.now()
        if save:
            self.save(update_fields=["is_active", "revoked_at"])


# ---------------------------------------------------------------------------
# Cross-office / cross-agency directory sharing
# ---------------------------------------------------------------------------

class DirectoryShare(models.Model):
    """
    Lets one scope see another scope's users inside a single shared feature.

    Example: link "UNDP Gambia" → "UNICEF Gambia" on ``esign`` so an UNDP
    sender can address an envelope to a UNICEF colleague, without either side
    gaining visibility anywhere else in the platform.
    """

    feature_code = models.CharField(
        max_length=50, choices=SHAREABLE_CHOICES, db_index=True,
        help_text="Only features marked shareable in the catalogue.",
    )

    source_agency = models.ForeignKey(
        "accounts.Agency", on_delete=models.CASCADE,
        null=True, blank=True, related_name="directory_shares_out",
    )
    source_office = models.ForeignKey(
        CountryOffice, on_delete=models.CASCADE,
        null=True, blank=True, related_name="directory_shares_out",
    )
    target_agency = models.ForeignKey(
        "accounts.Agency", on_delete=models.CASCADE,
        null=True, blank=True, related_name="directory_shares_in",
    )
    target_office = models.ForeignKey(
        CountryOffice, on_delete=models.CASCADE,
        null=True, blank=True, related_name="directory_shares_in",
    )

    is_mutual = models.BooleanField(
        default=True,
        help_text="If on, both sides can see each other. If off, only source sees target.",
    )
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="directory_shares_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Directory share"
        verbose_name_plural = "Directory shares"
        ordering = ["feature_code", "-created_at"]
        indexes = [
            models.Index(fields=["feature_code", "is_active"]),
        ]

    def __str__(self):
        arrow = "↔" if self.is_mutual else "→"
        return f"{self.source_label} {arrow} {self.target_label} ({self.feature_code})"

    def clean(self):
        super().clean()
        if bool(self.source_agency_id) == bool(self.source_office_id):
            raise ValidationError("Pick exactly one source: an agency or an office.")
        if bool(self.target_agency_id) == bool(self.target_office_id):
            raise ValidationError("Pick exactly one target: an agency or an office.")
        if self.source_key == self.target_key:
            raise ValidationError("A scope cannot be linked to itself.")

    @property
    def source_key(self) -> str:
        return (f"office:{self.source_office_id}" if self.source_office_id
                else f"agency:{self.source_agency_id}")

    @property
    def target_key(self) -> str:
        return (f"office:{self.target_office_id}" if self.target_office_id
                else f"agency:{self.target_agency_id}")

    @property
    def source_label(self) -> str:
        return str(self.source_office or self.source_agency)

    @property
    def target_label(self) -> str:
        return str(self.target_office or self.target_agency)


# ---------------------------------------------------------------------------
# Microsoft SSO — schema and console now, wiring later
# ---------------------------------------------------------------------------

class SSOConfiguration(ScopedModel):
    """
    Holds Microsoft Entra ID (Azure AD) settings so an office can be switched
    to SSO later without a schema change or a redeploy.

    Nothing in this model performs authentication yet. ``tenancy/sso.py``
    contains the stub endpoints and the exact list of what is left to build.
    """

    PROVIDER_MICROSOFT = "microsoft"
    PROVIDER_CHOICES = ((PROVIDER_MICROSOFT, "Microsoft Entra ID"),)

    provider = models.CharField(
        max_length=20, choices=PROVIDER_CHOICES, default=PROVIDER_MICROSOFT
    )
    display_name = models.CharField(
        max_length=80, default="Sign in with Microsoft",
        help_text="Label on the login button.",
    )

    tenant_id = models.CharField(max_length=64, blank=True, default="")
    client_id = models.CharField(max_length=64, blank=True, default="")
    client_secret = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Store the secret in the environment for production; this "
                  "field is for staging convenience only.",
    )
    authority = models.URLField(
        blank=True, default="",
        help_text="Leave blank to derive from the tenant ID.",
    )
    redirect_uri = models.URLField(blank=True, default="")
    scopes = models.CharField(
        max_length=255, default="openid profile email User.Read",
        help_text="Space-separated OIDC scopes.",
    )

    allowed_email_domains = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Comma-separated. Blank means any domain in the tenant.",
    )
    auto_provision_users = models.BooleanField(
        default=False,
        help_text="Create a UN PASS account on first successful sign-in.",
    )
    default_role = models.CharField(
        max_length=20, default="requester",
        help_text="Role given to auto-provisioned users.",
    )
    group_role_map = models.JSONField(
        default=dict, blank=True,
        help_text='Optional map of Entra group object ID to UN PASS role, e.g. '
                  '{"7f3c...": "ict_focal"}',
    )

    enforce_sso = models.BooleanField(
        default=False,
        help_text="Hide username/password login for users in this scope.",
    )
    bypass_for_superusers = models.BooleanField(
        default=True,
        help_text="Keep password login available to superusers even when enforced.",
    )

    is_enabled = models.BooleanField(default=False)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_result = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "SSO configuration"
        verbose_name_plural = "SSO configurations"
        constraints = [
            models.UniqueConstraint(
                fields=["agency"],
                condition=models.Q(country_office__isnull=True),
                name="uniq_sso_per_agency",
            ),
            models.UniqueConstraint(
                fields=["country_office"],
                condition=models.Q(country_office__isnull=False),
                name="uniq_sso_per_office",
            ),
        ]

    def __str__(self):
        return f"{self.scope_label} · {self.get_provider_display()}"

    @property
    def is_ready(self) -> bool:
        """True when there is enough here to attempt a real sign-in."""
        return bool(self.is_enabled and self.tenant_id and self.client_id)

    @property
    def resolved_authority(self) -> str:
        if self.authority:
            return self.authority.rstrip("/")
        if self.tenant_id:
            return f"https://login.microsoftonline.com/{self.tenant_id}"
        return ""

    def domain_list(self):
        return [d.strip().lower() for d in self.allowed_email_domains.split(",") if d.strip()]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class FeatureAuditLog(models.Model):
    """Append-only record of who changed what, for handover and inspections."""

    ACTIONS = (
        ("feature_on", "Feature enabled"),
        ("feature_off", "Feature disabled"),
        ("admin_granted", "Admin role granted"),
        ("admin_revoked", "Admin role revoked"),
        ("share_created", "Directory share created"),
        ("share_removed", "Directory share removed"),
        ("sso_updated", "SSO configuration updated"),
        ("office_created", "Country office created"),
        ("office_updated", "Country office updated"),
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tenancy_audit_entries",
    )
    action = models.CharField(max_length=30, choices=ACTIONS)
    scope_key = models.CharField(max_length=40, blank=True, default="", db_index=True)
    scope_label = models.CharField(max_length=160, blank=True, default="")
    feature_code = models.CharField(max_length=50, blank=True, default="")
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Feature audit entry"
        verbose_name_plural = "Feature audit log"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.get_action_display()} {self.scope_label}"
