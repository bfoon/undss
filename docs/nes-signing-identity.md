# NES signing identities and sole control

What this adds, why it is built this way, and what it still does not promise.

---

## The problem it solves

UNDP's Electronic Signatures Policy requires that a signatory be a person who
can be uniquely identified and linked to the signature, and **who has sole
control over the authentication key**. It says an electronic signature is
personal and cannot be shared, and it states outright that a copied-and-pasted
image of a signature is not an electronic signature.

NES used to store a picture. A picture fails all of that: the server held it,
an administrator could apply it, and nothing about it was under the
signatory's control. The PAdES seal did not fix it either — that is the
*organisation* attesting to the record, which is exactly why its commitment
type is `proof_of_origin` and not `proof_of_approval`.

So each person now has a signing key that **this system cannot use**.

---

## How sole control actually works

Four facts, in order:

1. **The keypair is born in the browser.** `crypto.subtle.generateKey` makes
   an ECDSA P-256 keypair on the signatory's own device. The private key is
   never transmitted in usable form.

2. **It is encrypted before it leaves.** The browser derives an AES-256 key
   from the signatory's passphrase (PBKDF2-HMAC-SHA256, 600,000 rounds) and
   encrypts the private key with AES-GCM. Only the ciphertext is uploaded.

3. **The server stores nothing it can use.** A public key, an opaque blob, and
   the salt and IV the *browser* needs to re-derive the unwrapping key. No
   passphrase, no private key, no recovery path. Not for an administrator, not
   for a superuser, not for whoever takes a backup.

4. **Signing happens in the browser.** The server issues a challenge naming
   the signature ID, the envelope and the SHA-256 of the documents as the
   signatory saw them. The browser asks for the passphrase, decrypts the key,
   signs the challenge, and sends back only the signature. **The passphrase
   never reaches the server.**

```
     browser                                        server
        |                                              |
        |  <----------- challenge (statement + nonce)  |
        |                                              |
  [passphrase typed]                                   |
  [key decrypted locally]                              |
  [statement signed]                                   |
        |                                              |
        |  signature (64 bytes) ---------------------> |
        |                            [verified against |
        |                             the public key]  |
```

### Verified, not asserted

The browser side was driven through Node's WebCrypto — the same SubtleCrypto
API — against the shipped `identity.js`, and the output verified by the
shipped Python. Both directions, including the failure cases: a wrong
passphrase, a changed document hash, a swapped envelope, a swapped signature
ID, another person's key, a malformed signature, and a non-P-256 key at
enrolment. The stored blob was then attacked directly with seven passphrase
guesses and did not open.

---

## The lifetime signature ID

```
NES-GM-2BE9-W79Q-MTVY-TC
 |   |   \_______________/  \
 |   |         body        check
 |  office
system
```

**One per person, once, for as long as the account exists.** It names the
*person's signing identity*, not a mark or a key. Redraw your signature twenty
times, rotate your key after forgetting a passphrase — the ID does not move.
Two documents five years apart carrying the same ID were signed by the same
person.

### Why it is computed rather than counted

It is an HMAC-SHA256 over the enrolment facts — office, account, a random
nonce fixed at enrolment, the moment it happened — under a secret only the
office holds. A sequential number would prove nothing; anyone could write one
down. This one is:

* **Self-authenticating** — the office can recompute it from the enrolment
  record and show it issued it. `esign_identity --verify-ids` does this across
  every identity.
* **Unguessable** — you cannot predict the next one or enumerate the range, so
  it cannot be used to count staff or to forge a plausible one.
* **Opaque** — no account id, no sequence, no enrolment order.

### Why this alphabet

Crockford base32: digits and letters with **I, L, O and U removed**. I/1 and
O/0 are the classic transcription errors; dropping U means it cannot
accidentally spell something unfortunate on a document read aloud over the
phone. Input is forgiving — lowercase, missing hyphens, `O` typed for zero all
read correctly — and output is always canonical.

The last two characters are a check derived from a digest of the body, so a
typo is caught before anyone hunts for a record that never existed.
**Measured: 19,676 random single-character substitutions and transpositions,
100% caught.** Over 200,000 synthetic identities, no collisions.

Crockford's own scheme uses `* ~ $ = U` as check symbols; this does not,
because roughly one ID in seven would then end in a character that is awkward
in a URL, missing from a phone keyboard and impossible to dictate.

### Immutable, and meant

Three layers, because the first two are not enough:

| Layer | Stops | Does not stop |
|---|---|---|
| `editable=False` | the admin form, `ModelForm` | code |
| `save()` guard | `.save()`, model code | `QuerySet.update()`, raw SQL |
| **Postgres trigger** | **everything, including raw SQL** | — |

Install the third once, after migrating:

```bash
docker compose exec web python manage.py esign_identity --install-guard
```

Tested against a real PostgreSQL 16: `QuerySet.update()`, raw
`UPDATE … SET signature_id`, and raw changes to `enrollment_nonce` and
`user_id` were all rejected by the database. Unrelated columns and legitimate
status changes still work.

On SQLite the command explains that model-level protection still applies and
changes nothing. Production is PostgreSQL — run it there.

---

## One signature per person, changeable at will

| Rule | How |
|---|---|
| One identity per person | `OneToOneField` — the database refuses a second |
| The mark can change any time | New `SignatureMark` revision; the ID does not move |
| The key can change any time | New `SignatureKey` revision; the ID does not move |
| Nothing is overwritten | Old revisions retained and marked not-current |
| Nothing is deleted | `delete()` raises on identities, keys, marks, attestations |

**Why keep the history?** A document signed in 2026 must still verify in 2032
against the key that was current in 2026, and the certificate of completion
must be able to show the mark that was actually applied. Overwriting would
quietly invalidate the past. Tested: after rotating to a new key, the earlier
signature still verifies against the retired key, and the new key does *not*
verify it.

---

## Only the owner — enforced, not conventional

`require_owner()` compares the acting user to the identity's owner and refuses
everything else. It deliberately ignores `is_staff` and `is_superuser`.

There is no `?user=` parameter, no pk in any identity URL, and no
administrative override anywhere — not because one would be hard to write, but
because the moment it exists somebody is granted it and the signature stops
being the signatory's.

Tested: a superuser and an ordinary second user were both refused, in Python
and over HTTP. Each user's `material` endpoint returns only their own key.

**What an administrator *can* do**, legitimately, from the admin:

* See that an identity exists, its ID, its fingerprint, how often it is used.
* **Suspend** it — offboarding, suspected compromise. This stops the person
  signing. It does not let anyone sign as them and does not release the key.

**What no one can do:** change someone's key or mark, recover a passphrase, or
produce a signature on their behalf. The material to do it is not in the
system.

---

## Enrolling

The signatory goes to **My signature** in the NES menu
(`/accounts/esign/signature/`), chooses a passphrase, and the page does the
rest. Then they draw or type their mark.

Say this out loud when rolling it out, because people will not read it:

> **There is no password reset for your signing passphrase.** If you forget
> it, nobody here can recover it — that is what stops anyone signing as you.
> You can create a new key and your signature ID stays the same; documents you
> already signed stay signed with the old key and remain valid.

The page offers a **Check my passphrase** button, which confirms locally that
the passphrase still unlocks the key without sending anything. Worth
encouraging now and then.

---

## Turning on enforcement

Leave it off while people enrol:

```python
ESIGN_REQUIRE_ATTESTATION = False      # then True once people have enrolled
```

With it on, somebody who **has** an active identity cannot sign with just an
image — they must use their key. External signers with no account are never
blocked by it; they cannot have an identity, and blocking them would make
outside envelopes unsignable.

Either way the audit trail records `attested` or `attest_failed`, and the
certificate of completion lists which signatories used a personal key and
names anyone who did not. **The weaker form is never silently mistaken for the
stronger one.**

---

## Where it shows up

| Place | What appears |
|---|---|
| Certificate of Completion | A *Personal signatures (sole control)* table: ID, signatory, key revision, verified, timestamp — and a line naming anyone who signed without one |
| Verify seal page | The same, re-verified live; a failed attestation turns the overall verdict red |
| Evidence package | `evidence/personal-signatures.json` with the public key, statement and signature, plus step 5 of `VERIFY.txt` showing how to check it offline |
| Audit trail | `attested` / `attest_failed` events with the ID and key fingerprint |
| Admin | Read-only identities, keys, marks and attestations |

The evidence package entry is the important one: it carries everything needed
to verify a person's signature with `python` and `cryptography` alone, years
later, with this system gone.

---

## Commands

```bash
# how enrolment is going, and whether the guard is installed
python manage.py esign_identity --status

# the database-level immutability guard (PostgreSQL) — run once
python manage.py esign_identity --install-guard

# recompute every ID from its enrolment record
python manage.py esign_identity --verify-ids

# re-verify every stored signature from scratch
python manage.py esign_identity --audit

# everything on file for one identity
python manage.py esign_identity --show NES-GM-2BE9-W79Q-MTVY-TC
```

None of these can change a key or a mark. That is not an omission.

Put the audit in cron beside the others:

```cron
53 4 * * *  cd /srv/unpass && docker compose exec -T web \
            python manage.py esign_identity --audit >> /var/log/nes-identity.log 2>&1
```

---

## What this still does not promise

**1. It trusts the page it is served on.** The crypto runs in JavaScript this
server sent. A server that chose to serve modified JavaScript could capture a
passphrase as it is typed. Defending against your own server needs a smartcard
or an external signing device.

This is the strongest arrangement available in a plain browser, and it already
rules out the far more likely threats: a stolen database, a backup tape, a
curious administrator, a compromised host *after the fact*, and an
administrator applying somebody's saved image. Previously **all** of those
worked.

**2. Memory cannot be wiped.** JavaScript strings are immutable, so the
passphrase cannot be zeroised. It is held in one local variable for the
duration and never stored, logged or placed in a form field — but "zeroise" is
not something the platform can honestly offer.

**3. Still an advanced, not a qualified, signature.** eIDAS *qualified* needs a
qualified certificate from a QTSP and a qualified signature creation device.
Relevant only with EU counterparties who ask for QES.

**4. Identity assurance is as good as your account provisioning.** The key
proves *the same person who enrolled* signed. That the enrolling person was
who they claimed is a question about how UN PASS accounts are issued, not
about this cryptography. Microsoft SSO would strengthen the link.

**5. External signers get the old guarantees.** Someone with no account cannot
have an identity. Their signature rests on the audit trail, the envelope token
and the organisational seal — the position everyone was in before. The
certificate says so explicitly.

**6. There is genuinely no recovery.** Worth stating twice. A lost passphrase
means a new key. If that is unacceptable for a particular role, the answer is
an HSM-backed central key with its own custody arrangements — which is
explicitly *not* sole control, and should not be called it.

---

## Settings

```python
# Secret the lifetime ID is derived under. Set it, then leave it alone.
# Rotating it does not invalidate issued IDs — they are stored — but it does
# stop them recomputing, which is how the office proves it issued them.
ESIGN_SIGNATURE_ID_SECRET = "<a long random string, kept out of the repo>"

# Refuse an image-only signature from someone who HAS an active identity.
# Off while people enrol; on afterwards.
ESIGN_REQUIRE_ATTESTATION = False
```

The office prefix inside the ID comes from `ESIGN_WITNESS_OFFICE_CODE`
(`UNDP-GM` → `GM`), or `ESIGN_SIGNATURE_ID_OFFICE` if witnessing is not in
use.
