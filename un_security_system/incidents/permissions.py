"""
incidents/permissions.py
========================

Who may act on a common service request.
"""


def can_user_manage_csr(user, csr) -> bool:
    """
    Can this person progress, assign, escalate or close this CSR?

    Order matters here. The previous version checked the agency first:

        if not user.agency_id or user.agency_id != csr.agency_id:
            return False
        if user.is_superuser:
            return True

    so a superuser was refused unless their own `agency_id` happened to match
    the request's. A superuser with no agency — the usual case for a platform
    account — could never manage any CSR at all, and one attached to UNDP could
    not touch a UNICEF request. The superuser check now comes first, which is
    what the comment "Superuser override" always intended.
    """
    if not user or not user.is_authenticated:
        return False

    # Superuser override — before any scope test, or it is not an override.
    if user.is_superuser:
        return True

    # Agency scope
    if not getattr(user, "agency_id", None) or user.agency_id != csr.agency_id:
        return False

    # Country office scope.
    #
    # Agency alone is too wide once an agency runs several country offices: a
    # UNDP Gambia approver would otherwise be able to close a UNDP Senegal
    # request. Compared through the requester, since CommonServiceRequest has
    # no office field of its own.
    #
    # Skipped when either side has no office, so this cannot lock people out of
    # requests that predate the tenancy work.
    user_office_id = getattr(user, "country_office_id", None)
    requester = getattr(csr, "requested_by", None)
    csr_office_id = getattr(requester, "country_office_id", None)
    if user_office_id and csr_office_id and user_office_id != csr_office_id:
        return False

    # Role override for escalation teams
    if getattr(user, "role", "") in ("soc", "lsa", "ict_focal", "common_services_manager"):
        return True

    # Config level 1 manager override
    cfg = getattr(csr.agency, "common_service_config", None)
    if cfg and cfg.level_1_manager_id and user.id == cfg.level_1_manager_id:
        return True

    # Any configured approver can manage (or restrict to current_level only if
    # you prefer).
    from .models import CommonServiceApprover
    return CommonServiceApprover.objects.filter(
        agency_id=csr.agency_id,
        user_id=user.id,
        is_active=True,
    ).exists()


def is_common_services_manager(user) -> bool:
    return bool(
        user
        and user.is_authenticated
        and (user.is_superuser or getattr(user, "role", "") == "common_services_manager")
    )
