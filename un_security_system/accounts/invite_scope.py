"""
Stable Agency / Country Office ownership for UNPASS registration links.

New invite codes carry a scope marker:

    o<office_id>-<random uuid32>   -> Country Office scoped
    a<agency_id>-<random uuid32>   -> Agency scoped

The random portion remains unguessable. The numeric prefix is only tenancy
routing metadata and does not grant access by itself.

Old invitation codes remain supported and fall back to the invite creator's
current scope.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Optional


OFFICE_CODE_RE = re.compile(r"^o(?P<pk>\d+)-(?P<token>[0-9a-f]{32})$")
AGENCY_CODE_RE = re.compile(r"^a(?P<pk>\d+)-(?P<token>[0-9a-f]{32})$")


@dataclass(frozen=True)
class InviteScope:
    agency_id: Optional[int] = None
    office_id: Optional[int] = None
    source: str = "none"

    @property
    def is_scoped(self) -> bool:
        return bool(self.agency_id or self.office_id)


def creator_scope(user) -> InviteScope:
    """
    Country Office is authoritative when one is assigned.
    """
    if user is None:
        return InviteScope()

    office = getattr(user, "country_office", None)
    office_id = getattr(user, "country_office_id", None)

    if office_id:
        agency_id = getattr(office, "agency_id", None)
        if agency_id is None:
            try:
                from tenancy.models import CountryOffice
                agency_id = (
                    CountryOffice.objects
                    .filter(pk=office_id)
                    .values_list("agency_id", flat=True)
                    .first()
                )
            except Exception:
                agency_id = None

        return InviteScope(
            agency_id=agency_id,
            office_id=office_id,
            source="creator_office",
        )

    agency_id = getattr(user, "agency_id", None)
    if agency_id:
        return InviteScope(
            agency_id=agency_id,
            office_id=None,
            source="creator_agency",
        )

    return InviteScope(source="creator_unscoped")


def scoped_invite_code(user) -> str:
    """
    Build a new random invitation code that permanently remembers its scope.

    RegistrationInvite.code is 64 chars, and this format fits comfortably
    even with normal BIGINT primary keys.
    """
    scope = creator_scope(user)
    token = uuid.uuid4().hex

    if scope.office_id:
        return f"o{scope.office_id}-{token}"

    if scope.agency_id:
        return f"a{scope.agency_id}-{token}"

    # Preserve normal behavior for a deliberately unscoped superuser.
    return token


def scope_from_code(code: str) -> InviteScope:
    value = (code or "").strip().lower()

    match = OFFICE_CODE_RE.match(value)
    if match:
        office_id = int(match.group("pk"))

        try:
            from tenancy.models import CountryOffice
            office = (
                CountryOffice.objects
                .filter(pk=office_id)
                .only("pk", "agency_id")
                .first()
            )
        except Exception:
            office = None

        if office is not None:
            return InviteScope(
                agency_id=office.agency_id,
                office_id=office.pk,
                source="code_office",
            )

        # The link explicitly targeted an office which no longer exists.
        return InviteScope(
            agency_id=None,
            office_id=office_id,
            source="missing_office",
        )

    match = AGENCY_CODE_RE.match(value)
    if match:
        return InviteScope(
            agency_id=int(match.group("pk")),
            office_id=None,
            source="code_agency",
        )

    return InviteScope(source="legacy")


def invite_scope(invite) -> InviteScope:
    """
    Resolve the permanent scope for an invite.

    New links use the code snapshot. Old links fall back to their creator so
    existing URLs and QR codes keep working.
    """
    parsed = scope_from_code(getattr(invite, "code", ""))

    if parsed.source in {
        "code_office",
        "code_agency",
        "missing_office",
    }:
        return parsed

    return creator_scope(getattr(invite, "created_by", None))


def apply_invite_scope(user, invite) -> InviteScope:
    """
    Force a newly registered user into the invite's scope.

    Office-scoped links always derive Agency from the office, preventing an
    Agency/CO mismatch.
    """
    scope = invite_scope(invite)

    if scope.office_id:
        try:
            from tenancy.models import CountryOffice
            office = (
                CountryOffice.objects
                .filter(pk=scope.office_id)
                .only("pk", "agency_id")
                .first()
            )
        except Exception:
            office = None

        if office is None:
            # Do not silently move a user into the creator's new scope when the
            # original targeted office has disappeared.
            return scope

        changed = []

        if getattr(user, "country_office_id", None) != office.pk:
            user.country_office_id = office.pk
            changed.append("country_office")

        if getattr(user, "agency_id", None) != office.agency_id:
            user.agency_id = office.agency_id
            changed.append("agency")

        if changed:
            user.save(update_fields=changed)

        return InviteScope(
            agency_id=office.agency_id,
            office_id=office.pk,
            source="applied_office",
        )

    if scope.agency_id:
        changed = []

        if getattr(user, "agency_id", None) != scope.agency_id:
            user.agency_id = scope.agency_id
            changed.append("agency")

        # An Agency-only registration link intentionally creates an
        # Agency-level user. It must not inherit a Country Office from some
        # unrelated creator state.
        if getattr(user, "country_office_id", None) is not None:
            user.country_office_id = None
            changed.append("country_office")

        if changed:
            user.save(update_fields=changed)

        return InviteScope(
            agency_id=scope.agency_id,
            office_id=None,
            source="applied_agency",
        )

    return scope


def scope_label(invite) -> str:
    """
    Human-readable destination for logs/debugging.
    """
    scope = invite_scope(invite)

    if scope.office_id:
        try:
            from tenancy.models import CountryOffice
            office = (
                CountryOffice.objects
                .filter(pk=scope.office_id)
                .select_related("agency")
                .first()
            )
        except Exception:
            office = None

        return str(office) if office else f"Country Office #{scope.office_id}"

    if scope.agency_id:
        try:
            from .models import Agency
            agency = Agency.objects.filter(pk=scope.agency_id).first()
        except Exception:
            agency = None

        return str(agency) if agency else f"Agency #{scope.agency_id}"

    return "Unassigned"
