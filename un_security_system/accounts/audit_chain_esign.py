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


def _chain_head(rows) -> str:
    """
    The entry_hash at the end of a chain, found by its LINKS.

    The head is the chained entry nothing else points to. It used to be "the
    chained entry with the highest id", which is the same thing only while
    entries are chained in the order their rows were inserted. Two events on
    one envelope at the same moment break that: the second insert can take
    the lock first. Picking the head by id then hands the next entry the
    wrong predecessor — a fork, and a false tamper alarm on an envelope
    nobody touched. Reproduced on PostgreSQL with eight concurrent writers.

    `rows` is an iterable of (id, prev_hash, entry_hash) for chained entries.
    If there is somehow more than one tip (a trail damaged before this fix),
    the newest is extended so the damage is not compounded — and the verifier
    still reports it.
    """
    rows = list(rows)
    referenced = {prev for _id, prev, _h in rows if prev}
    tips = [(pk, h) for pk, _prev, h in rows if h not in referenced]
    if not tips:
        return ""
    return max(tips)[1]


def chain_event(event) -> bool:
    """
    Link `event` to the end of its envelope's chain and persist the hashes.

    Returns True when the entry was chained. Never raises.

    The envelope row is locked for the duration so two concurrent events on
    the same envelope cannot both claim the same predecessor and fork the
    chain. The lock is per-envelope, so it does not serialise unrelated
    signing activity.

    Whoever holds the lock also chains every OTHER entry of this envelope that
    has been written but not yet chained, in id order, before its own. So when
    eight requests insert at once and take the lock in a different order, the
    chain still comes out in insertion order — not because the verifier
    needs that any more (it follows links), but because a chain that reads in
    the order things happened is the one a person can audit by eye.
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

            current = (
                EnvelopeEvent.objects.filter(pk=event.pk)
                .values_list("entry_hash", flat=True).first()
            )
            if current:
                # Another request already chained this entry while chaining
                # its own. Nothing to do — and recomputing would be wrong.
                event.entry_hash = current
                return True

            chained = list(
                EnvelopeEvent.objects
                .filter(envelope_id=event.envelope_id, entry_hash__gt="")
                .values_list("id", "prev_hash", "entry_hash")
            )
            prev = _chain_head(chained)

            # Entries waiting to be chained. Bounded below by the first
            # chained entry: anything older and unchained predates the chain
            # itself, is legacy, and must stay unchained — hashing it now
            # would claim a provenance it never had.
            floor = min((pk for pk, _p, _h in chained), default=None)
            batch = EnvelopeEvent.objects.filter(
                envelope_id=event.envelope_id, entry_hash="", id__lte=event.pk,
            )
            batch = (batch.filter(id__gt=floor) if floor is not None
                     else batch.filter(id=event.pk))
            pending = list(batch.order_by("id"))
            if not any(row.pk == event.pk for row in pending):
                pending.append(event)

            digest = ""
            for row in pending:
                digest = entry_digest(prev, canonical_payload(row))
                EnvelopeEvent.objects.filter(pk=row.pk).update(
                    prev_hash=prev, entry_hash=digest
                )
                if row.pk == event.pk:
                    event.prev_hash = prev
                    event.entry_hash = digest
                prev = digest

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
    try:
        from .db_guard import detached_values

        restore = detached_values(r.pk for r in rows if r.entry_hash)
    except Exception:
        restore = {}
    walk = _walk(rows, restore)
    out["detached"] = walk.get("detached", [])
    out["unchained"] = len(walk["unchained"])
    out["chained"] = len(rows) - out["unchained"]
    out["head"] = walk["head"]
    out["problems"] = walk["problems"]
    out["order"] = [row.pk for row in walk["order"]]
    # Chained hashes in link order: what the anchor is checked against.
    out["chain"] = [row.entry_hash for row in walk["order"] if row.entry_hash]
    out["events"] = [row.event for row in walk["order"] if row.entry_hash]
    out["ok"] = not out["problems"]
    return out


def _problem(kind, row, detail):
    return {"kind": kind, "event_id": row.pk, "event": row.event,
            "at": row.at, "detail": detail}


def _restored(row, values: dict):
    """A copy of `row` with detached references put back, for re-hashing."""
    import copy

    clone = copy.copy(row)
    for column, value in values.items():
        setattr(clone, column, value)
    return clone


def _walk(rows, restore: dict | None = None) -> dict:
    """
    Follow the chain by its links and report what does not fit.

    A hash chain is defined by its links — each entry names the hash of the
    one before it — so that is how it is walked: from the entry that names no
    predecessor, to the entry that names it, and so on. Walking it in row-id
    order instead, as this did before, is the same thing only while entries
    are chained in insertion order; under concurrent writes they are not, and
    the old walk reported tampering on trails nobody had touched.

    Following links loses nothing an attacker could exploit:

        edit a row      -> its stored hash does not match its content
        delete a row    -> its successor names a hash that no longer exists
        insert a row    -> it shares a predecessor with a genuine entry: a fork
        move a row      -> broken at the source; at the destination its payload
                           binds the original envelope, so its hash fails
        drop the end    -> the signed anchor (count and head) — as before

    Returns {"order", "unchained", "head", "problems"}. `order` is the chain
    in link order, preceded by any legacy unchained entries in id order and
    followed by any chained entries the walk could not reach.
    """
    unchained = [r for r in rows if not r.entry_hash]
    chained = [r for r in rows if r.entry_hash]
    problems = []

    restore = restore or {}
    detached = []
    by_hash = {}
    for row in chained:
        recomputed = entry_digest(row.prev_hash or "", canonical_payload(row))
        if recomputed != row.entry_hash and row.pk in restore:
            # A person this entry named was deleted later, and the database
            # guard logged who it was. Hash it as it was written.
            again = entry_digest(row.prev_hash or "",
                                 canonical_payload(_restored(row, restore[row.pk])))
            if again == row.entry_hash:
                recomputed = again
                detached.append({"event_id": row.pk, "event": row.event,
                                 "columns": sorted(restore[row.pk])})
        if recomputed != row.entry_hash:
            problems.append(_problem(
                "content_altered", row,
                "The stored hash does not match this entry's contents — the "
                "row was modified after it was written."))
        by_hash.setdefault(row.entry_hash, row)

    children: dict[str, list] = {}
    for row in chained:
        children.setdefault(row.prev_hash or "", []).append(row)
    for kids in children.values():
        kids.sort(key=lambda r: r.pk)

    for prev, kids in children.items():
        if len(kids) > 1:
            for row in kids[1:]:
                problems.append(_problem(
                    "forked", row,
                    "This entry claims the same predecessor as another entry. "
                    "Exactly one can follow any entry — one of these was "
                    "inserted."))
        if prev and prev not in by_hash:
            for row in kids:
                problems.append(_problem(
                    "broken_link", row,
                    "The entry this one follows no longer exists — an entry "
                    "was deleted here."))

    # Walk from the start. Where the chain forks, follow the earliest-written
    # branch; the fork itself is already reported.
    order, seen = [], set()
    node = (children.get("") or [None])[0]
    while node is not None and node.pk not in seen:
        order.append(node)
        seen.add(node.pk)
        nxt = children.get(node.entry_hash) or []
        node = nxt[0] if nxt else None

    reported = {p["event_id"] for p in problems}
    stray = [r for r in chained if r.pk not in seen]
    for row in stray:
        if row.pk not in reported:
            problems.append(_problem(
                "broken_link", row,
                "This entry cannot be reached from the start of the trail — "
                "an entry between them was deleted or altered."))

    return {
        "order": unchained + order + sorted(stray, key=lambda r: r.pk),
        "unchained": unchained,
        "head": order[-1].entry_hash if order else "",
        "problems": problems,
        "detached": detached,
    }


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

    # The anchor attests the trail AS IT WAS AT COMPLETION: its length, and
    # the hash it ended on. Entries after that are normal — every download
    # of the signed copy appends one — so the question is not "does the
    # trail still end on the anchored hash?" but "is the anchored hash still
    # exactly where the anchor says, with the later entries chained after
    # it?". The first reading made every completed envelope fail its anchor
    # check the moment anyone downloaded it.
    anchored_len = seal.audit_chain_length or 0
    chain = state.get("chain", [])
    try:
        position = chain.index(seal.audit_chain_head)
    except ValueError:
        position = -1

    prefix_ok = position == anchored_len - 1 and anchored_len > 0
    out["head_matches"] = prefix_ok
    out["length_matches"] = prefix_ok
    out["truncated"] = len(chain) < anchored_len or position == -1
    after = state.get("events", [])[position + 1:] if prefix_ok else []
    out["after_anchor"] = len(after)
    out["after_anchor_events"] = after

    # Nothing that creates a signature should happen after completion. An
    # entry like that after the anchor is not a download — it is the shape of
    # a signature added to a closed envelope.
    late = sorted({e for e in after if e in _CLOSED_EVENTS})
    out["late_signing_events"] = late

    if not seal.audit_anchor:
        out["detail"] = (
            "The head and length were recorded but never signed, so they "
            "attest only as strongly as the database they sit in."
        )
        out["ok"] = prefix_ok and state["ok"] and not late
        if out["ok"] and out.get("after_anchor"):
            out["detail"] += (
                f" {out['after_anchor']} later entr"
                f"{'y' if out['after_anchor'] == 1 else 'ies'} chain after it.")
        elif not out["ok"]:
            _anchor_detail(out, state, seal)
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

    _anchor_detail(out, state, seal)
    out["ok"] = (
        out["signature_intact"]
        and prefix_ok
        and state["ok"]
        and not late
    )
    return out


#: Events that only make sense BEFORE an envelope completes.
#: ("sealed" is not here: the office seal is legitimately re-applied after
#: completion when a certificate is installed later or a seal is upgraded.)
_CLOSED_EVENTS = {"signed", "approved", "attested", "completed",
                  "declined", "returned"}


def _anchor_detail(out: dict, state: dict, seal) -> None:
    """Plain-words explanation of an anchor check, written into `out`."""
    anchored_len = seal.audit_chain_length or 0
    if out["truncated"]:
        present = len(state.get("chain", []))
        missing = max(anchored_len - present, 0)
        out["detail"] = (
            f"{missing} entr{'y' if missing == 1 else 'ies'} present at "
            f"completion {'is' if missing == 1 else 'are'} now missing."
            if missing else
            "The entry the trail ended on at completion is no longer in it — "
            "the trail before completion was altered."
        )
    elif not out["head_matches"]:
        out["detail"] = (
            "The trail at completion is no longer a prefix of the trail now: "
            "entries were inserted before the anchor."
        )
    elif not state.get("ok", True):
        kinds = sorted({p["kind"] for p in state.get("problems", [])})
        out["detail"] = (
            "The trail itself fails verification (" + ", ".join(kinds)
            + ") — an entry was altered, removed or inserted."
        )
    elif out.get("late_signing_events"):
        out["detail"] = (
            "Signing activity was recorded AFTER completion ("
            + ", ".join(out["late_signing_events"])
            + "). A closed envelope cannot be signed — investigate."
        )
    elif out.get("after_anchor"):
        out["detail"] = (
            f"Intact. {out['after_anchor']} entr"
            f"{'y' if out['after_anchor'] == 1 else 'ies'} added after "
            "completion (" + ", ".join(sorted(set(out['after_anchor_events'])))
            + "), each chained after the anchored one."
        )


def chain_export(envelope) -> list[dict]:
    """
    The trail as plain dictionaries, for the evidence package. Includes the
    hashes so the chain can be re-walked offline with nothing but SHA-256.
    """
    from .models_esign import EnvelopeEvent

    # In chain order, not row order, so that an auditor walking the file top
    # to bottom — "each entry's prev_hash is the entry_hash above it" — gets
    # the answer the verifier gets. The two orders differ whenever events
    # were written concurrently.
    all_rows = list(
        EnvelopeEvent.objects.filter(envelope=envelope)
        .select_related("recipient", "actor").order_by("id")
    )
    try:
        from .db_guard import detached_values

        restore = detached_values(r.pk for r in all_rows if r.entry_hash)
    except Exception:
        restore = {}
    rows = _walk(all_rows, restore)["order"]
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
            # The payload as hashed. Where a person this entry named was
            # deleted later, their id is put back from the guard's log, so
            # the offline re-walk still works; `detached` says which.
            "payload": canonical_payload(_restored(r, restore[r.pk])
                                         if r.pk in restore else r),
            "detached": restore.get(r.pk, {}),
        })
    return out


def sign_recorded_anchor(envelope, seal) -> dict:
    """
    Sign an anchor whose head and length were recorded at completion but not
    signed (there was no seal certificate then).

    The statement signed is exactly the one recorded — same head, length and
    anchored_at — so the ordinary verifier accepts it. What it proves is
    honest: the CMS signing time and the TSA token say *when* it was signed,
    so it attests that the trail has not been cut short since that moment,
    not since completion. The detail says so in words.
    """
    import asyncio

    from . import pades_esign as P

    out = {"ok": False, "detail": ""}
    if seal is None or not seal.audit_chain_head:
        out["detail"] = "No recorded head to sign."
        return out
    if seal.audit_anchor:
        out.update(ok=True, detail="Already signed.")
        return out
    ready, reason = P.pades_configured()
    if not ready:
        out["detail"] = reason
        return out

    at_text = seal.audit_anchored_at.isoformat() if seal.audit_anchored_at else ""
    statement = anchor_statement(envelope, seal.audit_chain_head,
                                 seal.audit_chain_length or 0, at_text)
    signer = P.load_seal_signer()
    cfg = P.pades_config()
    try:
        timestamper = P._timestamper(cfg)
    except Exception:
        timestamper = None

    async def _sign(ts):
        return await signer.async_sign_general_data(statement, cfg["digest"],
                                                    detached=True, timestamper=ts)
    try:
        content_info = asyncio.run(_sign(timestamper))
        stamped = timestamper is not None
    except Exception:
        if timestamper is None:
            raise
        content_info = asyncio.run(_sign(None))
        stamped = False

    from django.utils import timezone

    seal.audit_anchor = base64.b64encode(content_info.dump()).decode("ascii")
    seal.audit_anchor_timestamped = stamped
    seal.audit_detail = (
        f"Head recorded at completion ({at_text[:19]}) and signed later, on "
        f"{timezone.now():%Y-%m-%d %H:%M} UTC, when a seal certificate was installed. "
        "It proves the trail is unshortened since the signing time."
    )[:2000]
    seal.save(update_fields=["audit_anchor", "audit_anchor_timestamped", "audit_detail"])
    out.update(ok=True, timestamped=stamped, detail=seal.audit_detail)
    return out
