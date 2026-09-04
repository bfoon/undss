"""
accounts/context_processors_esign.py
====================================

The number of envelopes waiting on this person's signature, for the nav badge.

Add to settings.TEMPLATES["OPTIONS"]["context_processors"], after the tenancy
one:

    "accounts.context_processors_esign.esign_badge",

Why this is cached
------------------
A context processor runs on **every** page render, including pages that have
nothing to do with eSign. An uncached count is one extra query on every request
in the platform, forever, to render a number that changes a few times a day.

So the count is cached per user for a minute, and invalidated the moment it
could actually change — when an envelope is sent, signed, declined, returned or
voided. `bump_esign_badge(user)` does that; call it from those views.

A minute of staleness is the worst case if you forget to call it somewhere,
which is a better failure than a permanently wrong badge.
"""

from django.conf import settings
from django.core.cache import cache

#: How long a count survives without being invalidated explicitly.
BADGE_TTL = getattr(settings, "ESIGN_BADGE_TTL", 60)

#: Above this the badge reads "9+" rather than a number nobody reads precisely.
BADGE_MAX = getattr(settings, "ESIGN_BADGE_MAX", 9)


def _key(user_id) -> str:
    return f"esign:badge:{user_id}"


def bump_esign_badge(user_or_id):
    """
    Drop a person's cached count.

    Call after anything that changes what is waiting on someone: sending an
    envelope, signing, declining, returning, voiding, or adding a recipient.
    Safe to call with a user, an id, or None.
    """
    if user_or_id is None:
        return
    user_id = getattr(user_or_id, "id", user_or_id)
    if user_id:
        cache.delete(_key(user_id))


def bump_envelope_badges(envelope):
    """Drop the count for every recipient on an envelope, plus the sender."""
    try:
        ids = set(
            envelope.recipients.exclude(user__isnull=True).values_list("user_id", flat=True)
        )
        if envelope.created_by_id:
            ids.add(envelope.created_by_id)
        cache.delete_many([_key(i) for i in ids if i])
    except Exception:  # noqa: BLE001 - a badge must never break a signing flow
        pass


def esign_action_count(user) -> int:
    """
    Envelopes this person can sign right now.

    Counts the same rows the dashboard's "Waiting on me" panel shows, so the
    badge and the page can never disagree — and only those the person can
    actually act on, so a queued signer waiting on an earlier one is not told
    they have something to do.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return 0

    key = _key(user.id)
    cached = cache.get(key)
    if cached is not None:
        return cached

    try:
        from .esign_access import inbox_rows

        rows = inbox_rows(user).only("id", "order", "status", "envelope_id")
        count = sum(1 for row in rows if row.can_sign_now())
    except Exception:  # noqa: BLE001
        count = 0

    cache.set(key, count, BADGE_TTL)
    return count


def esign_badge(request):
    """Adds `esign_action_count` and `esign_badge_label` to every template."""
    user = getattr(request, "user", None)

    if user is None or not user.is_authenticated:
        return {"esign_action_count": 0, "esign_badge_label": ""}

    # Skip the query entirely when the module is off for this office. The
    # tenancy middleware has already resolved the flags by this point.
    flags = getattr(request, "tenancy_features", None) or {}
    if flags and not flags.get("esign"):
        return {"esign_action_count": 0, "esign_badge_label": ""}

    count = esign_action_count(user)
    return {
        "esign_action_count": count,
        "esign_badge_label": f"{BADGE_MAX}+" if count > BADGE_MAX else str(count),
    }
