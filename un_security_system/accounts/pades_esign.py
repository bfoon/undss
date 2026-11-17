# accounts/pades_esign.py
"""
UN PASS — NES PAdES signing service (ETSI EN 319 142).

Role
----
NES draws signatures onto the page and keeps a full audit trail. This module
adds the part a PDF reader can check on its own: a cryptographic signature
embedded in the completed PDF, covering every byte, issued under the office's
own signing certificate.

Once sealed, the file answers three questions without UN PASS:

    Has it been altered since completion?   -> byte-range digest
    Who issued it?                          -> signing certificate chain
    When?                                   -> RFC 3161 trusted timestamp

Design contract
---------------
**Sealing never breaks completion.** `seal_pdf()` does not raise. If pyHanko is
absent, no certificate is configured, the timestamp authority is unreachable or
the PDF is unusual, it returns the *original* bytes plus an info dict saying
what happened, and the envelope completes exactly as it does today. The seal is
an enhancement to a system that is already evidentially sound, so it must not
become a new way for signing to fail.

**It degrades, it does not guess.** The target level comes from settings. If a
TSA is unreachable the run steps down (B-LT -> B-T -> B-B) and records the level
it actually reached, so nobody later reads an unverifiable claim of B-LT.

**The seal is invisible.** NES already renders every signer's mark on the page.
A second visible widget would overlap that artwork and confuse signers, so the
seal is an invisible signature field. Readers still show it in their signature
panel.

Configuration (settings.py)
---------------------------
    ESIGN_PADES_ENABLED = True

    # either a PKCS#12 bundle ...
    ESIGN_PADES_PKCS12 = "/etc/unpass/nes-seal.p12"
    ESIGN_PADES_PKCS12_PASSPHRASE = "..."       # or env var

    # ... or separate PEM files
    ESIGN_PADES_CERT = "/etc/unpass/seal.crt"
    ESIGN_PADES_KEY  = "/etc/unpass/seal.key"
    ESIGN_PADES_KEY_PASSPHRASE = ""
    ESIGN_PADES_CHAIN = ["/etc/unpass/issuing-ca.crt"]

    ESIGN_PADES_LEVEL = "b-lt"                   # b-b | b-t | b-lt | b-lta
    ESIGN_PADES_TSA_URL = "http://timestamp.digicert.com"
    ESIGN_PADES_TRUST_ROOTS = ["/etc/unpass/root-ca.crt"]

Run `python manage.py esign_pades --check` to confirm the configuration, and
`--make-dev-cert` to generate a self-signed seal for a test environment.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

#: Field name for the invisible seal widget. Stable, so a re-seal replaces it.
DEFAULT_FIELD_NAME = "NESDocumentSeal"

#: Weakest first — the ladder `seal_pdf` walks down when a step is impossible.
LEVEL_ORDER = ["b-b", "b-t", "b-lt", "b-lta"]

_signer_lock = threading.Lock()
_signer_cache: dict[str, Any] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

def _s(name: str, default=""):
    return getattr(settings, name, default)


def pades_config() -> dict:
    """Resolved PAdES configuration. Pure settings read, no I/O."""
    level = str(_s("ESIGN_PADES_LEVEL", "b-lt") or "b-lt").lower().strip()
    if level not in LEVEL_ORDER:
        level = "b-lt"

    chain = _s("ESIGN_PADES_CHAIN", []) or []
    if isinstance(chain, str):
        chain = [chain]

    roots = _s("ESIGN_PADES_TRUST_ROOTS", []) or []
    if isinstance(roots, str):
        roots = [roots]

    return {
        "enabled": bool(_s("ESIGN_PADES_ENABLED", False)),
        "pkcs12": str(_s("ESIGN_PADES_PKCS12", "") or ""),
        "pkcs12_passphrase": str(_s("ESIGN_PADES_PKCS12_PASSPHRASE", "") or ""),
        "cert": str(_s("ESIGN_PADES_CERT", "") or ""),
        "key": str(_s("ESIGN_PADES_KEY", "") or ""),
        "key_passphrase": str(_s("ESIGN_PADES_KEY_PASSPHRASE", "") or ""),
        "chain": [str(p) for p in chain],
        "trust_roots": [str(p) for p in roots],
        "level": level,
        "tsa_url": str(_s("ESIGN_PADES_TSA_URL", "") or ""),
        "tsa_username": str(_s("ESIGN_PADES_TSA_USERNAME", "") or ""),
        "tsa_password": str(_s("ESIGN_PADES_TSA_PASSWORD", "") or ""),
        "field_name": str(_s("ESIGN_PADES_FIELD_NAME", DEFAULT_FIELD_NAME)
                          or DEFAULT_FIELD_NAME),
        "reason": str(_s("ESIGN_PADES_REASON", "") or ""),
        "location": str(_s("ESIGN_PADES_LOCATION", "") or ""),
        "digest": str(_s("ESIGN_PADES_DIGEST", "sha256") or "sha256"),
        # ETSI signed attributes: what policy the seal is issued under and
        # what it asserts. See `_signed_attr_spec`.
        "policy_oid": str(_s("ESIGN_PADES_POLICY_OID", "") or ""),
        "policy_uri": str(_s("ESIGN_PADES_POLICY_URI", "") or ""),
        "policy_doc": str(_s("ESIGN_PADES_POLICY_DOC", "") or ""),
        "commitment": str(
            _s("ESIGN_PADES_COMMITMENT", "proof_of_origin") or ""
        ).lower().strip(),
        # Long-term validity maintenance.
        "seal_certificate": bool(_s("ESIGN_PADES_SEAL_CERTIFICATE", True)),
        "refresh_days": int(_s("ESIGN_PADES_REFRESH_DAYS", 180) or 180),
    }


def pades_available() -> bool:
    """True when pyHanko is importable in this environment."""
    try:
        import pyhanko  # noqa: F401
        from pyhanko.sign import signers  # noqa: F401
    except Exception:
        return False
    return True


def pades_configured() -> tuple[bool, str]:
    """(ready, reason). `ready` means a seal can actually be produced."""
    cfg = pades_config()

    if not cfg["enabled"]:
        return False, "ESIGN_PADES_ENABLED is off."

    if not pades_available():
        return False, (
            "pyHanko is not installed. Add `pyHanko>=0.20` to requirements.txt "
            "and rebuild the image."
        )

    if cfg["pkcs12"]:
        if not os.path.exists(cfg["pkcs12"]):
            return False, f"ESIGN_PADES_PKCS12 not found: {cfg['pkcs12']}"
        return True, "PKCS#12 seal configured."

    if cfg["cert"] and cfg["key"]:
        for label, path in (("ESIGN_PADES_CERT", cfg["cert"]),
                            ("ESIGN_PADES_KEY", cfg["key"])):
            if not os.path.exists(path):
                return False, f"{label} not found: {path}"
        return True, "PEM seal configured."

    return False, (
        "No signing certificate configured. Set ESIGN_PADES_PKCS12, or both "
        "ESIGN_PADES_CERT and ESIGN_PADES_KEY."
    )


def max_achievable_level(cfg: dict | None = None) -> str:
    """The best level the current configuration can reach."""
    cfg = cfg or pades_config()
    if not cfg["tsa_url"]:
        # B-T and above are defined by the presence of a trusted timestamp.
        return "b-b"
    return cfg["level"]


# ─────────────────────────────────────────────────────────────────────────────
# Signer
# ─────────────────────────────────────────────────────────────────────────────

def _cache_key(cfg: dict) -> str:
    return "|".join([
        cfg["pkcs12"], cfg["cert"], cfg["key"], ",".join(cfg["chain"]),
    ])


def load_seal_signer(cfg: dict | None = None):
    """
    Return a cached pyHanko `SimpleSigner` for the office seal.

    Cached because reading and parsing the key on every completion is wasted
    work, and because a Celery worker may finalise many envelopes in a row.
    Raises on a genuine configuration error — callers in the signing path go
    through `seal_pdf`, which catches.
    """
    from pyhanko.sign import signers

    cfg = cfg or pades_config()
    key = _cache_key(cfg)

    with _signer_lock:
        cached = _signer_cache.get(key)
        if cached is not None:
            return cached

        if cfg["pkcs12"]:
            signer = signers.SimpleSigner.load_pkcs12(
                cfg["pkcs12"],
                passphrase=(cfg["pkcs12_passphrase"].encode()
                            if cfg["pkcs12_passphrase"] else None),
                ca_chain_files=cfg["chain"] or None,
            )
        else:
            signer = signers.SimpleSigner.load(
                cfg["key"],
                cfg["cert"],
                key_passphrase=(cfg["key_passphrase"].encode()
                                if cfg["key_passphrase"] else None),
                ca_chain_files=cfg["chain"] or None,
            )

        if signer is None:
            raise RuntimeError(
                "The signing certificate could not be loaded — check the "
                "passphrase and that the key matches the certificate."
            )

        _signer_cache[key] = signer
        return signer


def reset_signer_cache() -> None:
    """Drop the cached signer. Call after rotating the certificate on disk."""
    with _signer_lock:
        _signer_cache.clear()


def _timestamper(cfg: dict):
    """An RFC 3161 timestamper, or None when no TSA is configured."""
    if not cfg["tsa_url"]:
        return None
    from pyhanko.sign.timestamps import HTTPTimeStamper

    kwargs = {"url": cfg["tsa_url"]}
    if cfg["tsa_username"]:
        kwargs["auth"] = (cfg["tsa_username"], cfg["tsa_password"])
    return HTTPTimeStamper(**kwargs)


def _validation_context(cfg: dict, *, fetching: bool):
    """
    Validation context used to collect revocation data at signing time (B-LT)
    and to validate a seal afterwards.
    """
    from pyhanko_certvalidator import ValidationContext
    from pyhanko.keys import load_cert_from_pemder

    roots = []
    for path in cfg["trust_roots"]:
        try:
            roots.append(load_cert_from_pemder(path))
        except Exception:
            logger.warning(
                "NES PAdES: trust root %s could not be read — ignoring it", path
            )

    kwargs: dict[str, Any] = {"allow_fetching": fetching}
    if roots:
        kwargs["trust_roots"] = roots
    return ValidationContext(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Certificate details, for the stored record
# ─────────────────────────────────────────────────────────────────────────────

COMMITMENTS = {
    "proof_of_origin": "Proof of origin — this is the authentic record issued "
                       "by the organisation",
    "proof_of_approval": "Proof of approval",
    "proof_of_receipt": "Proof of receipt",
    "proof_of_delivery": "Proof of delivery",
    "proof_of_sender": "Proof of sender",
    "proof_of_creation": "Proof of creation",
}


def _signed_attr_spec(cfg: dict):
    """
    Build the ETSI/CAdES signed attributes, or None when there is nothing
    meaningful to assert.

    Two attributes, both from ETSI EN 319 122-1:

    **commitment-type-indication** — what the signature *means*. Without it a
    verifier knows the bytes are authentic but not what the organisation was
    claiming by sealing them. For an envelope NES has already collected
    signatures on, the seal asserts `proof_of_origin`: the organisation
    attests this is the authentic completed record. It is deliberately not
    `proof_of_approval` — the approvals belong to the people who signed
    inside the document, not to the server.

    **signature-policy-identifier** — the published rules the seal was issued
    under, by OID, bound to a hash of the policy document itself so the text
    cannot be swapped later. Emitted only when an OID *and* a readable policy
    document are both configured: an OID with a fabricated digest would be
    worse than no attribute at all.
    """
    try:
        from pyhanko.sign.ades.api import (
            CAdESSignedAttrSpec,
            GenericCommitment,
        )
    except Exception:
        return None

    commitment = None
    name = (cfg.get("commitment") or "").upper()
    if name and name in GenericCommitment.__members__:
        # CAdESSignedAttrSpec wants the ASN.1 CommitmentTypeIndication, not
        # the enum member — `.asn1` is the conversion.
        commitment = GenericCommitment[name].asn1

    policy = _policy_identifier(cfg)

    if commitment is None and policy is None:
        return None

    kwargs = {}
    if commitment is not None:
        kwargs["commitment_type"] = commitment
    if policy is not None:
        kwargs["signature_policy_identifier"] = policy

    try:
        return CAdESSignedAttrSpec(**kwargs)
    except Exception:
        logger.warning(
            "NES PAdES: the signed-attribute spec could not be built — "
            "sealing without it", exc_info=True
        )
        return None


def _policy_identifier(cfg: dict):
    """
    `SignaturePolicyId` over the configured policy document, or None.

    The ASN.1 structure requires a digest of the policy, which is the point:
    it pins the signature to one exact version of the published rules.
    """
    oid = (cfg.get("policy_oid") or "").strip()
    doc = (cfg.get("policy_doc") or "").strip()
    if not oid or not doc:
        return None

    if not os.path.exists(doc):
        logger.warning(
            "NES PAdES: ESIGN_PADES_POLICY_DOC not found (%s) — sealing "
            "without a policy identifier", doc
        )
        return None

    try:
        from asn1crypto import algos, cms, core
        from pyhanko.sign.ades.cades_asn1 import (
            SignaturePolicyId,
            SignaturePolicyIdentifier,
            SigPolicyQualifierInfo,
            SigPolicyQualifierInfos,
        )

        with open(doc, "rb") as fh:
            digest = hashlib.sha256(fh.read()).digest()

        fields = {
            "sig_policy_id": core.ObjectIdentifier(oid),
            "sig_policy_hash": algos.DigestInfo({
                "digest_algorithm": algos.DigestAlgorithm(
                    {"algorithm": "sha256"}
                ),
                "digest": digest,
            }),
        }

        uri = (cfg.get("policy_uri") or "").strip()
        if uri:
            fields["sig_policy_qualifiers"] = SigPolicyQualifierInfos([
                SigPolicyQualifierInfo({
                    "sig_policy_qualifier_id": "sp_uri",
                    "sig_qualifier": core.IA5String(uri),
                })
            ])

        return SignaturePolicyIdentifier(
            name="signature_policy_id", value=SignaturePolicyId(fields)
        )
    except Exception:
        logger.warning(
            "NES PAdES: the signature policy identifier could not be built — "
            "sealing without it", exc_info=True
        )
        return None


def _name_part(cert, oid_name: str) -> str:
    """Pull one RDN out of an asn1crypto x509 certificate subject."""
    try:
        native = cert.subject.native or {}
        return str(native.get(oid_name, "") or "")
    except Exception:
        return ""


def signer_details(signer) -> dict:
    """Human-readable facts about the signing certificate."""
    out = {
        "common_name": "",
        "organization": "",
        "serial": "",
        "issuer": "",
        "expires_at": None,
    }
    try:
        cert = signer.signing_cert
        out["common_name"] = _name_part(cert, "common_name")
        out["organization"] = _name_part(cert, "organization_name")
        out["serial"] = str(cert.serial_number)
        try:
            out["issuer"] = cert.issuer.human_friendly
        except Exception:
            out["issuer"] = ""
        try:
            # asn1crypto returns a tz-aware datetime.
            out["expires_at"] = cert["tbs_certificate"]["validity"]["not_after"].native
        except Exception:
            out["expires_at"] = None
    except Exception:
        logger.warning("NES PAdES: could not read the signing certificate details",
                       exc_info=True)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Sealing
# ─────────────────────────────────────────────────────────────────────────────

def _blank_info(**over) -> dict:
    info = {
        "status": "skipped",          # sealed | skipped | failed
        "level_requested": "",
        "level_achieved": "",
        "field_name": "",
        "digest_algorithm": "",
        "document_sha256": "",
        "timestamped": False,
        "timestamp_authority": "",
        "signer_common_name": "",
        "signer_organization": "",
        "signer_serial": "",
        "signer_issuer": "",
        "certificate_expires_at": None,
        "policy_oid": "",
        "policy_uri": "",
        "commitment_type": "",
        "detail": "",
    }
    info.update(over)
    return info


def _level_params(level: str, cfg: dict) -> dict:
    """pyHanko metadata flags for one PAdES baseline level."""
    return {
        "b-b": {"embed_validation_info": False, "use_pades_lta": False,
                "needs_tsa": False},
        "b-t": {"embed_validation_info": False, "use_pades_lta": False,
                "needs_tsa": True},
        "b-lt": {"embed_validation_info": True, "use_pades_lta": False,
                 "needs_tsa": True},
        "b-lta": {"embed_validation_info": True, "use_pades_lta": True,
                  "needs_tsa": True},
    }[level]


def _sign_once(pdf_bytes: bytes, level: str, cfg: dict, signer, envelope) -> bytes:
    """Apply one PAdES signature at `level`. Raises on failure."""
    from pyhanko.sign import signers
    from pyhanko.sign.fields import SigFieldSpec, SigSeedSubFilter
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    params = _level_params(level, cfg)
    timestamper = _timestamper(cfg) if params["needs_tsa"] else None

    if params["needs_tsa"] and timestamper is None:
        raise RuntimeError(f"PAdES {level.upper()} needs ESIGN_PADES_TSA_URL.")

    brand = getattr(settings, "ESIGN_BRAND", "NES")
    reason = cfg["reason"] or (
        f"Completed in {brand} — all signatures collected"
    )
    if envelope is not None:
        ref = getattr(envelope, "envelope_id", "") or ""
        if ref:
            reason = f"{reason} (envelope {ref})"

    meta = signers.PdfSignatureMetadata(
        field_name=cfg["field_name"],
        reason=reason[:200],
        location=cfg["location"] or None,
        subfilter=SigSeedSubFilter.PADES,      # ETSI.CAdES.detached
        md_algorithm=cfg["digest"],
        embed_validation_info=params["embed_validation_info"],
        use_pades_lta=params["use_pades_lta"],
        validation_context=(
            _validation_context(cfg, fetching=True)
            if params["embed_validation_info"] else None
        ),
        # Declares the policy the seal is issued under and what it asserts.
        cades_signed_attr_spec=_signed_attr_spec(cfg),
    )

    pdf_signer = signers.PdfSigner(
        meta,
        signer=signer,
        timestamper=timestamper,
        # box=None -> an invisible signature field. NES already draws every
        # signer's visible mark; a second widget would sit on top of it.
        new_field_spec=SigFieldSpec(
            sig_field_name=cfg["field_name"], on_page=-1, box=None
        ),
    )

    writer = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
    out = io.BytesIO()
    pdf_signer.sign_pdf(writer, output=out, in_place=False)
    return out.getvalue()


def seal_pdf(pdf_bytes: bytes, envelope=None) -> tuple[bytes, dict]:
    """
    Embed a PAdES signature in `pdf_bytes`.

    Returns `(bytes, info)`. On any problem the returned bytes are the ORIGINAL
    input and `info["status"]` is `skipped` or `failed` — callers can save the
    result unconditionally.
    """
    cfg = pades_config()
    info = _blank_info(level_requested=cfg["level"])

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-"):
        info.update(status="skipped", detail="Not a PDF — nothing to seal.")
        return pdf_bytes, info

    ready, reason = pades_configured()
    if not ready:
        info.update(status="skipped", detail=reason)
        return pdf_bytes, info

    try:
        signer = load_seal_signer(cfg)
    except Exception as exc:
        logger.exception("NES PAdES: the signing certificate could not be loaded")
        info.update(status="failed", detail=f"Certificate not usable: {exc}")
        return pdf_bytes, info

    details = signer_details(signer)
    info.update(
        signer_common_name=details["common_name"],
        signer_organization=details["organization"],
        signer_serial=details["serial"],
        signer_issuer=details["issuer"],
        certificate_expires_at=details["expires_at"],
        field_name=cfg["field_name"],
        digest_algorithm=cfg["digest"],
        timestamp_authority=cfg["tsa_url"],
        commitment_type=cfg["commitment"],
        # Only reported when the attribute was actually embedded.
        policy_oid=(
            cfg["policy_oid"] if _policy_identifier(cfg) is not None else ""
        ),
        policy_uri=(
            cfg["policy_uri"] if _policy_identifier(cfg) is not None else ""
        ),
    )

    # Walk down from the requested level to B-B. An unreachable TSA or OCSP
    # responder costs a level, not the seal.
    target = cfg["level"]
    ladder = LEVEL_ORDER[: LEVEL_ORDER.index(target) + 1][::-1]
    problems: list[str] = []

    for level in ladder:
        params = _level_params(level, cfg)
        if params["needs_tsa"] and not cfg["tsa_url"]:
            problems.append(f"{level.upper()}: no ESIGN_PADES_TSA_URL configured")
            continue
        try:
            # Quiet pyHanko's own path-building traceback: stepping down a
            # level is an expected outcome here and is logged below in one
            # line, with the reason kept in `info["detail"]`.
            with _quiet_certvalidator():
                sealed = _sign_once(pdf_bytes, level, cfg, signer, envelope)
        except Exception as exc:
            problems.append(f"{level.upper()}: {exc}")
            logger.warning(
                "NES PAdES: %s failed for envelope %s — trying a lower level",
                level.upper(),
                getattr(envelope, "envelope_id", "?"),
                exc_info=True,
            )
            continue

        info.update(
            status="sealed",
            level_achieved=level,
            timestamped=bool(params["needs_tsa"]),
            document_sha256=hashlib.sha256(sealed).hexdigest(),
        )
        if level != target:
            info["detail"] = (
                f"Reached PAdES {level.upper()} instead of {target.upper()}. "
                + " | ".join(problems)
            )[:2000]
        return sealed, info

    logger.error(
        "NES PAdES: every level failed for envelope %s",
        getattr(envelope, "envelope_id", "?"),
    )
    info.update(status="failed", detail=" | ".join(problems)[:2000])
    return pdf_bytes, info


# ─────────────────────────────────────────────────────────────────────────────
# Long-term validity maintenance — what "always verifiable" actually requires
# ─────────────────────────────────────────────────────────────────────────────
#
# A signature is not verifiable forever by default, and this is the part most
# e-signature systems quietly skip.
#
# Three clocks run against a sealed document:
#
#   1. The signing certificate expires. After that a verifier cannot tell
#      whether it was valid *at signing time* unless the signing time itself
#      is proven — that is what the timestamp in B-T gives.
#   2. The revocation data (OCSP/CRL) that proves the certificate was good at
#      signing time stops being retrievable. B-LT embeds it in the document
#      so it no longer has to be fetched.
#   3. The *timestamp's own* certificate expires, and the digest algorithm
#      weakens. B-LTA answers this by adding a document timestamp over
#      everything, which can be renewed before the previous one goes stale —
#      an unbroken chain of timestamps, each vouching for the last.
#
# So "always verifiable" is not a property you set once. It is a maintenance
# task: refresh before the current timestamp's material expires. That is what
# `refresh_ltv` does, and why `EnvelopeSeal.next_refresh_due` exists.


def upgrade_to_ltv(pdf_bytes: bytes) -> tuple[bytes, dict]:
    """
    Embed validation material for signatures already in `pdf_bytes`.

    Turns a B-B or B-T signature into B-LT retroactively, which matters for
    envelopes sealed before a TSA or an OCSP responder was reachable. Needs
    network access to the responders named in the certificate chain.

    Returns `(bytes, info)`; on failure the bytes are the original.
    """
    info = {"ok": False, "upgraded": 0, "detail": ""}

    if not pades_available():
        info["detail"] = "pyHanko is not installed."
        return pdf_bytes, info

    try:
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation import add_validation_info

        cfg = pades_config()
        reader = PdfFileReader(io.BytesIO(pdf_bytes))
        sigs = list(reader.embedded_signatures)
        if not sigs:
            info["detail"] = "No embedded signature to upgrade."
            return pdf_bytes, info

        current = pdf_bytes
        upgraded = 0

        for index in range(len(sigs)):
            # Re-read each round: the previous iteration rewrote the file.
            writer = IncrementalPdfFileWriter(io.BytesIO(current))
            embedded = list(writer.prev.embedded_signatures)[index]
            out = io.BytesIO()
            with _quiet_certvalidator():
                add_validation_info(
                    embedded,
                    _validation_context(cfg, fetching=True),
                    in_place=False,
                    output=out,
                    force_write=False,
                )
            candidate = out.getvalue()
            if candidate and candidate.startswith(b"%PDF-"):
                current = candidate
                upgraded += 1

        info.update(ok=upgraded > 0, upgraded=upgraded)
        if not upgraded:
            info["detail"] = "Nothing was added — validation data unavailable."
        return current, info

    except Exception as exc:
        logger.warning("NES PAdES: LTV upgrade failed", exc_info=True)
        info["detail"] = str(exc)
        return pdf_bytes, info


def add_document_timestamp(pdf_bytes: bytes) -> tuple[bytes, dict]:
    """
    Extend the archival document timestamp chain (the B-LTA mechanism).

    Each call adds one timestamp covering the whole file, including every
    earlier timestamp. Run periodically — before the newest timestamp's own
    certificate expires — and the document stays verifiable indefinitely.

    Returns `(bytes, info)`; on failure the bytes are the original.
    """
    info = {"ok": False, "detail": ""}

    cfg = pades_config()
    if not cfg["tsa_url"]:
        info["detail"] = (
            "No ESIGN_PADES_TSA_URL — a document timestamp needs a timestamp "
            "authority. Without one the seal cannot reach B-LTA."
        )
        return pdf_bytes, info

    if not pades_available():
        info["detail"] = "pyHanko is not installed."
        return pdf_bytes, info

    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.signers import PdfTimeStamper

        reader = PdfFileReader(io.BytesIO(pdf_bytes))
        stamper = PdfTimeStamper(_timestamper(cfg))
        out = io.BytesIO()
        with _quiet_certvalidator():
            stamper.update_archival_timestamp_chain(
                reader,
                _validation_context(cfg, fetching=True),
                in_place=False,
                output=out,
                default_md_algorithm=cfg["digest"],
            )
        stamped = out.getvalue()

        if not stamped or not stamped.startswith(b"%PDF-"):
            info["detail"] = "The timestamped output was not a valid PDF."
            return pdf_bytes, info

        info["ok"] = True
        return stamped, info

    except Exception as exc:
        logger.warning(
            "NES PAdES: document timestamp failed", exc_info=True
        )
        info["detail"] = str(exc)
        return pdf_bytes, info


def refresh_ltv(envelope, *, force: bool = False) -> dict:
    """
    Bring one envelope's sealed PDF up to the best level now reachable, and
    reschedule the next refresh.

    Order matters: embed validation data first, then timestamp over it, so the
    new timestamp covers the material it is meant to protect.

    Returns a report dict. Never raises.
    """
    from django.core.files.base import ContentFile
    from django.utils import timezone

    report = {
        "envelope_id": getattr(envelope, "envelope_id", ""),
        "changed": False,
        "level_before": "",
        "level_after": "",
        "steps": [],
        "detail": "",
        "ok": False,
    }

    try:
        from .models_esign import Envelope
        from .models_esign_pades import EnvelopeSeal

        if envelope.status != Envelope.STATUS_COMPLETED:
            report["detail"] = "Not completed — nothing to refresh."
            return report

        seal = EnvelopeSeal.objects.filter(envelope=envelope).first()
        report["level_before"] = (seal.level_achieved if seal else "") or ""

        if seal is None or not seal.is_sealed:
            report["detail"] = (
                "No seal on this envelope. Seal it first, then refresh."
            )
            return report

        if not force and not seal.refresh_overdue and seal.next_refresh_due:
            report["detail"] = (
                f"Not due until {seal.next_refresh_due:%Y-%m-%d}. "
                "Use force to refresh anyway."
            )
            report["ok"] = True
            return report

        ready, reason = pades_configured()
        if not ready:
            report["detail"] = reason
            return report

        if not envelope.completed_pdf:
            report["detail"] = "The completed PDF is missing from storage."
            return report

        envelope.completed_pdf.open("rb")
        try:
            raw = envelope.completed_pdf.read()
        finally:
            envelope.completed_pdf.close()

        working = raw
        cfg = pades_config()

        # 1. embed revocation material (-> B-LT)
        if seal.level_achieved in ("b-b", "b-t"):
            working, ltv = upgrade_to_ltv(working)
            report["steps"].append({
                "step": "embed_validation_info",
                "ok": ltv["ok"],
                "detail": ltv["detail"],
            })

        # 2. document timestamp over the whole file (-> B-LTA, renewable)
        working, ts = add_document_timestamp(working)
        report["steps"].append({
            "step": "document_timestamp",
            "ok": ts["ok"],
            "detail": ts["detail"],
        })

        changed = working != raw
        achieved = seal.level_achieved
        if ts["ok"]:
            achieved = "b-lta"
        elif any(
            s["step"] == "embed_validation_info" and s["ok"]
            for s in report["steps"]
        ):
            achieved = "b-lt"

        now = timezone.now()
        due = now + timezone.timedelta(days=max(1, cfg["refresh_days"]))

        if changed:
            envelope.completed_pdf.save(
                f"{envelope.envelope_id}-signed.pdf",
                ContentFile(working),
                save=False,
            )
            envelope.save(update_fields=["completed_pdf"])

        seal.level_achieved = achieved
        seal.timestamped = seal.timestamped or ts["ok"]
        seal.document_sha256 = sha256_of(working)
        seal.last_refreshed_at = now
        seal.refresh_count = (seal.refresh_count or 0) + 1
        seal.next_refresh_due = due
        seal.refresh_detail = "; ".join(
            f"{s['step']}: {'ok' if s['ok'] else s['detail']}"
            for s in report["steps"]
        )[:2000]
        seal.save(update_fields=[
            "level_achieved", "timestamped", "document_sha256",
            "last_refreshed_at", "refresh_count", "next_refresh_due",
            "refresh_detail",
        ])

        # A refresh changes the file, so the old fingerprint no longer
        # describes it. Re-witness, or the next audit would correctly report
        # the document as matching no attestation.
        if changed:
            try:
                from .witness_esign import issue_witness

                issue_witness(envelope, kind="refresh")
            except Exception:
                logger.exception(
                    "NES witness: could not re-issue after refreshing %s",
                    getattr(envelope, "envelope_id", "?"),
                )

        report.update(
            changed=changed,
            level_after=achieved,
            ok=True,
            detail=seal.refresh_detail,
        )
        return report

    except Exception as exc:
        logger.exception(
            "NES PAdES: refresh failed for envelope %s",
            getattr(envelope, "envelope_id", "?"),
        )
        report["detail"] = str(exc)
        return report


def probe_tsa(url: str, *, username: str = "", password: str = "") -> dict:
    """
    Do a real RFC 3161 round trip against `url` and report what came back.

    Used by `manage.py esign_pades --test-tsa` so a candidate authority can be
    vetted before it is wired into production. Never raises.

    The clock check matters as much as the signature: a TSA whose clock is
    wrong produces timestamps that are worse than none, because they look
    authoritative while asserting the wrong time.
    """
    import datetime

    out = {
        "ok": False,
        "url": url,
        "token_bytes": 0,
        "policy_oid": "",
        "gen_time": None,
        "serial": "",
        "accuracy": "",
        "signer_subject": "",
        "signer_issuer": "",
        "signer_not_after": None,
        "certs_in_token": 0,
        "skew_seconds": None,
        "trusted_here": None,
        "error": "",
        "notes": [],
    }

    if not pades_available():
        out["error"] = "pyHanko is not installed."
        return out

    try:
        import asyncio

        from asn1crypto import tsp  # noqa: F401  (ensures the module loads)
        from pyhanko.sign.timestamps import HTTPTimeStamper

        cfg = pades_config()
        kwargs = {"url": url}
        if username:
            kwargs["auth"] = (username, password)
        stamper = HTTPTimeStamper(**kwargs)

        probe = hashlib.sha256(
            b"NES TSA probe " + datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat().encode()
        ).digest()

        sent_at = datetime.datetime.now(datetime.timezone.utc)

        async def _ask():
            return await stamper.async_timestamp(probe, cfg["digest"])

        token = asyncio.run(_ask())
        raw = token.dump()
        out["token_bytes"] = len(raw)

        signed_data = token["content"]
        tst = signed_data["encap_content_info"]["content"].parsed

        out["policy_oid"] = str(tst["policy"].native or "")
        out["gen_time"] = tst["gen_time"].native
        out["serial"] = str(tst["serial_number"].native)

        try:
            acc = tst["accuracy"].native or {}
            if acc:
                out["accuracy"] = ", ".join(
                    f"{k}={v}" for k, v in acc.items() if v
                )
        except Exception:
            pass

        certs = list(signed_data["certificates"] or [])
        out["certs_in_token"] = len(certs)
        if certs:
            signer = certs[0].chosen
            try:
                out["signer_subject"] = signer.subject.human_friendly
                out["signer_issuer"] = signer.issuer.human_friendly
                out["signer_not_after"] = (
                    signer["tbs_certificate"]["validity"]["not_after"].native
                )
            except Exception:
                pass
        else:
            out["notes"].append(
                "The token carries no certificates. pyHanko requested them, "
                "so this authority may need its chain supplied separately — "
                "harder to validate offline."
            )

        # Clock skew against our own time.
        if out["gen_time"] is not None:
            try:
                out["skew_seconds"] = abs(
                    (out["gen_time"] - sent_at).total_seconds()
                )
                if out["skew_seconds"] > 60:
                    out["notes"].append(
                        f"The timestamp is {out['skew_seconds']:.0f}s away "
                        "from this server's clock. One of the two is wrong — "
                        "check NTP here before blaming the authority."
                    )
            except Exception:
                pass

        # Does its chain validate against our configured trust roots?
        if cfg["trust_roots"]:
            try:
                from pyhanko_certvalidator import CertificateValidator

                vc = _validation_context(cfg, fetching=False)
                if certs:
                    validator = CertificateValidator(
                        certs[0].chosen,
                        intermediate_certs=[c.chosen for c in certs[1:]],
                        validation_context=vc,
                    )

                    async def _check_chain():
                        # `timeStamping` EKU, which is what a TSA certificate
                        # must carry and must not share with other uses.
                        return await validator.async_validate_usage(
                            set(), {"time_stamping"}
                        )

                    with _quiet_certvalidator():
                        asyncio.run(_check_chain())
                    out["trusted_here"] = True
            except Exception as exc:
                out["trusted_here"] = False
                out["notes"].append(
                    "Its chain does not validate against "
                    f"ESIGN_PADES_TRUST_ROOTS: {exc}"
                )
        else:
            out["notes"].append(
                "ESIGN_PADES_TRUST_ROOTS is empty, so trust was not checked. "
                "Add the authority's root to check it here; recipients' "
                "readers use their own trust stores regardless."
            )

        if out["signer_not_after"] is not None:
            try:
                days = (
                    out["signer_not_after"]
                    - datetime.datetime.now(datetime.timezone.utc)
                ).days
                if days < 0:
                    out["notes"].append(
                        "The authority's own certificate has expired."
                    )
                elif days < 90:
                    out["notes"].append(
                        f"The authority's certificate expires in {days} days. "
                        "Timestamps already issued stay valid; new ones will "
                        "stop once it lapses."
                    )
            except Exception:
                pass

        out["ok"] = True
        return out

    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("NES PAdES: TSA probe failed for %s", url, exc_info=True)
        return out


def seals_due_for_refresh(limit: int = 200):
    """Sealed envelopes whose archival material should be renewed now."""
    from django.utils import timezone
    from .models_esign_pades import EnvelopeSeal

    return (
        EnvelopeSeal.objects
        .filter(status=EnvelopeSeal.STATUS_SEALED)
        .filter(next_refresh_due__lte=timezone.now())
        .select_related("envelope")
        .order_by("next_refresh_due")[:limit]
    )


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def record_seal(envelope, info: dict):
    """
    Write the seal record for `envelope`. Never raises — a missing record must
    not roll back a completed envelope.
    """
    try:
        from .models_esign_pades import EnvelopeSeal

        defaults = {
            "status": info.get("status") or EnvelopeSeal.STATUS_SKIPPED,
            "level_requested": info.get("level_requested") or "",
            "level_achieved": info.get("level_achieved") or "",
            "field_name": (info.get("field_name") or "")[:64],
            "digest_algorithm": (info.get("digest_algorithm") or "")[:16],
            "document_sha256": (info.get("document_sha256") or "")[:64],
            "signer_common_name": (info.get("signer_common_name") or "")[:200],
            "signer_organization": (info.get("signer_organization") or "")[:200],
            "signer_serial": (info.get("signer_serial") or "")[:80],
            "signer_issuer": (info.get("signer_issuer") or "")[:300],
            "certificate_expires_at": info.get("certificate_expires_at"),
            "timestamped": bool(info.get("timestamped")),
            "timestamp_authority": (info.get("timestamp_authority") or "")[:300],
            "policy_oid": (info.get("policy_oid") or "")[:120],
            "policy_uri": (info.get("policy_uri") or "")[:300],
            "commitment_type": (info.get("commitment_type") or "")[:40],
            "certificate_sealed": bool(info.get("certificate_sealed")),
            "certificate_sha256": (info.get("certificate_sha256") or "")[:64],
            "detail": info.get("detail") or "",
        }

        # Audit-trail anchor, when the caller collected one.
        anchor = info.get("audit") or {}
        if anchor:
            defaults.update({
                "audit_chain_head": (anchor.get("head") or "")[:64],
                "audit_chain_length": int(anchor.get("length") or 0),
                "audit_anchor": anchor.get("token_b64") or "",
                "audit_anchored_at": anchor.get("at"),
                "audit_anchor_timestamped": bool(anchor.get("timestamped")),
                "audit_detail": anchor.get("detail") or "",
            })

        if info.get("status") == "sealed":
            from django.utils import timezone

            now = timezone.now()
            defaults["applied_at"] = now
            # Schedule the first archival refresh. Without this the seal is a
            # point-in-time artefact that slowly stops validating.
            cfg = pades_config()
            defaults["next_refresh_due"] = now + timezone.timedelta(
                days=max(1, cfg["refresh_days"])
            )

        seal, _created = EnvelopeSeal.objects.update_or_create(
            envelope=envelope, defaults=defaults
        )
        return seal
    except Exception:
        logger.exception(
            "NES PAdES: could not record the seal for envelope %s",
            getattr(envelope, "pk", "?"),
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────────

def _coverage_at_least_revision(coverage) -> bool:
    """
    True when a signature covers at least its entire revision.

    The enum is ordered (UNCLEAR < CONTIGUOUS_BLOCK_FROM_START <
    ENTIRE_REVISION < ENTIRE_FILE), so compare by value and fall back to the
    name if pyHanko's internals move.
    """
    if coverage is None:
        return False
    try:
        from pyhanko.sign.validation.status import SignatureCoverageLevel

        return int(coverage.value) >= int(SignatureCoverageLevel.ENTIRE_REVISION.value)
    except Exception:
        name = str(getattr(coverage, "name", coverage) or "").upper()
        return "ENTIRE_REVISION" in name or "ENTIRE_FILE" in name


def _modifications_acceptable(level) -> bool:
    """
    True when nothing worse than archival timestamping happened after signing.

    ModificationLevel is ordered NONE < LTA_UPDATES < FORM_FILLING <
    ANNOTATIONS < OTHER. A document timestamp refresh lands on LTA_UPDATES,
    which is exactly what `refresh_ltv` produces and must not be treated as
    tampering. Anything above it changes content and is not acceptable here.

    `None` means pyHanko reported no modification level — the case for
    document timestamps themselves — which is not a failure.
    """
    if level is None:
        return True
    try:
        from pyhanko.sign.diff_analysis import ModificationLevel

        return int(level.value) <= int(ModificationLevel.LTA_UPDATES.value)
    except Exception:
        return str(getattr(level, "name", level) or "").upper() in {
            "NONE", "LTA_UPDATES"
        }


def _trailing_bytes_after_eof(pdf_bytes: bytes) -> int:
    """
    How many bytes sit after the final `%%EOF`, ignoring whitespace.

    Checked explicitly because signature validation cannot see them: appended
    bytes form no valid revision, so the parser reads the last good xref and
    reports every signature intact. Verified by experiment, not assumed.

    A few trailing newlines are normal; anything more was appended.
    """
    if not pdf_bytes:
        return 0
    marker = pdf_bytes.rfind(b"%%EOF")
    if marker == -1:
        return 0
    tail = pdf_bytes[marker + len(b"%%EOF"):]
    return len(tail.strip(b"\r\n \t\x00"))


class _quiet_certvalidator:
    """
    Silence pyHanko's own certificate-path logging for the duration of a
    validation call.

    pyHanko logs a full traceback at ERROR when it cannot build a trust path —
    including the ordinary case of a certificate whose root is simply not in
    `ESIGN_PADES_TRUST_ROOTS`. That condition is already reported to the caller
    as `trusted: False`, with the reason in `error`, so letting the traceback
    through would fill the production log with noise on every visit to the
    verify page. Nothing is swallowed: the outcome still reaches the caller.
    """

    LOGGERS = (
        "pyhanko.sign.validation.generic_cms",
        "pyhanko.sign.validation",
        "pyhanko_certvalidator",
    )

    def __enter__(self):
        self._previous = {}
        for name in self.LOGGERS:
            log = logging.getLogger(name)
            self._previous[name] = log.level
            log.setLevel(logging.CRITICAL)
        return self

    def __exit__(self, *exc):
        for name, level in self._previous.items():
            logging.getLogger(name).setLevel(level)
        return False


def verify_pdf(pdf_bytes: bytes) -> dict:
    """
    Validate every PAdES signature in `pdf_bytes`.

    Returns a dict the UI can render directly:

        {"ok": bool, "available": bool, "signatures": [...], "error": str}

    `ok` is True only when at least one signature is intact and covers the
    whole file. `trusted` is reported separately, because a self-signed seal in
    a test environment is intact but not trusted.
    """
    out = {
        "ok": False,
        "available": True,
        "signatures": [],
        "error": "",
        "document_timestamps": 0,
        "all_signatures_intact": False,
        "outermost_covers_file": False,
    }

    if not pades_available():
        out.update(available=False, error="pyHanko is not installed.")
        return out

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-"):
        out["error"] = "Not a PDF."
        return out

    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation import (
            validate_pdf_signature,
            validate_pdf_timestamp,
        )

        cfg = pades_config()
        reader = PdfFileReader(io.BytesIO(pdf_bytes))
        embedded = list(reader.embedded_signatures)

        if not embedded:
            out["error"] = "This PDF carries no embedded signature."
            return out

        for sig in embedded:
            # /Sig is an approval signature; /DocTimeStamp is an archival
            # document timestamp added by a refresh. They validate through
            # different pyHanko entry points.
            obj_type = str(getattr(sig, "sig_object_type", "") or "")
            is_doc_timestamp = "DocTimeStamp" in obj_type

            row = {
                "field_name": getattr(sig, "field_name", "") or "",
                "kind": "document_timestamp" if is_doc_timestamp else "signature",
                "intact": False,
                "valid": False,
                "trusted": False,
                "coverage": "",
                "signer": "",
                "summary": "",
                "md_algorithm": "",
                "signed_at": None,
                "timestamp": None,
                "modification_level": "",
                "modifications_acceptable": False,
                "covers_revision": False,
                "error": "",
            }
            try:
                vc = _validation_context(cfg, fetching=False)
                with _quiet_certvalidator():
                    if is_doc_timestamp:
                        status = validate_pdf_timestamp(sig, vc)
                    else:
                        status = validate_pdf_signature(sig, vc)
                row.update(
                    intact=bool(status.intact),
                    valid=bool(status.valid),
                    trusted=bool(getattr(status, "trusted", False)),
                    coverage=str(getattr(status, "coverage", "") or ""),
                    summary=str(status.summary()),
                    md_algorithm=str(getattr(status, "md_algorithm", "") or ""),
                )
                try:
                    row["signer"] = status.signing_cert.subject.human_friendly
                except Exception:
                    row["signer"] = ""
                row["signed_at"] = getattr(status, "signer_reported_dt", None)
                row["timestamp"] = getattr(status, "timestamp_validity", None) and \
                    getattr(status.timestamp_validity, "timestamp", None)

                # Coverage of at least the whole revision, and modifications
                # after it no worse than archival timestamp updates. These
                # are the two properties that actually matter; see the note
                # on `ok` below.
                row["covers_revision"] = _coverage_at_least_revision(
                    getattr(status, "coverage", None)
                )
                level = getattr(status, "modification_level", None)
                row["modification_level"] = (
                    getattr(level, "name", "") if level is not None else ""
                )
                row["modifications_acceptable"] = _modifications_acceptable(level)
            except Exception as exc:
                row["error"] = str(exc)
                logger.warning("NES PAdES: validation failed for a signature",
                               exc_info=True)

            out["signatures"].append(row)

        # Judging a PAdES document, carefully.
        #
        # A PAdES document grows by incremental update: each signature covers
        # the file as it stood when it was applied. Once an archival document
        # timestamp is appended, the original signature reports
        # ENTIRE_REVISION rather than ENTIRE_FILE — correctly. Demanding
        # ENTIRE_FILE would mark every properly refreshed B-LTA document as
        # broken, which is the opposite of the intent.
        #
        # So the test is: every signature intact, each covering at least its
        # whole revision, with nothing done afterwards beyond archival
        # timestamp updates (pyHanko's LTA_UPDATES modification level).
        #
        # One sharp edge, verified by experiment rather than assumed: raw
        # bytes appended after the last %%EOF do NOT change any of the above.
        # They form no valid revision, so the parser reads the last good xref
        # and ignores them — validation still reports everything intact. That
        # is why `trailing_bytes` is checked directly here, and why the
        # authoritative integrity statement in the UI also compares the
        # stored file against the SHA-256 recorded at completion. Signature
        # validation alone does not close this hole; the recorded hash does.
        sigs = out["signatures"]
        out["trailing_bytes"] = _trailing_bytes_after_eof(pdf_bytes)

        if sigs:
            all_intact = all(r["intact"] for r in sigs)
            all_cover = all(r["covers_revision"] for r in sigs)
            all_mods_ok = all(r["modifications_acceptable"] for r in sigs)

            out["all_signatures_intact"] = all_intact
            out["outermost_covers_file"] = all_cover
            out["document_timestamps"] = sum(
                1 for r in sigs if r["kind"] == "document_timestamp"
            )
            out["ok"] = bool(
                all_intact
                and all_cover
                and all_mods_ok
                and not out["trailing_bytes"]
            )

            if all_intact and not all_cover:
                out["error"] = (
                    "A signature does not cover its whole revision — part of "
                    "the file is not vouched for by anything."
                )
            elif all_intact and not all_mods_ok:
                out["error"] = (
                    "The document was modified after signing by more than "
                    "archival timestamping."
                )
            elif all_intact and out["trailing_bytes"]:
                out["error"] = (
                    f"{out['trailing_bytes']} byte(s) sit after the final "
                    "%%EOF. Every signature is intact, so the signed content "
                    "is unchanged, but something was appended to the file."
                )
        return out

    except Exception as exc:
        logger.exception("NES PAdES: the PDF could not be read for validation")
        out["error"] = str(exc)
        return out


def sha256_of(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes or b"").hexdigest()
