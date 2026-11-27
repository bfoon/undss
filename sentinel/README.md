# NES Sentinel

An independent tamper witness for documents signed with NES in UN PASS.

A participating office installs it on a machine **it** controls: a cloud VM,
a server in its building, or a laptop that stays on. It runs on Linux,
Windows, macOS and Docker. It keeps its own signed copy of the NES ledger and
keeps checking that every signed document and every audit trail on UN PASS
still matches that copy. If anything changes, it raises the alarm itself.

```
 UN PASS (Banjul)                         Sentinel (another office)
 ┌──────────────────────┐   HTTPS, out    ┌──────────────────────────┐
 │ audit entries, seals │ ◄────────────── │ pulls signed blocks      │
 │ → signed ledger      │                 │ checks every signature   │
 │   blocks (hashes     │ ◄────────────── │ asks: prove file + trail │
 │   only, no content)  │                 │ still match              │
 │                      │ ◄── alert ───── │ mismatch → alarm         │
 └──────────────────────┘                 └─────┬──────────┬─────────┘
          │ e-mails owner, watchers, ICT        │ e-mails  │ webhook,
          ▼                                     ▼ directly ▼ OS log
```

## What it catches

| If someone with full control of the UN PASS server… | the Sentinel raises |
|---|---|
| replaces or edits a signed PDF | `document_altered` |
| deletes the PDF | `document_missing` |
| re-seals a document and hides the change | `document_unwitnessed` |
| edits a seal's audit anchor, or deletes the seal | `seal_altered` |
| deletes or edits an audit-trail entry | `chain_entry_missing` / `chain_broken` |
| cuts the end off a trail | `chain_truncated` |
| deletes a sent or sealed envelope outright | `envelope_deleted` |
| rewrites ledger history it already published | `ledger_rewritten` |
| removes published blocks | `ledger_rolled_back` |
| serves a block not signed by its key | `ledger_bad_signature` |
| replaces the server, or its key, with an impostor | `server_identity_changed` |
| blocks the Sentinel from checking | `server_unreachable` (Sentinel) and `node_silent` (UN PASS) |
| edits this Sentinel's own copy on its machine | `local_store_altered` |

Legitimate changes — a new signature, an archival re-seal, deleting a draft
that was never sent — raise nothing.

## And in a dispute

```
nes-sentinel verify contract.pdf
```

answers, offline, whether that exact file is a document NES sealed, which
envelope, and when this Sentinel first witnessed it. `nes-sentinel export
ENVELOPE -o proof.json` writes a portable evidence file anyone can check with
`nes-sentinel check-bundle proof.json`, with no access to UN PASS.

## Install

You need a one-time **enrolment code** from UN PASS ICT, and they will read
you the server's **fingerprint** (8 groups of 4 characters) over the phone.

### Linux (systemd)

```
sudo ./install-linux.sh
sudo -u nes-sentinel NES_SENTINEL_HOME=/var/lib/nes-sentinel \
     nes-sentinel enroll --server https://unpass.example.org --code NES-XXXXX-XXXXX-XXXXX-XXXXX
sudo -u nes-sentinel NES_SENTINEL_HOME=/var/lib/nes-sentinel \
     nes-sentinel set-email --host smtp.office.org --user sentinel@office.org \
     --from sentinel@office.org --to ict@office.org
sudo systemctl enable --now nes-sentinel
```

### Windows

In an elevated PowerShell, in the folder with `nes-sentinel.exe`:

```
Set-ExecutionPolicy -Scope Process Bypass
.\install-windows.ps1 -Server https://unpass.example.org -Code NES-XXXXX-XXXXX-XXXXX-XXXXX
& "C:\Program Files\NES Sentinel\nes-sentinel.exe" --home "C:\ProgramData\NES Sentinel" set-email --host smtp.office.org --user sentinel@office.org --from sentinel@office.org --to ict@office.org
```

It runs at start-up as the built-in LOCAL SERVICE account. Alerts also go to
the Windows Application event log (source "NES Sentinel", event 900).

### Docker / any cloud

```
docker compose run --rm sentinel enroll --server https://unpass.example.org --code NES-…
docker compose run --rm sentinel set-email --host … --from … --to …
docker compose up -d
```

### Plain Python

`pip install -r requirements.txt` then `python nes_sentinel.py …` — the whole
program is the one file `nes_sentinel.py`.

## Commands

| | |
|---|---|
| `enroll` | join a UN PASS server with a one-time code (you confirm its fingerprint) |
| `run` / `run --once` | watch continuously / one pass |
| `verify FILE…` | is this exact file a document NES sealed? |
| `status` | what this Sentinel holds, last contact, trust |
| `alerts` | what it has found |
| `selfcheck` | re-verify this machine's own copy |
| `set-email`, `set-webhook URL` | how it raises the alarm without UN PASS |
| `test-alert` | send a test through every channel |
| `export ENVELOPE -o FILE`, `check-bundle FILE` | portable proof |

## Security

* **Nothing listens.** It only makes outbound HTTPS (and SMTP) connections;
  there is no port to attack on the machine you install it on.
* **TLS is always verified**; plain `http://` is refused except to
  localhost. Private CAs: `enroll --ca bundle.pem`. Redirects are refused.
* **Its key never leaves the machine.** Windows: DPAPI bound to this computer
  plus an ACL (SYSTEM, Administrators, LOCAL SERVICE). Linux: a `0600` file in
  a `0700` folder, optionally encrypted with `NES_SENTINEL_PASSPHRASE` or a
  systemd credential. It refuses to start if the key is readable by others.
* **Every request is signed** (Ed25519) with a timestamp and a one-time nonce;
  replayed or altered requests are refused. **Every response is signed** by the
  UN PASS key pinned at enrolment, bound to that request — a forged answer is
  detected even through a compromised TLS proxy.
* **It cannot change anything on UN PASS.** It reads hashes and reports.
  A stolen Sentinel key can only raise false alarms, and ICT revokes it in one
  click.
* **It obeys the server only within limits**: however the server's suggested
  policy is set, it still checks at least hourly and still alarms after at
  most 72 hours of silence.
* **Data held**: hashes, envelope ids, event names and times, and the owner's
  and watchers' e-mail addresses (so it can warn them directly). No document
  content, no subjects, no names.
* **No auto-update.** Install updates deliberately; `install-*.{sh,ps1}`
  check `SHA256SUMS` when it is present.
* Runs unprivileged: a dedicated system user with a hardened systemd unit,
  LOCAL SERVICE on Windows, UID 10001 with a read-only filesystem in Docker.
