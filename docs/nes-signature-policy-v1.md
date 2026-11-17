# NES Signature Policy, version 1

**DRAFT FOR REVIEW — this is a template, not an approved policy.**

Before publishing: replace every `[bracketed]` placeholder, delete this
notice, have it reviewed by the Operations Manager and whoever owns records
management, and publish it as a PDF at a stable URL. Then point
`ESIGN_PADES_POLICY_DOC` at the exact published file.

Where this draft describes something you do not actually do, **change the
draft, not your practice**. A policy that overstates its controls is worse
than one that admits a gap: the overstatement is what gets found.

---

| | |
|---|---|
| **Policy name** | NES Signature Policy |
| **Version** | 1 |
| **Object identifier** | `[1.3.6.1.4.1.NNNNN.1.1]` |
| **Published at** | `[https://unpass.gm/policies/nes-signature-policy-v1.pdf]` |
| **Issuing office** | `[UNDP The Gambia / Easy Solutions, Banjul]` |
| **Effective from** | `[date]` |
| **Approved by** | `[name, Operations Manager]` |
| **Review due** | `[date — annually is reasonable]` |
| **Supersedes** | None |

---

## 1. Purpose and scope

This policy states the rules under which the NES module of UN PASS applies an
organisational electronic seal to completed signature envelopes, and what that
seal does and does not assert.

It applies to every document sealed by the NES module operated by
`[issuing office]`. It does not apply to documents signed outside NES, nor to
electronic approvals recorded in other systems.

Every seal carries this policy's object identifier, bound to a cryptographic
hash of the published policy document. A verifier can therefore establish not
only that a document is authentic, but which exact version of these rules was
in force when it was sealed.

## 2. What the seal asserts

The NES seal is an **organisational seal**. Its declared commitment type is
`proof_of_origin` (ETSI EN 319 122-1). It asserts:

> `[Issuing office]` attests that this document is the authentic, complete
> record of the signature envelope identified within it, as that envelope
> stood when all required signatures had been collected, and that the document
> has not been altered since.

## 3. What the seal does NOT assert

This section is as important as the one above and must not be removed.

**The seal is not any individual's signature.** It does not represent the
approval, consent or intent of any person. The signatures of the individuals
who signed the document are recorded *within* the document and in its audit
trail; the seal attests to the integrity and origin of that record, not to the
acts recorded in it.

**The seal does not assert sole control by a signatory.** The seal key is held
by the issuing office and applied automatically by the NES system on the
office's behalf. It is not under the sole control of any signatory, and no
party should treat it as equivalent to a personal signing key.

**The seal does not revalidate the signatories' identities.** Identity
assurance for each signatory is as recorded in the audit trail — see §5.

## 4. Cryptographic controls

| Control | Value |
|---|---|
| Signature format | PAdES, ETSI EN 319 142-1 (also ISO 14533-3) |
| Target conformance | `[B-LT]` — long-term, with embedded validation data |
| Digest algorithm | SHA-256 |
| Signature algorithm | `[RSA 3072 / as issued]` |
| Signing certificate | Issued to `[organisation]` by `[CA]` |
| Timestamping | RFC 3161, from `[TSA name and URL]` |
| Archival maintenance | Document timestamps renewed at least every `[180]` days |

The seal is applied as an **invisible** signature field. Each signatory's
visible mark is already rendered on the page by NES; a second visible widget
would obscure it. Conformant readers show the seal in their signature panel.

Where a timestamp authority or revocation responder is unreachable, the system
applies the highest conformance level it can reach and **records the level
actually achieved**. A seal never claims a level it did not attain.

## 5. Evidence retained with each document

For each sealed document the issuing office retains:

1. The sealed PDF, carrying the embedded signature.
2. The Certificate of Completion, itself sealed.
3. The audit trail: every action on the envelope, with actor, timestamp,
   network address and user agent. Each entry is hash-linked to the previous
   one, so alteration, deletion, insertion and reordering are detectable.
4. A signature over the audit trail's head hash and entry count, so entries
   removed from the end of the trail are also detectable.
5. The signing certificate chain and the revocation data current at sealing.

These are released together as a single ASiC-E evidence package, which
verifies with `sha256sum` and `openssl` alone and requires no access to UN
PASS. This is intended to satisfy the UNDP Electronic Signatures Policy
requirement that a critical electronic record be retained together with its
signature and the associated records necessary to prove that signature's
authenticity.

## 6. Distribution of fingerprints

`[Delete this section if cross-office witnessing is not in use.]`

Fingerprints of each sealed document — the document hash, the audit-chain
head and the sealing time — are distributed to participating country offices
and held in their own systems, so that alteration at the issuing office can be
demonstrated against independently held copies.

Only fingerprints are distributed. No document content, signature image, field
value or recipient address leaves the issuing office through this mechanism.

Participating offices as at this version: `[UNDP-SN, …]`

## 7. Periodic seal register

`[Delete if not in use.]`

At least `[monthly]`, the issuing office publishes a register of every document
sealed in the period, listing each document's SHA-256 with a Merkle root over
the entries. The register is itself sealed and timestamped and is retained at
`[location]`. A register entry establishes that a document existed in exactly
that form by that date, independently of that document's own certificate
chain.

## 8. Key management

| | |
|---|---|
| Key custody | `[HSM / KMS / dedicated host — state what is true]` |
| Passphrase handling | Supplied from the environment; never stored in source control |
| Administrators with access | `[roles, not names]` |
| Rotation | `[on expiry / every N years]` |
| Compromise procedure | `[who is told, what is suspended, how documents already sealed are re-attested]` |

If key custody is weaker than you would like, say so here and record the
compensating controls (timestamping, cross-office witnessing, the register).
A stated limitation with compensating controls is defensible. A false claim is
not.

## 9. Validity period of a seal

A seal remains verifiable for as long as its validation material can be
assembled:

* At **B-B**, until the signing certificate expires, after which validation
  becomes difficult.
* At **B-T**, the signing time remains provable indefinitely.
* At **B-LT**, validation remains possible after certificate expiry, because
  revocation data is embedded.
* At **B-LTA** with scheduled refresh, indefinitely.

The issuing office undertakes to refresh archival timestamps on the schedule
in §4. A lapsed timestamp chain cannot be repaired retroactively.

## 10. Obligations of a verifying party

A party relying on a sealed document should:

1. Verify the embedded signature with a conformant reader, or the supplied
   evidence package.
2. Confirm the document's SHA-256 matches the value recorded in the evidence
   package, which also detects content appended after the seal.
3. Re-walk the audit trail's hash chain if the sequence of acts is material.
4. Treat the seal as evidence of the record's integrity and origin, and the
   audit trail as the evidence of who acted and when.

## 11. Limitations

1. This is an **advanced** electronic seal, not a qualified one in the sense
   of Regulation (EU) 910/2014.
2. The seal attests to organisational origin, not to individual sole control
   (§3).
3. Documents are not converted to PDF/A (ISO 19005) before sealing. They are
   cryptographically archival but not archival-format conformant.
   `[State your plan or accept the gap.]`
4. The evidence package follows ASiC-E container conventions; the
   authoritative signature is embedded in the PDF rather than detached under
   `META-INF/`.
5. The system is **tamper-evident**, not tamper-proof. A party holding both
   the database and the signing key could construct a consistent record. The
   controls in §6, §7 and the external timestamp in §4 exist to make that
   detectable.

## 12. Related instruments

* UNDP Electronic Signatures Policy (POPP, Information and Communications
  Technology → ICT Security)
* UNDP ICT Standards
* *Use of ICT Resources and Data* (ST/SGB/2004/15)
* UNCITRAL Model Law on Electronic Signatures (2001), GA resolution 56/80
* ETSI EN 319 142-1/-2 — PAdES; ETSI EN 319 122-1 — CAdES attributes;
  ETSI EN 319 162-1 — ASiC containers
* ISO 14533-3; RFC 3161; RFC 5652; RFC 5280; RFC 6960

## 13. Version history

| Version | OID | Effective | Change |
|---|---|---|---|
| 1 | `[…1.1]` | `[date]` | First issue. |

A change to these rules takes a **new version and a new OID**. The identifier
in an existing seal must continue to resolve to the text that was in force
when it was applied.
