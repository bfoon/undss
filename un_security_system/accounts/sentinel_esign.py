# accounts/sentinel_esign.py
"""
UN PASS — NES Sentinel, server side.

See models_esign_sentinel.py for the idea. This module does four things:

    publish()         cron   — turns new audit entries and seal fingerprints
                               into signed, hash-linked ledger blocks
    authenticate()    API    — checks a Sentinel's signed request
    envelope_state()  API    — recomputes, from the real file and the real
                               trail, what a Sentinel needs to compare
    notify()          cron   — e-mails owners, watchers and ICT about alerts,
                               and notices Sentinels that have gone quiet

Security model, briefly
-----------------------
* **No passwords, no bearer tokens.** A Sentinel signs every request with an
  Ed25519 key it generated itself. The server stores only the public half, so
  a database dump gives an attacker nothing to impersonate a Sentinel with.
* **Every request is fresh.** It carries a time (±5 minutes) and a random
  nonce the server refuses to see twice.
* **Every response is signed** by the server's publication key over the
  request's nonce and the body, so a response cannot be forged or replayed —
  even by something sitting between the two with a valid TLS certificate.
* **Sentinels only read and report.** Nothing a Sentinel sends can change a
  document, a trail, a user or a setting. The worst a stolen Sentinel key can
  do is read hashes and raise false alarms; revoking the node stops both.
* **Sentinels never accept connections.** They call out over HTTPS. There is
  no port to scan on the machine an office installs them on.
* **Data minimisation.** Blocks carry hashes, envelope ids, event names and
  times. No subjects, no names, no file content. The owner's and watchers'
  e-mail addresses go to Sentinels only so they can raise the alarm without
  this server — switch that off with ESIGN_SENTINEL_SHARE_CONTACTS = False.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

PROTOCOL = "nes-sentinel-v1"
BLOCK_TAG = "nes-sentinel-block-v1"
REQUEST_TAG = "nes-sentinel-req-v1"
RESPONSE_TAG = "nes-sentinel-resp-v1"

CLOCK_SKEW = 300                 # seconds a request's time may be off
MAX_BODY = 64 * 1024
FEED_MAX = 1000
STATE_MAX = 25

HEX64 = re.compile(r"^[0-9a-f]{64}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")
UID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
KIND = re.compile(r"^[a-z_]{3,40}$")

#: Findings a Sentinel may report. Anything else is refused.
ALERT_KINDS = {
    "document_altered", "document_missing", "document_unwitnessed",
    "seal_altered", "chain_broken", "chain_entry_missing", "chain_truncated",
    "envelope_deleted", "ledger_rewritten", "ledger_rolled_back",
    "ledger_bad_signature", "server_identity_changed", "server_unreachable",
    "local_store_altered", "test",
}


class SentinelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ─────────────────────────────────────────────────────────────────────────────
# Configuration and the publication key
# ─────────────────────────────────────────────────────────────────────────────

def enabled() -> bool:
    return bool(getattr(settings, "ESIGN_SENTINEL_ENABLED", True))


def key_path() -> Path:
    configured = getattr(settings, "ESIGN_SENTINEL_KEY", "") or ""
    if configured:
        return Path(configured)
    pades = getattr(settings, "ESIGN_PADES_KEY", "") or ""
    base = Path(pades).parent if pades else Path(settings.BASE_DIR) / "secure"
    return base / "nes_sentinel_ed25519.pem"


def _passphrase() -> bytes | None:
    value = getattr(settings, "ESIGN_SENTINEL_KEY_PASSPHRASE", "") or ""
    return value.encode("utf-8") if value else None


_KEY_CACHE: dict = {}


def generate_key(force: bool = False) -> Path:
    """Create the publication key (0600). Refuses to overwrite unless forced."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = key_path()
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pw = _passphrase()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(pw) if pw else serialization.NoEncryption(),
    )
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    _KEY_CACHE.clear()
    return path


def private_key():
    from cryptography.hazmat.primitives import serialization

    path = key_path()
    stamp = path.stat().st_mtime if path.exists() else None
    cached = _KEY_CACHE.get("key")
    if cached and _KEY_CACHE.get("stamp") == stamp:
        return cached
    if stamp is None:
        raise SentinelError(503, "The Sentinel publication key has not been created "
                                 "(manage.py nes_sentinel keygen).")
    key = serialization.load_pem_private_key(path.read_bytes(), password=_passphrase())
    _KEY_CACHE.update(key=key, stamp=stamp)
    return key


def public_key_b64() -> str:
    from cryptography.hazmat.primitives import serialization

    raw = private_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def fingerprint(public_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(public_b64)).hexdigest()


def pretty_fingerprint(fp: str) -> str:
    """Grouped so two people can read it to each other over the phone."""
    return " ".join(fp[i:i + 4] for i in range(0, 32, 4)).upper()


def key_ready() -> bool:
    try:
        private_key()
        return True
    except Exception:
        return False


def _sign(message: bytes) -> str:
    return base64.b64encode(private_key().sign(message)).decode("ascii")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def block_hash(seq: int, prev_hash: str, body: str) -> str:
    material = f"{BLOCK_TAG}\n{seq}\n{prev_hash}\n{body}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Publishing
# ─────────────────────────────────────────────────────────────────────────────

def _event_body(event, uid: str) -> dict:
    return {
        "v": 1, "kind": "event", "envelope": uid, "event_id": event.pk,
        "event": event.event, "at": event.at.isoformat(),
        "prev_hash": event.prev_hash or "", "entry_hash": event.entry_hash,
    }


def _seal_body(seal, uid: str) -> dict:
    return {
        "v": 1, "kind": "seal", "envelope": uid,
        "document_sha256": seal.document_sha256,
        "certificate_sha256": seal.certificate_sha256 or "",
        "chain_head": seal.audit_chain_head or "",
        "chain_length": seal.audit_chain_length or 0,
        "level": seal.level_achieved or "",
        "sealed_at": seal.applied_at.isoformat() if seal.applied_at else "",
    }


def _append(kind: str, uid: str, ref: str, body: dict, head):
    from .models_esign_sentinel import GENESIS, SentinelBlock

    seq = (head.seq + 1) if head else 1
    prev = head.block_hash if head else GENESIS
    text = canonical(body)
    digest = block_hash(seq, prev, text)
    return SentinelBlock.objects.create(
        seq=seq, kind=kind, envelope_uid=uid, ref=ref, body=text,
        prev_hash=prev, block_hash=digest, signature_b64=_sign(digest.encode("ascii")),
        key_fingerprint=fingerprint(public_key_b64()),
    )


def publish(limit: int = 2000) -> dict:
    """
    Append every chained audit entry and seal fingerprint not yet published.

    Runs under the scheduler's job lock; the UNIQUE prev_hash makes a second
    concurrent publisher fail rather than fork the ledger.
    """
    from django.db.models import Exists, OuterRef

    from .models_esign import EnvelopeEvent
    from .models_esign_pades import EnvelopeSeal
    from .models_esign_sentinel import SentinelBlock

    report = {"events": 0, "seals": 0, "head": 0}
    if not enabled():
        report["skipped"] = "ESIGN_SENTINEL_ENABLED is off"
        return report
    private_key()  # fail loudly before touching anything

    published_event = SentinelBlock.objects.filter(
        kind=SentinelBlock.KIND_EVENT, envelope_uid=OuterRef("envelope__envelope_id"),
        ref=OuterRef("pk_text"),
    )
    from django.db.models.functions import Cast
    from django.db.models import CharField

    events = (
        EnvelopeEvent.objects.exclude(entry_hash="")
        .annotate(pk_text=Cast("pk", CharField()))
        .annotate(done=Exists(published_event)).filter(done=False)
        .select_related("envelope").order_by("id")[:limit]
    )
    published_seal = SentinelBlock.objects.filter(
        kind=SentinelBlock.KIND_SEAL, envelope_uid=OuterRef("envelope__envelope_id"),
        ref=OuterRef("document_sha256"),
    )
    seals = (
        EnvelopeSeal.objects.exclude(document_sha256="")
        .annotate(done=Exists(published_seal)).filter(done=False)
        .select_related("envelope").order_by("id")[:limit]
    )

    with transaction.atomic():
        head = SentinelBlock.objects.select_for_update().order_by("-seq").first()
        for event in events:
            uid = event.envelope.envelope_id
            head = _append("event", uid, str(event.pk), _event_body(event, uid), head)
            report["events"] += 1
        for seal in seals:
            uid = seal.envelope.envelope_id
            head = _append("seal", uid, seal.document_sha256, _seal_body(seal, uid), head)
            report["seals"] += 1
    report["head"] = head.seq if head else 0
    return report


def ledger_head() -> dict:
    from .models_esign_sentinel import GENESIS, SentinelBlock

    head = SentinelBlock.objects.order_by("-seq").first()
    return {"seq": head.seq if head else 0, "hash": head.block_hash if head else GENESIS}


def verify_ledger(limit: int | None = None) -> dict:
    """Walk the published ledger and check every link and signature."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from .models_esign_sentinel import GENESIS, SentinelBlock

    out = {"ok": True, "blocks": 0, "problems": []}
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64()))
    prev, expected = GENESIS, 1
    rows = SentinelBlock.objects.order_by("seq")
    if limit:
        rows = rows[:limit]
    for block in rows.iterator():
        out["blocks"] += 1
        problem = ""
        if block.seq != expected or block.prev_hash != prev:
            problem = "broken_link"
        elif block_hash(block.seq, block.prev_hash, block.body) != block.block_hash:
            problem = "content_altered"
        else:
            try:
                public.verify(base64.b64decode(block.signature_b64),
                              block.block_hash.encode("ascii"))
            except Exception:
                problem = "bad_signature"
        if problem:
            out["ok"] = False
            out["problems"].append({"seq": block.seq, "kind": problem})
        prev, expected = block.block_hash, block.seq + 1
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Enrolment
# ─────────────────────────────────────────────────────────────────────────────

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


def _hash_code(code: str) -> str:
    normal = re.sub(r"[^A-Z0-9]", "", code.upper())
    return hashlib.sha256(f"nes-sentinel-enrol\n{normal}".encode()).hexdigest()


def create_enrolment(name: str, office_code: str, user, hours: int = 24) -> str:
    """Return a new one-time code. Only its hash is stored."""
    from .models_esign_sentinel import SentinelEnrolment

    raw = "".join(secrets.choice(_ALPHABET) for _ in range(20))   # 100 bits
    code = "NES-" + "-".join(raw[i:i + 5] for i in range(0, 20, 5))
    SentinelEnrolment.objects.create(
        code_hash=_hash_code(code), name=name[:120], office_code=office_code[:40],
        created_by=user if getattr(user, "pk", None) else None,
        expires_at=timezone.now() + timedelta(hours=max(1, min(hours, 72))),
    )
    return code


def consume_enrolment(code: str, public_b64: str, platform: str, version: str, ip):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from .models_esign_sentinel import SentinelEnrolment, SentinelNode

    try:
        raw = base64.b64decode(public_b64, validate=True)
        Ed25519PublicKey.from_public_bytes(raw)
    except Exception:
        raise SentinelError(400, "public_key is not an Ed25519 key")

    with transaction.atomic():
        enrolment = (
            SentinelEnrolment.objects.select_for_update()
            .filter(code_hash=_hash_code(code)).first()
        )
        if enrolment is None or not enrolment.is_usable:
            raise SentinelError(403, "This enrolment code is not valid. Ask ICT for a new one.")
        if SentinelNode.objects.filter(public_key=public_b64).exists():
            raise SentinelError(409, "This key is already enrolled.")
        node = SentinelNode.objects.create(
            name=enrolment.name, office_code=enrolment.office_code,
            public_key=public_b64, key_fingerprint=hashlib.sha256(raw).hexdigest(),
            enrolled_by=enrolment.created_by, platform=platform[:80],
            version=version[:20], last_ip=ip, last_seen_at=timezone.now(),
        )
        enrolment.used_at = timezone.now()
        enrolment.node = node
        enrolment.save(update_fields=["used_at", "node"])
    return node


# ─────────────────────────────────────────────────────────────────────────────
# Request authentication and response signing
# ─────────────────────────────────────────────────────────────────────────────

def request_message(method: str, path: str, query: str, stamp: str, nonce: str,
                    body: bytes) -> bytes:
    return (
        f"{REQUEST_TAG}\n{method.upper()}\n{path}\n{query}\n{stamp}\n{nonce}\n"
        f"{hashlib.sha256(body).hexdigest()}"
    ).encode("utf-8")


def response_message(nonce: str, body: bytes) -> bytes:
    return f"{RESPONSE_TAG}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode("utf-8")


def read_body(request) -> bytes:
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > MAX_BODY:
        raise SentinelError(413, "Request too large.")
    body = request.body
    if len(body) > MAX_BODY:
        raise SentinelError(413, "Request too large.")
    return body


def request_nonce(request) -> str:
    nonce = (request.META.get("HTTP_X_NES_NONCE") or "").strip()
    if not NONCE.match(nonce):
        raise SentinelError(400, "Missing or malformed X-NES-Nonce.")
    return nonce


def authenticate(request, body: bytes):
    """Return the calling SentinelNode, or raise SentinelError."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from .models_esign_sentinel import SentinelNode, SentinelNonce

    node_id = (request.META.get("HTTP_X_NES_NODE") or "").strip()
    stamp = (request.META.get("HTTP_X_NES_TIME") or "").strip()
    signature = (request.META.get("HTTP_X_NES_SIGNATURE") or "").strip()
    nonce = request_nonce(request)

    if not node_id.isdigit() or not stamp.isdigit() or not signature:
        raise SentinelError(401, "Unsigned request.")
    if abs(int(stamp) - int(time.time())) > CLOCK_SKEW:
        raise SentinelError(401, "Request time is outside the allowed window — "
                                 "check this machine's clock.")

    node = SentinelNode.objects.filter(pk=int(node_id)).first()
    # Same answer for unknown and revoked, and verify anyway, so the endpoint
    # cannot be used to learn which node ids exist.
    public = node.public_key if node else base64.b64encode(b"\x00" * 32).decode()
    message = request_message(request.method, request.path,
                              request.META.get("QUERY_STRING", ""), stamp, nonce, body)
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public)).verify(
            base64.b64decode(signature), message)
        good = True
    except Exception:
        good = False
    if not good or node is None or not node.is_active:
        raise SentinelError(401, "Signature not accepted.")

    try:
        with transaction.atomic():
            SentinelNonce.objects.create(node=node, nonce=nonce)
    except IntegrityError:
        raise SentinelError(401, "Replayed request.")

    SentinelNode.objects.filter(pk=node.pk).update(
        last_seen_at=timezone.now(), last_ip=_client_ip(request),
    )
    return node


def _client_ip(request):
    value = (request.META.get("REMOTE_ADDR") or "").strip()
    return value or None


def signed_json(data: dict, nonce: str, status: int = 200):
    from django.http import HttpResponse

    body = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    response = HttpResponse(body, status=status, content_type="application/json")
    try:
        response["X-NES-Response-Signature"] = _sign(response_message(nonce, body))
    except Exception:
        logger.exception("NES Sentinel: could not sign a response")
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ─────────────────────────────────────────────────────────────────────────────
# What a Sentinel compares
# ─────────────────────────────────────────────────────────────────────────────

def contacts_for(envelope) -> list[str]:
    from .models_esign_sentinel import EnvelopeWatcher

    emails = set()
    owner = getattr(envelope, "created_by", None)
    if owner is not None and owner.email:
        emails.add(owner.email.strip().lower())
    for watcher in EnvelopeWatcher.objects.filter(envelope=envelope).select_related("user"):
        if watcher.user.email and watcher.user.is_active:
            emails.add(watcher.user.email.strip().lower())
    return sorted(emails)


def envelope_state(uids: list[str]) -> list[dict]:
    from .audit_chain_esign import verify_chain
    from .models_esign import Envelope
    from .models_esign_pades import EnvelopeSeal
    from .witness_esign import _read_pdf

    share = bool(getattr(settings, "ESIGN_SENTINEL_SHARE_CONTACTS", True))
    out = []
    for uid in uids:
        env = Envelope.objects.filter(envelope_id=uid).select_related("created_by").first()
        if env is None:
            out.append({"envelope": uid, "exists": False})
            continue
        raw = _read_pdf(env)
        seal = EnvelopeSeal.objects.filter(envelope=env).first()
        chain = verify_chain(env)
        out.append({
            "envelope": uid,
            "exists": True,
            "status": env.status,
            "file": {
                "present": bool(raw),
                "sha256": hashlib.sha256(raw).hexdigest() if raw else "",
            },
            "seal": None if seal is None or not seal.document_sha256 else {
                "document_sha256": seal.document_sha256,
                "certificate_sha256": seal.certificate_sha256 or "",
                "chain_head": seal.audit_chain_head or "",
                "chain_length": seal.audit_chain_length or 0,
            },
            "chain": {
                "ok": bool(chain.get("ok")),
                "problems": sorted({p.get("kind", "") for p in chain.get("problems", [])}),
                "entries": list(chain.get("chain", [])),
            },
            "contacts": contacts_for(env) if share else [],
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────────────────────

def _clean(text, limit: int) -> str:
    text = str(text or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text[:limit]


def record_alert(node, payload: dict):
    """Store one finding from a Sentinel. Returns (alert, created)."""
    from .models_esign_sentinel import SentinelAlert

    kind = str(payload.get("kind") or "")
    if not KIND.match(kind) or kind not in ALERT_KINDS:
        raise SentinelError(400, "Unknown alert kind.")
    uid = str(payload.get("envelope") or "")
    if uid and not UID.match(uid):
        raise SentinelError(400, "Malformed envelope id.")
    severity = payload.get("severity")
    if severity not in (SentinelAlert.SEV_CRITICAL, SentinelAlert.SEV_WARNING):
        severity = SentinelAlert.SEV_CRITICAL
    fp = str(payload.get("fingerprint") or "")
    if not HEX64.match(fp):
        raise SentinelError(400, "Malformed fingerprint.")
    evidence = payload.get("evidence") or {}
    if not isinstance(evidence, dict) or len(canonical(evidence)) > 16 * 1024:
        raise SentinelError(400, "Evidence must be a small object.")

    recent = SentinelAlert.objects.filter(
        node=node, reported_at__gte=timezone.now() - timedelta(hours=1)
    ).count()
    if recent >= int(getattr(settings, "ESIGN_SENTINEL_ALERTS_PER_HOUR", 200)):
        raise SentinelError(429, "Too many alerts from this Sentinel in the last hour.")

    # A Sentinel may say who should hear about this — useful when the
    # envelope was deleted — but it can only name people who are already
    # active UN PASS users, so a stolen Sentinel key is not a spam relay.
    claimed = payload.get("contacts") or []
    contacts = []
    if isinstance(claimed, list):
        wanted = {str(e).strip().lower() for e in claimed[:50] if isinstance(e, str) and len(e) <= 254}
        if wanted:
            from django.contrib.auth import get_user_model
            contacts = sorted({
                e.lower() for e in get_user_model().objects
                .filter(is_active=True, email__in=wanted).values_list("email", flat=True)
            } & wanted)

    existing = SentinelAlert.objects.filter(node=node, fingerprint=fp).first()
    if existing:
        return existing, False
    try:
        with transaction.atomic():
            alert = SentinelAlert.objects.create(
                node=node, envelope_uid=uid, kind=kind, severity=severity,
                detail=_clean(payload.get("detail"), 2000), evidence=evidence,
                fingerprint=fp, contacts=contacts,
            )
    except IntegrityError:
        return SentinelAlert.objects.get(node=node, fingerprint=fp), False
    logger.warning("NES Sentinel %s reported %s on %s", node, kind, uid or "-")
    return alert, True


def server_alert(kind: str, detail: str, *, node=None, envelope_uid: str = "",
                 evidence: dict | None = None, severity: str = "critical"):
    """An alert the server raises about the Sentinel network itself."""
    from .models_esign_sentinel import SentinelAlert

    fp = hashlib.sha256(f"server\n{kind}\n{getattr(node, 'pk', '')}\n{envelope_uid}\n"
                        f"{timezone.now():%Y-%m-%d}".encode()).hexdigest()
    alert, _ = SentinelAlert.objects.get_or_create(
        node=node, fingerprint=fp,
        defaults={"kind": kind, "detail": detail, "envelope_uid": envelope_uid,
                  "evidence": evidence or {}, "severity": severity},
    )
    return alert


_KIND_TEXT = {
    "document_altered": "The signed document no longer matches its witnessed fingerprint.",
    "document_missing": "The signed document has disappeared from storage.",
    "document_unwitnessed": "The signed document changed and the change was never published.",
    "seal_altered": "The seal record no longer matches what was witnessed.",
    "chain_broken": "The audit trail fails verification.",
    "chain_entry_missing": "Audit-trail entries that were witnessed are gone or changed.",
    "chain_truncated": "The audit trail is shorter than when it was witnessed.",
    "envelope_deleted": "An envelope that was witnessed no longer exists.",
    "ledger_rewritten": "Published ledger history was rewritten on the server.",
    "ledger_rolled_back": "The server's ledger went backwards.",
    "ledger_bad_signature": "The server served a block with an invalid signature.",
    "server_identity_changed": "The server's publication key changed.",
    "server_unreachable": "The Sentinel could not reach UN PASS for a long time.",
    "local_store_altered": "The Sentinel's own copy was altered on its machine.",
    "node_silent": "A Sentinel has stopped checking in.",
    "test": "Test alert — no action needed.",
}


def alert_title(kind: str) -> str:
    return _KIND_TEXT.get(kind, kind.replace("_", " ").capitalize())


def recipients_for(alert) -> list[str]:
    from .models_esign import Envelope
    from .witness_esign import alert_recipients

    emails = set(e.lower() for e in alert_recipients())
    emails.update(e.lower() for e in (alert.contacts or []) if isinstance(e, str))
    if alert.envelope_uid:
        env = Envelope.objects.filter(envelope_id=alert.envelope_uid).first()
        if env is not None:
            emails.update(contacts_for(env))
    return sorted(e for e in emails if e)


def _send_alert(alert) -> bool:
    from .asset_email import send_email_async
    from .models_esign import Envelope

    to = recipients_for(alert)
    if not to:
        return True
    env = (Envelope.objects.filter(envelope_id=alert.envelope_uid).first()
           if alert.envelope_uid else None)
    brand = getattr(settings, "ESIGN_BRAND", "NES")
    tag = "TEST" if alert.kind == "test" else alert.severity.upper()
    subject = f"[{brand} Sentinel · {tag}] {alert_title(alert.kind)}"
    if env is not None:
        subject += f" — {env.subject[:80]}"
    send_email_async(
        subject=subject.replace("\r", " ").replace("\n", " "),
        to_emails=to,
        html_template="accounts/esign/email/sentinel_alert.html",
        context={
            "brand": brand, "subject": subject, "alert": alert,
            "title": alert_title(alert.kind), "envelope": env,
            "node": alert.node, "evidence": sorted(alert.evidence.items())[:20],
        },
    )
    return True


def notify(limit: int = 100) -> dict:
    """Cron: e-mail unsent alerts; flag silent Sentinels; prune nonces."""
    from .models_esign_sentinel import SentinelAlert, SentinelNode, SentinelNonce

    report = {"sent": 0, "failed": 0, "silent": 0, "nonces_pruned": 0}
    now = timezone.now()

    hours = float(getattr(settings, "ESIGN_SENTINEL_SILENT_HOURS", 6))
    for node in SentinelNode.objects.filter(status=SentinelNode.STATUS_ACTIVE,
                                            last_seen_at__lt=now - timedelta(hours=hours)):
        if node.silent_alerted_at and node.silent_alerted_at > node.last_seen_at:
            continue
        server_alert(
            "node_silent",
            f"Sentinel '{node.name}' ({node.office_code or 'no office'}) last checked in "
            f"{timezone.localtime(node.last_seen_at):%Y-%m-%d %H:%M}. A Sentinel that goes "
            "quiet may have been switched off or blocked — check it.",
            node=node, severity="warning",
        )
        SentinelNode.objects.filter(pk=node.pk).update(silent_alerted_at=now)
        report["silent"] += 1

    for alert in SentinelAlert.objects.filter(notified_at__isnull=True).order_by("id")[:limit]:
        try:
            _send_alert(alert)
            SentinelAlert.objects.filter(pk=alert.pk).update(notified_at=timezone.now())
            report["sent"] += 1
        except Exception:
            logger.exception("NES Sentinel: could not e-mail alert %s", alert.pk)
            report["failed"] += 1

    pruned, _ = SentinelNonce.objects.filter(seen_at__lt=now - timedelta(minutes=30)).delete()
    report["nonces_pruned"] = pruned
    return report
