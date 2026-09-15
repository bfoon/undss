"""
tenancy/scope_control.py
========================

Platform-level Agency and Country Office activation controls.

CountryOffice already has ``is_active`` in the existing UNPASS schema.

Agency has no ``is_active`` field, so Agency status is stored without a schema
change in the existing FeatureGrant table using a reserved internal feature
code. Normal feature resolution ignores this row because the code is not part
of the public feature catalogue.

This gives UNPASS a reversible tenant suspension mechanism without deleting
users, data, module grants, trusted devices, or office configuration.
"""

from typing import Dict, Optional, Set, Tuple

from .catalog import FEATURE_CODES

AGENCY_STATUS_CODE = "__agency_active__"

AGENCY_ENABLED_ACTION = "agency_enabled"
AGENCY_DISABLED_ACTION = "agency_disabled"
OFFICE_ENABLED_ACTION = "office_enabled"
OFFICE_DISABLED_ACTION = "office_disabled"


def install_audit_action_labels():
    """
    Extend FeatureAuditLog's Python-side choices without a schema migration.

    CharField choices are application metadata, not a database constraint.
    """
    from .models import FeatureAuditLog

    field = FeatureAuditLog._meta.get_field("action")
    current = list(field.choices or ())
    existing = {value for value, _label in current}

    additions = (
        (AGENCY_ENABLED_ACTION, "Agency activated"),
        (AGENCY_DISABLED_ACTION, "Agency suspended"),
        (OFFICE_ENABLED_ACTION, "Country office activated"),
        (OFFICE_DISABLED_ACTION, "Country office suspended"),
    )

    field.choices = current + [
        item for item in additions if item[0] not in existing
    ]


def _agency_model():
    from django.apps import apps
    return apps.get_model("accounts", "Agency")


def _agency_id(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return getattr(value, "pk", None) or getattr(value, "id", None)


def agency_is_active(agency) -> bool:
    """
    Agency status defaults to active until a superuser explicitly suspends it.
    """
    agency_id = _agency_id(agency)
    if not agency_id:
        return True

    from .models import FeatureGrant

    row = (
        FeatureGrant.objects
        .filter(
            agency_id=agency_id,
            country_office__isnull=True,
            feature_code=AGENCY_STATUS_CODE,
        )
        .order_by("-updated_at", "-pk")
        .first()
    )
    return True if row is None else bool(row.enabled)


def disabled_agency_ids() -> Set[int]:
    """
    IDs of agencies currently suspended by the platform superuser.
    """
    from .models import FeatureGrant

    rows = (
        FeatureGrant.objects
        .filter(
            country_office__isnull=True,
            feature_code=AGENCY_STATUS_CODE,
            enabled=False,
        )
        .values_list("agency_id", flat=True)
    )
    return {pk for pk in rows if pk}


def set_agency_active(*, agency, enabled: bool, actor=None, reason: str = ""):
    """
    Activate or suspend an Agency without touching its offices/users/grants.
    """
    agency_id = _agency_id(agency)
    if not agency_id:
        raise ValueError("A valid agency is required.")

    from .models import FeatureAuditLog, FeatureGrant

    qs = (
        FeatureGrant.objects
        .filter(
            agency_id=agency_id,
            country_office__isnull=True,
            feature_code=AGENCY_STATUS_CODE,
        )
        .order_by("-updated_at", "-pk")
    )
    row = qs.first()

    if row is None:
        row = FeatureGrant.objects.create(
            agency_id=agency_id,
            country_office=None,
            feature_code=AGENCY_STATUS_CODE,
            enabled=enabled,
            notes=(reason or "").strip(),
            updated_by=actor,
            is_paid=False,
            cost_amount=None,
            cost_currency="USD",
            valid_until=None,
        )
    else:
        row.enabled = enabled
        row.notes = (reason or "").strip()
        row.updated_by = actor
        row.is_paid = False
        row.cost_amount = None
        row.cost_currency = "USD"
        row.valid_until = None
        row.save(update_fields=[
            "enabled",
            "notes",
            "updated_by",
            "is_paid",
            "cost_amount",
            "cost_currency",
            "valid_until",
            "updated_at",
        ])
        qs.exclude(pk=row.pk).delete()

    label = str(agency)
    FeatureAuditLog.objects.create(
        actor=actor,
        action=AGENCY_ENABLED_ACTION if enabled else AGENCY_DISABLED_ACTION,
        scope_key=f"agency:{agency_id}",
        scope_label=label,
        detail=(
            f"Agency {'ACTIVATED' if enabled else 'SUSPENDED'}"
            + (f" — {(reason or '').strip()}" if (reason or "").strip() else "")
        ),
    )

    from .services import bump_cache
    bump_cache()
    return row


def set_office_active(*, office, enabled: bool, actor=None, reason: str = ""):
    """
    Activate or suspend one CountryOffice.

    Uses the existing CountryOffice.is_active column.
    """
    from .models import FeatureAuditLog

    office.is_active = enabled
    office.save(update_fields=["is_active", "updated_at"])

    FeatureAuditLog.objects.create(
        actor=actor,
        action=OFFICE_ENABLED_ACTION if enabled else OFFICE_DISABLED_ACTION,
        scope_key=office.scope_key,
        scope_label=str(office),
        detail=(
            f"Country office {'ACTIVATED' if enabled else 'SUSPENDED'}"
            + (f" — {(reason or '').strip()}" if (reason or "").strip() else "")
        ),
    )

    from .services import bump_cache
    bump_cache()
    return office


def scope_status_for_ids(
    agency_id: Optional[int],
    office_id: Optional[int],
) -> Dict[str, object]:
    """
    Effective tenant status for an agency/office pair.

    Agency suspension overrides office status.
    """
    Agency = _agency_model()

    agency = Agency.objects.filter(pk=agency_id).first() if agency_id else None
    agency_active = agency_is_active(agency_id) if agency_id else True

    office = None
    office_active = True

    if office_id:
        from .models import CountryOffice
        office = (
            CountryOffice.objects
            .filter(pk=office_id)
            .select_related("agency")
            .first()
        )
        if office is not None:
            office_active = bool(office.is_active)

            # An explicit Country Office is authoritative for its Agency, just
            # like tenancy.services.scope_of(). Do not trust a stale user.agency
            # value when the office says otherwise.
            agency_id = office.agency_id
            agency = office.agency
            agency_active = agency_is_active(agency_id)

    effective = bool(agency_active and office_active)

    if not agency_active:
        reason = "agency_disabled"
        message = (
            f"{getattr(agency, 'name', None) or getattr(agency, 'code', None) or 'Your agency'} "
            "is currently suspended in UNPASS."
        )
    elif not office_active:
        reason = "office_disabled"
        message = (
            f"{getattr(office, 'name', None) or 'Your country office'} "
            "is currently suspended in UNPASS."
        )
    else:
        reason = ""
        message = ""

    return {
        "active": effective,
        "agency_active": bool(agency_active),
        "office_active": bool(office_active),
        "agency": agency,
        "office": office,
        "reason": reason,
        "message": message,
    }


def user_scope_status(user) -> Dict[str, object]:
    """
    Effective tenant status for a user.

    Superusers always retain platform-control access. For ordinary users an
    explicitly assigned Country Office is authoritative for the parent Agency.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return {
            "active": True,
            "agency_active": True,
            "office_active": True,
            "agency": None,
            "office": None,
            "reason": "",
            "message": "",
        }

    office = getattr(user, "country_office", None)

    if office is not None:
        agency = getattr(office, "agency", None)
    else:
        agency = getattr(user, "agency", None)

    if getattr(user, "is_superuser", False):
        return {
            "active": True,
            "agency_active": True,
            "office_active": True,
            "agency": agency,
            "office": office,
            "reason": "",
            "message": "",
        }

    agency_active = agency_is_active(agency)
    office_active = bool(getattr(office, "is_active", True))
    effective = bool(agency_active and office_active)

    if not agency_active:
        reason = "agency_disabled"
        message = (
            f"{getattr(agency, 'name', None) or getattr(agency, 'code', None) or 'Your agency'} "
            "is currently suspended in UNPASS."
        )
    elif not office_active:
        reason = "office_disabled"
        message = (
            f"{getattr(office, 'name', None) or 'Your country office'} "
            "is currently suspended in UNPASS."
        )
    else:
        reason = ""
        message = ""

    return {
        "active": effective,
        "agency_active": bool(agency_active),
        "office_active": bool(office_active),
        "agency": agency,
        "office": office,
        "reason": reason,
        "message": message,
    }


def user_scope_is_active(user) -> bool:
    return bool(user_scope_status(user)["active"])


def filter_users_to_active_scopes(queryset):
    """
    Remove users belonging to a suspended Agency or inactive CountryOffice.

    Users with no office remain visible unless their Agency itself is suspended.
    """
    disabled_agencies = disabled_agency_ids()

    if disabled_agencies:
        queryset = queryset.exclude(agency_id__in=disabled_agencies)
        queryset = queryset.exclude(
            country_office__agency_id__in=disabled_agencies
        )

    return queryset.exclude(country_office__is_active=False)
