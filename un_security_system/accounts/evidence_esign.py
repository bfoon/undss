# accounts/evidence_esign.py
"""
UN PASS — NES offline evidence packages and the seal register.

Why
---
Everything else in NES assumes something survives: the server, the CA, the
timestamp authority, pyHanko. For a UN record with a 7-year or permanent
retention schedule that is an unsafe assumption. Contracts outlive systems.
The office that has to answer a question about this document in 2040 may have
none of the above.

So this module produces artefacts that need **nothing**:

1. **Evidence package** (`envelope_evidence`) — one ZIP per envelope holding
   the sealed PDF, the sealed Certificate of Completion, the full certificate
   chain, the revocation material, the signed audit trail with its hash chain,
   the anchor signature, and a plain-text manifest of SHA-256 digests. An
   auditor with `sha256sum` and `openssl` can verify the whole thing with no
   network and no UN PASS. A `VERIFY.txt` inside spells out how, in commands.

2. **Seal register** (`build_register`) — a periodic, sealed, timestamped list
   of every document hash issued in a window. This is the backstop: if the
   signing certificate is lost, the CA is gone and the PDF's own chain cannot
   be assembled, a register entry still proves that *this exact document*
   existed, with that hash, by that date. One small file vouches for
   thousands of documents, and it is cheap to replicate — mail it to the
   registry, commit it, print it.

Container format
----------------
The evidence package uses the **ASiC-E** container conventions from
ETSI EN 319 162-1: `mimetype` as the first entry, stored uncompressed, and a
`META-INF/ASiCManifest.xml` describing the payload.

One honest caveat, stated here and in the package itself: in a strict ASiC-E
container the signature is a detached object under `META-INF/`. Here the
authoritative signature is **embedded in the PDF** per PAdES, which is the
right place for it — it travels with the document when someone pulls the PDF
out of the ZIP. So this is an ASiC-E *container carrying* PAdES-signed
payloads, not a container whose own signature is the authority. Readers that
expect the latter will not validate the ZIP itself; they will validate the
PDF, which is what matters.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import zipfile
from datetime import timedelta
from xml.sax.saxutils import escape as xml_escape

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

ASICE_MIMETYPE = b"application/vnd.etsi.asic-e+zip"

EVIDENCE_FORMAT_VERSION = "nes-evidence-v1"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _read_field(field) -> bytes:
    if not field:
        return b""
    try:
        field.open("rb")
        try:
            return field.read()
        finally:
            field.close()
    except Exception:
        logger.warning("NES evidence: a stored file could not be read",
                       exc_info=True)
        return b""


def _certificate_chain_pem() -> tuple[str, list[dict]]:
    """
    The signing certificate and its chain as PEM, plus a readable summary.

    Frozen into the package because a chain you cannot fetch in 2040 is a
    chain you cannot validate.
    """
    try:
        from asn1crypto import pem

        from . import pades_esign as P

        ready, _reason = P.pades_configured()
        if not ready:
            return "", []

        signer = P.load_seal_signer()
        certs = [signer.signing_cert] + list(
            getattr(signer, "cert_registry", None) or []
        )

        blocks, summary, seen = [], [], set()
        for cert in certs:
            try:
                der = cert.dump()
            except Exception:
                continue
            fingerprint = _sha256(der)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            blocks.append(
                pem.armor("CERTIFICATE", der).decode("ascii")
            )
            try:
                summary.append({
                    "subject": cert.subject.human_friendly,
                    "issuer": cert.issuer.human_friendly,
                    "serial": str(cert.serial_number),
                    "not_after": str(
                        cert["tbs_certificate"]["validity"]["not_after"].native
                    ),
                    "sha256": fingerprint,
                })
            except Exception:
                summary.append({"sha256": fingerprint})

        return "".join(blocks), summary
    except Exception:
        logger.warning("NES evidence: the certificate chain was unavailable",
                       exc_info=True)
        return "", []


def _revocation_material(pdf_bytes: bytes) -> dict:
    """
    The OCSP responses and CRLs the PDF already carries in its DSS.

    Extracted and written out separately so an auditor can inspect them
    without a PDF library.
    """
    out = {"ocsp": [], "crl": [], "detail": ""}
    try:
        from pyhanko.pdf_utils.reader import PdfFileReader

        reader = PdfFileReader(io.BytesIO(pdf_bytes))
        dss = None
        try:
            from pyhanko.sign.validation import DocumentSecurityStore

            dss = DocumentSecurityStore.read_dss(reader)
        except Exception:
            out["detail"] = "No Document Security Store in this PDF."
            return out

        # DSS.ocsps / DSS.crls hold *references* to PDF streams, not parsed
        # ASN.1 objects, so each one has to be resolved and decoded.
        # pyHanko has moved this enum between modules across releases, so
        # try the current home first and fall back rather than hard-failing
        # the whole evidence package over an import path.
        try:
            from pyhanko.pdf_utils.decode_limits import DecodePurpose
        except Exception:
            from pyhanko.pdf_utils.generic import DecodePurpose

        for kind, refs, purpose in (
            ("ocsp", getattr(dss, "ocsps", None) or [],
             DecodePurpose.OCSP_RESPONSE),
            ("crl", getattr(dss, "crls", None) or [],
             DecodePurpose.CRL),
        ):
            for ref in list(refs):
                try:
                    raw = ref.get_object().read_data(purpose)
                    out[kind].append(
                        base64.b64encode(raw).decode("ascii")
                    )
                except Exception:
                    logger.debug(
                        "NES evidence: a %s entry in the DSS could not be "
                        "read", kind, exc_info=True
                    )
                    continue

        if not out["ocsp"] and not out["crl"]:
            out["detail"] = (
                "The DSS holds no revocation material — this seal is below "
                "B-LT. Run `manage.py esign_pades --refresh-ltv` to add it."
            )
        return out
    except Exception as exc:
        out["detail"] = str(exc)
        return out


def _asic_manifest(entries: list[tuple[str, str]]) -> bytes:
    """
    META-INF/ASiCManifest.xml — the ASiC-E description of the payload.

    `entries` is a list of (path, sha256-hex).
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<asic:ASiCManifest '
        'xmlns:asic="http://uri.etsi.org/02918/v1.2.1#" '
        'xmlns:ds="http://www.w3.org/2000/09/xmldsig#">',
        '  <!-- The authoritative signature is embedded in the PDF payloads',
        '       themselves (PAdES, ETSI EN 319 142), not detached here. -->',
    ]
    for path, digest in entries:
        lines.append(
            f'  <asic:DataObjectReference URI="{xml_escape(path)}">'
        )
        lines.append(
            '    <ds:DigestMethod Algorithm='
            '"http://www.w3.org/2001/04/xmlenc#sha256"/>'
        )
        lines.append(
            f'    <ds:DigestValue>'
            f'{base64.b64encode(bytes.fromhex(digest)).decode("ascii")}'
            f'</ds:DigestValue>'
        )
        lines.append('  </asic:DataObjectReference>')
    lines.append('</asic:ASiCManifest>')
    return "\n".join(lines).encode("utf-8")


_VERIFY_TEXT = """\
HOW TO VERIFY THIS DOCUMENT WITHOUT UN PASS
===========================================

Everything needed is in this package. No network, no server, no account.
Four checks, in increasing depth. The first two need only standard tools.


1. ARE THE FILES THE ONES THAT WERE ISSUED?
-------------------------------------------
MANIFEST.sha256 lists a SHA-256 for every file here.

    sha256sum -c MANIFEST.sha256

Every line must say OK. A FAILED line means that file was altered after the
package was built.


2. IS THE DOCUMENT SIGNATURE INTACT?
------------------------------------
Open document/*-signed.pdf in Adobe Acrobat Reader (free). The signature
panel will report whether the document has been modified since signing, and
name the signing organisation.

Acrobat decides *trust* using its own trust store, so it may say "validity
unknown" while still confirming the document is unmodified. Unmodified is the
property that matters here; trust is a question about the certificate, and
evidence/certificate-chain.pem lets you answer it yourself:

    openssl x509 -in evidence/certificate-chain.pem -noout -text


3. IS THE AUDIT TRAIL INTACT?
-----------------------------
evidence/audit-trail.json lists every action on this envelope. Each entry
carries prev_hash, entry_hash and the exact payload that was hashed. Re-walk
the chain with nothing but SHA-256:

    entry_hash == sha256( prev_hash + 0x1e + payload )

where prev_hash is the previous entry's entry_hash (empty for the first), the
separator is a single 0x1E byte, and payload is the entry's "payload" string
in UTF-8. Any mismatch marks the entry where the trail was altered.

evidence/audit-anchor.txt holds the statement that was signed at completion,
naming the chain head and the number of entries. If the trail in this package
has fewer entries than the anchor says, entries were removed from the end.

    openssl cms -verify -binary -content evidence/audit-anchor.txt \\
        -inform DER -in evidence/audit-anchor.p7s -noverify


5. WHO SIGNED IT, PROVED BY THEIR OWN KEY?
------------------------------------------
If evidence/personal-signatures.json is present, each entry is a signature
made by the signatory themselves, with a key the issuing office does not hold
and cannot use. This is the strongest identity evidence in the package.

Each entry carries the public key, the exact statement that was signed, and
the signature. Verify one with openssl:

    python3 - <<'EOF'
    import base64, json
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    e = json.load(open('evidence/personal-signatures.json'))['signatures'][0]
    pub = serialization.load_der_public_key(
        base64.b64decode(e['public_key_spki_b64']))
    raw = base64.b64decode(e['signature_b64'])
    der = utils.encode_dss_signature(
        int.from_bytes(raw[:32],'big'), int.from_bytes(raw[32:],'big'))
    pub.verify(der, e['statement'].encode(), ec.ECDSA(hashes.SHA256()))
    print('OK —', e['signature_id'], 'signed this document')
    EOF

The statement names the signature ID, the envelope and the SHA-256 of the
documents as the signatory saw them, so the signature cannot be lifted and
replayed against anything else.

A signature ID (NES-XX-....) identifies one person for the life of their
account. It does not change when they redraw their signature or rotate their
key, so two documents years apart bearing the same ID were signed by the same
person.


6. DOES THE REGISTER INDEPENDENTLY CONFIRM IT?
----------------------------------------------
If the issuing office published a seal register for the period, it contains
this document's SHA-256. Finding the hash in a register that was sealed and
timestamped independently proves the document existed, in exactly this form,
by that date -- even if every certificate involved has since expired.

Compare evidence/seal.json -> document_sha256 against the register entry.


IF A CHECK FAILS
----------------
Do not discard the package. Report it to the issuing office with the file
that failed and the output you saw. A failure is evidence too.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Evidence package
# ─────────────────────────────────────────────────────────────────────────────

def envelope_evidence(envelope) -> tuple[bytes, dict]:
    """
    Build the offline evidence package for one envelope.

    Returns `(zip_bytes, report)`. Always returns a usable ZIP: if a part is
    missing, the package says so in `evidence/build-notes.txt` rather than
    failing, because a partial evidence package still has value and a missing
    one has none.
    """
    from .audit_chain_esign import chain_export, verify_anchor, verify_chain
    from . import pades_esign as P

    report = {"ok": False, "notes": [], "files": 0, "bytes": 0}
    notes: list[str] = []

    seal = None
    try:
        seal = envelope.seal
    except Exception:
        notes.append("No seal record exists for this envelope.")

    signed_pdf = _read_field(getattr(envelope, "completed_pdf", None))
    cert_pdf = _read_field(getattr(envelope, "certificate_pdf", None))

    if not signed_pdf:
        notes.append("The completed PDF was missing from storage.")
    if not cert_pdf:
        notes.append("The Certificate of Completion was missing from storage.")

    chain_state = verify_chain(envelope)
    anchor_state = verify_anchor(envelope, seal)
    validation = P.verify_pdf(signed_pdf) if signed_pdf else {
        "ok": False, "signatures": [], "error": "No PDF."
    }
    chain_pem, chain_summary = _certificate_chain_pem()
    if not chain_pem:
        notes.append(
            "The certificate chain could not be exported — sealing may not be "
            "configured on this server."
        )
    revocation = _revocation_material(signed_pdf) if signed_pdf else {}
    if revocation.get("detail"):
        notes.append(revocation["detail"])

    base = envelope.envelope_id
    payload: list[tuple[str, bytes]] = []

    if signed_pdf:
        payload.append((f"document/{base}-signed.pdf", signed_pdf))
    if cert_pdf:
        payload.append((f"document/{base}-certificate.pdf", cert_pdf))

    # -- the seal record -------------------------------------------------
    seal_json = {
        "format": EVIDENCE_FORMAT_VERSION,
        "envelope_id": base,
        "subject": envelope.subject,
        "reference": envelope.reference or "",
        "status": envelope.status,
        "completed_at": (
            envelope.completed_at.isoformat() if envelope.completed_at else ""
        ),
        "built_at": timezone.now().isoformat(),
        "brand": getattr(settings, "ESIGN_BRAND", "NES"),
        "document_sha256": _sha256(signed_pdf) if signed_pdf else "",
        "certificate_sha256": _sha256(cert_pdf) if cert_pdf else "",
        "seal": None if seal is None else {
            "status": seal.status,
            "level_requested": seal.level_requested,
            "level_achieved": seal.level_achieved,
            "standard": "ETSI EN 319 142-1 (PAdES baseline)",
            "applied_at": (
                seal.applied_at.isoformat() if seal.applied_at else ""
            ),
            "signer_common_name": seal.signer_common_name,
            "signer_organization": seal.signer_organization,
            "signer_serial": seal.signer_serial,
            "signer_issuer": seal.signer_issuer,
            "certificate_expires_at": (
                seal.certificate_expires_at.isoformat()
                if seal.certificate_expires_at else ""
            ),
            "timestamped": seal.timestamped,
            "timestamp_authority": seal.timestamp_authority,
            "policy_oid": seal.policy_oid,
            "policy_uri": seal.policy_uri,
            "commitment_type": seal.commitment_type,
            "certificate_sealed": seal.certificate_sealed,
            "document_sha256_recorded": seal.document_sha256,
            "refresh_count": seal.refresh_count,
            "last_refreshed_at": (
                seal.last_refreshed_at.isoformat()
                if seal.last_refreshed_at else ""
            ),
            "long_term_outlook": seal.long_term_outlook,
        },
        "validation_at_build": {
            "ok": validation.get("ok"),
            "signatures": validation.get("signatures", []),
            "error": validation.get("error", ""),
        },
        "certificate_chain": chain_summary,
    }
    payload.append((
        "evidence/seal.json",
        json.dumps(seal_json, indent=2, default=str).encode("utf-8"),
    ))

    # -- audit trail -----------------------------------------------------
    events = chain_export(envelope)
    audit = {
        "format": EVIDENCE_FORMAT_VERSION,
        "envelope_id": base,
        "hash_rule": "entry_hash = sha256(prev_hash || 0x1e || payload)",
        "chain": {
            "ok": chain_state["ok"],
            "total": chain_state["total"],
            "chained": chain_state["chained"],
            "unchained": chain_state["unchained"],
            "head": chain_state["head"],
            "problems": chain_state["problems"],
        },
        "anchor": {
            "present": anchor_state["present"],
            "signature_intact": anchor_state["signature_intact"],
            "head_matches": anchor_state["head_matches"],
            "length_matches": anchor_state["length_matches"],
            "truncated": anchor_state["truncated"],
            "detail": anchor_state["detail"],
        },
        "events": events,
    }
    payload.append((
        "evidence/audit-trail.json",
        json.dumps(audit, indent=2, default=str).encode("utf-8"),
    ))

    # -- personal signatures, with everything needed to re-verify offline --
    # The public key travels with each attestation so an auditor can check it
    # with openssl alone. The private key does not exist here to travel.
    try:
        from . import identity_esign as _identity

        rows = []
        for a in _identity.envelope_attestations(envelope):
            ok, detail = _identity.verify_stored_attestation(a)
            rows.append({
                "signature_id": a.signature_id,
                "signer_name": a.signer_name,
                "signer_email": a.signer_email,
                "algorithm": a.algorithm,
                "key_revision": a.key.revision if a.key_id else None,
                "key_fingerprint": a.key_fingerprint,
                "public_key_spki_b64": (
                    a.key.public_key_spki if a.key_id else ""
                ),
                "statement": a.statement,
                "signature_b64": a.signature_b64,
                "nonce": a.nonce,
                "document_sha256": a.document_sha256,
                "signed_at": a.created_at.isoformat() if a.created_at else "",
                "ip": str(a.ip or ""),
                "verified_at_build": ok,
                "verify_detail": detail,
            })

        if rows:
            payload.append((
                "evidence/personal-signatures.json",
                json.dumps({
                    "format": EVIDENCE_FORMAT_VERSION,
                    "envelope_id": base,
                    "standard": "ECDSA P-256 over SHA-256, IEEE P1363",
                    "note": (
                        "Each signature was made in the signatory's browser "
                        "with a private key held under their passphrase "
                        "alone. The issuing office stores only the public "
                        "key and cannot reproduce these signatures."
                    ),
                    "how_to_verify": (
                        "For each entry: base64-decode public_key_spki_b64 to "
                        "a DER SubjectPublicKeyInfo; base64-decode "
                        "signature_b64 to 64 bytes r||s and convert to DER; "
                        "verify over the exact UTF-8 bytes of `statement` "
                        "with ECDSA/SHA-256. See VERIFY.txt step 5."
                    ),
                    "signatures": rows,
                }, indent=2, default=str).encode("utf-8"),
            ))
    except Exception:
        notes.append("Personal signatures could not be exported.")

    # A flat CSV too: the trail should be readable by someone with a
    # spreadsheet and no appetite for JSON.
    csv_buf = io.StringIO()
    try:
        import csv as _csv

        writer = _csv.writer(csv_buf)
        writer.writerow([
            "id", "at", "event", "actor", "recipient", "note", "ip",
            "prev_hash", "entry_hash",
        ])
        for e in events:
            writer.writerow([
                e["id"], e["at"], e["event_display"], e["actor"],
                e["recipient"], e["note"], e["ip"],
                e["prev_hash"], e["entry_hash"],
            ])
        payload.append((
            "evidence/audit-trail.csv", csv_buf.getvalue().encode("utf-8")
        ))
    except Exception:
        notes.append("The CSV audit trail could not be written.")

    # -- anchor statement and its signature ------------------------------
    if seal is not None and seal.audit_chain_head:
        from .audit_chain_esign import anchor_statement

        at_text = (
            seal.audit_anchored_at.isoformat()
            if seal.audit_anchored_at else ""
        )
        payload.append((
            "evidence/audit-anchor.txt",
            anchor_statement(
                envelope, seal.audit_chain_head,
                seal.audit_chain_length or 0, at_text,
            ),
        ))
        if seal.audit_anchor:
            try:
                payload.append((
                    "evidence/audit-anchor.p7s",
                    base64.b64decode(seal.audit_anchor),
                ))
            except Exception:
                notes.append("The anchor signature could not be decoded.")
        else:
            notes.append(
                "The chain head was recorded but never signed, so the anchor "
                "cannot be verified cryptographically."
            )

    # -- certificates and revocation material ----------------------------
    if chain_pem:
        payload.append(
            ("evidence/certificate-chain.pem", chain_pem.encode("ascii"))
        )
    if revocation:
        payload.append((
            "evidence/revocation.json",
            json.dumps(revocation, indent=2).encode("utf-8"),
        ))

    payload.append(("VERIFY.txt", _VERIFY_TEXT.encode("utf-8")))

    if notes:
        payload.append((
            "evidence/build-notes.txt",
            ("Notes recorded while building this package:\n\n"
             + "\n".join(f"- {n}" for n in notes)
             + "\n").encode("utf-8"),
        ))

    # -- manifest of digests ---------------------------------------------
    digests = [(path, _sha256(data)) for path, data in payload]
    manifest_lines = [f"{digest}  {path}" for path, digest in digests]
    manifest = ("\n".join(manifest_lines) + "\n").encode("utf-8")
    asic = _asic_manifest(digests)

    # -- assemble ---------------------------------------------------------
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # ASiC requires `mimetype` first and uncompressed.
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, ASICE_MIMETYPE)

        zf.writestr("META-INF/ASiCManifest.xml", asic)
        for path, data in payload:
            zf.writestr(path, data)
        zf.writestr("MANIFEST.sha256", manifest)

    blob = buf.getvalue()
    report.update(
        ok=bool(signed_pdf),
        notes=notes,
        files=len(payload) + 3,
        bytes=len(blob),
        chain_ok=chain_state["ok"],
        anchor_ok=anchor_state["ok"],
        validation_ok=validation.get("ok", False),
    )
    return blob, report


# ─────────────────────────────────────────────────────────────────────────────
# Seal register — the backstop when the PKI itself is gone
# ─────────────────────────────────────────────────────────────────────────────

def build_register(since=None, until=None) -> tuple[bytes, dict]:
    """
    Build a register of every sealed document in a window, then seal and
    timestamp the register itself.

    The point is leverage. One register entry is a few hundred bytes, and a
    register that has been timestamped by an external authority proves the
    existence and exact content of every document it lists — at that date,
    independently of those documents' own certificate chains. Publish it
    somewhere durable (the records registry, a git repository, print it) and
    verification survives the loss of the signing key, the CA and this
    application.

    Returns `(pdf_or_json_bytes, report)`. The register is emitted as JSON and
    sealed as a PDF carrying the same JSON, so it is both machine-readable and
    openable by a person in twenty years.
    """
    from .models_esign import Envelope
    from .models_esign_pades import EnvelopeSeal
    from . import pades_esign as P

    until = until or timezone.now()
    since = since or (until - timedelta(days=30))

    rows = (
        EnvelopeSeal.objects
        .filter(status=EnvelopeSeal.STATUS_SEALED)
        .filter(applied_at__gte=since, applied_at__lte=until)
        .select_related("envelope")
        .order_by("applied_at", "id")
    )

    entries = []
    for seal in rows:
        env = seal.envelope
        entries.append({
            "envelope_id": env.envelope_id,
            "reference": env.reference or "",
            "subject": env.subject,
            "completed_at": (
                env.completed_at.isoformat() if env.completed_at else ""
            ),
            "sealed_at": seal.applied_at.isoformat() if seal.applied_at else "",
            "level": seal.level_achieved,
            "document_sha256": seal.document_sha256,
            "certificate_sha256": seal.certificate_sha256,
            "audit_chain_head": seal.audit_chain_head,
            "audit_chain_length": seal.audit_chain_length,
        })

    # A Merkle-style root over the entries, so a single value stands for the
    # whole register and one entry can later be shown to belong to it.
    leaves = [
        hashlib.sha256(
            json.dumps(e, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for e in entries
    ]
    root = _merkle_root(leaves)

    register = {
        "format": "nes-seal-register-v1",
        "brand": getattr(settings, "ESIGN_BRAND", "NES"),
        "period_start": since.isoformat(),
        "period_end": until.isoformat(),
        "generated_at": timezone.now().isoformat(),
        "entry_count": len(entries),
        "merkle_root": root,
        "leaf_rule": "sha256(canonical_json(entry)); pairwise sha256(a+b), "
                     "odd leaf promoted",
        "entries": entries,
    }
    register_json = json.dumps(register, indent=2, default=str).encode("utf-8")

    pdf = _register_pdf(register, register_json)
    sealed, info = P.seal_pdf(pdf, None)

    report = {
        "entry_count": len(entries),
        "merkle_root": root,
        "period_start": since,
        "period_end": until,
        "seal_status": info.get("status"),
        "seal_level": info.get("level_achieved"),
        "seal_detail": info.get("detail"),
        "json": register_json,
        "register_sha256": _sha256(sealed),
    }
    return sealed, report


def _merkle_root(leaves: list[str]) -> str:
    """Pairwise SHA-256 tree; an odd node is promoted unchanged."""
    if not leaves:
        return ""
    level = list(leaves)
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(
                    hashlib.sha256(
                        (level[i] + level[i + 1]).encode()
                    ).hexdigest()
                )
            else:
                nxt.append(level[i])
        level = nxt
    return level[0]


def _register_pdf(register: dict, register_json: bytes) -> bytes:
    """
    A human-readable register PDF that also carries the exact JSON as an
    attachment, so the machine-readable and the readable copy cannot drift.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"{register['brand']} seal register",
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle(
        "small", parent=styles["BodyText"], fontSize=7.5, leading=9.5
    )
    mono = ParagraphStyle(
        "mono", parent=styles["BodyText"], fontName="Courier",
        fontSize=6.5, leading=8,
    )

    story = [
        Paragraph(
            f"<b>{register['brand']} — seal register</b>", styles["Title"]
        ),
        Paragraph(
            f"Documents sealed between {register['period_start']} and "
            f"{register['period_end']}.",
            small,
        ),
        Spacer(1, 4 * mm),
        Paragraph(
            "<b>What this is.</b> Each row fixes one document's SHA-256 at the "
            "date it was sealed. This register is itself sealed and, where a "
            "timestamp authority was reachable, timestamped. Finding a "
            "document's hash here proves that exact file existed by this date "
            "— independently of that document's own certificate chain, and "
            "so still provable after every certificate involved has expired.",
            small,
        ),
        Spacer(1, 3 * mm),
        Paragraph(
            f"<b>Entries:</b> {register['entry_count']}<br/>"
            f"<b>Merkle root:</b> {register['merkle_root'] or '—'}<br/>"
            f"<b>Generated:</b> {register['generated_at']}",
            small,
        ),
        Spacer(1, 5 * mm),
    ]

    if register["entries"]:
        data = [[
            Paragraph("<b>Envelope</b>", small),
            Paragraph("<b>Sealed</b>", small),
            Paragraph("<b>Lvl</b>", small),
            Paragraph("<b>Document SHA-256</b>", small),
        ]]
        for e in register["entries"]:
            data.append([
                Paragraph(e["envelope_id"], mono),
                Paragraph((e["sealed_at"] or "")[:19], small),
                Paragraph((e["level"] or "").upper(), small),
                Paragraph(e["document_sha256"] or "—", mono),
            ])
        table = Table(
            data, colWidths=[32 * mm, 30 * mm, 12 * mm, 106 * mm],
            repeatRows=1,
        )
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D1D5DB")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(table)
    else:
        story.append(Paragraph(
            "No documents were sealed in this period.", small
        ))

    doc.build(story)
    pdf = buf.getvalue()

    # Attach the canonical JSON so the two copies cannot drift apart.
    try:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
        writer.add_attachment("seal-register.json", register_json)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:
        logger.warning(
            "NES register: the JSON could not be attached to the PDF — "
            "emitting the PDF alone", exc_info=True
        )
        return pdf
