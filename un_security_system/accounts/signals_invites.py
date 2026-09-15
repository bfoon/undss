"""
Signals that make UNPASS registration links permanently inherit the creator's
Agency / Country Office.

No schema migration is required.
"""

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .invite_scope import apply_invite_scope, creator_scope, scoped_invite_code
from .models import RegistrationInvite, RegistrationInviteUsage


@receiver(pre_save, sender=RegistrationInvite)
def snapshot_registration_invite_scope(sender, instance, **kwargs):
    """
    On creation, replace the plain UUID with a scoped random code.

    Only new links are touched. Existing invite URLs are never rewritten.
    """
    if instance.pk:
        return

    creator = getattr(instance, "created_by", None)
    if creator is None and getattr(instance, "created_by_id", None):
        try:
            creator = instance.created_by
        except Exception:
            creator = None

    if creator is None:
        return

    scope = creator_scope(creator)

    # If the creator has a tenancy scope, always snapshot it into the link.
    # If not, leave the default UUID in place (useful for an intentionally
    # unscoped platform superuser).
    if scope.is_scoped:
        instance.code = scoped_invite_code(creator)


@receiver(post_save, sender=RegistrationInviteUsage)
def enforce_registration_invite_scope(sender, instance, created, **kwargs):
    """
    After successful registration, force the new user to the scope embedded in
    the invite.

    The existing registration view already assigns the creator's current scope;
    this signal is the final authority and corrects it if the creator has moved
    since the link was issued.
    """
    if not created:
        return

    invite = getattr(instance, "invite", None)
    user = getattr(instance, "user", None)

    if invite is None or user is None:
        return

    apply_invite_scope(user, invite)
