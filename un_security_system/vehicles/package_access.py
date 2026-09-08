"""
vehicles/package_access.py
==========================

Who can see a package.

    Superuser              every package, for support.

    Reception / Registry   every package in their own country office. These are
                           the people who run the mailroom: they log items,
                           hand them over, and chase the ones that have not
                           moved. They cannot see another office's mail.

    Everybody else         only packages they are part of — theirs, addressed
                           to them, handled by them, waiting on their step, or
                           carrying a document they are signing.

Sharing an agency or a job title with someone does not let you read their mail.
Running the mailroom does, for your own office only.

How office is decided
---------------------
`Package` has no office field, so a package belongs to the office of whoever
logged it. When that person has no office — a legacy account, or one created
before country offices existed — the comparison falls back to their agency, so
old records stay reachable rather than vanishing from the mailroom.
"""

from django.db.models import Q

from .models import Package, PackageFlowStep

SIGNING_ROLES = ("signer", "approver")

#: The roles that run the mailroom. They see their whole office's traffic and
#: may register new incoming mail.
MAILROOM_ROLES = {"reception", "registry"}

#: Group names, for deployments using Django Groups instead of `user.role`.
MAILROOM_GROUPS = ["RECEPTION", "RECEPTIONIST", "REGISTRY",
                   "Reception", "Receptionist", "Registry"]


def is_mailroom_staff(user) -> bool:
    """Reception or Registry — by role, or by group on older deployments."""
    if not user or not getattr(user, "is_authenticated", False):
        return False

    role = (getattr(user, "role", "") or "").strip().lower()
    if role in MAILROOM_ROLES:
        return True

    try:
        return user.groups.filter(name__in=MAILROOM_GROUPS).exists()
    except Exception:  # noqa: BLE001 - a permission check must not 500
        return False


def can_log_incoming_package(user) -> bool:
    """
    Who may register new incoming mail.

    Separate from visibility on purpose: you can see a package addressed to you
    without being allowed to create one.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return is_mailroom_staff(user)


def _office_scope_q(user):
    """
    Everything belonging to this person's office, for mailroom staff.

    Matched through the logger's office rather than a field on Package, with an
    agency fallback for accounts that have no office yet.
    """
    office_id = getattr(user, "country_office_id", None)
    agency_id = getattr(user, "agency_id", None)

    if office_id:
        return (
            Q(logged_by__country_office_id=office_id)
            # Logged before offices existed, or by an account still unassigned:
            # keep it visible to its own agency rather than to nobody.
            | Q(logged_by__country_office__isnull=True, logged_by__agency_id=agency_id)
        )

    if agency_id:
        return Q(logged_by__agency_id=agency_id)

    return Q(pk__in=[])


def _actionable_step_ids(user):
    """
    Steps this person may perform.

    Only steps that *name* a role or a user. `PackageFlowStep.user_can_act`
    ends with:

        if not self.allowed_roles and not self.allowed_users.exists():
            return True        # no restrictions -> any agency member can act

    which is a sensible permission rule and a terrible visibility rule. Left
    in, it meant every package sitting on an unconfigured step was readable by
    everyone in the agency — which is the leak this module exists to prevent.
    An unrestricted step is still performable by anyone; it just no longer
    reveals the package to people who have nothing to do with it. Mailroom
    staff and the people on the package can still see and advance it.

    Evaluated through `user_can_act` rather than rebuilt as a queryset:
    `allowed_roles` is a comma-separated string, so an `icontains` match would
    let the role "soc" match a step allowing "associate". A template has a
    handful of steps, so the cost is trivial and the list can never disagree
    with the permission check.
    """
    agency_id = getattr(user, "agency_id", None)
    if not agency_id:
        return []

    steps = (
        PackageFlowStep.objects
        .filter(template__agency_id=agency_id)
        .exclude(allowed_roles="", allowed_users__isnull=True)
        .select_related("template")
        .prefetch_related("allowed_users")
        .distinct()
    )
    return [s.pk for s in steps if s.user_can_act(user)]


def visible_packages_for(user, queryset=None):
    """Reduce `queryset` to the packages `user` may see."""
    qs = queryset if queryset is not None else Package.objects.all()

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()
    if getattr(user, "is_superuser", False):
        return qs

    # ── Mailroom staff: their whole office ──────────────────────────────────
    if is_mailroom_staff(user):
        return qs.filter(_office_scope_q(user)).distinct()

    # ── Everyone else: only what they are part of ───────────────────────────

    # On it, or has handled it.
    visibility = (
        Q(logged_by=user)
        | Q(reception_received_by=user)
        | Q(agency_received_by=user)
        | Q(delivered_by=user)
    )

    # A signing participant, matched by account. Outside the email branch on
    # purpose — this join needs no email address, and nesting it there hid a
    # signer's own package from them whenever their account had none.
    visibility |= Q(step_logs__signature_recipient_user=user)
    visibility |= Q(
        step_logs__documents__esign_envelope__recipients__user=user,
        step_logs__documents__esign_envelope__recipients__role__in=SIGNING_ROLES,
    )

    # Addressed to them, or a signing participant matched by email.
    email = (getattr(user, "email", "") or "").strip()
    if email:
        visibility |= Q(sender_email__iexact=email)
        visibility |= Q(recipient_email__iexact=email)
        visibility |= Q(dest_focal_email__iexact=email)
        visibility |= Q(
            step_logs__documents__esign_envelope__recipients__email__iexact=email,
            step_logs__documents__esign_envelope__recipients__role__in=SIGNING_ROLES,
        )

    # Their turn to act. Without this, a step assigned to a role other than
    # reception or registry — an ICT focal point approving a delivery, say —
    # would 404 in package_advance_step before the role check ever ran.
    step_ids = _actionable_step_ids(user)
    if step_ids:
        visibility |= Q(current_step_id__in=step_ids)
        # And anything they have already acted on, so the trail stays readable
        # after the package has moved past their desk.
        visibility |= Q(step_logs__step_id__in=step_ids)

    return qs.filter(visibility).distinct()


def can_view_package(user, package) -> bool:
    if not package or not getattr(package, "pk", None):
        return False
    return visible_packages_for(user, Package.objects.filter(pk=package.pk)).exists()


def get_visible_package_or_404(user, pk, queryset=None):
    """For direct package URLs."""
    from django.shortcuts import get_object_or_404

    qs = queryset if queryset is not None else Package.objects.all()
    return get_object_or_404(visible_packages_for(user, qs), pk=pk)
