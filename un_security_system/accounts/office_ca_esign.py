# accounts/office_ca_esign.py
"""
UN PASS — the office's own small PKI for the NES document seal.

Why this exists
---------------
Two things the evidence package reports come from the same root cause —
**no seal certificate on the server**:

* "No Document Security Store in this PDF": nothing was sealed, so there is
  no embedded validation data (DSS) either;
* "The chain head was recorded but never signed": the audit-trail anchor is
  signed with the seal key, and there was none.

A certificate from a CA in Adobe's Approved Trust List is the gold standard
and should be the long-term answer. Until the office has one, this module
gives it a *proper* chain rather than the old self-signed test seal:

    UN PASS Office Root CA  (offline-style root, 20 years, signs CRLs)
        └── NES Document Seal  (3 years, documentSigning, CRL + AIA URLs)

The seal certificate points at a revocation list UN PASS publishes itself
(`/accounts/esign/pki/office-ca.crl`) and the root it chains to
(`/accounts/esign/pki/office-ca.crt`). UN PASS also hands that CRL straight
to the signer, so the PDF's Document Security Store is filled at sealing time
with no network round trip — the seal reaches PAdES B-LTA, which keeps
validating after the seal certificate itself has expired.

Anyone can check a document against the root's fingerprint, which the
office publishes and the Sentinels can carry. Revoking a compromised seal is
one setting: ESIGN_OFFICE_CA_REVOKED.
"""
from __future__ import annotations

import datetime
import logging
import os
import secrets
import time

from django.conf import settings

logger = logging.getLogger(__name__)

CRL_DAYS = 7
_CRL_CACHE: dict = {}


def _secrets_dir() -> str:
    return getattr(settings, "NES_SECRETS_DIR", "/etc/unpass")


def ca_cert_path() -> str:
    return getattr(settings, "ESIGN_OFFICE_CA_CERT", "") or os.path.join(_secrets_dir(), "office-ca.crt")


def ca_key_path() -> str:
    return getattr(settings, "ESIGN_OFFICE_CA_KEY", "") or os.path.join(_secrets_dir(), "office-ca.key")


def site_url() -> str:
    return (getattr(settings, "SITE_URL", "") or "").rstrip("/")


def crl_url(base: str | None = None) -> str:
    return f"{(base or site_url())}/accounts/esign/pki/office-ca.crl"


def root_url(base: str | None = None) -> str:
    return f"{(base or site_url())}/accounts/esign/pki/office-ca.crt"


def present() -> bool:
    return os.path.exists(ca_cert_path())


def load_ca_cert():
    from cryptography import x509

    with open(ca_cert_path(), "rb") as fh:
        return x509.load_pem_x509_certificate(fh.read())


def load_ca_key():
    from cryptography.hazmat.primitives import serialization

    pw = (getattr(settings, "ESIGN_OFFICE_CA_KEY_PASSPHRASE", "") or "").encode() or None
    with open(ca_key_path(), "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), password=pw)


def fingerprint(cert) -> str:
    from cryptography.hazmat.primitives import hashes

    raw = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(raw[i:i + 2] for i in range(0, len(raw), 2))


# ─────────────────────────────────────────────────────────────────────────────
# Creating the PKI
# ─────────────────────────────────────────────────────────────────────────────

def create(out_dir: str, *, office: str, base_url: str, force: bool = False) -> dict:
    """
    Write office-ca.crt, office-ca.key (encrypted) and seal.p12 (encrypted)
    into `out_dir`. Returns the passphrases and fingerprints. Refuses to
    overwrite anything unless forced.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

    paths = {name: os.path.join(out_dir, name) for name in ("office-ca.crt", "office-ca.key", "seal.p12")}
    clash = [p for p in paths.values() if os.path.exists(p)]
    if clash and not force:
        raise FileExistsError(", ".join(clash))
    os.makedirs(out_dir, exist_ok=True)

    brand = getattr(settings, "ESIGN_BRAND", "NES")
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_pass = secrets.token_urlsafe(24)
    seal_pass = secrets.token_urlsafe(24)

    def name(cn):
        parts = [x509.NameAttribute(NameOID.ORGANIZATION_NAME, office[:64])]
        unit = getattr(settings, "ESIGN_PADES_ORG_UNIT", "") or "UN PASS"
        parts.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, unit[:64]))
        parts.append(x509.NameAttribute(NameOID.COMMON_NAME, cn[:64]))
        return x509.Name(parts)

    # Root CA ----------------------------------------------------------------
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    ca_name = name(f"{office} — {brand} Office Root CA")
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * 20))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    # Seal certificate -------------------------------------------------------
    seal_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    document_signing = ObjectIdentifier("1.3.6.1.5.5.7.3.36")        # RFC 9336
    ms_document_signing = ObjectIdentifier("1.3.6.1.4.1.311.10.3.12")  # Adobe/Microsoft
    seal = (
        x509.CertificateBuilder()
        .subject_name(name(f"{office} — {brand} Document Seal"))
        .issuer_name(ca_name)
        .public_key(seal_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * 3))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=True, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([document_signing, ms_document_signing,
                                              ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(seal_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
        .add_extension(x509.CRLDistributionPoints([x509.DistributionPoint(
            full_name=[x509.UniformResourceIdentifier(crl_url(base_url))],
            relative_name=None, reasons=None, crl_issuer=None)]), critical=False)
        .add_extension(x509.AuthorityInformationAccess([x509.AccessDescription(
            x509.oid.AuthorityInformationAccessOID.CA_ISSUERS,
            x509.UniformResourceIdentifier(root_url(base_url)))]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    def write(path, data):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    write(paths["office-ca.crt"], ca.public_bytes(serialization.Encoding.PEM))
    os.chmod(paths["office-ca.crt"], 0o644)
    write(paths["office-ca.key"], ca_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(ca_pass.encode())))
    write(paths["seal.p12"], pkcs12.serialize_key_and_certificates(
        name=b"nes-office-seal", key=seal_key, cert=seal, cas=[ca],
        encryption_algorithm=serialization.BestAvailableEncryption(seal_pass.encode())))

    return {
        "paths": paths,
        "ca_passphrase": ca_pass,
        "seal_passphrase": seal_pass,
        "root_fingerprint": fingerprint(ca),
        "seal_serial": format(seal.serial_number, "X"),
        "seal_expires": seal.not_valid_after_utc if hasattr(seal, "not_valid_after_utc") else seal.not_valid_after,
        "crl_url": crl_url(base_url),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Revocation list
# ─────────────────────────────────────────────────────────────────────────────

def revoked_serials() -> list[int]:
    raw = getattr(settings, "ESIGN_OFFICE_CA_REVOKED", "") or ""
    if isinstance(raw, (list, tuple)):
        raw = ",".join(raw)
    out = []
    for item in str(raw).replace(";", ",").split(","):
        item = item.strip().replace(":", "")
        if item:
            try:
                out.append(int(item, 16))
            except ValueError:
                logger.warning("ESIGN_OFFICE_CA_REVOKED: %r is not a hex serial", item)
    return out


def current_crl_der() -> bytes | None:
    """A fresh CRL signed by the office root, cached for an hour. None if unavailable."""
    cached = _CRL_CACHE.get("der")
    if cached and _CRL_CACHE.get("until", 0) > time.time():
        return cached
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization

        ca, key = load_ca_cert(), load_ca_key()
        now = datetime.datetime.now(datetime.timezone.utc)
        builder = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(ca.subject)
            .last_update(now - datetime.timedelta(minutes=5))
            .next_update(now + datetime.timedelta(days=CRL_DAYS))
            .add_extension(x509.CRLNumber(int(time.time())), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),
                           critical=False)
        )
        for serial in revoked_serials():
            builder = builder.add_revoked_certificate(
                x509.RevokedCertificateBuilder().serial_number(serial)
                .revocation_date(now - datetime.timedelta(minutes=5)).build())
        der = builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)
        _CRL_CACHE.update(der=der, until=time.time() + 3600)
        return der
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("NES office CA: could not issue a CRL")
        return None


def validation_material() -> tuple[list, list]:
    """(extra trust roots, CRLs) to hand to pyHanko. Empty when there is no office CA."""
    if not present():
        return [], []
    try:
        from asn1crypto import crl as asn1_crl
        from pyhanko.keys import load_certs_from_pemder

        roots = list(load_certs_from_pemder([ca_cert_path()]))
        der = current_crl_der()
        crls = [asn1_crl.CertificateList.load(der)] if der else []
        return roots, crls
    except Exception:
        logger.exception("NES office CA: could not load validation material")
        return [], []
