from dataclasses import dataclass
from typing import List, Optional

from django.conf import settings
from django.db import DatabaseError, connection, models

from tenancy.models import CountryOffice, OfficeAdmin
from .models import Agency


LEVEL_OVERVIEW = 1
LEVEL_ANALYTICS = 2
LEVEL_DRILLDOWN = 3
LEVEL_ACCESS_MANAGER = 4

LEVEL_CHOICES = (
    (LEVEL_OVERVIEW, "Level 1 — Overview"),
    (LEVEL_ANALYTICS, "Level 2 — Analytics"),
    (LEVEL_DRILLDOWN, "Level 3 — Drill-down"),
    (LEVEL_ACCESS_MANAGER, "Level 4 — Access management"),
)

LEVEL_LABELS = dict(LEVEL_CHOICES)

SCOPE_PLATFORM = "platform"
SCOPE_AGENCY = "agency"
SCOPE_OFFICE = "office"

SCOPE_CHOICES = (
    (SCOPE_PLATFORM, "Platform"),
    (SCOPE_AGENCY, "Agency"),
    (SCOPE_OFFICE, "Country Office"),
)


class DashboardAccessGrant(models.Model):
    """
    Explicit dashboard access for eSign / Forms / Flow analytics.

    The table is intentionally managed outside Django migrations because this
    UNPASS deployment currently has migration-history inconsistencies.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
    )
    scope_kind = models.CharField(max_length=12, choices=SCOPE_CHOICES)
    scope_id = models.PositiveIntegerField(default=0)
    level = models.PositiveSmallIntegerField(choices=LEVEL_CHOICES)
    is_active = models.BooleanField(default=True)

    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "accounts"
        managed = False
        db_table = "accounts_esign_dashboard_access"
        ordering = ["scope_kind", "scope_id", "-level", "user__username"]
        unique_together = (("user", "scope_kind", "scope_id"),)
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["scope_kind", "scope_id", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user} · {self.scope_kind}:{self.scope_id} · L{self.level}"


@dataclass(frozen=True)
class ScopeAccess:
    kind: str
    scope_id: int
    label: str
    level: int
    source: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.scope_id}"

    @property
    def level_label(self) -> str:
        return LEVEL_LABELS.get(self.level, f"Level {self.level}")

    @property
    def can_view(self) -> bool:
        return self.level >= LEVEL_OVERVIEW

    @property
    def can_analytics(self) -> bool:
        return self.level >= LEVEL_ANALYTICS

    @property
    def can_drilldown(self) -> bool:
        return self.level >= LEVEL_DRILLDOWN

    @property
    def can_manage_access(self) -> bool:
        return self.level >= LEVEL_ACCESS_MANAGER


def grant_table_exists() -> bool:
    try:
        return DashboardAccessGrant._meta.db_table in connection.introspection.table_names()
    except Exception:
        return False


def _scope_label(kind: str, scope_id: int) -> Optional[str]:
    if kind == SCOPE_PLATFORM:
        return "All Agencies / Platform"

    if kind == SCOPE_AGENCY:
        obj = Agency.objects.filter(pk=scope_id).only("id", "code", "name").first()
        return str(obj) if obj else None

    if kind == SCOPE_OFFICE:
        obj = (
            CountryOffice.objects
            .filter(pk=scope_id)
            .select_related("agency")
            .only("id", "name", "code", "agency__code")
            .first()
        )
        return str(obj) if obj else None

    return None


def _merge_scope(scopes, access: ScopeAccess):
    existing = scopes.get(access.key)
    if existing is None or access.level > existing.level:
        scopes[access.key] = access


def available_scopes(user) -> List[ScopeAccess]:
    if not user or not getattr(user, "is_authenticated", False):
        return []

    if user.is_superuser:
        scopes = {
            "platform:0": ScopeAccess(
                kind=SCOPE_PLATFORM,
                scope_id=0,
                label="All Agencies / Platform",
                level=LEVEL_ACCESS_MANAGER,
                source="superuser",
            )
        }

        for agency in Agency.objects.order_by("code", "name"):
            access = ScopeAccess(
                kind=SCOPE_AGENCY,
                scope_id=agency.pk,
                label=str(agency),
                level=LEVEL_ACCESS_MANAGER,
                source="superuser",
            )
            scopes[access.key] = access

        for office in (
            CountryOffice.objects
            .select_related("agency")
            .order_by("agency__code", "name")
        ):
            access = ScopeAccess(
                kind=SCOPE_OFFICE,
                scope_id=office.pk,
                label=str(office),
                level=LEVEL_ACCESS_MANAGER,
                source="superuser",
            )
            scopes[access.key] = access

        return list(scopes.values())

    scopes = {}

    # Every active Country Office admin automatically receives Level 4 for
    # that office. No explicit DashboardAccessGrant row is required.
    for role in (
        OfficeAdmin.objects
        .filter(user=user, is_active=True)
        .select_related("country_office__agency")
        .order_by("country_office__agency__code", "country_office__name")
    ):
        office = role.country_office
        _merge_scope(
            scopes,
            ScopeAccess(
                kind=SCOPE_OFFICE,
                scope_id=office.pk,
                label=str(office),
                level=LEVEL_ACCESS_MANAGER,
                source=f"office_admin:{role.level}",
            ),
        )

    if grant_table_exists():
        try:
            for grant in DashboardAccessGrant.objects.filter(
                user=user,
                is_active=True,
            ).order_by("-level"):
                label = _scope_label(grant.scope_kind, grant.scope_id)
                if not label:
                    continue
                _merge_scope(
                    scopes,
                    ScopeAccess(
                        kind=grant.scope_kind,
                        scope_id=grant.scope_id,
                        label=label,
                        level=grant.level,
                        source="grant",
                    ),
                )
        except DatabaseError:
            pass

    result = list(scopes.values())
    user_office_id = getattr(user, "country_office_id", None)
    user_agency_id = getattr(user, "agency_id", None)

    def sort_key(access):
        if access.kind == SCOPE_OFFICE and access.scope_id == user_office_id:
            return (0, access.label)
        if access.kind == SCOPE_AGENCY and access.scope_id == user_agency_id:
            return (1, access.label)
        if access.kind == SCOPE_OFFICE:
            return (2, access.label)
        if access.kind == SCOPE_AGENCY:
            return (3, access.label)
        return (4, access.label)

    return sorted(result, key=sort_key)


def resolve_scope_access(user, requested_key: str = "") -> Optional[ScopeAccess]:
    scopes = available_scopes(user)
    if not scopes:
        return None

    requested_key = (requested_key or "").strip().lower()
    if requested_key:
        for access in scopes:
            if access.key.lower() == requested_key:
                return access

    return scopes[0]


def access_summary(user):
    scopes = available_scopes(user)
    if not scopes:
        return {
            "can_view": False,
            "max_level": 0,
            "preferred_scope": "",
            "can_manage": False,
        }

    return {
        "can_view": True,
        "max_level": max(scope.level for scope in scopes),
        "preferred_scope": scopes[0].key,
        "can_manage": any(scope.can_manage_access for scope in scopes),
    }
