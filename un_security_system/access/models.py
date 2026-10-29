# access/models.py
"""
Dynamic roles, groups and access for UN PASS.

AccessRole        A named bundle of capabilities ("Operations Manager",
                  "Procurement Committee", "Gate Supervisor" …). Shared roles
                  have no agency; others belong to one agency and optionally
                  one country office. Roles can inherit other roles.
                  The old fixed roles are seeded as *system* roles so nothing
                  changes on day one — and they can be edited like any other.

AccessGroup       A team. Give the group roles and every member holds them.
                  Membership is manual, smart (a rule such as "everyone in the
                  Finance unit of the Gambia office"), or both.

RoleAssignment    Who holds a role, where (everywhere / one agency / one
                  office) and for how long. Delegations ("Awa covers my
                  approvals until the 15th") are assignments with a source.

AccessRequest     Self-service: ask for a role, someone approves it.

AccessAudit       Every change, for the record.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

SCOPES = [
    ("global", "Everywhere"),
    ("agency", "One agency"),
    ("office", "One country office"),
]


class AccessRole(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=90)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=40, default="bi-person-badge",
                            help_text="Bootstrap icon name, e.g. bi-shield-check")
    color = models.CharField(max_length=9, default="#0F766E")

    agency = models.ForeignKey("accounts.Agency", on_delete=models.CASCADE, null=True, blank=True,
                               related_name="access_roles",
                               help_text="Empty = a shared role every agency can use.")
    country_office = models.ForeignKey("tenancy.CountryOffice", on_delete=models.CASCADE,
                                       null=True, blank=True, related_name="access_roles")

    capabilities = models.JSONField(default=list, blank=True,
                                    help_text="Capability codes from access.registry.")
    inherits = models.ManyToManyField("self", symmetrical=False, blank=True, related_name="inherited_by",
                                      help_text="Holders of this role also hold these roles.")

    seeded = models.JSONField(default=list, blank=True, editable=False,
                              help_text="Default capabilities already offered to this system role.")
    is_system = models.BooleanField(default=False, help_text="Seeded from an old fixed role.")
    legacy_code = models.CharField(max_length=20, blank=True, db_index=True,
                                   help_text="For system roles: the old role code (lsa, registry …).")

    requestable = models.BooleanField(default=False, help_text="Staff may ask for it from My access.")
    max_days = models.PositiveIntegerField(null=True, blank=True,
                                           help_text="Time-box: every assignment must end within this many days.")
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="access_roles_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_system", "name"]
        constraints = [
            models.UniqueConstraint(fields=["legacy_code"], condition=~Q(legacy_code=""),
                                    name="access_role_unique_legacy_code"),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:90] or "role"
        self.capabilities = sorted(set(self.capabilities or []))
        super().save(*args, **kwargs)

    @property
    def locked_capabilities(self):
        """A system role always keeps its own legacy power."""
        return {f"legacy.{self.legacy_code}"} if self.is_system and self.legacy_code else set()

    @property
    def scope_label(self):
        if self.country_office_id:
            return str(self.country_office)
        if self.agency_id:
            return str(self.agency)
        return "Shared"


class AccessGroup(models.Model):
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=40, default="bi-people")
    color = models.CharField(max_length=9, default="#1D4ED8")

    agency = models.ForeignKey("accounts.Agency", on_delete=models.CASCADE, null=True, blank=True,
                               related_name="access_groups")
    country_office = models.ForeignKey("tenancy.CountryOffice", on_delete=models.CASCADE,
                                       null=True, blank=True, related_name="access_groups")

    members = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="access_groups")
    roles = models.ManyToManyField(AccessRole, blank=True, related_name="groups")

    #: Smart membership: {"agency": id, "office": id, "unit": id, "base_role": "registry"}
    #: Every key present must match. Empty = manual members only.
    rule = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="access_groups_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_smart(self):
        return bool(self.rule)

    def rule_q(self):
        """Q over User for the smart rule, or None."""
        r = self.rule or {}
        if not r:
            return None
        q = Q(is_active=True)
        if r.get("agency"):
            q &= Q(agency_id=r["agency"])
        if r.get("office"):
            q &= Q(country_office_id=r["office"])
        if r.get("unit"):
            q &= Q(unit_id=r["unit"])
        if r.get("base_role"):
            q &= Q(role=r["base_role"])
        return q

    def member_ids(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        ids = set(self.members.values_list("pk", flat=True))
        q = self.rule_q()
        if q is not None:
            # _base_manager: the plain queryset, so `role` is the stored value.
            ids |= set(User._base_manager.filter(q).values_list("pk", flat=True))
        return ids

    def has_member(self, user):
        if self.members.filter(pk=user.pk).exists():
            return True
        r = self.rule or {}
        if not r:
            return False
        checks = {"agency": "agency_id", "office": "country_office_id", "unit": "unit_id"}
        for key, attr in checks.items():
            if r.get(key) and getattr(user, attr, None) != r[key]:
                return False
        if r.get("base_role") and user.__dict__.get("role") != r["base_role"]:
            return False
        return bool(getattr(user, "is_active", True))

    @property
    def scope(self):
        if self.country_office_id:
            return "office", self.country_office_id
        if self.agency_id:
            return "agency", self.agency_id
        return "global", None


class ActiveAssignmentQuerySet(models.QuerySet):
    def active(self, at=None):
        at = at or timezone.now()
        return self.filter(revoked_at__isnull=True, starts_at__lte=at).filter(
            Q(ends_at__isnull=True) | Q(ends_at__gt=at))


class RoleAssignment(models.Model):
    role = models.ForeignKey(AccessRole, on_delete=models.CASCADE, related_name="assignments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                             related_name="access_assignments")
    group = models.ForeignKey(AccessGroup, on_delete=models.CASCADE, null=True, blank=True,
                              related_name="assignments")

    scope = models.CharField(max_length=10, choices=SCOPES, default="office")
    scope_agency = models.ForeignKey("accounts.Agency", on_delete=models.CASCADE, null=True, blank=True,
                                     related_name="+")
    scope_office = models.ForeignKey("tenancy.CountryOffice", on_delete=models.CASCADE, null=True,
                                     blank=True, related_name="+")

    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)

    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="access_assignments_granted")
    delegated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name="access_delegations_given",
                                     help_text="Set when this came from 'hand over my roles'.")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="access_assignments_revoked")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ActiveAssignmentQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "revoked_at"]), models.Index(fields=["group", "revoked_at"])]

    def __str__(self):
        who = self.user or self.group
        return f"{who} → {self.role} ({self.scope_label})"

    def clean(self):
        if bool(self.user_id) == bool(self.group_id):
            raise ValidationError("Choose a person or a group.")
        if self.scope == "agency" and not self.scope_agency_id:
            raise ValidationError("Choose the agency.")
        if self.scope == "office" and not self.scope_office_id:
            raise ValidationError("Choose the country office.")
        if self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError("The end must be after the start.")

    @property
    def scope_label(self):
        if self.scope == "office":
            return str(self.scope_office or "office")
        if self.scope == "agency":
            return str(self.scope_agency or "agency")
        return "Everywhere"

    @property
    def is_active(self):
        now = timezone.now()
        return (self.revoked_at is None and self.starts_at <= now
                and (self.ends_at is None or self.ends_at > now))

    @property
    def scope_key(self):
        if self.scope == "office":
            return ("office", self.scope_office_id)
        if self.scope == "agency":
            return ("agency", self.scope_agency_id)
        return ("global", None)


class AccessRequest(models.Model):
    STATUS = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected"),
              ("cancelled", "Cancelled")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="access_requests")
    role = models.ForeignKey(AccessRole, on_delete=models.CASCADE, related_name="requests")
    reason = models.TextField()
    days = models.PositiveIntegerField(null=True, blank=True, help_text="Empty = no end date.")
    status = models.CharField(max_length=10, choices=STATUS, default="pending")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="access_requests_decided")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)
    assignment = models.ForeignKey(RoleAssignment, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} asks for {self.role} ({self.status})"


class AccessAudit(models.Model):
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+")
    action = models.CharField(max_length=40)
    summary = models.CharField(max_length=300)
    detail = models.JSONField(default=dict, blank=True)
    agency = models.ForeignKey("accounts.Agency", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="+")
    country_office = models.ForeignKey("tenancy.CountryOffice", on_delete=models.SET_NULL, null=True,
                                       blank=True, related_name="+")

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.action}: {self.summary}"
