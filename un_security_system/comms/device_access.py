"""
comms/device_access.py
======================

Which radios, sat phones and users a person sees.

The problem
-----------
Every list view in `comms/views.py` was unscoped:

    CommunicationDevice.objects.filter(device_type__in=["hf", "vhf"])
    User.objects.filter(is_active=True).exclude(role__in=["guard", "data_entry"])

So a UNDP Gambia LSA saw Senegal's radios, UNICEF's sat phones, and every
active user in the entire platform on the "users without radios" page — which
is effectively a staff directory for the whole deployment.

`CommunicationDevice` has no office field of its own, so scoping goes through
`assigned_to`. A device issued to someone in your office is yours to see.

Devices in the store
--------------------
An unassigned device has no owner and therefore no office. Those stay visible
to everyone, because a radio sitting in the store is exactly what an LSA needs
to find when issuing one, and hiding it would break the normal workflow.

If your offices keep physically separate stores, add an `owner_office` FK to
`CommunicationDevice` and switch `_unassigned_q` to compare on that instead.
The rest of this module needs no change.
"""

from django.conf import settings
from django.db.models import Q

#: While True, devices with nobody assigned are visible to every office.
COMMS_SHARED_STORE = getattr(settings, "COMMS_SHARED_STORE", True)


def _office_id(user):
    return getattr(user, "country_office_id", None)


def _agency_id(user):
    agency_id = getattr(user, "agency_id", None)
    if agency_id is None:
        office = getattr(user, "country_office", None)
        agency_id = getattr(office, "agency_id", None)
    return agency_id


def visible_devices(user, base=None):
    """
    Devices this person may see. Use anywhere you would have written
    `CommunicationDevice.objects.filter(...)` directly.
    """
    from .models import CommunicationDevice

    qs = base if base is not None else CommunicationDevice.objects.all()

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()
    if getattr(user, "is_superuser", False):
        return qs

    office_id = _office_id(user)
    agency_id = _agency_id(user)

    q = Q(assigned_to=user)                       # always your own kit

    if office_id:
        q |= Q(assigned_to__country_office_id=office_id)
    elif agency_id:
        # No office set: fall back to the agency, which is no wider than the
        # behaviour before country offices existed.
        q |= Q(assigned_to__agency_id=agency_id)

    if COMMS_SHARED_STORE:
        q |= Q(assigned_to__isnull=True)

    return qs.filter(q).distinct()


def visible_users(user, base=None):
    """
    Users this person may see on the "who has no radio" page.

    Same rule as the devices: own office, or own agency when no office is set.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    qs = base if base is not None else User.objects.all()

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()
    if getattr(user, "is_superuser", False):
        return qs

    office_id = _office_id(user)
    if office_id:
        return qs.filter(country_office_id=office_id)

    agency_id = _agency_id(user)
    if agency_id:
        return qs.filter(agency_id=agency_id)

    return qs.none()


def can_view_device(user, device) -> bool:
    """Single-device check, for detail and status views."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True

    if device.assigned_to_id == getattr(user, "id", None):
        return True

    if device.assigned_to_id is None:
        return COMMS_SHARED_STORE

    holder = device.assigned_to
    office_id = _office_id(user)
    if office_id:
        return getattr(holder, "country_office_id", None) == office_id

    agency_id = _agency_id(user)
    if agency_id:
        return getattr(holder, "agency_id", None) == agency_id

    return False


def visible_sessions(user, base=None):
    """
    Radio check sessions. Scoped by who started them, so an office sees its own
    checks rather than every check ever run on the platform.
    """
    from .models import RadioCheckSession

    qs = base if base is not None else RadioCheckSession.objects.all()

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()
    if getattr(user, "is_superuser", False):
        return qs

    office_id = _office_id(user)
    if office_id:
        return qs.filter(
            Q(created_by__country_office_id=office_id) | Q(created_by__isnull=True)
        ).distinct()

    agency_id = _agency_id(user)
    if agency_id:
        return qs.filter(
            Q(created_by__agency_id=agency_id) | Q(created_by__isnull=True)
        ).distinct()

    return qs.filter(created_by__isnull=True)
