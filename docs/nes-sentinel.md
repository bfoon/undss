# NES Sentinels — for UN PASS ICT

Sentinels are small programs that other country offices install on their own
machines. Each keeps an independent, signed copy of the NES ledger and keeps
asking UN PASS to prove that every sealed document and every audit trail
still matches it. They close the one gap the ledger guard cannot: someone who
owns this server outright.

The program, its installers and its own README are in `sentinel/`.

## How it fits together

1. Every minute, `nes-sentinel-publish` turns new audit-trail entries and seal
   fingerprints into **ledger blocks**: hash-linked, numbered, and signed with
   the server's Ed25519 publication key. Blocks hold hashes, envelope ids,
   event names and times — never content, subjects or names. The table is
   append-only under the ledger guard and `prev_hash` is unique, so the
   database itself refuses a fork.
2. Each Sentinel pulls new blocks, checks every signature and link, and
   stores them. It re-asks for random old blocks to catch rewritten history.
3. It sends batches of envelope ids to `state/`. UN PASS reads the **actual
   PDF from storage** and walks the **actual trail**, and answers with what it
   finds. The Sentinel compares that with what was published.
4. A mismatch is reported to `alert/`, e-mailed by UN PASS (every two minutes,
   `nes-sentinel-notify`) to the **owner, the watchers and ICT** — and e-mailed
   by the Sentinel itself, so the alarm still goes out if this server is the
   thing that was compromised.
5. A Sentinel that stops checking in for `ESIGN_SENTINEL_SILENT_HOURS` raises
   `node_silent` here; a server that stops answering raises
   `server_unreachable` there. Blocking one side is noticed by the other.

## Setting it up

```
scripts/sentinel_keygen.sh                 # publication key into ./secrets (once)
scripts/owner.sh makemigrations accounts
scripts/owner.sh migrate
scripts/owner.sh unpass_harden_db --install-guards
docker compose up -d
scripts/owner.sh nes_sentinel status       # fingerprint, ledger, nodes
```

Back `secrets/nes_sentinel_ed25519.pem` up with the seal key. Replacing it
makes every Sentinel raise `server_identity_changed` and stop trusting the
server until it is re-enrolled — which is exactly what should happen if
someone else replaced it.

## Adding a Sentinel

**NES → Sentinels → Create enrolment code.** The code is shown once, works
once, and expires in 24 hours. Give it to the person installing, and **read
them the fingerprint** shown on the same page: the program will not enrol
unless they confirm it. That phone call is what stops an impostor server.

Good homes for Sentinels: at least two other offices, one of them on a cloud
provider UN PASS does not use, so no single organisation can switch them all
off.

## Watchers

On any envelope, **Tamper watchers** lets the owner name colleagues who are
told if that document is tampered with. The owner is always told.

## When an alert arrives

1. Do not re-send or re-sign the document.
2. On the Sentinel page, read the evidence: the hash that was witnessed and
   the hash found.
3. Ask the reporting office to run `nes-sentinel verify` on any copy you have
   — the original signed file will show as **GENUINE**.
4. Restore from backup, then close the alert with a note saying what you found.
   Closing needs a note, and the note is kept.

## Settings

| Setting | Default | |
|---|---|---|
| `ESIGN_SENTINEL_ENABLED` | `True` | |
| `ESIGN_SENTINEL_KEY` | `/etc/unpass/nes_sentinel_ed25519.pem` in Docker | publication key |
| `ESIGN_SENTINEL_KEY_PASSPHRASE` | empty | if the PEM is encrypted |
| `ESIGN_SENTINEL_SHARE_CONTACTS` | `True` | send owner/watcher e-mails to Sentinels so they can warn them directly |
| `ESIGN_SENTINEL_POLL_SECONDS` | 300 | suggested; Sentinels clamp to 60–3600 |
| `ESIGN_SENTINEL_AUDIT_EVERY_HOURS` | 24 | each envelope fully re-checked at least this often, plus random spot checks every cycle |
| `ESIGN_SENTINEL_SILENT_HOURS` | 6 | |

## The API, for the record

All under `/accounts/esign/sentinel/api/`. Requests carry `X-NES-Node`,
`X-NES-Time`, `X-NES-Nonce` and an Ed25519 `X-NES-Signature` over

    nes-sentinel-req-v1 \n METHOD \n path \n query \n time \n nonce \n sha256(body)

Responses carry `X-NES-Response-Signature` over
`nes-sentinel-resp-v1 \n nonce \n sha256(body)`. Requests older than five
minutes, replayed nonces, unknown or revoked nodes, bodies over 64 KB and
unknown alert kinds are refused. A Sentinel may name alert recipients, but
only addresses of active UN PASS users are kept.

| | |
|---|---|
| `POST enroll/` | one-time code + public key → node id, server key |
| `GET feed/?after=N&limit=M` | signed blocks |
| `POST state/` | up to 25 envelope ids → live file hash, seal, trail |
| `POST alert/` | a finding |
| `POST heartbeat/` | counts; refreshes "last seen" |
