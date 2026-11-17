# NES — standards conformance and the UN/UNDP profile

How the NES seal, audit chain and evidence package map onto the standards a
UN or UNDP office is actually assessed against, what the deployment must do to
satisfy them, and — stated plainly — what this does **not** yet give you.

Read the last section before telling anyone this is "UNDP compliant". Some of
what compliance needs is configuration and custody, not code, and one
requirement cannot be met by an organisational seal at all.

---

## 1. What UNDP's own policy requires

UNDP's **Electronic Signatures Policy** sits in the POPP under Information and
Communications Technology → ICT Security. Its definitions, not generic
e-signature marketing, are what an auditor will hold this against.

**Authenticity.** The policy defines an electronic signature as authentic if it
can be proved that the signature was not modified, altered or otherwise
compromised after it was placed, and establishes authenticity through two
parameters:

> i. Association of the electronic signature with a signatory
> ii. Indication of the date and time of the electronic signature

**Retention.** Where a critical electronic record carries an electronic
signature, the policy requires the record to be retained together with its
signature *and any other associated records necessary to prove the
authenticity of that signature* — the digital certificate among them.

**What does not count.** An email may indicate approval under some business
processes but is not an electronic signature; and **copied-and-pasted images of
a signature are explicitly not electronic signatures** under the policy.

**Sole control.** The policy's signatory is a person who can be uniquely
identified and linked to the signature and *who has sole control over the
authentication key*. It states that an electronic signature is personal and
cannot be shared.

**Who signs off.** The POPP procedures *Electronic Signatures Solution
(Internal)* and *(External)* make the **Operations Manager** the responsible
party for mapping users, checking a solution against the policy's authenticity
and integrity requirements, comparing it with the corporate and regional LTAs
for e-signatures and the list of existing corporate tools, confirming it suits
the UNDP IT environment, and keeping it current. DocuSign and Quantum appear on
that corporate list. **That assessment is a procedural step no code can
perform** — it has to be run for NES by the Operations Manager.

Sources: POPP Electronic Signatures Policy glossary
(`popp.undp.org/glossary/policy-page-title/Electronic Signatures Policy`),
POPP procedures *Electronic Signatures Solution (Internal)* and *(External)*,
*List of UNDP Systems and Tools for Electronic Records and Electronic
Approvals*, and *Use of ICT Resources and Data* (ST/SGB/2004/15). The policy
carried a planned review date of December 2022, so confirm you are reading the
current text before citing it.

### How NES answers each one

| Policy requirement | How NES meets it | Where |
|---|---|---|
| Signature not modified after placing | PAdES signature over the entire byte range; any change invalidates it | `pades_esign.seal_pdf` |
| (i) association with a signatory | A lifetime signature ID plus an ECDSA signature made with the signatory's own key, over a statement naming the envelope and the document hash | `identity_esign`, `signature_id` |
| (ii) date and time of signature | Event timestamps, **provable only with an RFC 3161 timestamp** — see §4 | `ESIGN_PADES_TSA_URL` |
| **Sole control over the authentication key** | Keypair generated in the signatory's browser; private key encrypted there under their passphrase; this system holds only a public key and an opaque blob | `docs/nes-signing-identity.md` |
| Signature is personal and cannot be shared | One identity per person (`OneToOneField`); only the owner may change the key or mark — no administrative override exists | `identity_esign.require_owner` |
| Not a pasted signature image | The image is presentation only; the evidential weight is the key and the attestation | §7 caveat 1 |
| Retain record + signature + associated records | ASiC-E evidence package: document, certificate of completion, certificate chain, revocation data, audit trail, anchor, and each signatory's public key and signature | `evidence_esign.envelope_evidence` |
| Integrity of the record over time | Document timestamp chain, renewed on a schedule | `pades_esign.refresh_ltv` |

---

## 2. Technical standards implemented

| Standard | What it covers | Status |
|---|---|---|
| **ETSI EN 319 142-1/-2** | PAdES baseline profile — PDF Advanced Electronic Signatures | Implemented: B-B, B-T, B-LT, B-LTA |
| **ISO 14533-3** | Long-term signature profiles for PAdES (the ISO publication of the same profile) | Implemented |
| **ETSI EN 319 122-1** | CAdES signed attributes | `commitment-type-indication`, `signature-policy-identifier` |
| **ETSI EN 319 162-1** | ASiC container formats | Evidence package, with the caveat in §7 |
| **ETSI EN 319 102-1** | Procedures for AdES validation | Validation follows it; report is our own format, not TS 119 102-2 |
| **RFC 3161** | Time-Stamp Protocol | Signature timestamps and document timestamps |
| **RFC 5652** | Cryptographic Message Syntax | Seal and the audit-trail anchor signature |
| **RFC 5280 / RFC 6960** | X.509 paths, CRL, OCSP | Collected at signing, embedded in the DSS at B-LT |
| **ISO 32000** | PDF | Incremental-update signing; the original bytes are never rewritten |
| **FIPS 180-4** | SHA-256 | Document digests, audit chain, manifests, register |

### Conformance levels, and what each actually buys

| Level | Adds | Needs | Honest expectation |
|---|---|---|---|
| `b-b` | Signature + certificate | nothing | Tamper-evident. **No proof of signing time** — fails POPP authenticity parameter (ii). |
| `b-t` | RFC 3161 signature timestamp | a TSA | Signing time proven. Validation gets harder once the certificate expires. |
| `b-lt` | Embedded revocation data | TSA + reachable OCSP/CRL | **Minimum for a UN record.** Verifies after the certificate expires. |
| `b-lta` | Archival document timestamp | as above, renewed | Indefinite, as long as it is refreshed. |

NES steps **down** rather than failing, and records the level it actually
reached — so a seal never claims a level it did not achieve.

---

## 3. The UN/UNDP settings profile

```python
# ── NES — UN/UNDP profile ────────────────────────────────────────────
ESIGN_BRAND      = "NES"
ESIGN_BRAND_FULL = "NES — Not eSign"

# Sealing
ESIGN_PADES_ENABLED = True
ESIGN_PADES_PKCS12 = "/etc/unpass/nes-seal.p12"      # mode 0600, root-owned
ESIGN_PADES_PKCS12_PASSPHRASE = os.environ["NES_SEAL_PASSPHRASE"]
ESIGN_PADES_CHAIN = ["/etc/unpass/issuing-ca.crt"]
ESIGN_PADES_LEVEL = "b-lt"                            # not negotiable for records
ESIGN_PADES_LOCATION = "Banjul, The Gambia"
ESIGN_PADES_SEAL_CERTIFICATE = True

# Proof of signing time — required by POPP authenticity parameter (ii)
ESIGN_PADES_TSA_URL = "http://<your-tsa>/tsa"
ESIGN_PADES_TRUST_ROOTS = ["/etc/unpass/root-ca.crt"]

# What the seal asserts. proof_of_origin, NOT proof_of_approval: the
# approvals belong to the people who signed inside the document.
ESIGN_PADES_COMMITMENT = "proof_of_origin"

# The published rules the seal is issued under. Register the OID under your
# organisation's own arc; never invent one in a namespace you do not control.
ESIGN_PADES_POLICY_OID = "<your registered OID>"
ESIGN_PADES_POLICY_URI = "https://<office>/policies/nes-signature-policy-v1.pdf"
ESIGN_PADES_POLICY_DOC = "/etc/unpass/nes-signature-policy-v1.pdf"

# Long-term validity
ESIGN_PADES_REFRESH_DAYS = 180

# Tamper-evident audit trail
ESIGN_AUDIT_CHAIN = True
```

Both the OID **and** a readable policy document are required, or the policy
attribute is omitted entirely — an OID bound to a fabricated digest would be
worse than no attribute.

---

## 4. A timestamp authority is not optional

This is the single most important configuration decision, so it gets its own
section.

Without `ESIGN_PADES_TSA_URL`, the seal is capped at **B-B**. The document is
tamper-evident, but the only evidence of *when* it was signed is the server's
own clock, asserted by the same system that holds the signing key. That does
not satisfy POPP authenticity parameter (ii), and it is the first thing a
serious challenge will attack.

An RFC 3161 timestamp is issued by a third party that does not hold your key
and cannot be induced to backdate by someone with database access. It is what
turns "our system says 14:32" into a provable fact.

Options, in order of preference:

1. A UN-operated or Unite-provided TSA, if one is available to your office.
2. A commercial TSA from a CA your partners already trust.
3. A national TSA, where The Gambia or a partner state operates one.

Verify it reaches B-LT once configured:

```bash
docker compose exec web python manage.py esign_pades --check
```

---

## 5. Operating requirements

Code alone does not make this trustworthy. These are the parts that live
outside the repository.

**Key custody.** The seal key is the whole system's trust anchor. Anyone who
holds it can produce documents indistinguishable from authentic ones *and*
re-anchor a rewritten audit trail. Keep it off the application server — an HSM,
a cloud KMS, or at minimum a separate host with the passphrase supplied from
the environment and never in the repository. If the key lives beside the
database, the audit chain protects you from a careless administrator, not a
determined one. The external timestamp is what still bites in that case,
because a re-anchored chain cannot be backdated.

**Certificate.** Issued to the organisation, not a person — this is an
organisational seal (see §7). Use a CA your counterparties already trust;
self-signed seals detect tampering but no reader will show them as trusted.
Watch expiry: `--check` warns inside 30 days.

**Refresh on a schedule.** Archival material lapses silently. Run monthly:

```cron
17 2 1 * *  cd /srv/unpass && docker compose exec -T web \
            python manage.py esign_pades --refresh-ltv >> /var/log/nes-refresh.log 2>&1
```

A lapsed timestamp chain **cannot be repaired after the fact**. This cron entry
is the difference between "verifiable" and "was verifiable".

**Publish the register.** Monthly, and put it somewhere that outlives the
system — the records registry, a git repository, print it into the file:

```bash
docker compose exec web python manage.py esign_pades --register 31 \
    --out /srv/records/nes-register-$(date +%Y%m).pdf
```

A register entry proves a document existed in exactly that form by that date,
*independently of that document's own certificate chain*. It is the only
mechanism here that survives total loss of the PKI.

**Hand over the evidence package, not the PDF**, whenever a document leaves for
audit, arbitration or the registry. It is what the POPP retention requirement
describes: the record plus its signature plus the associated records needed to
prove authenticity.

---

## 6. Answering an auditor

| Question | Answer | How to show it |
|---|---|---|
| Has this document been altered? | No | Acrobat signature panel, or the Verify seal page |
| Who signed, and when? | Audit trail, hash-linked | `evidence/audit-trail.json` |
| Has the trail been edited? | Detectable per entry | `--verify-chain <id>` |
| Were entries deleted from the end? | Detectable via the signed anchor | `--verify-chain <id>` |
| Can you prove this without your system? | Yes | Evidence package + `VERIFY.txt` |
| Will it still verify in 2040? | Yes, if refreshed | `--refresh-ltv` cron + register |
| Under what rules was it sealed? | Declared in the signature | `policy_oid` on the seal record |

---

## 7. What this does NOT give you

Read this section before claiming conformance anywhere.

**1. The seal is organisational; sole control is a separate mechanism — and it
now exists.** An earlier version of this document said closing the
sole-control gap needed a procurement and identity programme. That was too
pessimistic, and it has since been implemented: see
`docs/nes-signing-identity.md`.

Two distinct things now sit on a completed envelope, and they must not be
conflated:

* **The organisational seal** (PAdES) still asserts `proof_of_origin` — *the
  office attests this is the authentic completed record*. It is applied
  server-side and is deliberately **not** `proof_of_approval`. Never describe
  it as any individual's signature.
* **Each signatory's own signature** is an ECDSA P-256 signature made in their
  browser with a private key encrypted under a passphrase this system has
  never seen and cannot derive. That satisfies the policy's sole-control
  requirement: no administrator, superuser or database operator can produce
  it, and a forgotten passphrase is unrecoverable by anyone.

Each person also holds one lifetime signature ID (`NES-GM-2BE9-W79Q-MTVY-TC`)
which does not change when they redraw their mark or rotate their key, so the
same ID across years means the same person.

The residual limitation is narrower than before and worth stating precisely:
the crypto runs in JavaScript served by this host, so a server that chose to
serve modified JavaScript could capture a passphrase as it is typed.
Defending against your own server needs a smartcard or an external signing
device. What this does rule out — and previously did not — is a stolen
database, a backup tape, a curious administrator, a host compromised after the
fact, and an administrator applying somebody's saved signature image.

Where a signatory has no UN PASS account, nothing changes for them: their
signature rests on the audit trail and the organisational seal, and the
certificate of completion says so by name rather than leaving it to be
assumed.

**2. Advanced, not qualified.** In eIDAS terms this is an *advanced* electronic
seal. *Qualified* requires a qualified certificate from a QTSP on an EU trusted
list and a qualified signature creation device. Relevant only if you deal with
EU counterparties who ask for QES.

**3. The ASiC-E caveat.** The evidence package follows the ASiC-E container
conventions — `mimetype` first and uncompressed, `META-INF/ASiCManifest.xml` —
but in a strict ASiC-E container the signature is a detached object under
`META-INF/`. Here the authoritative signature is embedded in the PDF per
PAdES, which is the right place for it: it travels with the document when
someone pulls the PDF out of the ZIP. Validators expecting a detached
container signature will not validate the ZIP itself; they will validate the
PDF. Describe it as "an ASiC-E container carrying PAdES-signed payloads", not
as "ASiC-E conformant".

**4. No PDF/A.** Documents are not converted to PDF/A (ISO 19005) before
sealing, so they are not archival-format conformant even though they are
cryptographically archival. If your retention schedule calls for PDF/A — most
UN records schedules do for permanent records — that conversion needs adding
ahead of sealing, and validating with veraPDF. Doing it *after* sealing would
break the signature.

**5. Validation reports are our own format**, not ETSI TS 119 102-2 signature
validation reports. Fine for people; a counterparty's automated validator may
want the standard XML.

**6. Tamper-evident, not tamper-proof.** The distinction is the whole point.
Someone holding both the database and the signing key could rebuild a
consistent chain and re-anchor it. What they cannot do is backdate an external
timestamp. Key custody (§5) and a real TSA (§4) are what make the evidence
stand up — not the code in this repository.

**7. The POPP assessment still has to be done.** The Operations Manager must
compare NES against the Electronic Signatures Policy, the corporate and
regional LTAs, and the list of corporate tools, and confirm it suits the UNDP
IT environment. This document is input to that assessment, not a substitute
for it.

---

## 8. Commands

```bash
# configuration + end-to-end self-test, including a tamper check
python manage.py esign_pades --check

# a self-signed seal for a test environment
python manage.py esign_pades --make-dev-cert /etc/unpass/nes-dev-seal.p12

# seal an envelope completed before sealing was enabled
python manage.py esign_pades --seal-envelope  <ENVELOPE_ID>
python manage.py esign_pades --verify-envelope <ENVELOPE_ID>

# audit trail and its anchor
python manage.py esign_pades --verify-chain <ENVELOPE_ID>

# archival refresh — everything due, or one envelope
python manage.py esign_pades --refresh-ltv
python manage.py esign_pades --refresh-ltv <ENVELOPE_ID> --force

# offline evidence package
python manage.py esign_pades --evidence <ENVELOPE_ID> --out pkg.asice.zip

# monthly seal register
python manage.py esign_pades --register 31 --out register.pdf
```
