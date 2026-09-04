"""
incidents/cs_access.py
======================

Which common service requests a person may see, and who they may assign one to.

`can_user_manage_csr()` in permissions.py answers "may this person act on this
one request?". It is a per-object check, so every list view had to re-derive the
same rule from scratch — and each derived a slightly different one:

    csr_fulfiller_queue   agency, then assigned/escalated to me
    csr_dashboard         everything, for CSM and superuser
    cs_detail             per-object, via can_user_manage_csr
    cs_update_status      no scope check at all beyond the role gate

This module is the queryset form of the same rule, so a list and a permission
check cannot disagree.

Country offices
---------------
`CommonServiceRequest` has an `agency` FK but no office field, so office is
compared through `requested_by`, the same way permissions.py does it. The
comparison is skipped when either side has no office, so requests that predate
the tenancy work stay reachable.
"""

from django.db.models import Q


def _office_id(user):
    return getattr(user, "country_office_id", None)


def visible_csrs(user, base=None):
    """
    Requests this person may see.

    Superuser and Common Service Manager: everything, which is what the CSM
    role is for — it exists to work across agencies in a shared compound.

    Everyone else: their own agency and their own office, and within that,
    requests they raised, are assigned, are escalated to, or approve.
    """
    from .models import CommonServiceApprover, CommonServiceRequest
    from .permissions import is_common_services_manager

    qs = base if base is not None else CommonServiceRequest.objects.all()

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()
    if getattr(user, "is_superuser", False) or is_common_services_manager(user):
        return qs

    agency_id = getattr(user, "agency_id", None)
    if not agency_id:
        # No agency: only what they raised themselves. Better than nothing,
        # which is what the queue used to return.
        return qs.filter(requested_by_id=user.id)

    qs = qs.filter(agency_id=agency_id)

    office_id = _office_id(user)
    if office_id:
        qs = qs.filter(
            Q(requested_by__country_office_id=office_id)
            | Q(requested_by__country_office__isnull=True)
        )

    role = getattr(user, "role", "") or ""

    involved = (
        Q(requested_by_id=user.id)
        | Q(assigned_to_id=user.id)
        | Q(escalated_to_user_id=user.id)
    )
    if role:
        involved |= Q(escalated_to=role)

    # The escalation roles and configured approvers see the whole office queue,
    # not only what is already pointed at them — otherwise nobody could pick up
    # a new request.
    if role in ("soc", "lsa", "ict_focal", "common_services_manager"):
        return qs.distinct()

    if CommonServiceApprover.objects.filter(
        agency_id=agency_id, user_id=user.id, is_active=True
    ).exists():
        return qs.distinct()

    return qs.filter(involved).distinct()


def assignable_users_for(user, csr, base=None):
    """
    Who this person may assign a request to.

    Was agency-wide for everyone below CSM, so a Gambia approver could hand a
    Gambia request to someone in Senegal — who would then not be able to see it,
    because their own queue is office-scoped. An assignment nobody can act on is
    worse than no assignment.
    """
    from django.contrib.auth import get_user_model
    from .permissions import is_common_services_manager

    User = get_user_model()
    qs = base if base is not None else User.objects.all()
    qs = qs.filter(is_active=True)

    if getattr(user, "is_superuser", False) or is_common_services_manager(user):
        return qs.order_by("first_name", "last_name", "username")

    qs = qs.filter(agency_id=csr.agency_id)

    # Match the office of the request, not of the person assigning — they may
    # be the same, but the request is what has to be worked on.
    requester_office_id = getattr(
        getattr(csr, "requested_by", None), "country_office_id", None
    )
    if requester_office_id:
        qs = qs.filter(
            Q(country_office_id=requester_office_id)
            | Q(country_office__isnull=True)
        )

    return qs.order_by("first_name", "last_name", "username")


def can_view_csr(user, csr) -> bool:
    """
    Single-request check for the detail page.

    Kept in step with visible_csrs by asking the queryset, rather than
    re-implementing the rule and letting the two drift.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if csr.requested_by_id == user.id or csr.assigned_to_id == user.id:
        return True
    return visible_csrs(user).filter(pk=csr.pk).exists()
