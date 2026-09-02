"""
accounts/esign_access.py
========================

Who can see an envelope, and what they can do with it.

The rule
--------
An envelope is visible to its **participants** and to nobody else:

    owner       the person who created it — full control
    signer      can open it, sign it, see it in their inbox
    approver    same as signer
    cc / bcc    can open and read it, cannot sign
    viewer      can open and read it, cannot sign

That is the whole list. Not "everyone in the agency". Not the ICT focal point.
Not the operations manager. Not, by default, the superuser.

Why the admin roles were removed
--------------------------------
The previous `_can_manage_envelope` granted access to `_is_ict(user, agency)`
and `_is_ops_manager(user, agency)`, and the dashboard skipped its participant
filter for those same roles. Those helpers come from `view_asset_management` —
they mark the people who administer laptops and asset requests. An envelope may
be a contract, a disciplinary letter, a medical clearance or a separation
agreement. Administering hardware is not a reason to be able to read one.

The superuser is treated the same way. A signature platform where the
administrator can silently open any signed document has a weaker audit story
than a filing cabinet with a lock, and it is the one thing signatories assume is
not true.

If you genuinely need an administrative override — voiding an envelope whose
sender has left, for instance — turn one of these on in settings:

    ESIGN_SUPERUSER_CAN_VIEW_ALL = True     # superusers see every envelope
    ESIGN_ADMIN_ROLES = ("ict_focal",)      # named roles see every envelope

Both are off by default. When either is used, the access is written to the
envelope's own audit trail as an `EnvelopeEvent`, so it shows on the signing
certificate. An override that leaves a trace is defensible; a silent one is not.

Cross-agency envelopes
----------------------
Access is decided by participation, not by agency. That is deliberate: when you
link two offices for eSign under Platform → Directory links, a UNICEF signer on
a UNDP envelope has to be able to open it. The old `agency=request.user.agency`
filter made that a 404.

Usage
-----
    from accounts.esign_access import (
        visible_envelopes, can_view, can_manage, can_sign,
        get_envelope_or_404, visible_recipients,
    )
"""

import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Overrides — both off unless you switch them on
# ---------------------------------------------------------------------------

def _superuser_override_enabled() -> bool:
    return bool(getattr(settings, "ESIGN_SUPERUSER_CAN_VIEW_ALL", False))


def _admin_roles() -> tuple:
    return tuple(getattr(settings, "ESIGN_ADMIN_ROLES", ()))


def has_override(user) -> bool:
    """True if this user can reach envelopes they are not a party to."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) and _superuser_override_enabled():
        return True
    roles = _admin_roles()
    return bool(roles and getattr(user, "role", None) in roles)


def log_override_access(envelope, user, action="viewed"):
    """
    Record an override on the envelope's own audit trail, so it appears on the
    certificate alongside the signatures.
    """
    try:
        from .models_esign import EnvelopeEvent

        EnvelopeEvent.objects.create(
            envelope=envelope,
            actor=user,
            event="viewed",
            note=f"Administrative access ({action}) by a non-participant",
            meta={
                "administrative_override": True,
                "action": action,
                "username": getattr(user, "username", ""),
                "is_superuser": bool(getattr(user, "is_superuser", False)),
                "role": getattr(user, "role", ""),
            },
        )
    except Exception:
        logger.exception("eSign: could not record override access on envelope %s",
                         getattr(envelope, "pk", "?"))


# ---------------------------------------------------------------------------
# Participation
# ---------------------------------------------------------------------------

def participant_q(user, prefix: str = "") -> Q:
    """
    Q object matching envelopes this user is a party to.

    ``prefix`` lets you use it from a related model, e.g.
    ``participant_q(user, "envelope__")``.

    A recipient is matched by user FK first. Email is only used as a fallback
    and only when the user actually has one — matching on a blank email would
    pair every user without an address to every recipient row saved without one.
    """
    q = Q(**{f"{prefix}created_by": user}) | Q(**{f"{prefix}recipients__user": user})
    email = (getattr(user, "email", "") or "").strip()
    if email:
        q |= Q(**{f"{prefix}recipients__email__iexact": email})
    return q


def is_participant(user, envelope) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if envelope.created_by_id and envelope.created_by_id == user.id:
        return True
    if envelope.recipients.filter(user_id=user.id).exists():
        return True
    email = (getattr(user, "email", "") or "").strip()
    if email and envelope.recipients.filter(email__iexact=email).exists():
        return True
    return False


def recipient_for(user, envelope):
    """The user's own recipient row on this envelope, or None."""
    if not user or not getattr(user, "is_authenticated", False):
        return None
    row = envelope.recipients.filter(user_id=user.id).first()
    if row:
        return row
    email = (getattr(user, "email", "") or "").strip()
    if email:
        return envelope.recipients.filter(email__iexact=email).first()
    return None


def is_owner(user, envelope) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and envelope.created_by_id
        and envelope.created_by_id == user.id
    )


# ---------------------------------------------------------------------------
# The three questions a view asks
# ---------------------------------------------------------------------------

def can_view(user, envelope) -> bool:
    """Open the envelope, read its documents, download the certificate."""
    if is_participant(user, envelope):
        return True
    return has_override(user)


def can_manage(user, envelope) -> bool:
    """
    Edit, add or remove recipients and documents, send, remind, void, delete.

    The owner only. Being a signer on an envelope does not let you rewrite it,
    and an override grants reading, not authorship.
    """
    if is_owner(user, envelope):
        return True
    if getattr(user, "is_superuser", False) and _superuser_override_enabled():
        return True
    return False


def can_sign(user, envelope) -> bool:
    """
    Place a signature. Requires a signer or approver row that is still open,
    and the envelope to be out for signature.
    """
    from .models_esign import Envelope, EnvelopeRecipient

    if envelope.status not in (Envelope.STATUS_SENT, Envelope.STATUS_RETURNED):
        return False
    row = recipient_for(user, envelope)
    if row is None:
        return False
    if row.role not in (EnvelopeRecipient.ROLE_SIGNER, EnvelopeRecipient.ROLE_APPROVER):
        return False
    if row.status in (EnvelopeRecipient.STATUS_SIGNED, EnvelopeRecipient.STATUS_DECLINED):
        return False
    can_now = getattr(row, "can_sign_now", None)
    return bool(can_now()) if callable(can_now) else True


# ---------------------------------------------------------------------------
# Querysets
# ---------------------------------------------------------------------------

def visible_envelopes(user, base=None):
    """
    Every envelope this user may see. Use this anywhere you would previously
    have written `Envelope.objects.filter(agency=agency)`.
    """
    from .models_esign import Envelope

    qs = base if base is not None else Envelope.objects.all()
    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()
    if has_override(user):
        return qs
    return qs.filter(participant_q(user)).distinct()


def inbox_rows(user, base=None):
    """
    The user's own recipient rows on live envelopes — what "waiting on me" and
    "queued for you" are built from.
    """
    from .models_esign import Envelope, EnvelopeRecipient

    qs = base if base is not None else EnvelopeRecipient.objects.all()
    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()

    match = Q(user=user)
    email = (getattr(user, "email", "") or "").strip()
    if email:
        match |= Q(email__iexact=email)

    return (
        qs.filter(
            match,
            envelope__status__in=[Envelope.STATUS_SENT, Envelope.STATUS_RETURNED],
            role__in=[EnvelopeRecipient.ROLE_SIGNER, EnvelopeRecipient.ROLE_APPROVER],
        )
        .exclude(status__in=[EnvelopeRecipient.STATUS_SIGNED,
                             EnvelopeRecipient.STATUS_DECLINED])
        .select_related("envelope", "envelope__created_by")
        .distinct()
    )


def visible_recipients(user, envelope):
    """
    The recipient list as this user should see it.

    BCC rows are hidden from everyone except the owner. The detail template
    labels them "hidden from other parties" while rendering them to whoever
    opens the page, which makes the label untrue — this makes it true.
    """
    from .models_esign import EnvelopeRecipient

    qs = envelope.recipients.all()
    if is_owner(user, envelope) or has_override(user):
        return qs

    own = recipient_for(user, envelope)
    q = ~Q(role=EnvelopeRecipient.ROLE_BCC)
    if own is not None:
        q |= Q(pk=own.pk)          # a BCC recipient still sees their own row
    return qs.filter(q)


# ---------------------------------------------------------------------------
# View helper
# ---------------------------------------------------------------------------

def get_envelope_or_404(request, pk, mode="view"):
    """
    Fetch an envelope and enforce access in one call.

    mode="view"    participant or override
    mode="manage"  owner only
    mode="sign"    an open signer/approver row

    Note there is no agency filter. Participation is the check, which is what
    lets a recipient in a linked office open an envelope sent to them.
    """
    from .models_esign import Envelope

    envelope = get_object_or_404(
        Envelope.objects.select_related("agency", "created_by"), pk=pk
    )
    user = request.user

    if mode == "manage":
        if not can_manage(user, envelope):
            if is_participant(user, envelope):
                raise PermissionDenied("Only the sender can change this envelope.")
            raise PermissionDenied("You do not have access to this envelope.")
        return envelope

    if mode == "sign":
        if not can_sign(user, envelope):
            raise PermissionDenied("You have nothing to sign on this envelope.")
        return envelope

    if not can_view(user, envelope):
        raise PermissionDenied("You do not have access to this envelope.")

    if not is_participant(user, envelope):
        log_override_access(envelope, user, action=f"opened envelope {envelope.pk}")

    return envelope
