# accounts/key_watch.py
"""
NES key watch — the person a key impersonates is told about it.

The gap this closes
-------------------
Anyone who can write to the database can add a signing key to someone
else's lifetime signature ID and then sign in their name. The ledger guard
cannot stop that: adding a key is something the application legitimately
does. The server has no way to tell a genuine new key from a planted one.

The owner can. So every key added to an identity is announced to its owner,
by email, outside UN PASS — in an inbox the attacker does not control:

1.  **At once**, when a key is added through UN PASS (enrolment, rotation).
2.  **Every night**, a sweep looks for keys that were never announced. A key
    inserted straight into the database never passed through (1), so it
    shows up here: the owner is told, and ICT is alerted, because a key that
    arrived without going through the application is the shape of exactly
    this attack.

"Announced" is remembered in Redis, not in the database the attacker is
writing to. The very first sweep takes a silent baseline of the keys that
already exist, so turning this on does not email everyone.

What it is not
--------------
A defence against someone who controls the database, Redis AND outbound mail
at once. Against that, only witnessing by other offices holds.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

ANNOUNCED = "unpass:keys:announced"
BASELINE = "unpass:keys:baseline"
TEMPLATE = "emails/expiry/digest.html"


def _redis():
    from .scheduler import redis_client

    client = redis_client()
    if client is None:
        return None
    try:
        client.ping()
        return client
    except Exception:
        return None


def _owner_email(key) -> str:
    identity = key.identity
    user = getattr(identity, "user", None)
    return (getattr(user, "email", "") or identity.enrolled_email or "").strip()


def _row(key) -> list[str]:
    identity = key.identity
    when = timezone.localtime(key.created_at).strftime("%Y-%m-%d %H:%M")
    origin = key.created_ip or "unknown address"
    return [identity.signature_id, f"revision {key.revision}",
            key.key_fingerprint[:16] + "…", when, origin]


_COLUMNS = ["Signature ID", "Key", "Fingerprint", "Added", "From"]


def _send(subject, to, heading, intro, rows, footer):
    from .asset_email import send_email_async

    to = sorted({e for e in to if e})
    if not to:
        return 0
    send_email_async(
        subject=subject, to_emails=to, html_template=TEMPLATE,
        context={"subject": subject, "heading": heading, "intro": intro,
                 "rows": rows, "columns": _COLUMNS, "action_url": _my_page(),
                 "brand": getattr(settings, "ESIGN_BRAND", "NES"),
                 "footer": footer},
    )
    return len(to)


def _my_page() -> str:
    try:
        from django.urls import reverse

        base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
        return f"{base}{reverse('accounts:esign_identity_home')}"
    except Exception:
        return ""


def announce(key) -> bool:
    """Tell the owner a key was added. Marks it announced. Never raises."""
    try:
        sent = _send(
            "A signing key was added to your NES signature",
            [_owner_email(key)],
            "New signing key on your signature",
            "A new signing key was added to your lifetime signature ID. If "
            "you just set up or replaced your key, there is nothing to do. "
            "If you did not, contact ICT at once: someone may be able to sign "
            "in your name.",
            [_row(key)],
            "Sent whenever a key is added to your signature ID, so that you "
            "are the first to know if one is added that you did not add.",
        )
        client = _redis()
        if client is not None:
            client.sadd(ANNOUNCED, key.key_fingerprint)
        return bool(sent)
    except Exception:
        logger.warning("NES key watch: could not announce key %s",
                       getattr(key, "pk", "?"), exc_info=True)
        return False


def sweep() -> dict:
    """Nightly: find keys nobody was told about, and tell."""
    from .models_esign_identity import SignatureKey

    client = _redis()
    if client is None:
        raise RuntimeError("Redis unavailable — cannot tell which keys were "
                           "announced. Nothing was sent.")

    keys = list(SignatureKey.objects.select_related("identity",
                                                    "identity__user"))
    if not client.exists(BASELINE):
        fingerprints = [k.key_fingerprint for k in keys]
        if fingerprints:
            client.sadd(ANNOUNCED, *fingerprints)
        client.set(BASELINE, timezone.now().isoformat())
        return {"baseline": len(fingerprints),
                "note": "first run: existing keys recorded, nobody emailed"}

    known = client.smembers(ANNOUNCED)
    unannounced = [k for k in keys if k.key_fingerprint not in known]
    owners = 0
    for key in unannounced:
        owners += _send(
            "Check this: a signing key was added to your NES signature",
            [_owner_email(key)],
            "A key was added to your signature without notice",
            "A signing key was found on your lifetime signature ID that you "
            "were not told about when it was added. If you did not add it, "
            "contact ICT immediately and do not sign anything until it has "
            "been looked at.",
            [_row(key)],
            "Found by the nightly key check. ICT has been told as well.",
        )

    ict = 0
    if unannounced:
        to = list(getattr(settings, "ESIGN_WITNESS_ALERT_EMAILS", []) or [])
        to += [e for _n, e in getattr(settings, "ADMINS", []) or []]
        ict = _send(
            f"NES: {len(unannounced)} signing key(s) appeared without notice",
            to,
            "Signing keys added outside UN PASS",
            "These keys exist in the database but were never announced, which "
            "is what happens when a key is written into the database directly "
            "instead of through UN PASS — or when the notice could not be "
            "sent. Confirm each with its owner before trusting any signature "
            "made with it.",
            [_row(k) for k in unannounced],
            "Sent once by the nightly key check, for each key it finds.",
        )
        if not to:
            logger.error("NES key watch: %d unannounced key(s) and nobody to "
                         "tell — set ESIGN_WITNESS_ALERT_EMAILS",
                         len(unannounced))
        client.sadd(ANNOUNCED, *[k.key_fingerprint for k in unannounced])

    return {"keys": len(keys), "unannounced": len(unannounced),
            "owners_emailed": owners, "ict_emailed": ict}
