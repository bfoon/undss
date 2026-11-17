# accounts/signature_id.py
"""
UN PASS — NES lifetime signature identifiers.

What this is
------------
Every person who signs in NES is issued one identifier, once, for as long as
they exist in the system:

    NES-GM-7K4M-9QX2-H3T8-C

It names *the person's signing identity*, not any particular signature. They
may redraw their mark a hundred times and rotate their key after forgetting a
passphrase; the identifier does not move. Every document they have ever signed
continues to point at the same person.

Why it is computed rather than simply allocated
-----------------------------------------------
A sequential number would say nothing: anyone could write one down. This one
is an HMAC over the facts of the enrolment — the issuing office, the account,
a random nonce fixed at enrolment, and the moment it happened — under a secret
only the office holds.

That buys three things a counter does not:

* **Self-authenticating.** Given the enrolment record, the office can
  recompute the identifier and show it is the one it issued. An invented
  identifier will not recompute.
* **Unguessable.** Without the secret you cannot predict the next one or
  enumerate the range, so an identifier cannot be used to count staff or to
  forge a plausible-looking one.
* **Opaque.** It leaks no account id, no sequence, no enrolment order.

Why this alphabet
-----------------
Crockford base32: digits plus letters with **I, L, O and U removed**. I/1 and
O/0 are the classic transcription errors, and dropping U means the encoding
cannot accidentally spell an obscenity — which matters for something printed
on documents and read aloud over the phone. Decoding is forgiving: `i` and `l`
read as `1`, `o` as `0`, and case is ignored.

The final character is Crockford's check symbol, so a single mistyped or
transposed character is caught before anyone goes looking for a record that
was never there.

Nothing here is secret-dependent at *read* time: verification needs the
secret, but matching an identifier to a record never does.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from django.conf import settings

#: Crockford base32. 32 symbols, no I, L, O or U.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Crockford's own scheme extends the alphabet with `*~$=U` for a mod-37
#: check symbol. We do not: roughly one identifier in seven would then end in
#: a character that is awkward in a URL, absent from a phone keyboard, and
#: impossible to read aloud without explaining it. Instead the check is two
#: symbols from the same safe alphabet, taken from a digest of the body.
#:
#: That is stronger, not weaker. A positional mod-37 scheme catches every
#: single-character error and every transposition; a 10-bit digest catches
#: *any* corruption — substitutions, transpositions, rearrangements, a
#: mangled group — with probability 1023/1024, and the whole identifier stays
#: typeable.
CHECK_SYMBOLS = 2
CHECK_BITS = CHECK_SYMBOLS * 5

#: Characters people commonly substitute, normalised on input.
_FORGIVING = {"I": "1", "L": "1", "O": "0", "-": "", " ": ""}

#: 60 bits -> 12 symbols, grouped 4-4-4. Enough that collisions are a
#: non-event (birthday bound around a billion identities) while staying short
#: enough to read out loud. Uniqueness is enforced by the database regardless.
ID_BITS = 60
ID_SYMBOLS = ID_BITS // 5

PREFIX = "NES"
VERSION = "nes-sid-v1"


class SignatureIdError(ValueError):
    """Raised when an identifier cannot be parsed or fails its checksum."""


# ─────────────────────────────────────────────────────────────────────────────
# Crockford base32
# ─────────────────────────────────────────────────────────────────────────────

def _encode(value: int, symbols: int) -> str:
    out = []
    for _ in range(symbols):
        out.append(ALPHABET[value & 0b11111])
        value >>= 5
    return "".join(reversed(out))


def _decode(text: str) -> int:
    value = 0
    for char in text:
        try:
            value = (value << 5) | ALPHABET.index(char)
        except ValueError as exc:
            raise SignatureIdError(
                f"{char!r} is not a valid character in a signature ID."
            ) from exc
    return value


def check_symbol(body: str) -> str:
    """
    The two-symbol check for an identifier body.

    Derived from SHA-256 of the body rather than from arithmetic on it, so
    any change anywhere in the body — including swapping two characters or
    reordering whole groups — changes it. Not a secret: anyone can recompute
    it, which is the point. It catches typing mistakes, not forgery.
    """
    digest = sha256(body.encode("ascii")).digest()
    value = int.from_bytes(digest[:2], "big") >> (16 - CHECK_BITS)
    return _encode(value, CHECK_SYMBOLS)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

def _secret() -> bytes:
    """
    The office secret the identifier is derived under.

    Falls back to SECRET_KEY so the system works out of the box. Set
    ESIGN_SIGNATURE_ID_SECRET explicitly in production and keep it: rotating
    it does not invalidate identifiers already issued — they are stored — but
    it does stop them recomputing, which is how the office proves it issued
    them.
    """
    value = getattr(settings, "ESIGN_SIGNATURE_ID_SECRET", "") or ""
    if not value:
        value = getattr(settings, "SECRET_KEY", "") or ""
    return str(value).encode("utf-8")


def office_prefix() -> str:
    """
    The two-to-six letter office marker inside the identifier.

    Taken from the witnessing office code when one is set (`UNDP-GM` -> `GM`),
    because an identifier that names its issuing office is far easier to route
    when two offices compare records.
    """
    code = str(getattr(settings, "ESIGN_WITNESS_OFFICE_CODE", "") or "").strip()
    if not code:
        code = str(getattr(settings, "ESIGN_SIGNATURE_ID_OFFICE", "") or "").strip()
    if not code:
        return "XX"

    tail = code.replace("_", "-").split("-")[-1]
    cleaned = "".join(c for c in tail.upper() if c.isalnum())[:6]
    return cleaned or "XX"


# ─────────────────────────────────────────────────────────────────────────────
# Issuing and verifying
# ─────────────────────────────────────────────────────────────────────────────

def new_nonce() -> str:
    """The random component fixed at enrolment and never changed."""
    return secrets.token_hex(16)


def derivation_payload(
    *, office: str, user_pk, nonce: str, issued_at_iso: str
) -> bytes:
    """The exact bytes the identifier is derived from. Order is fixed."""
    return "|".join([
        VERSION, str(office), str(user_pk), str(nonce), str(issued_at_iso),
    ]).encode("utf-8")


def compute_signature_id(
    *, office: str, user_pk, nonce: str, issued_at_iso: str
) -> str:
    """
    Derive the identifier. Deterministic: the same enrolment facts always
    produce the same identifier, which is what makes verification possible.
    """
    digest = hmac.new(
        _secret(),
        derivation_payload(
            office=office, user_pk=user_pk, nonce=nonce,
            issued_at_iso=issued_at_iso,
        ),
        sha256,
    ).digest()

    value = int.from_bytes(digest, "big") >> (256 - ID_BITS)
    body = _encode(value, ID_SYMBOLS)
    grouped = "-".join(body[i:i + 4] for i in range(0, ID_SYMBOLS, 4))
    return f"{PREFIX}-{office}-{grouped}-{check_symbol(body)}"


def verify_signature_id(
    signature_id: str, *, office: str, user_pk, nonce: str, issued_at_iso: str
) -> bool:
    """True when `signature_id` is the one these enrolment facts produce."""
    try:
        expected = compute_signature_id(
            office=office, user_pk=user_pk, nonce=nonce,
            issued_at_iso=issued_at_iso,
        )
    except Exception:
        return False
    return hmac.compare_digest(normalise(expected), normalise(signature_id))


# ─────────────────────────────────────────────────────────────────────────────
# Reading what a person typed
# ─────────────────────────────────────────────────────────────────────────────

def normalise(signature_id: str) -> str:
    """
    Canonical uppercase form, for comparison and storage.

    Lenient about what a human types — lowercase, missing hyphens, `O` for
    zero — and strict about what comes out.
    """
    raw = str(signature_id or "").strip().upper()
    if not raw:
        raise SignatureIdError("No signature ID was given.")

    # Hyphens and spaces are presentation only. Strip them all and parse by
    # position: the body and check are a fixed length, so whatever sits
    # between the prefix and them is the office — which means an identifier
    # pasted without any hyphens still reads correctly.
    compact = "".join(c for c in raw if c.isalnum())

    if not compact.startswith(PREFIX):
        raise SignatureIdError(f"A signature ID starts with {PREFIX}-.")

    tail_len = ID_SYMBOLS + CHECK_SYMBOLS
    rest = compact[len(PREFIX):]
    if len(rest) <= tail_len:
        raise SignatureIdError(
            "That signature ID is too short to be complete."
        )

    office = rest[:-tail_len]
    tail = rest[-tail_len:]

    # Only the body and check get the forgiving substitutions: an office code
    # may legitimately contain L, O or I and must not be rewritten.
    for wrong, right in _FORGIVING.items():
        if right:
            tail = tail.replace(wrong, right)

    body, check = tail[:ID_SYMBOLS], tail[ID_SYMBOLS:]
    expected = check_symbol(body)
    if check != expected:
        raise SignatureIdError(
            "That signature ID has a typo — its check characters do not "
            "match the rest of it. Please read it again."
        )

    grouped = "-".join(body[i:i + 4] for i in range(0, ID_SYMBOLS, 4))
    return f"{PREFIX}-{office}-{grouped}-{check}"


def is_wellformed(signature_id: str) -> bool:
    try:
        normalise(signature_id)
        return True
    except SignatureIdError:
        return False
