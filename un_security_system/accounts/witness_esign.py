# accounts/witness_esign.py
"""
UN PASS — NES cross-office witnessing service.

See models_esign_witness.py for why this exists. In short: a seal held by one
office is only as strong as that office's key custody. Distributing signed
fingerprints to participating country offices means forging a document
requires compromising all of them at once.

Shape of the thing
------------------
    sealing an envelope   -> issue_witness()      writes records + queues
    cron                  -> send_pending()       pushes to peers over HTTP
    a peer pushes to us   -> receive_statement()  verifies and stores
    cron / on demand      -> audit()              compares and raises alerts

Two rules govern everything here.

**Never block sealing.** `issue_witness` only touches the database. Nothing in
the signing path makes a network call, so an unreachable peer in another
country cannot slow down or fail a signature in Banjul.

**Authenticate the statement, not just the connection.** A bearer token
authenticates the HTTP request and is worth little on its own — tokens leak.
What we actually trust is the detached CMS signature over the statement,
checked against the peer's registered certificate. A stolen token lets an
attacker talk to us; it does not let them say anything we will believe.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

STATEMENT_VERSION = "nes-witness-v1"

#: Statements are small; anything larger is not one.
MAX_PAYLOAD_BYTES = 64 * 1024

DEFAULT_TIMEOUT = 15


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

def witnessing_enabled() -> bool:
    return bool(getattr(settings, "ESIGN_WITNESS_ENABLED", False))


def office_code() -> str:
    """
    This office's code, as it appears in statements we issue.

    Required: a statement that does not say who issued it is unusable, so
    issuing is refused rather than producing anonymous attestations.
    """
    return str(getattr(settings, "ESIGN_WITNESS_OFFICE_CODE", "") or "").strip()


def alert_recipients() -> list[str]:
    value = getattr(settings, "ESIGN_WITNESS_ALERT_EMAILS", None)
    if not value:
        admins = getattr(settings, "ADMINS", None) or []
        return [email for _name, email in admins if email]
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


# ─────────────────────────────────────────────────────────────────────────────
# The statement
# ─────────────────────────────────────────────────────────────────────────────

def build_statement(
    *,
    origin_code: str,
    envelope_uid: str,
    kind: str,
    document_sha256: str,
    certificate_sha256: str,
    chain_head: str,
    chain_length: int,
    seal_level: str,
    sealed_at_text: str,
    issued_at_text: str,
    reference: str = "",
) -> bytes:
    """
    The canonical text that gets signed.

    Human-readable on purpose. Somebody opening this in 2040 should be able to
    see what was attested without a parser, and every field that matters is on
    its own line so a diff shows exactly what changed.
    """
    return (
        f"{STATEMENT_VERSION}\n"
        f"origin={origin_code}\n"
        f"envelope={envelope_uid}\n"
        f"kind={kind}\n"
        f"reference={reference}\n"
        f"document_sha256={document_sha256}\n"
        f"certificate_sha256={certificate_sha256}\n"
        f"chain_head={chain_head}\n"
        f"chain_length={chain_length}\n"
        f"seal_level={seal_level}\n"
        f"sealed_at={sealed_at_text}\n"
        f"issued_at={issued_at_text}\n"
    ).encode("utf-8")


def _sign_statement(statement: bytes) -> tuple[str, str]:
    """
    Sign a statement with the office seal key.

    Returns `(signature_b64, detail)`. An unsigned statement is still stored
    and still useful — a peer holding an unsigned fingerprint can still detect
    a change — but it cannot be attributed, so the detail says so.
    """
    try:
        import asyncio

        from . import pades_esign as P

        ready, reason = P.pades_configured()
        if not ready:
            return "", f"Unsigned: {reason}"

        signer = P.load_seal_signer()
        cfg = P.pades_config()
        timestamper = None
        try:
            timestamper = P._timestamper(cfg)
        except Exception:
            timestamper = None

        async def _sign(ts):
            return await signer.async_sign_general_data(
                statement, cfg["digest"], detached=True, timestamper=ts
            )

        try:
            content_info = asyncio.run(_sign(timestamper))
        except Exception as exc:
            if timestamper is None:
                raise
            logger.warning(
                "NES witness: signing without a timestamp — %s", exc
            )
            content_info = asyncio.run(_sign(None))

        return base64.b64encode(content_info.dump()).decode("ascii"), ""
    except Exception as exc:
        logger.exception("NES witness: could not sign a statement")
        return "", f"Unsigned: {exc}"


def verify_statement_signature(
    statement: bytes, signature_b64: str, certificate_pem: str
) -> tuple[bool, str]:
    """
    Check a peer's statement.

    Two things must hold, and both matter:

    1. The CMS signature is intact over exactly this statement.
    2. The certificate that signed it **is** the certificate we have on file
       for that peer, compared by SHA-256 of its DER.

    Without (2) any valid certificate from anywhere would pass, which would
    make the check decorative.
    """
    if not signature_b64:
        return False, "No signature was supplied."
    if not certificate_pem.strip():
        return False, "No certificate on file for this peer."

    try:
        import asyncio

        from asn1crypto import cms, pem, x509
        from pyhanko.sign.validation import async_validate_detached_cms

        from . import pades_esign as P

        der = None
        data = certificate_pem.encode() if isinstance(certificate_pem, str) \
            else certificate_pem
        for _type_name, _headers, body in pem.unarmor(data, multiple=True):
            der = body
            break
        if der is None:
            return False, "The stored certificate could not be parsed."

        expected_fp = hashlib.sha256(der).hexdigest()

        content_info = cms.ContentInfo.load(base64.b64decode(signature_b64))
        signed_data = content_info["content"]

        # Which certificate actually signed this?
        presented = list(signed_data["certificates"] or [])
        if not presented:
            return False, "The signature carries no certificate."
        actual_fp = hashlib.sha256(presented[0].chosen.dump()).hexdigest()

        if actual_fp != expected_fp:
            return False, (
                "Signed by a different certificate than the one on file for "
                f"this peer (got {actual_fp[:16]}…, expected "
                f"{expected_fp[:16]}…)."
            )

        async def _check():
            return await async_validate_detached_cms(statement, signed_data)

        with P._quiet_certvalidator():
            status = asyncio.run(_check())

        if not (status.intact and status.valid):
            return False, f"Signature did not validate: {status.summary()}"

        return True, ""
    except Exception as exc:
        logger.warning("NES witness: signature check failed", exc_info=True)
        return False, f"Signature could not be checked: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Issuing — called from the signing path, database only
# ─────────────────────────────────────────────────────────────────────────────

def issue_witness(envelope, kind: str = "seal") -> dict:
    """
    Record and queue a fingerprint attestation for `envelope`.

    Database work only — no network. Never raises.
    """
    out = {"ok": False, "record_id": None, "queued": 0, "detail": ""}

    if not witnessing_enabled():
        out["detail"] = "ESIGN_WITNESS_ENABLED is off."
        return out

    code = office_code()
    if not code:
        out["detail"] = (
            "ESIGN_WITNESS_OFFICE_CODE is not set, so a statement could not "
            "say which office issued it."
        )
        logger.warning("NES witness: %s", out["detail"])
        return out

    try:
        from .models_esign_pades import EnvelopeSeal
        from .models_esign_witness import (
            WitnessDelivery, WitnessPeer, WitnessRecord,
        )

        seal = EnvelopeSeal.objects.filter(envelope=envelope).first()
        if seal is None or not seal.document_sha256:
            out["detail"] = "No seal fingerprint to witness yet."
            return out

        now = timezone.now()
        sealed_text = seal.applied_at.isoformat() if seal.applied_at else ""
        statement = build_statement(
            origin_code=code,
            envelope_uid=envelope.envelope_id,
            kind=kind,
            document_sha256=seal.document_sha256,
            certificate_sha256=seal.certificate_sha256 or "",
            chain_head=seal.audit_chain_head or "",
            chain_length=seal.audit_chain_length or 0,
            seal_level=seal.level_achieved or "",
            sealed_at_text=sealed_text,
            issued_at_text=now.isoformat(),
            reference=envelope.reference or "",
        )
        signature_b64, detail = _sign_statement(statement)

        record, created = WitnessRecord.objects.get_or_create(
            origin_code=code,
            envelope_uid=envelope.envelope_id,
            document_sha256=seal.document_sha256,
            kind=kind,
            defaults={
                "reference": (envelope.reference or "")[:120],
                "certificate_sha256": seal.certificate_sha256 or "",
                "chain_head": seal.audit_chain_head or "",
                "chain_length": seal.audit_chain_length or 0,
                "seal_level": seal.level_achieved or "",
                "sealed_at": seal.applied_at,
                "issued_at": now,
                "statement": statement.decode("utf-8"),
                "signature_b64": signature_b64,
                "signature_verified": bool(signature_b64),
                "verify_detail": detail,
                "local_envelope": envelope,
            },
        )
        out["record_id"] = record.pk

        peers = WitnessPeer.objects.filter(active=True, accepts_push=True)
        queued = 0
        for peer in peers:
            _row, made = WitnessDelivery.objects.get_or_create(
                record=record, peer=peer,
                defaults={"status": WitnessDelivery.STATUS_PENDING},
            )
            if made:
                queued += 1

        out.update(ok=True, queued=queued)
        if not peers:
            out["detail"] = (
                "No active peers are configured, so the fingerprint was "
                "recorded locally but not distributed."
            )
        elif detail:
            out["detail"] = detail
        return out

    except Exception as exc:
        logger.exception(
            "NES witness: could not issue for envelope %s",
            getattr(envelope, "pk", "?"),
        )
        out["detail"] = str(exc)
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Sending — cron, never the signing path
# ─────────────────────────────────────────────────────────────────────────────

def _post_json(url: str, token: str, payload: dict, timeout: int) -> tuple[int, bytes]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "UNPASS-NES-Witness/1",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read(MAX_PAYLOAD_BYTES)


def record_payload(record) -> dict:
    return {
        "format": STATEMENT_VERSION,
        "origin_code": record.origin_code,
        "envelope_uid": record.envelope_uid,
        "kind": record.kind,
        "reference": record.reference,
        "document_sha256": record.document_sha256,
        "certificate_sha256": record.certificate_sha256,
        "chain_head": record.chain_head,
        "chain_length": record.chain_length,
        "seal_level": record.seal_level,
        "sealed_at": record.sealed_at.isoformat() if record.sealed_at else "",
        "issued_at": record.issued_at.isoformat() if record.issued_at else "",
        "statement": record.statement,
        "signature_b64": record.signature_b64,
    }


def send_pending(limit: int = 200, max_attempts: int = 8) -> dict:
    """
    Push queued attestations to peers.

    Failures are retried on the next run with a widening backoff, and give up
    after `max_attempts` so a decommissioned office does not fill the log
    forever. Never raises.
    """
    from .models_esign_witness import WitnessDelivery

    report = {"attempted": 0, "sent": 0, "failed": 0, "gave_up": 0, "errors": []}
    timeout = int(getattr(settings, "ESIGN_WITNESS_TIMEOUT", DEFAULT_TIMEOUT))
    now = timezone.now()

    rows = (
        WitnessDelivery.objects
        .filter(status=WitnessDelivery.STATUS_PENDING)
        .select_related("peer", "record")
        .order_by("id")[:limit]
    )

    for delivery in rows:
        # Exponential-ish backoff: skip a row whose last attempt is too recent.
        if delivery.last_attempt_at and delivery.attempts:
            wait = timedelta(minutes=min(6 * 60, 5 * (2 ** (delivery.attempts - 1))))
            if now - delivery.last_attempt_at < wait:
                continue

        peer = delivery.peer
        report["attempted"] += 1
        delivery.attempts += 1
        delivery.last_attempt_at = now

        if not peer.active or not peer.base_url:
            delivery.status = WitnessDelivery.STATUS_FAILED
            delivery.last_error = "Peer is inactive or has no URL."
            delivery.save(update_fields=[
                "attempts", "last_attempt_at", "status", "last_error"])
            report["failed"] += 1
            continue

        try:
            status, raw = _post_json(
                peer.receive_url, peer.outbound_token,
                record_payload(delivery.record), timeout,
            )
            if 200 <= status < 300:
                delivery.status = WitnessDelivery.STATUS_SENT
                delivery.sent_at = now
                delivery.last_error = ""
                peer.last_contact_at = now
                peer.last_error = ""
                peer.save(update_fields=["last_contact_at", "last_error"])
                report["sent"] += 1
            else:
                raise RuntimeError(
                    f"HTTP {status}: {raw[:200].decode('utf-8', 'replace')}"
                )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            delivery.last_error = message[:2000]
            peer.last_error = message[:2000]
            peer.save(update_fields=["last_error"])
            if delivery.attempts >= max_attempts:
                delivery.status = WitnessDelivery.STATUS_FAILED
                report["gave_up"] += 1
                raise_alert(
                    kind="peer_unreachable",
                    severity="warning",
                    peer=peer,
                    envelope_uid=delivery.record.envelope_uid,
                    origin_code=delivery.record.origin_code,
                    detail=(
                        f"Gave up distributing to {peer.code} after "
                        f"{delivery.attempts} attempts: {message}"
                    ),
                )
            report["failed"] += 1
            report["errors"].append(f"{peer.code}: {message}")

        delivery.save(update_fields=[
            "attempts", "last_attempt_at", "status", "sent_at", "last_error",
        ])

    return report


# ─────────────────────────────────────────────────────────────────────────────
# Receiving
# ─────────────────────────────────────────────────────────────────────────────

def receive_statement(payload: dict, peer) -> dict:
    """
    Store an attestation pushed by `peer`, after verifying its signature.

    Rejects anything the peer's registered certificate did not sign. Returns
    `{"ok", "stored", "detail"}`.
    """
    from .models_esign_witness import WitnessRecord

    out = {"ok": False, "stored": False, "detail": ""}

    required = ("origin_code", "envelope_uid", "document_sha256", "statement")
    missing = [f for f in required if not str(payload.get(f) or "").strip()]
    if missing:
        out["detail"] = f"Missing field(s): {', '.join(missing)}"
        return out

    origin = str(payload["origin_code"])[:32]
    uid = str(payload["envelope_uid"])[:64]
    doc_sha = str(payload["document_sha256"])[:64]
    kind = str(payload.get("kind") or "seal")[:12]
    statement = str(payload["statement"])

    if len(doc_sha) != 64 or not all(c in "0123456789abcdef" for c in doc_sha.lower()):
        out["detail"] = "document_sha256 is not a SHA-256 hex digest."
        return out

    # An office may only speak for itself. Accepting a statement that claims
    # another office's origin would let one peer forge for all of them.
    if origin != peer.code:
        out["detail"] = (
            f"{peer.code} presented a statement claiming origin {origin!r}. "
            "A peer may only witness its own documents."
        )
        raise_alert(
            kind="bad_signature", severity="critical", peer=peer,
            envelope_uid=uid, origin_code=origin, detail=out["detail"],
        )
        return out

    verified, detail = verify_statement_signature(
        statement.encode("utf-8"),
        str(payload.get("signature_b64") or ""),
        peer.certificate_pem,
    )
    if not verified:
        out["detail"] = detail
        raise_alert(
            kind="bad_signature", severity="critical", peer=peer,
            envelope_uid=uid, origin_code=origin,
            detail=f"Rejected a statement from {peer.code}: {detail}",
        )
        return out

    # The signed text is authoritative; the JSON fields are a convenience and
    # must agree with it, or someone is trying to have it both ways.
    for field, value in (
        ("origin", origin), ("envelope", uid),
        ("document_sha256", doc_sha), ("kind", kind),
    ):
        if f"{field}={value}\n" not in statement:
            out["detail"] = (
                f"The JSON field {field}={value!r} does not appear in the "
                "signed statement."
            )
            raise_alert(
                kind="bad_signature", severity="critical", peer=peer,
                envelope_uid=uid, origin_code=origin, detail=out["detail"],
            )
            return out

    def _dt(key):
        from django.utils.dateparse import parse_datetime
        try:
            return parse_datetime(str(payload.get(key) or "")) or None
        except Exception:
            return None

    now = timezone.now()
    _record, created = WitnessRecord.objects.get_or_create(
        origin_code=origin,
        envelope_uid=uid,
        document_sha256=doc_sha,
        kind=kind,
        defaults={
            "reference": str(payload.get("reference") or "")[:120],
            "certificate_sha256": str(payload.get("certificate_sha256") or "")[:64],
            "chain_head": str(payload.get("chain_head") or "")[:64],
            "chain_length": int(payload.get("chain_length") or 0),
            "seal_level": str(payload.get("seal_level") or "")[:8],
            "sealed_at": _dt("sealed_at"),
            "issued_at": _dt("issued_at") or now,
            "statement": statement,
            "signature_b64": str(payload.get("signature_b64") or ""),
            "received_from": peer,
            "received_at": now,
            "signature_verified": True,
        },
    )

    peer.last_contact_at = now
    peer.save(update_fields=["last_contact_at"])

    out.update(ok=True, stored=created)
    out["detail"] = "Stored." if created else "Already held."
    return out


def local_attestations(envelope_uid: str, origin_code: str = "") -> list[dict]:
    """What this office holds for an envelope — the answer to a lookup."""
    from .models_esign_witness import WitnessRecord

    query = WitnessRecord.objects.filter(envelope_uid=envelope_uid)
    if origin_code:
        query = query.filter(origin_code=origin_code)
    return [record_payload(r) for r in query.order_by("issued_at", "id")]


# ─────────────────────────────────────────────────────────────────────────────
# Auditing — the part that actually catches tampering
# ─────────────────────────────────────────────────────────────────────────────

def _read_pdf(envelope) -> bytes:
    field = getattr(envelope, "completed_pdf", None)
    if not field:
        return b""
    try:
        field.open("rb")
        try:
            return field.read()
        finally:
            field.close()
    except Exception:
        return b""


def _ask_peer(peer, envelope_uid: str, origin: str, timeout: int) -> tuple[list, str]:
    """Ask one peer what it holds. Returns `(attestations, error)`."""
    try:
        url = (
            f"{peer.lookup_url}?envelope_uid="
            f"{urllib.parse.quote(envelope_uid)}&origin_code="
            f"{urllib.parse.quote(origin)}"
        )
        request = urllib.request.Request(
            url, method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "UNPASS-NES-Witness/1",
                **({"Authorization": f"Bearer {peer.outbound_token}"}
                   if peer.outbound_token else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(MAX_PAYLOAD_BYTES) or b"{}")
        return list(data.get("attestations") or []), ""
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def check_envelope(envelope, *, ask_peers: bool = True) -> dict:
    """
    Compare one envelope's stored file against every attestation available.

    Returns a findings dict. Never raises. This is the function that answers
    "has this been tampered with, and does anybody else agree?"
    """
    from .models_esign_witness import WitnessPeer, WitnessRecord

    out = {
        "envelope_uid": getattr(envelope, "envelope_id", ""),
        "ok": False,
        "live_sha256": "",
        "local_attestations": 0,
        "matched_local": False,
        "peers_asked": 0,
        "peers_agreeing": 0,
        "peers_disagreeing": 0,
        "peers_unreachable": 0,
        "findings": [],
        "peer_detail": [],
    }

    raw = _read_pdf(envelope)
    if not raw:
        out["findings"].append({
            "kind": "no_attestation",
            "severity": "warning",
            "detail": "The completed PDF is missing from storage.",
        })
        return out

    live = hashlib.sha256(raw).hexdigest()
    out["live_sha256"] = live

    code = office_code()
    records = list(
        WitnessRecord.objects
        .filter(envelope_uid=envelope.envelope_id)
        .order_by("issued_at", "id")
    )
    out["local_attestations"] = len(records)

    if not records:
        out["findings"].append({
            "kind": "not_witnessed",
            "severity": "warning",
            "detail": (
                "This document was never witnessed, so there is nothing to "
                "compare it against. Run --reissue to distribute it now."
            ),
        })
    else:
        known = {r.document_sha256 for r in records}
        out["matched_local"] = live in known
        if not out["matched_local"]:
            latest = records[-1]
            out["findings"].append({
                "kind": "hash_mismatch",
                "severity": "critical",
                "detail": (
                    "The stored file matches no attestation. The most recent "
                    f"attestation ({latest.issued_at:%Y-%m-%d %H:%M}) records "
                    f"{latest.document_sha256[:16]}…, the file on disk is "
                    f"{live[:16]}…. Either the file was altered, or it was "
                    "refreshed without re-witnessing."
                ),
            })

        # The audit chain head should match the newest attestation too.
        try:
            seal = envelope.seal
            newest = records[-1]
            if (newest.chain_head and seal.audit_chain_head
                    and newest.chain_head != seal.audit_chain_head):
                out["findings"].append({
                    "kind": "chain_mismatch",
                    "severity": "critical",
                    "detail": (
                        "The audit-chain head differs from the witnessed "
                        "value — the trail changed after it was witnessed."
                    ),
                })
        except Exception:
            pass

    if ask_peers:
        timeout = int(getattr(settings, "ESIGN_WITNESS_TIMEOUT", DEFAULT_TIMEOUT))
        peers = WitnessPeer.objects.filter(active=True, answers_lookup=True)
        for peer in peers:
            out["peers_asked"] += 1
            attestations, error = _ask_peer(
                peer, envelope.envelope_id, code or "", timeout
            )
            if error:
                out["peers_unreachable"] += 1
                out["peer_detail"].append(
                    {"peer": peer.code, "status": "unreachable", "detail": error}
                )
                continue

            hashes = {
                str(a.get("document_sha256") or "") for a in attestations
            }
            if not hashes:
                out["peer_detail"].append(
                    {"peer": peer.code, "status": "holds_nothing",
                     "detail": "This peer holds no attestation for it."}
                )
                continue

            if live in hashes:
                out["peers_agreeing"] += 1
                out["peer_detail"].append(
                    {"peer": peer.code, "status": "agrees",
                     "detail": "Holds a fingerprint matching the stored file."}
                )
            else:
                out["peers_disagreeing"] += 1
                sample = sorted(hashes)[0]
                out["peer_detail"].append(
                    {"peer": peer.code, "status": "disagrees",
                     "detail": (
                         f"Holds {sample[:16]}… but the file here hashes to "
                         f"{live[:16]}…."
                     )}
                )
                out["findings"].append({
                    "kind": "peer_disagrees",
                    "severity": "critical",
                    "peer": peer.code,
                    "detail": (
                        f"{peer.code} witnessed a different fingerprint for "
                        "this document."
                    ),
                })

    out["ok"] = not out["findings"]
    return out


def audit(limit: int = 200, *, ask_peers: bool = True, envelope=None) -> dict:
    """
    Run `check_envelope` across sealed envelopes and raise alerts.

    With `envelope`, checks just that one. Otherwise walks the most recently
    sealed envelopes, newest first, up to `limit`.
    """
    from .models_esign_pades import EnvelopeSeal

    report = {"checked": 0, "clean": 0, "flagged": 0, "alerts": 0, "results": []}

    if envelope is not None:
        targets = [envelope]
    else:
        targets = [
            seal.envelope
            for seal in EnvelopeSeal.objects
            .filter(status=EnvelopeSeal.STATUS_SEALED)
            .select_related("envelope")
            .order_by("-applied_at")[:limit]
        ]

    for env in targets:
        result = check_envelope(env, ask_peers=ask_peers)
        report["checked"] += 1
        report["results"].append(result)

        if result["ok"]:
            report["clean"] += 1
            continue

        report["flagged"] += 1
        for finding in result["findings"]:
            created = raise_alert(
                kind=finding["kind"],
                severity=finding.get("severity", "warning"),
                envelope=env,
                envelope_uid=env.envelope_id,
                origin_code=office_code(),
                detail=finding["detail"],
                evidence={
                    "live_sha256": result["live_sha256"],
                    "peers_agreeing": result["peers_agreeing"],
                    "peers_disagreeing": result["peers_disagreeing"],
                },
            )
            if created:
                report["alerts"] += 1

    if report["alerts"]:
        notify_alerts()
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Alerting
# ─────────────────────────────────────────────────────────────────────────────

def raise_alert(
    *, kind: str, severity: str = "warning", detail: str = "",
    envelope=None, envelope_uid: str = "", origin_code: str = "",
    peer=None, evidence: dict | None = None,
):
    """
    Record an alert, unless an identical unresolved one already exists.

    Deduplicated so a nightly audit against a genuinely altered file raises
    one alarm, not one per night until somebody looks. Never raises.
    """
    try:
        from .models_esign_witness import WitnessAlert

        existing = WitnessAlert.objects.filter(
            kind=kind, envelope_uid=envelope_uid or "",
            peer=peer, resolved=False,
        ).first()
        if existing is not None:
            # Keep the newest detail; the condition is still live.
            if detail and existing.detail != detail:
                existing.detail = detail
                existing.save(update_fields=["detail"])
            return None

        alert = WitnessAlert.objects.create(
            kind=kind, severity=severity, detail=detail or "",
            local_envelope=envelope, envelope_uid=envelope_uid or "",
            origin_code=origin_code or "", peer=peer,
            evidence=evidence or {},
        )
        logger.error(
            "NES witness alert [%s/%s]: %s", severity, kind, detail
        )
        return alert
    except Exception:
        logger.exception("NES witness: could not record an alert")
        return None


def notify_alerts(limit: int = 50) -> dict:
    """
    Email unresolved, un-notified alerts.

    One message covering everything outstanding, because five separate emails
    about the same incident get filtered and ignored.
    """
    out = {"alerts": 0, "sent": False, "detail": ""}

    try:
        from django.core.mail import send_mail

        from .models_esign_witness import WitnessAlert

        pending = list(
            WitnessAlert.objects
            .filter(resolved=False, notified_at__isnull=True)
            .order_by("-severity", "-raised_at")[:limit]
        )
        if not pending:
            out["detail"] = "Nothing to notify."
            return out

        out["alerts"] = len(pending)
        recipients = alert_recipients()
        if not recipients:
            out["detail"] = (
                "No ESIGN_WITNESS_ALERT_EMAILS and no ADMINS configured — "
                "alerts were recorded but nobody was emailed."
            )
            logger.error("NES witness: %s", out["detail"])
            return out

        brand = getattr(settings, "ESIGN_BRAND", "NES")
        critical = sum(1 for a in pending if a.severity == "critical")
        subject = (
            f"[{brand}] {len(pending)} document integrity alert"
            f"{'s' if len(pending) != 1 else ''}"
            + (f" — {critical} critical" if critical else "")
        )

        lines = [
            f"{brand} integrity audit raised the following.",
            "",
            "A critical alert means a sealed document or its audit trail no "
            "longer matches what was witnessed. Do not delete anything; the "
            "divergence is itself evidence.",
            "",
        ]
        for a in pending:
            lines.append(f"[{a.severity.upper()}] {a.get_kind_display()}")
            lines.append(f"  envelope : {a.envelope_uid or '—'}")
            if a.peer_id:
                lines.append(f"  peer     : {a.peer.code}")
            lines.append(f"  raised   : {a.raised_at:%Y-%m-%d %H:%M} UTC")
            lines.append(f"  detail   : {a.detail}")
            lines.append("")
        lines.append(
            "Investigate with:  manage.py esign_witness --audit <ENVELOPE_ID>"
        )

        send_mail(
            subject=subject,
            message="\n".join(lines),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=recipients,
            fail_silently=False,
        )

        now = timezone.now()
        WitnessAlert.objects.filter(
            pk__in=[a.pk for a in pending]
        ).update(notified_at=now)

        out.update(sent=True, detail=f"Emailed {len(recipients)} recipient(s).")
        return out

    except Exception as exc:
        logger.exception("NES witness: alert notification failed")
        out["detail"] = str(exc)
        return out
