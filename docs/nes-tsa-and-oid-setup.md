# Setting up the TSA, the OID, and cross-office witnessing

Three things, in the order they should be done. No prior knowledge assumed.

1. **TSA** — proves *when* a document was sealed. Start here; it is the one
   that matters most and the only one that costs money.
2. **OID + policy document** — declares *under what rules* you seal. Free,
   mostly paperwork.
3. **Cross-office witnessing** — other country offices hold copies, so a
   change here can be proved against them. Free, and the strongest protection
   of the three.

---

# Part 1 — The timestamp authority

## What a TSA actually is

A timestamp authority is a service that answers one question: *"here is a
hash; what time is it?"* — and signs the answer.

The exchange, in full:

```
UN PASS                                        TSA
   |                                            |
   |  SHA-256 of the sealed document  --------> |
   |                                            |  signs: "I saw this hash
   |  <------- signed timestamp token           |   at 14:32:07 on 16 Nov 2026"
   |                                            |
   |  embeds the token inside the PDF           |
```

Three things to notice, because they are the whole point:

* **The TSA never sees your document.** Only a 32-byte hash goes out. It
  cannot tell whether you timestamped a contract, a payroll file, or a
  photograph. There is no confidentiality question to clear.
* **The token is signed by them, not you.** That is what makes it evidence. A
  date your own server asserts is a date your own server can change. A date a
  third party signed, you cannot alter — and neither can anyone who later
  compromises your server.
* **It cannot be backdated.** Not by you, not by an attacker with your seal
  key. This is why a TSA is the single thing that still protects you after a
  key compromise.

## Why NES needs one

Without a TSA, NES caps the seal at **PAdES B-B**. The document is
tamper-evident, but the only record of *when* is your own clock. UNDP's
Electronic Signatures Policy sets out two parameters for authenticity:
association with a signatory, and **indication of the date and time of the
signature**. B-B does not satisfy the second one in a way that survives
challenge.

With a TSA, NES reaches **B-LT** and, after the first refresh, **B-LTA** —
verifiable indefinitely.

## Choosing one

Work down this list and stop at the first that applies.

### Option 1 — Ask UNDP ICT / Unite first

Do this before anything else. It is one email and it may end the task.

Ask your Regional Bureau ICT or Unite Service Desk:

> Does UNDP operate or hold a long-term agreement for an RFC 3161 timestamp
> authority that a country-office system may use? We are adding PAdES
> long-term signature validity to an internal document-sealing system and
> need a TSA endpoint.

Why ask first: the POPP procedure makes the **Operations Manager**
responsible for comparing any e-signature solution against the corporate and
regional LTAs. If a corporate TSA exists, using it is both cheaper and the
answer that survives that review. UNDP already holds e-signature LTAs
(DocuSign appears on the corporate tools list), so a timestamping service may
well be in scope.

### Option 2 — A commercial TSA

Several CAs run public RFC 3161 endpoints. Two caveats before you paste one
into settings:

* Most free public TSAs exist to timestamp **code signing**. Their terms may
  not cover bulk document timestamping. Read them, or ask the CA. "It
  responded to my test" is not permission.
* Check the certificate chains to a root your counterparties trust. A
  timestamp from a CA nobody recognises still proves ordering, but a
  recipient's Acrobat will not vouch for it.

Endpoints that were operating as of late 2026, to test against — verify terms
before production use:

| Provider | Endpoint | Notes |
|---|---|---|
| DigiCert | `http://timestamp.digicert.com` | Free, widely used, on the Adobe Approved Trust List. Not eIDAS-qualified. Intended for code signing. |
| GlobalSign | `http://timestamp.globalsign.com/tsa/r6advanced1` | On the Adobe trust list and in the Windows certificate store. |
| Certum (Asseco) | `http://time.certum.pl` | Audited CA since 2002; free basic TSA offered for private and non-profit use, with paid qualified timestamping alongside. |
| Sectigo | `http://timestamp.sectigo.com/qualified` | eIDAS-qualified variant. |
| Open TSA | `https://tsr.open-tsa.eu` | Free, open-source service in Germany, public since April 2026. Newer, so less track record. |

For a UN record with a long retention schedule, a **paid qualified**
timestamp from a provider with an audit trail and an SLA is the defensible
choice. Timestamping is cheap — this is usually a small annual cost, not a
project budget line.

### Option 3 — Run your own

Open-source RFC 3161 servers exist (the Sigstore project publishes one). This
gives you control and no per-token cost.

**But read this before choosing it.** A TSA you operate is only evidence if it
is *independent of the thing it vouches for*. If the TSA key sits on the same
server as the NES seal key, under the same administrators, then anyone who can
forge a seal can also forge its timestamp, and you have added a step rather
than a safeguard.

Self-hosting is worth it only if the TSA runs under **different custody** — a
different office, a different administrator group, ideally different hardware.
If you cannot arrange that, Option 2 is stronger, and cross-office witnessing
(Part 3) gives you much of what you wanted from self-hosting anyway.

### Not an option

**FreeTSA and similar hobby services.** They work, and their roots are
self-signed and trusted by nobody. Fine for testing the plumbing; not for a
record anyone may contest.

## Testing one before you commit

NES has a command for exactly this. It does a real round trip and tells you
what came back:

```bash
docker compose exec web python manage.py esign_pades \
    --test-tsa http://timestamp.digicert.com
```

```
Timestamp authority test — http://timestamp.digicert.com
==============================================================
  OK    timestamp issued (2793 bytes)
        time asserted    : 2026-11-16 20:37:59+00:00
        clock difference : 0.1s vs this server
        policy OID       : 2.16.840.1.114412.7.1
        accuracy         : seconds=1
        authority        : DigiCert Timestamp Responder
        its cert expires : 2031-xx-xx
  OK    its chain validates against your trust roots
```

What to look at:

* **clock difference** — more than a few seconds and one of the two clocks is
  wrong. Check NTP on your own server before blaming the authority.
* **its cert expires** — when it does, new timestamps stop. Tokens already
  issued stay valid.
* **chain validates** — only checked if you have put their root in
  `ESIGN_PADES_TRUST_ROOTS`. Get the root from the provider's own site.

If it fails, the command names the usual causes: no outbound access from the
container, a URL that needs a path, or an authority that wants credentials.

## Wiring it in

```python
ESIGN_PADES_TSA_URL = "http://timestamp.digicert.com"
ESIGN_PADES_TRUST_ROOTS = ["/etc/unpass/tsa-root.crt"]
ESIGN_PADES_LEVEL = "b-lt"
```

Then confirm sealing actually reaches B-LT:

```bash
docker compose exec web python manage.py esign_pades --check
```

If it reports B-T instead of B-LT, the TSA works but the revocation
responders for your *seal* certificate are unreachable — a separate problem,
and the `--check` output names it.

**Then put the refresh in cron.** A timestamp chain that lapses cannot be
repaired:

```cron
17 2 1 * *  cd /srv/unpass && docker compose exec -T web \
            python manage.py esign_pades --refresh-ltv >> /var/log/nes-refresh.log 2>&1
```

---

# Part 2 — The OID and the policy document

## What an OID is

An OID (Object Identifier) is a globally unique number written as
dot-separated integers, like `1.3.6.1.4.1.32473.1.1`. It is a name, nothing
more. It has no cryptography in it and no magic. Its only property is that if
everyone respects the tree, no two organisations use the same number for
different things.

It is a tree. Each dot is a branch, and whoever owns a branch assigns what
sits below it:

```
1.3.6.1.4.1          IANA's "private enterprise" branch
            .32473   one organisation's number (this one is the documentation example)
                 .1  that organisation decides: "signature policies"
                 .1.1  ...and: "NES signature policy, version 1"
```

## Why NES uses one

The seal can carry a *signature-policy-identifier* attribute: a declaration of
the published rules the seal was issued under. It is bound to a **hash of the
policy document itself**, so the rules cannot be quietly rewritten after the
fact to mean something more convenient.

NES only writes this attribute when you supply **both** an OID and a readable
policy file. An OID with a fabricated digest would be worse than no attribute,
so it omits it rather than fake it.

This is optional. Sealing, timestamping, the audit chain and witnessing all
work without it. It matters when a counterparty asks "what does your seal
actually assert?" and you want the document to answer rather than a person.

## Getting one

### First — ask whether UNDP already has one

Almost certainly yes; large organisations hold OID arcs. Ask Regional Bureau
ICT:

> Does UNDP hold an OID arc (an IANA Private Enterprise Number or an
> equivalent registered branch) under which a country office may register a
> signature policy identifier?

If yes, they assign you something under it and you are done. **Do not invent a
number under a branch you do not control** — it is not enforceable, but it is
wrong in the same way as using somebody else's reference number, and a
reviewer who checks will find it.

### Otherwise — get an IANA Private Enterprise Number

Free, first-come-first-served, and the standard route for exactly this.

* The registry is at `https://www.iana.org/assignments/enterprise-numbers`,
  and new assignments are requested at that URL.
* PENs are issued from the prefix `1.3.6.1.4.1`, so a PEN of 60123 means you
  control everything under `1.3.6.1.4.1.60123`.
* Request **one** PEN and make your own sub-assignments beneath it; IANA does
  not want to hear about those, and you do not need to tell them.
* The procedure is described in RFC 9371.

Expect a short web form (organisation, contact) and some waiting. There is no
fee.

One honesty note: RFC 9371 is explicit that there is no cryptographic binding
between a registry entry and the PEN in use, so the registry proves custom,
not ownership. Nobody is relying on the OID for security — it is a name. The
hash of the policy document is what does the actual binding.

### Then lay out your own branch

Once you hold, say, `1.3.6.1.4.1.60123`, write down your assignments and
publish them. Something like:

| OID | Meaning |
|---|---|
| `1.3.6.1.4.1.60123.1` | Signature policies |
| `1.3.6.1.4.1.60123.1.1` | NES signature policy, version 1 |
| `1.3.6.1.4.1.60123.1.2` | NES signature policy, version 2 (when it changes) |

**A new version of the policy gets a new OID.** Do not reuse `…1.1` with
different text: documents already sealed point at it, and changing what it
means retroactively rewrites what those seals claimed.

## The policy document

This is the part people get stuck on, so there is a complete draft at
`docs/nes-signature-policy-v1.md` in this repository. Review it, change what
does not match your practice — the honest version of what you actually do
beats an aspirational one — publish it as a PDF, and point the settings at it:

```python
ESIGN_PADES_POLICY_OID = "1.3.6.1.4.1.60123.1.1"
ESIGN_PADES_POLICY_URI = "https://unpass.gm/policies/nes-signature-policy-v1.pdf"
ESIGN_PADES_POLICY_DOC = "/etc/unpass/nes-signature-policy-v1.pdf"
```

`ESIGN_PADES_POLICY_DOC` must be the **exact byte-identical file** that is
published at the URI. NES hashes the local copy, and that hash goes into every
seal. If the published copy differs by one character, anyone checking will get
a mismatch.

Confirm it took effect:

```bash
docker compose exec web python manage.py esign_pades --check
```

---

# Part 3 — Cross-office witnessing

## What it does

This is the answer to "can the participating COs also have a copy, so a change
is detected and someone is told".

When this office seals a document, every participating country office receives
a small signed statement:

```
nes-witness-v1
origin=UNDP-GM
envelope=9F3C7781A118...
kind=seal
document_sha256=a3f1...
chain_head=27a7...
chain_length=6
sealed_at=2026-11-16T20:49:47+00:00
```

Each office stores it in its own database. Later, anyone can ask the others
what they hold. To forge a document you would now have to alter it in every
participating office at once — in databases you do not control, each signed by
a key you do not have.

It is why a land registry keeps copies in more than one building.

## What travels, and what does not

**Fingerprints only.** Hashes, the envelope identifier, timestamps, the
reference. **Never** the document, the signature images, the field values or
recipient addresses.

This is deliberate. A UN envelope may be confidential to one office; shipping
the PDF to five others to protect it would create a worse problem than the one
being solved. A SHA-256 proves tampering and reveals nothing about content.

The consequence, stated plainly: **witnessing detects and proves alteration.
It does not restore the file.** Restoration comes from backups and from
evidence packages archived at completion. If you want other offices to hold
recoverable copies, archive the evidence package with them — that is a
deliberate, separate decision about confidentiality, not something this turns
on quietly.

## Setting up a pair of offices

Run on **each** side. The two offices are symmetric; neither is a server.

**Step 1 — each office exports its seal certificate and mints a token.**

```bash
# in The Gambia
docker compose exec web python manage.py esign_witness --export-cert /tmp/gm-seal.crt
docker compose exec web python manage.py esign_witness --new-token
#   -> prints a token. This is what SENEGAL will present when pushing to us.
```

```bash
# in Senegal
docker compose exec web python manage.py esign_witness --export-cert /tmp/sn-seal.crt
docker compose exec web python manage.py esign_witness --new-token
#   -> the token GAMBIA will present when pushing to Senegal.
```

**Step 2 — exchange the certificates and tokens out of band.** Not over the
channel the token is going to authenticate. Email the certificates (they are
public); send the tokens by a different route — a phone call, a password
manager share, anything separate.

**Step 3 — each office registers the other.**

```bash
# in The Gambia, registering Senegal
docker compose exec web python manage.py esign_witness --add-peer UNDP-SN \
    --name "UNDP Senegal" \
    --url https://unpass.sn \
    --outbound-token "<the token SENEGAL gave us>" \
    --inbound-token  "<the token WE gave Senegal>" \
    --cert /tmp/sn-seal.crt
```

The two tokens are separate on purpose: a leak in one direction does not grant
the other, and either can be rotated alone.

**Step 4 — turn it on** in each office's settings:

```python
ESIGN_WITNESS_ENABLED = True
ESIGN_WITNESS_OFFICE_CODE = "UNDP-GM"          # stable; it is written into signed statements
ESIGN_WITNESS_ALERT_EMAILS = ["ict@unpass.gm"]
```

**Step 5 — check the link.**

```bash
docker compose exec web python manage.py esign_witness --ping
docker compose exec web python manage.py esign_witness --status
```

A `401` from `--ping` means the peer does not recognise your outbound token —
they need to set it as their inbound token for you.

**Step 6 — two cron entries.**

```cron
# push queued fingerprints
*/15 * * * *  cd /srv/unpass && docker compose exec -T web \
              python manage.py esign_witness --send-pending >> /var/log/nes-witness.log 2>&1

# compare every sealed document against the copies peers hold
41 3 * * *    cd /srv/unpass && docker compose exec -T web \
              python manage.py esign_witness --audit >> /var/log/nes-audit.log 2>&1
```

The audit is what turns this from a filing cabinet into an alarm. Without it,
the copies exist and nobody looks.

## When something diverges

The nightly audit emails `ESIGN_WITNESS_ALERT_EMAILS`:

```
[NES] 2 document integrity alerts — 2 critical

[CRITICAL] Stored file does not match any attestation
  envelope : 9F3C7781A118...
  detail   : The most recent attestation (2026-11-16 20:49) records
             27a7812a80ecf0b7…, the file on disk is 9b061bac850f0b57….

[CRITICAL] A peer holds a different fingerprint
  envelope : 9F3C7781A118...
  detail   : UNDP-SN witnessed a different fingerprint for this document.
```

Then:

```bash
docker compose exec web python manage.py esign_witness --audit 9F3C7781A118
```

**Do not delete anything.** The divergence is itself evidence. The peer's copy
is what proves what the document used to be, and the evidence package is what
proves what it contained.

Alerts are deduplicated — one alarm per live condition, not one per night
until somebody looks. They are also visible in the app at
`/accounts/esign/witness/` and in the admin, where they can be resolved with a
note but never deleted.

## Security model, stated honestly

| Attack | Caught by | How |
|---|---|---|
| A byte changed in the PDF | The seal | Signature covers every byte |
| Bytes appended after the seal | The recorded hash | Signature validation alone does **not** see trailing bytes — verified by experiment, which is why the hash comparison exists |
| An audit entry edited or deleted | The hash chain | Each entry links to its predecessor |
| Entries removed from the end of the trail | The signed anchor | Records the head and the count |
| The whole record rewritten with DB access | Peer offices | Their copies disagree |
| …and the seal key stolen too | Peer offices + the TSA | Their copies still disagree, and the timestamps cannot be backdated |
| A peer's token stolen | The certificate check | A statement must be signed by that peer's registered key, not merely arrive with its token |
| A peer forging for another office | Origin check | A peer may only witness its own documents; attempting otherwise raises a critical alert |

What remains: an attacker holding **every** participating office's database and
signing key simultaneously. That is the point of adding offices — each one
raises that bar, and none of them is under the same administration.
