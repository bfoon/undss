# accounts/audit_chain_esign.py
"""
UN PASS — NES tamper-evident audit trail.

The problem
-----------
The PAdES seal protects the *document*. It says nothing about the *trail* —
who viewed it, who signed when, from which address. That trail lives in
`EnvelopeEvent` rows, and until now "append-only" was a convention, not a
property: anyone with database access could edit a timestamp, drop a "viewed"
row, or insert a signature that never happened, and nothing would show.

For a UN record that is the weaker half. A dispute is rarely "was the PDF
altered?" — it is "did this person really approve it on that date?".

The mechanism
-------------
Every entry is linked to the one before it by a hash:

    entry_hash = SHA-256( prev_hash || canonical_payload(entry) )

That makes four attacks visible:

    edit a row      -> its entry_hash no longer matches its content
    delete a row    -> the next row's prev_hash points at nothing
    insert a row    -> the inserted row's prev_hash does not match
    reorder rows    -> the payload binds the row id, so the links break

It does NOT by itself catch the fifth: lopping entries off the *end*. Nothing
inside a chain can, because a shorter chain is still internally consistent.
That is why completion records the head hash and the entry count on
`EnvelopeSeal` and signs that statement — see `anchor_chain()`. With the
anchor, a truncated trail is detectable: the count will not match and the
head will not be reachable.

What this is not
----------------
This is tamper-EVIDENT, not tamper-PROOF. Someone with the database *and* the
signing key could rebuild a consistent chain and re-anchor it. The defence
there is that the anchor can be timestamped by an external TSA, which the
attacker does not control: they cannot backdate a re-anchored chain. Keep the
seal key off the application server for that to mean anything.

Design contract
---------------
Chaining must never break an envelope. `log_event()` is documented to swallow
its own failures, and it is called from the middle of signing flows. So every
function here either succeeds or returns/records a problem; none raises into
the caller. An entry that could not be chained is written unchained, and the
verifier reports it as "not chained" rather than "broken" — an honest gap
beats a false alarm.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

#: Bump when `canonical_payload` changes shape, or every previously written
#: hash becomes unverifiable. The version is part of the payload, so old
#: entries keep validating under the rules they were written with.
PAYLOAD_VERSION = "nes-audit-v1"

#: Prefix of the statement the anchor signs. Also versioned.
ANCHOR_STATEMENT_VERSION = "nes-audit-anchor-v1"


# ─────────────────────────────────────────────────────────────────────────────
# Hashing
# ─────────────────────────────────────────────────────────────────────────────

def _canonical_json(value: Any) -> str:
    """
    Stable JSON. Sorted keys and no incidental whitespace, so the same dict
    always hashes the same way regardless of how Python happened to order it.
    """
    try:
        return json.dumps(
            value or {},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=True,
        )
    except Exception:
        return json.dumps({"_unserialisable": str(value)}, sort_keys=True)


def canonical_payload(event) -> str:
    """
    The exact bytes an entry's hash is taken over.

    Field order is fixed and the row id is included, so rows cannot be
    reordered or re-pointed at another envelope without breaking the chain.
    `None` and `""` are normalised to the same empty marker so a NULL/empty
    change cannot slip through.
    """
    def s(v):
        return "" if v is None else str(v)

    at = getattr(event, "at", None)
    at_text = at.isoformat() if at is not None else ""

    parts = [
        PAYLOAD_VERSION,
        s(event.pk),
        s(event.envelope_id),
        s(event.event),
        at_text,
        s(event.recipient_id),
        s(event.actor_id),
        s(event.note),
        s(event.ip),
        s(event.user_agent),
        _canonical_json(event.meta),
    ]
    # Field separator that cannot occur in the components above: a literal
    # NUL would be rejected by some drivers, so use a unit separator.
    return "\x1f".join(parts)


def entry_digest(prev_hash: str, payload: str) -> str:
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("utf-8"))
    h.update(b"\x1e")
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────────

def chain_enabled() -> bool:
    return bool(getattr(settings, "ESIGN_AUDIT_CHAIN", True))


def chain_event(event) -> bool:
    """
    Link `event` to the end of its envelope's chain and persist the hashes.

    Returns True when the entry was chained. Never raises.

    The envelope row is locked for the duration so two concurrent events on
    the same envelope cannot both claim the same predecessor and fork the
    chain. The lock is per-envelope, so it does not serialise unrelated
    signing activity.
    """
    if not chain_enabled():
        return False

    try:
        from .models_esign import Envelope, EnvelopeEvent

        with transaction.atomic():
            # Lock the envelope, not the events: cheap, and it is the natural
            # mutex for "append to this envelope's trail".
            Envelope.objects.select_for_update().filter(
                pk=event.envelope_id
            ).values_list("pk", flat=True).first()

            prev = (
                EnvelopeEvent.objects
                .filter(envelope_id=event.envelope_id, entry_hash__gt="")
                .exclude(pk=event.pk)
                .order_by("-id")
                .values_list("entry_hash", flat=True)
                .first()
            ) or ""

            digest = entry_digest(prev, canonical_payload(event))

            EnvelopeEvent.objects.filter(pk=event.pk).update(
                prev_hash=prev, entry_hash=digest
            )

        event.prev_hash = prev
        event.entry_hash = digest
        return True

    except Exception:
        logger.warning(
            "NES audit chain: could not chain event %s on envelope %s — "
            "the entry is written unchained",
            getattr(event, "pk", "?"),
            getattr(event, "envelope_id", "?"),
            exc_info=True,
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────────

def verify_chain(envelope) -> dict:
    """
    Walk an envelope's trail and report on it.

        {
          "ok": bool,              # no broken link among chained entries
          "total": int,
          "chained": int,
          "unchained": int,        # written before chaining existed
          "head": str,             # entry_hash of the last chained entry
          "problems": [ {...} ],
          "checked_at": ...,
        }

    `ok` is True when every chained entry verifies. Unchained legacy entries
    are counted and reported, never treated as failures — they were written
    before the chain existed and saying "tampered" about them would be wrong.
    """
    from django.utils import timezone
    from .models_esign import EnvelopeEvent

    out = {
        "ok": False,
        "total": 0,
        "chained": 0,
        "unchained": 0,
        "head": "",
        "problems": [],
        "checked_at": timezone.now(),
    }

    try:
        rows = list(
            EnvelopeEvent.objects
            .filter(envelope=envelope)
            .order_by("id")
        )
    except Exception as exc:
        out["problems"].append({"kind": "unreadable", "detail": str(exc)})
        return out

    out["total"] = len(rows)
    expected_prev = ""
    seen_chained = False

    for row in rows:
        if not row.entry_hash:
            out["unchained"] += 1
            continue

        recomputed = entry_digest(row.prev_hash or "", canonical_payload(row))

        if recomputed != row.entry_hash:
            out["problems"].append({
                "kind": "content_altered",
                "event_id": row.pk,
                "event": row.event,
                "at": row.at,
                "detail": (
                    "The stored hash does not match this entry's contents — "
                    "the row was modified after it was written."
                ),
            })
        elif seen_chained and (row.prev_hash or "") != expected_prev:
            out["problems"].append({
                "kind": "broken_link",
                "event_id": row.pk,
                "event": row.event,
                "at": row.at,
                "detail": (
                    "This entry does not follow the one before it — an entry "
                    "was deleted, inserted or reordered here."
                ),
            })

        expected_prev = row.entry_hash
        out["head"] = row.entry_hash
        out["chained"] += 1
        seen_chained = True

    out["ok"] = not out["problems"]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Anchoring — what makes truncation detectable
# ─────────────────────────────────────────────────────────────────────────────

def anchor_statement(envelope, head: str, count: int, at_text: str) -> bytes:
    """
    The canonical sentence the anchor signs. Deliberately human-readable: an
    auditor opening the evidence package should be able to see what was
    attested without a parser.
    """
    return (
        f"{ANCHOR_STATEMENT_VERSION}\n"
        f"envelope_id={getattr(envelope, 'envelope_id', '')}\n"
        f"chain_head={head}\n"
        f"chain_length={count}\n"
        f"anchored_at={at_text}\n"
    ).encode("utf-8")


def anchor_chain(envelope) -> dict:
    """
    Sign (and where possible timestamp) this envelope's chain head.

    Returns a dict for `EnvelopeSeal`:

        {"head", "length", "token_b64", "timestamped", "at", "detail"}

    Never raises. Without a signing certificate there is nothing to sign and
    the head/length are still returned, so truncation remains detectable by
    anyone who recorded them — just not provably so.
    """
    from django.utils import timezone

    state = verify_chain(envelope)
    now = timezone.now()
    out = {
        "head": state["head"],
        "length": state["chained"],
        "token_b64": "",
        "timestamped": False,
        "at": now,
        "detail": "",
    }

    if not state["head"]:
        out["detail"] = "Nothing to anchor — no chained entries."
        return out

    try:
        from . import pades_esign as P

        ready, reason = P.pades_configured()
        if not ready:
            out["detail"] = f"Head recorded but not signed: {reason}"
            return out

        import asyncio

        signer = P.load_seal_signer()
        cfg = P.pades_config()
        timestamper = None
        try:
            timestamper = P._timestamper(cfg)
        except Exception:
            timestamper = None

        statement = anchor_statement(
            envelope, state["head"], state["chained"], now.isoformat()
        )

        async def _sign():
            return await signer.async_sign_general_data(
                statement, cfg["digest"], detached=True, timestamper=timestamper
            )

        try:
            content_info = asyncio.run(_sign())
            out["timestamped"] = timestamper is not None
        except Exception as exc:
            if timestamper is None:
                raise
            # The TSA is the optional part. Keep the signature.
            logger.warning(
                "NES audit chain: anchoring without a timestamp for envelope "
                "%s — %s", getattr(envelope, "envelope_id", "?"), exc
            )

            async def _sign_plain():
                return await signer.async_sign_general_data(
                    statement, cfg["digest"], detached=True
                )

            content_info = asyncio.run(_sign_plain())
            out["detail"] = f"Signed but not timestamped: {exc}"

        out["token_b64"] = base64.b64encode(content_info.dump()).decode("ascii")
        return out

    except Exception as exc:
        logger.exception(
            "NES audit chain: could not anchor envelope %s",
            getattr(envelope, "envelope_id", "?"),
        )
        out["detail"] = f"Head recorded but not signed: {exc}"
        return out


def verify_anchor(envelope, seal) -> dict:
    """
    Check the trail against the signed anchor.

        {"ok", "present", "signature_intact", "head_matches",
         "length_matches", "truncated", "detail"}

    This is the check that catches a trail with entries removed from the end.
    """
    out = {
        "ok": False,
        "present": False,
        "signature_intact": False,
        "head_matches": False,
        "length_matches": False,
        "truncated": False,
        "detail": "",
    }

    if seal is None or not getattr(seal, "audit_chain_head", ""):
        out["detail"] = "No anchor was recorded for this envelope."
        return out

    out["present"] = True
    state = verify_chain(envelope)

    out["head_matches"] = state["head"] == seal.audit_chain_head
    out["length_matches"] = state["chained"] == (seal.audit_chain_length or 0)
    out["truncated"] = state["chained"] < (seal.audit_chain_length or 0)

    if not seal.audit_anchor:
        out["detail"] = (
            "The head and length were recorded but never signed, so they "
            "attest only as strongly as the database they sit in."
        )
        out["ok"] = out["head_matches"] and out["length_matches"]
        return out

    # Validate the detached CMS signature over the recorded statement.
    try:
        import asyncio

        from asn1crypto import cms
        from pyhanko.sign.validation import async_validate_detached_cms

        from . import pades_esign as P

        at_text = (
            seal.audit_anchored_at.isoformat()
            if seal.audit_anchored_at else ""
        )
        statement = anchor_statement(
            envelope,
            seal.audit_chain_head,
            seal.audit_chain_length or 0,
            at_text,
        )

        content_info = cms.ContentInfo.load(
            base64.b64decode(seal.audit_anchor)
        )

        async def _check():
            return await async_validate_detached_cms(
                statement, content_info["content"]
            )

        with P._quiet_certvalidator():
            status = asyncio.run(_check())

        out["signature_intact"] = bool(status.intact and status.valid)
        if not out["signature_intact"]:
            out["detail"] = (
                "The anchor signature did not validate: " + str(status.summary())
            )
    except Exception as exc:
        out["detail"] = f"The anchor signature could not be checked: {exc}"
        logger.warning("NES audit chain: anchor check failed", exc_info=True)
        return out

    if out["truncated"]:
        missing = (seal.audit_chain_length or 0) - state["chained"]
        out["detail"] = (
            f"{missing} entr{'y' if missing == 1 else 'ies'} present at "
            f"completion {'is' if missing == 1 else 'are'} now missing."
        )
    elif not out["head_matches"]:
        out["detail"] = (
            "The trail no longer ends where it did at completion. Entries "
            "after the anchor are expected on an active envelope; on a "
            "completed one, investigate."
        )

    out["ok"] = (
        out["signature_intact"]
        and out["head_matches"]
        and out["length_matches"]
        and state["ok"]
    )
    return out


def chain_export(envelope) -> list[dict]:
    """
    The trail as plain dictionaries, for the evidence package. Includes the
    hashes so the chain can be re-walked offline with nothing but SHA-256.
    """
    from .models_esign import EnvelopeEvent

    rows = EnvelopeEvent.objects.filter(envelope=envelope).order_by("id")
    out = []
    for r in rows:
        out.append({
            "id": r.pk,
            "event": r.event,
            "event_display": r.get_event_display(),
            "at": r.at.isoformat() if r.at else "",
            "recipient_id": r.recipient_id,
            "recipient": getattr(r.recipient, "name", "") if r.recipient_id else "",
            "actor_id": r.actor_id,
            "actor": str(r.actor) if r.actor_id else "",
            "note": r.note,
            "ip": r.ip or "",
            "user_agent": r.user_agent or "",
            "meta": r.meta or {},
            "prev_hash": r.prev_hash or "",
            "entry_hash": r.entry_hash or "",
            "payload": canonical_payload(r),
        })
    return out
