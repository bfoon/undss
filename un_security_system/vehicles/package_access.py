"""
vehicles/package_access.py
==========================

One privacy rule for Packages/Mailroom.

Normal users may see a package only when it is theirs or they are an actual
signing participant on one of its eSign-backed documents.  Office/agency roles
alone never grant package visibility.  Superusers retain global support access.
"""

from django.db.models import Q

from .models import Package


SIGNING_ROLES = ("signer", "approver")


def can_log_incoming_package(user) -> bool:
    """Only Reception, Registry, or a superuser may log incoming mail/packages.

    Package visibility is intentionally separate from creation permission: a user
    may be allowed to see a package because they own/sign it without being allowed
    to register new incoming mail.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True

    role = (getattr(user, "role", "") or "").strip().lower()
    if role in {"reception", "registry"}:
        return True

    # Keep compatibility with deployments that use Django Groups rather than
    # the custom user.role field.
    try:
        return user.groups.filter(
            name__in=["RECEPTION", "RECEPTIONIST", "REGISTRY",
                     "Reception", "Receptionist", "Registry"]
        ).exists()
    except Exception:
        return False


def visible_packages_for(user, queryset=None):
    """Return ``queryset`` reduced to packages visible to ``user``.

    Visible means one of:
      * the user logged/created the package;
      * the package was originated with the user's email address; or
      * the user is the selected Package Flow signature recipient / an eSign
        signer or approver for a document belonging to the package.

    Merely sharing an agency, Country Office or operational role does not make
    another person's package visible.
    """
    qs = queryset if queryset is not None else Package.objects.all()

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()

    if getattr(user, "is_superuser", False):
        return qs

    visibility = Q(logged_by=user) | Q(step_logs__signature_recipient_user=user)

    email = (getattr(user, "email", "") or "").strip()
    if email:
        # sender_email is the internal originator for outgoing Package Flow and
        # may also identify the user when Registry logged the item on their behalf.
        visibility |= Q(sender_email__iexact=email)
        visibility |= Q(
            step_logs__documents__esign_envelope__recipients__user=user,
            step_logs__documents__esign_envelope__recipients__role__in=SIGNING_ROLES,
        )
        visibility |= Q(
            step_logs__documents__esign_envelope__recipients__email__iexact=email,
            step_logs__documents__esign_envelope__recipients__role__in=SIGNING_ROLES,
        )

    return qs.filter(visibility).distinct()


def can_view_package(user, package) -> bool:
    if not package or not getattr(package, "pk", None):
        return False
    return visible_packages_for(user, Package.objects.filter(pk=package.pk)).exists()


def get_visible_package_or_404(user, pk, queryset=None):
    """Convenience helper for direct package URLs."""
    from django.shortcuts import get_object_or_404

    qs = queryset if queryset is not None else Package.objects.all()
    return get_object_or_404(visible_packages_for(user, qs), pk=pk)
