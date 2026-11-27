# NES document seal — the office CA

The seal (PAdES) and the audit-trail anchor are both signed with the office
seal certificate. Without one, completed documents carry no seal, no
Document Security Store (DSS), and an anchor that was "recorded but never
signed".

## Set it up (Docker)

From `/opt/un_security_system`:

    docker compose run --rm --no-deps -v "$PWD/secrets:/out" web \
        python manage.py esign_pades --make-office-ca /out \
        --office "UNDSS The Gambia" --base-url https://unpass.gm

It writes `secrets/office-ca.crt`, `secrets/office-ca.key` and
`secrets/seal.p12`, and prints three lines for `.env`. Add them, then:

    docker compose restart web worker beat
    scripts/owner.sh esign_pades --check          # must end "NES PAdES is working"
    scripts/owner.sh esign_pades --upgrade-all    # seals old documents, adds DSS, signs anchors

## What you get

    <office> — NES Office Root CA          20 years, signs the revocation list
        └── <office> — NES Document Seal   3 years, documentSigning

* Every new document is sealed at **PAdES B-LTA**: signature, trusted
  timestamp, a DSS holding the CRL and certificates, and an archival
  timestamp. It keeps validating after the seal certificate expires.
* UN PASS publishes the root and a fresh CRL at
  `/accounts/esign/pki/office-ca.crt` and `/accounts/esign/pki/office-ca.crl`
  (no login), and hands the CRL to the signer directly, so sealing needs no
  network round trip.
* Anchors recorded unsigned are signed by `--upgrade-all` (or
  `--sign-anchors`). The signature covers exactly what was recorded at
  completion; its timestamp says when it was signed. A signed anchor can never
  be replaced — the ledger guard refuses.

## Trust

Adobe Reader will show the seal as valid-but-unknown until the root is
trusted on the reader's machine. Publish the root's SHA-256 fingerprint
(printed at creation) and distribute `office-ca.crt` to partner offices
(Adobe: Edit → Preferences → Signatures → Identities & Trusted
Certificates → Import). For documents leaving the UN system, a certificate
from a CA on Adobe's Approved Trust List is still the gold standard — put it
in `secrets/seal.p12` and nothing else changes.

## If the seal key is ever compromised

Add its serial to `ESIGN_OFFICE_CA_REVOKED` in `.env` and restart: the next
CRL revokes it. Then create a new seal (`--make-office-ca … --force` after
moving the old files aside, or issue a new seal from the same CA).

Back up the three files and both passphrases together, offline.
