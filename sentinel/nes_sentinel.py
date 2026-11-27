#!/usr/bin/env python3
"""
NES Sentinel — an independent tamper witness for UN PASS NES documents.

A participating office runs this on any machine it controls (a cloud VM, a
server in its building, a laptop; Linux, Windows or macOS). It:

  1. pulls every block of the NES ledger from UN PASS — a hash of each
     audit-trail entry and each sealed document's fingerprint — and checks
     the server's Ed25519 signature and the hash link on every one;
  2. keeps its own copy, sealed with its own key, so the evidence outlives
     anything that happens to the server;
  3. keeps asking UN PASS to prove that each signed file and each audit
     trail still matches what was published;
  4. when something does not match, raises the alarm itself — to UN PASS, to
     the document's owner and watchers by e-mail, to a chat webhook, and to
     the operating system's log — so silencing the server does not silence
     the alarm.

It also answers the question that matters in a dispute, offline:

    nes-sentinel verify contract.pdf

Security design
---------------
* Outbound HTTPS only. It never listens on a port.
* TLS certificates are always verified. Plain http is refused except to
  localhost.
* It signs every request with a key it generated; the private half never
  leaves this machine. Windows: protected with DPAPI (bound to this machine)
  and an ACL. Linux/macOS: a 0600 file, optionally encrypted with a
  passphrase from the environment or a systemd credential. It refuses to run
  if the key file is readable by other users.
* It pins the server's publication key at enrolment and verifies the
  signature on every response, bound to the request's nonce, so a forged or
  replayed answer is detected even through a compromised TLS proxy.
* It executes nothing it receives, follows no redirects, caps every
  response, and validates every field before use.
* No auto-update. Updates are a deliberate, verifiable act (see SHA256SUMS).

Dependencies: Python 3.9+ and `cryptography`. Everything else is stdlib.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import random
import re
import secrets
import smtplib
import sqlite3
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
except ImportError:  # pragma: no cover
    sys.stderr.write("NES Sentinel needs the 'cryptography' package: pip install cryptography\n")
    sys.exit(2)

VERSION = "1.0.0"
PROTOCOL = "nes-sentinel-v1"
BLOCK_TAG = "nes-sentinel-block-v1"
REQUEST_TAG = "nes-sentinel-req-v1"
RESPONSE_TAG = "nes-sentinel-resp-v1"
SELF_TAG = "nes-sentinel-self-v1"
GENESIS = "0" * 64

API = "/accounts/esign/sentinel/api/"
MAX_RESPONSE = 16 * 1024 * 1024
TIMEOUT = 30
FEED_PAGE = 500
PENDING_GRACE = timedelta(minutes=30)

HEX64 = re.compile(r"^[0-9a-f]{64}$")
UID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
B64 = re.compile(r"^[A-Za-z0-9+/=]{40,120}$")
EMAIL = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")

IS_WINDOWS = os.name == "nt"
log = logging.getLogger("nes-sentinel")


class SentinelError(Exception):
    pass


class ServerIdentityError(SentinelError):
    """The answer was not signed by the server key pinned at enrolment."""


class ApiError(SentinelError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


# ═════════════════════════════════════════════════════════════════════════════
# Small helpers
# ═════════════════════════════════════════════════════════════════════════════

def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(text)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def block_hash(seq: int, prev_hash: str, body: str) -> str:
    return sha256_hex(f"{BLOCK_TAG}\n{seq}\n{prev_hash}\n{body}".encode("utf-8"))


def pretty_fp(fp: str) -> str:
    return " ".join(fp[i:i + 4] for i in range(0, 32, 4)).upper()


def one_line(text: str, limit: int = 300) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))[:limit]


def default_home() -> Path:
    if os.environ.get("NES_SENTINEL_HOME"):
        return Path(os.environ["NES_SENTINEL_HOME"])
    if os.environ.get("STATE_DIRECTORY"):            # systemd StateDirectory=
        return Path(os.environ["STATE_DIRECTORY"].split(":")[0])
    if IS_WINDOWS:
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "NES Sentinel"
    if os.geteuid() == 0 or Path("/var/lib/nes-sentinel").is_dir():
        return Path("/var/lib/nes-sentinel")
    return Path.home() / ".local" / "share" / "nes-sentinel"


# ═════════════════════════════════════════════════════════════════════════════
# Secret storage: DPAPI on Windows, 0600 files (optionally encrypted) elsewhere
# ═════════════════════════════════════════════════════════════════════════════

def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI, machine scope: the blob only opens on this computer."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw: bytes):
        buf = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    entropy, _e = blob(b"nes-sentinel-v1")
    source, _s = blob(data)
    out = DATA_BLOB()
    flags = 0x4 | 0x1   # CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(source), "NES Sentinel", ctypes.byref(entropy),
                                      None, None, flags, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy),
                                        None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise SentinelError("Windows could not " + ("protect" if protect else "open")
                            + " the Sentinel's secret (DPAPI).")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _restrict_windows_acl(path: Path):
    """Only SYSTEM, Administrators and the service account (LOCAL SERVICE) may
    touch it. On the data folder the grants are inherited by every file
    created inside it later."""
    inherit = "(OI)(CI)" if path.is_dir() else ""
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r",
             "/grant:r", f"*S-1-5-18:{inherit}F", "/grant:r", f"*S-1-5-32-544:{inherit}F",
             "/grant:r", f"*S-1-5-19:{inherit}M"],
            check=False, capture_output=True, timeout=30,
        )
    except Exception:
        pass


def _passphrase() -> bytes | None:
    value = os.environ.get("NES_SENTINEL_PASSPHRASE", "")
    cred = os.environ.get("CREDENTIALS_DIRECTORY")      # systemd LoadCredential=
    if not value and cred and (Path(cred) / "passphrase").exists():
        value = (Path(cred) / "passphrase").read_text().strip()
    return value.encode("utf-8") if value else None


def write_secret(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        data = _dpapi(data, protect=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)
    if IS_WINDOWS:
        _restrict_windows_acl(path)


def read_secret(path: Path) -> bytes:
    if not IS_WINDOWS:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SentinelError(
                f"{path} is readable by other users (mode {oct(mode & 0o777)}). "
                f"Fix it with: chmod 600 {path}")
    data = path.read_bytes()
    return _dpapi(data, protect=False) if IS_WINDOWS else data


# ═════════════════════════════════════════════════════════════════════════════
# The node: config, key, local ledger
# ═════════════════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blocks (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, envelope TEXT NOT NULL,
    body TEXT NOT NULL, prev_hash TEXT NOT NULL, block_hash TEXT NOT NULL UNIQUE,
    signature TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS blocks_envelope ON blocks(envelope);
CREATE TABLE IF NOT EXISTS envelopes (
    uid TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_audit TEXT,
    last_ok INTEGER, gone INTEGER NOT NULL DEFAULT 0,
    contacts TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS pending (
    envelope TEXT NOT NULL, sha TEXT NOT NULL, first_seen TEXT NOT NULL,
    PRIMARY KEY (envelope, sha));
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, at TEXT NOT NULL,
    envelope TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, severity TEXT NOT NULL,
    detail TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '{}',
    reported INTEGER NOT NULL DEFAULT 0, emailed INTEGER NOT NULL DEFAULT 0,
    hooked INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
"""


class Node:
    def __init__(self, home: Path):
        self.home = home
        self.config_path = home / "sentinel.json"
        self.key_path = home / ("node.key.dpapi" if IS_WINDOWS else "node.key")
        self.smtp_secret = home / ("smtp.secret.dpapi" if IS_WINDOWS else "smtp.secret")
        self.db_path = home / "ledger.db"
        self.config: dict = {}
        self._key = None
        self._server_key = None
        self.db: sqlite3.Connection | None = None

    # -- files ---------------------------------------------------------------
    def prepare_home(self):
        self.home.mkdir(parents=True, exist_ok=True)
        if not IS_WINDOWS:
            os.chmod(self.home, 0o700)
        else:
            _restrict_windows_acl(self.home)

    def load(self):
        if not self.config_path.exists():
            raise SentinelError(f"Not enrolled yet (no {self.config_path}). Run: nes-sentinel enroll")
        self.config = json.loads(self.config_path.read_text("utf-8"))
        self.open_db()
        return self

    def save_config(self):
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, indent=2, sort_keys=True), "utf-8")
        if not IS_WINDOWS:
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.config_path)

    def open_db(self):
        self.db = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        if not IS_WINDOWS:
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.db_path) + suffix)
                if p.exists():
                    os.chmod(p, 0o600)

    def meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str):
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (key, value))

    # -- keys ----------------------------------------------------------------
    def write_key(self, key):
        pw = _passphrase()
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(pw) if (pw and not IS_WINDOWS)
            else serialization.NoEncryption(),
        )
        write_secret(self.key_path, pem)
        self._key = key
        return key

    @property
    def key(self) -> Ed25519PrivateKey:
        if self._key is None:
            pem = read_secret(self.key_path)
            pw = None if IS_WINDOWS else _passphrase()
            try:
                self._key = serialization.load_pem_private_key(pem, password=pw)
            except TypeError:
                raise SentinelError("The node key is encrypted: set NES_SENTINEL_PASSPHRASE.")
        return self._key

    def public_b64(self, key=None) -> str:
        raw = (key or self.key).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode("ascii")

    @property
    def server_key(self) -> Ed25519PublicKey:
        if self._server_key is None:
            self._server_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(self.config["server_key"]))
        return self._server_key

    # -- the local seal ------------------------------------------------------
    def _self_material(self) -> bytes:
        head = self.db.execute("SELECT seq, block_hash FROM blocks ORDER BY seq DESC LIMIT 1").fetchone()
        count = self.db.execute("SELECT count(*) FROM blocks").fetchone()[0]
        alerts = self.db.execute("SELECT fingerprint FROM alerts ORDER BY id").fetchall()
        digest = sha256_hex("\n".join(a[0] for a in alerts).encode())
        seq, h = head if head else (0, GENESIS)
        return f"{SELF_TAG}\n{seq}\n{h}\n{count}\n{digest}".encode()

    def reseal(self):
        """Sign our own state, so editing this machine's copy is noticed."""
        self.set_meta("self_seal", base64.b64encode(self.key.sign(self._self_material())).decode())

    def self_seal_ok(self) -> bool:
        sig = self.meta("self_seal")
        if not sig:
            return True
        try:
            self.key.public_key().verify(base64.b64decode(sig), self._self_material())
            return True
        except Exception:
            return False


# ═════════════════════════════════════════════════════════════════════════════
# HTTP: signed requests, verified responses, no redirects
# ═════════════════════════════════════════════════════════════════════════════

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SentinelError("The server answered with a redirect; Sentinels do not follow them. "
                            "Enrol with the final https:// address.")


def check_server_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme == "https" and parsed.hostname:
        pass
    elif parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        pass
    else:
        raise SentinelError("The server address must start with https:// "
                            "(plain http is only allowed to localhost for testing).")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SentinelError("The server address must not contain credentials, a query or a fragment.")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


class Client:
    def __init__(self, node: Node, server: str | None = None, ca_file: str | None = None):
        self.node = node
        self.server = check_server_url(server or node.config["server"])
        context = ssl.create_default_context(cafile=ca_file or node.config.get("ca_file") or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), _NoRedirect())

    def call(self, method: str, endpoint: str, *, query: dict | None = None,
             payload: dict | None = None, sign: bool = True, server_key: Ed25519PublicKey | None = None):
        url = f"{self.server}{API}{endpoint}"
        qs = urllib.parse.urlencode(query or {})
        path = urllib.parse.urlparse(url).path
        body = canonical(payload).encode() if payload is not None else b""
        nonce = secrets.token_hex(16)
        headers = {
            "Accept": "application/json",
            "User-Agent": f"NES-Sentinel/{VERSION}",
            "X-NES-Nonce": nonce,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if sign:
            stamp = str(int(time.time()))
            message = (f"{REQUEST_TAG}\n{method}\n{path}\n{qs}\n{stamp}\n{nonce}\n"
                       f"{sha256_hex(body)}").encode()
            headers.update({
                "X-NES-Node": str(self.node.config["node_id"]),
                "X-NES-Time": stamp,
                "X-NES-Signature": base64.b64encode(self.node.key.sign(message)).decode(),
            })
        request = urllib.request.Request(url + (f"?{qs}" if qs else ""), method=method,
                                         data=body if method == "POST" else None, headers=headers)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                status, raw, sig = response.status, response.read(MAX_RESPONSE + 1), \
                    response.headers.get("X-NES-Response-Signature", "")
        except urllib.error.HTTPError as exc:
            status, raw, sig = exc.code, exc.read(MAX_RESPONSE + 1), \
                exc.headers.get("X-NES-Response-Signature", "")
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            raise SentinelError(f"Cannot reach {self.server}: {getattr(exc, 'reason', exc)}")
        if len(raw) > MAX_RESPONSE:
            raise SentinelError("Response too large.")

        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise ApiError(status, "the server did not answer with JSON "
                                   "(is the address right, and is UN PASS up to date?)")

        key = server_key or (self.node.server_key if "server_key" in self.node.config else None)
        if key is not None:
            if not sig and status >= 400:
                # A proxy error page, a restart, a 502 — not a forgery. Treat
                # it as the server being unavailable; if it lasts, the
                # server_unreachable alarm fires.
                raise ApiError(status, one_line(data.get("error", "unsigned error response")))
            try:
                key.verify(base64.b64decode(sig),
                           f"{RESPONSE_TAG}\n{nonce}\n{sha256_hex(raw)}".encode())
            except Exception:
                raise ServerIdentityError(
                    "The answer was not signed by the UN PASS key pinned at enrolment.")
        if status >= 400 or not data.get("ok"):
            raise ApiError(status, one_line(data.get("error", "request refused")))
        return data, raw, sig, nonce


# ═════════════════════════════════════════════════════════════════════════════
# Alerts: stored, then sent everywhere they can go
# ═════════════════════════════════════════════════════════════════════════════

def os_log(level: int, message: str):
    log.log(level, message)
    if IS_WINDOWS and level >= logging.WARNING:
        try:
            subprocess.run(
                ["eventcreate", "/T", "ERROR" if level >= logging.ERROR else "WARNING",
                 "/ID", "900", "/L", "APPLICATION", "/SO", "NES Sentinel",
                 "/D", one_line(message, 900)],
                check=False, capture_output=True, timeout=30)
        except Exception:
            pass


def raise_alert(node: Node, kind: str, detail: str, *, envelope: str = "", severity: str = "critical",
                evidence: dict | None = None, key: str = "") -> bool:
    """Record a finding once. Returns True if it is new."""
    fp = sha256_hex(f"{kind}\n{envelope}\n{key}".encode())
    try:
        node.db.execute(
            "INSERT INTO alerts(fingerprint, at, envelope, kind, severity, detail, evidence) "
            "VALUES(?,?,?,?,?,?,?)",
            (fp, iso(), envelope, kind, severity, one_line(detail, 2000), canonical(evidence or {})))
    except sqlite3.IntegrityError:
        return False
    os_log(logging.ERROR if severity == "critical" else logging.WARNING,
           f"TAMPER ALERT [{kind}] {envelope or '-'}: {detail}")
    return True


def _alert_text(row, node: Node) -> tuple[str, str]:
    _id, fp, at, envelope, kind, severity, detail, evidence = row[:8]
    name = node.config.get("name", "Sentinel")
    subject = one_line(f"[NES Sentinel · {severity.upper()}] {kind.replace('_', ' ')}"
                       + (f" — envelope {envelope}" if envelope else ""), 200)
    lines = [
        f"The NES Sentinel '{name}' found a problem.",
        "",
        f"What:      {kind}",
        f"Severity:  {severity}",
        f"Envelope:  {envelope or '-'}",
        f"When:      {at}",
        "",
        detail,
        "",
    ]
    try:
        for k, v in sorted(json.loads(evidence).items()):
            lines.append(f"  {k}: {one_line(v, 200)}")
    except Exception:
        pass
    lines += [
        "",
        "This message comes from an independent copy of the NES ledger kept outside",
        "UN PASS, so it reaches you even if UN PASS itself was the target.",
        "Do not re-send or re-sign the document until ICT has looked at it.",
        f"Alert reference: {fp[:16]}",
    ]
    return subject, "\n".join(lines)


def _send_email(node: Node, to: list[str], subject: str, text: str):
    cfg = node.config.get("email") or {}
    host, port = cfg.get("host"), int(cfg.get("port") or 587)
    if not host:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(to)
    msg["X-NES-Sentinel"] = VERSION
    msg.set_content(text)
    password = ""
    if node.smtp_secret.exists():
        password = read_secret(node.smtp_secret).decode("utf-8")
    password = os.environ.get("NES_SENTINEL_SMTP_PASSWORD", password)
    context = ssl.create_default_context()
    local = host in ("localhost", "127.0.0.1", "::1")
    if cfg.get("security") == "ssl":
        server = smtplib.SMTP_SSL(host, port, context=context, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
        if cfg.get("security", "starttls") == "starttls":
            server.starttls(context=context)
        elif not local:
            raise SentinelError("Refusing to send e-mail without TLS to a remote server.")
    with server:
        if cfg.get("user"):
            server.login(cfg["user"], password)
        server.send_message(msg)
    return True


def _send_webhook(node: Node, subject: str, text: str):
    url = node.config.get("webhook", "")
    if not url:
        return False
    if not url.startswith("https://"):
        raise SentinelError("Webhooks must be https://")
    body = json.dumps({"text": f"*{subject}*\n{text}"}).encode()
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": f"NES-Sentinel/{VERSION}"})
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    with opener.open(request, timeout=TIMEOUT) as response:
        response.read(4096)
    return True


def dispatch_alerts(node: Node, client: Client | None, trust_server: bool = True) -> dict:
    report = {"reported": 0, "emailed": 0, "webhook": 0, "errors": 0}
    rows = node.db.execute(
        "SELECT id, fingerprint, at, envelope, kind, severity, detail, evidence, reported, emailed, hooked "
        "FROM alerts WHERE (reported=0 OR emailed=0 OR hooked=0) AND attempts < 50 ORDER BY id").fetchall()
    local_admins = [e for e in (node.config.get("alert_emails") or []) if EMAIL.match(e)]
    for row in rows:
        aid, fp, _at, envelope, kind, severity, detail, evidence, reported, emailed, hooked = row
        node.db.execute("UPDATE alerts SET attempts=attempts+1 WHERE id=?", (aid,))
        subject, text = _alert_text(row, node)

        contacts = []
        if envelope:
            got = node.db.execute("SELECT contacts FROM envelopes WHERE uid=?", (envelope,)).fetchone()
            contacts = [e for e in json.loads(got[0] or "[]") if EMAIL.match(e)] if got else []

        if not reported and client is not None and trust_server:
            try:
                client.call("POST", "alert/", payload={
                    "kind": kind, "severity": severity, "envelope": envelope, "detail": detail,
                    "evidence": json.loads(evidence), "fingerprint": fp, "contacts": contacts})
                node.db.execute("UPDATE alerts SET reported=1 WHERE id=?", (aid,))
                report["reported"] += 1
            except Exception as exc:
                log.warning("Could not report alert %s to UN PASS: %s", fp[:12], exc)
                report["errors"] += 1

        if not emailed:
            to = sorted(set(e.lower() for e in local_admins + contacts))
            if not to or not (node.config.get("email") or {}).get("host"):
                node.db.execute("UPDATE alerts SET emailed=1 WHERE id=?", (aid,))
            else:
                try:
                    _send_email(node, to, subject, text)
                    node.db.execute("UPDATE alerts SET emailed=1 WHERE id=?", (aid,))
                    report["emailed"] += 1
                except Exception as exc:
                    log.warning("Could not e-mail alert %s: %s", fp[:12], exc)
                    report["errors"] += 1

        if not hooked:
            if not node.config.get("webhook"):
                node.db.execute("UPDATE alerts SET hooked=1 WHERE id=?", (aid,))
            else:
                try:
                    _send_webhook(node, subject, text)
                    node.db.execute("UPDATE alerts SET hooked=1 WHERE id=?", (aid,))
                    report["webhook"] += 1
                except Exception as exc:
                    log.warning("Could not post alert %s to the webhook: %s", fp[:12], exc)
                    report["errors"] += 1
    return report


# ═════════════════════════════════════════════════════════════════════════════
# 1. Following the ledger
# ═════════════════════════════════════════════════════════════════════════════

def _valid_block(b) -> bool:
    return (isinstance(b, dict) and isinstance(b.get("seq"), int) and b["seq"] > 0
            and b.get("kind") in ("event", "seal")
            and isinstance(b.get("envelope"), str) and UID.match(b["envelope"])
            and isinstance(b.get("body"), str) and len(b["body"]) < 8192
            and all(isinstance(b.get(k), str) and HEX64.match(b[k]) for k in ("prev_hash", "block_hash"))
            and isinstance(b.get("signature"), str) and B64.match(b["signature"]))


def local_head(node: Node) -> tuple[int, str]:
    row = node.db.execute("SELECT seq, block_hash FROM blocks ORDER BY seq DESC LIMIT 1").fetchone()
    return (row[0], row[1]) if row else (0, GENESIS)


def sync_ledger(node: Node, client: Client) -> dict:
    report = {"new": 0, "head": 0, "problems": 0}
    seq, head = local_head(node)
    while True:
        data, *_ = client.call("GET", "feed/", query={"after": seq, "limit": FEED_PAGE})
        server_head = data.get("head") or {}
        blocks = data.get("blocks") or []
        if not isinstance(blocks, list) or not isinstance(server_head.get("seq"), int):
            raise SentinelError("Malformed feed.")
        if server_head["seq"] < seq:
            raise_alert(node, "ledger_rolled_back",
                        f"UN PASS says its ledger ends at #{server_head['seq']}, but this Sentinel "
                        f"already holds #{seq}. Published history was removed from the server.",
                        evidence={"server_head": server_head["seq"], "held": seq}, key=str(server_head["seq"]))
            report["problems"] += 1
            break
        for b in blocks:
            if not _valid_block(b):
                raise SentinelError("Malformed block in feed.")
            if b["seq"] != seq + 1 or b["prev_hash"] != head:
                raise_alert(node, "ledger_rewritten",
                            f"Block #{b['seq']} does not follow block #{seq} held here. The server's "
                            "history differs from what it published before.",
                            evidence={"block": b["seq"], "expected_prev": head, "served_prev": b["prev_hash"]},
                            key=f"{b['seq']}:{b['prev_hash']}")
                report["problems"] += 1
                return report
            if block_hash(b["seq"], b["prev_hash"], b["body"]) != b["block_hash"]:
                raise_alert(node, "ledger_bad_signature",
                            f"Block #{b['seq']} does not hash to the value it claims.",
                            evidence={"block": b["seq"]}, key=str(b["seq"]))
                report["problems"] += 1
                return report
            try:
                node.server_key.verify(base64.b64decode(b["signature"]), b["block_hash"].encode())
            except Exception:
                raise_alert(node, "ledger_bad_signature",
                            f"Block #{b['seq']} is not signed by the pinned UN PASS key.",
                            evidence={"block": b["seq"]}, key=str(b["seq"]))
                report["problems"] += 1
                return report
            try:
                body = json.loads(b["body"])
            except ValueError:
                body = None
            if not isinstance(body, dict) or body.get("envelope") != b["envelope"] \
                    or body.get("kind") != b["kind"]:
                raise SentinelError(f"Block #{b['seq']} body does not match its header.")
            with _tx(node):
                node.db.execute(
                    "INSERT INTO blocks(seq, kind, envelope, body, prev_hash, block_hash, signature, received_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (b["seq"], b["kind"], b["envelope"], b["body"], b["prev_hash"], b["block_hash"],
                     b["signature"], iso()))
                node.db.execute("INSERT OR IGNORE INTO envelopes(uid, first_seen) VALUES(?,?)",
                                (b["envelope"], iso()))
                node.db.execute("UPDATE envelopes SET last_audit=NULL WHERE uid=?", (b["envelope"],))
                if b["kind"] == "seal":
                    node.db.execute("DELETE FROM pending WHERE envelope=? AND sha=?",
                                    (b["envelope"], body.get("document_sha256", "")))
            seq, head = b["seq"], b["block_hash"]
            report["new"] += 1
        if len(blocks) < FEED_PAGE:
            if server_head["seq"] == seq and server_head.get("hash") != head:
                raise_alert(node, "ledger_rewritten",
                            f"The server's block #{seq} is not the block #{seq} it published to this Sentinel.",
                            evidence={"block": seq, "held": head, "served": server_head.get("hash")},
                            key=f"head:{seq}:{server_head.get('hash')}")
                report["problems"] += 1
            break
    report["head"] = seq
    return report


def spot_check(node: Node, client: Client, samples: int = 3) -> int:
    """Ask again for a few blocks we already hold; any difference is rewritten history."""
    count = node.db.execute("SELECT count(*) FROM blocks").fetchone()[0]
    if not count:
        return 0
    seqs = {s for (s,) in node.db.execute(
        "SELECT seq FROM blocks ORDER BY random() LIMIT ?", (samples,)).fetchall()}
    seqs.add(1)
    problems = 0
    for s in sorted(seqs):
        held = node.db.execute("SELECT block_hash FROM blocks WHERE seq=?", (s,)).fetchone()[0]
        data, *_ = client.call("GET", "feed/", query={"after": s - 1, "limit": 1})
        blocks = data.get("blocks") or []
        served = blocks[0]["block_hash"] if blocks and isinstance(blocks[0], dict) \
            and blocks[0].get("seq") == s else None
        if served is None:
            raise_alert(node, "ledger_rolled_back",
                        f"Block #{s}, published earlier, is no longer on the server.",
                        evidence={"block": s, "held": held}, key=f"gone:{s}")
            problems += 1
        elif served != held:
            raise_alert(node, "ledger_rewritten",
                        f"Block #{s} on the server is not the block #{s} it published to this Sentinel.",
                        evidence={"block": s, "held": held, "served": served}, key=f"{s}:{served}")
            problems += 1
    return problems


@contextlib.contextmanager
def _tx(node: Node):
    node.db.execute("BEGIN IMMEDIATE")
    try:
        yield
        node.db.execute("COMMIT")
    except BaseException:
        node.db.execute("ROLLBACK")
        raise


# ═════════════════════════════════════════════════════════════════════════════
# 2. Auditing the server against the ledger
# ═════════════════════════════════════════════════════════════════════════════

def envelope_record(node: Node, uid: str) -> dict:
    seals, events, sent = [], [], False
    for kind, body in node.db.execute(
            "SELECT kind, body FROM blocks WHERE envelope=? ORDER BY seq", (uid,)):
        b = json.loads(body)
        if kind == "seal":
            seals.append(b)
        else:
            events.append(b)
            sent = sent or b.get("event") in ("sent", "signed", "approved", "completed", "sealed")
    return {"seals": seals, "events": events, "evidence": sent or bool(seals)}


def _is_subsequence(needles: list, haystack: list) -> bool:
    it = iter(haystack)
    return all(any(n == h for h in it) for n in needles)


def audit_envelope(node: Node, st: dict) -> list[str]:
    uid = st["envelope"]
    rec = envelope_record(node, uid)
    found = []

    def alarm(kind, detail, severity="critical", evidence=None, key=""):
        found.append(kind)
        raise_alert(node, kind, detail, envelope=uid, severity=severity, evidence=evidence, key=key)

    if not st.get("exists"):
        if rec["evidence"]:
            alarm("envelope_deleted", "This envelope was sent or sealed, and has now disappeared "
                                      "from UN PASS together with its audit trail.",
                  evidence={"events_held": len(rec["events"]), "seals_held": len(rec["seals"])})
        node.db.execute("UPDATE envelopes SET gone=1 WHERE uid=?", (uid,))
        return found

    contacts = [c for c in (st.get("contacts") or []) if isinstance(c, str) and EMAIL.match(c)][:50]
    node.db.execute("UPDATE envelopes SET contacts=?, gone=0 WHERE uid=?", (json.dumps(contacts), uid))

    known = {s["document_sha256"]: s for s in rec["seals"]}
    file = st.get("file") or {}
    seal = st.get("seal")
    if known:
        live = file.get("sha256", "")
        if not file.get("present"):
            alarm("document_missing", "The sealed PDF is no longer in UN PASS storage.",
                  evidence={"witnessed_sha256": rec["seals"][-1]["document_sha256"]})
        elif live not in known:
            if seal and seal.get("document_sha256") == live:
                row = node.db.execute("SELECT first_seen FROM pending WHERE envelope=? AND sha=?",
                                      (uid, live)).fetchone()
                if row is None:
                    node.db.execute("INSERT INTO pending VALUES(?,?,?)", (uid, live, iso()))
                elif now() - parse_iso(row[0]) > PENDING_GRACE:
                    alarm("document_unwitnessed",
                          "The sealed PDF and its seal record both changed, but the change was never "
                          "published to Sentinels. A legitimate re-seal is always published.",
                          evidence={"witnessed_sha256": rec["seals"][-1]["document_sha256"], "now_sha256": live},
                          key=live)
            else:
                alarm("document_altered",
                      "The sealed PDF on UN PASS is not any version this Sentinel witnessed.",
                      evidence={"witnessed_sha256": rec["seals"][-1]["document_sha256"], "now_sha256": live},
                      key=live)
        if seal is None:
            alarm("seal_altered", "The seal record for this envelope was removed.", key="gone")
        elif seal.get("document_sha256") in known:
            was = known[seal["document_sha256"]]
            if (seal.get("chain_head"), seal.get("chain_length")) != (was.get("chain_head"), was.get("chain_length")):
                alarm("seal_altered", "The seal's audit anchor differs from the one witnessed at sealing.",
                      evidence={"witnessed_head": was.get("chain_head"), "now_head": seal.get("chain_head"),
                                "witnessed_length": was.get("chain_length"), "now_length": seal.get("chain_length")},
                      key=f"{seal.get('chain_head')}:{seal.get('chain_length')}")

    chain = st.get("chain") or {}
    entries = [e for e in (chain.get("entries") or []) if isinstance(e, str)]
    held = [e["entry_hash"] for e in rec["events"]]
    if held:
        present = set(entries)
        missing = [h for h in held if h not in present]
        severity = "critical" if rec["evidence"] else "warning"
        if missing:
            alarm("chain_entry_missing",
                  f"{len(missing)} witnessed audit-trail entr{'y is' if len(missing) == 1 else 'ies are'} "
                  "missing or changed on UN PASS.", severity=severity,
                  evidence={"missing": len(missing), "first_missing": missing[0], "held": len(held),
                            "now": len(entries)}, key=",".join(missing[:5]))
        elif not _is_subsequence(held, entries):
            alarm("chain_broken", "Witnessed audit-trail entries are now in a different order.",
                  severity=severity, key="order")
        if len(entries) < len(held):
            alarm("chain_truncated", f"The audit trail has {len(entries)} entries; {len(held)} were witnessed.",
                  severity=severity, evidence={"held": len(held), "now": len(entries)}, key=str(len(entries)))
    if chain and not chain.get("ok", True):
        alarm("chain_broken", "UN PASS's own verification of this audit trail fails: "
                              + ", ".join(map(str, chain.get("problems") or [])),
              severity="critical" if rec["evidence"] else "warning",
              key=",".join(map(str, chain.get("problems") or [])))
    return found


def audit(node: Node, client: Client, batch: int, every_hours: float) -> dict:
    report = {"checked": 0, "findings": 0}
    cutoff = iso(now() - timedelta(hours=every_hours))
    rows = node.db.execute(
        "SELECT uid FROM envelopes WHERE gone=0 AND (last_audit IS NULL OR last_audit < ?) "
        "ORDER BY last_audit IS NOT NULL, last_audit LIMIT ?", (cutoff, batch)).fetchall()
    # Pending re-seals are re-checked every cycle until confirmed or alarmed.
    rows = node.db.execute("SELECT DISTINCT envelope FROM pending").fetchall() + rows
    # Plus a few envelopes picked at random every cycle, so nobody can time a
    # forgery to fall between two scheduled checks of the same document.
    rows += node.db.execute("SELECT uid FROM envelopes WHERE gone=0 ORDER BY random() LIMIT ?",
                            (max(1, batch // 5),)).fetchall()
    uids = list(dict.fromkeys(r[0] for r in rows))[:batch]
    if not uids:
        return report
    data, *_ = client.call("POST", "state/", payload={"envelopes": uids})
    for st in data.get("envelopes") or []:
        if not isinstance(st, dict) or st.get("envelope") not in uids:
            continue
        found = audit_envelope(node, st)
        node.db.execute("UPDATE envelopes SET last_audit=?, last_ok=? WHERE uid=?",
                        (iso(), 0 if found else 1, st["envelope"]))
        report["checked"] += 1
        report["findings"] += len(found)
    return report


# ═════════════════════════════════════════════════════════════════════════════
# 3. Checking this machine's own copy
# ═════════════════════════════════════════════════════════════════════════════

def selfcheck(node: Node) -> dict:
    out = {"ok": True, "blocks": 0, "problems": []}
    prev, expected = GENESIS, 1
    for seq, body, prev_hash, bh, sig in node.db.execute(
            "SELECT seq, body, prev_hash, block_hash, signature FROM blocks ORDER BY seq"):
        out["blocks"] += 1
        problem = ""
        if seq != expected or prev_hash != prev:
            problem = "broken_link"
        elif block_hash(seq, prev_hash, body) != bh:
            problem = "content_altered"
        else:
            try:
                node.server_key.verify(base64.b64decode(sig), bh.encode())
            except Exception:
                problem = "bad_signature"
        if problem:
            out["problems"].append({"seq": seq, "kind": problem})
        prev, expected = bh, seq + 1
    if not node.self_seal_ok():
        out["problems"].append({"seq": None, "kind": "self_seal"})
    out["ok"] = not out["problems"]
    return out


# ═════════════════════════════════════════════════════════════════════════════
# The loop
# ═════════════════════════════════════════════════════════════════════════════

def cycle(node: Node, client: Client) -> dict:
    """One full pass. Never raises for network trouble — that is reported."""
    try:
        return _cycle(node, client)
    finally:
        node.reseal()


#: The server suggests a policy; the Sentinel keeps it inside these bounds so
#: a compromised server cannot tell it to stop looking or to stop worrying.
POLICY_BOUNDS = {
    "poll_seconds": (60, 3600, 300),
    "audit_batch": (1, 25, 25),
    "audit_every_hours": (1, 168, 24),
    "silent_hours": (1, 72, 6),
}


def bounded_policy(raw: dict | None) -> dict:
    out = {}
    for key, (low, high, default) in POLICY_BOUNDS.items():
        value = (raw or {}).get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            value = default
        out[key] = min(high, max(low, value))
    return out


def _cycle(node: Node, client: Client) -> dict:
    report = {"at": iso()}
    policy = bounded_policy(node.config.get("policy"))
    trust = node.meta("trust_broken") != "1"

    check = selfcheck(node)
    if not check["ok"]:
        raise_alert(node, "local_store_altered",
                    f"This Sentinel's own copy of the ledger was altered on this machine: "
                    f"{check['problems'][:3]}", key=canonical(check["problems"][:3]))

    if trust:
        try:
            report["sync"] = sync_ledger(node, client)
            report["spot"] = spot_check(node, client)
            report["audit"] = audit(node, client, int(policy.get("audit_batch", 25)),
                                    float(policy.get("audit_every_hours", 24)))
            stats = {
                "blocks": node.db.execute("SELECT count(*) FROM blocks").fetchone()[0],
                "envelopes": node.db.execute("SELECT count(*) FROM envelopes").fetchone()[0],
                "alerts": node.db.execute("SELECT count(*) FROM alerts").fetchone()[0],
                "selfcheck": check["ok"],
            }
            data, *_ = client.call("POST", "heartbeat/", payload={
                "version": VERSION, "platform": f"{platform.system()} {platform.release()}"[:80],
                "stats": stats})
            if isinstance(data.get("policy"), dict):
                node.config["policy"] = bounded_policy(data["policy"])
                node.save_config()
            node.set_meta("last_contact", iso())
        except ServerIdentityError as exc:
            node.set_meta("trust_broken", "1")
            raise_alert(node, "server_identity_changed",
                        f"{exc} Either the server's key was replaced, or something is impersonating "
                        "UN PASS. This Sentinel will not trust the server again until it is re-enrolled.",
                        key="identity")
            trust = False
        except (SentinelError, OSError) as exc:
            report["error"] = str(exc)
            log.warning("%s", exc)

    last = parse_iso(node.meta("last_contact")) or parse_iso(node.config.get("enrolled_at", ""))
    silent = float(policy.get("silent_hours", 6))
    if last and now() - last > timedelta(hours=silent):
        raise_alert(node, "server_unreachable",
                    f"UN PASS has not answered this Sentinel since {iso(last)}. It may be down, blocked, "
                    "or someone may be stopping Sentinels from checking it.",
                    severity="warning", key=last.strftime("%Y-%m-%d"))

    report["alerts"] = dispatch_alerts(node, client, trust_server=trust)
    return report


@contextlib.contextmanager
def single_instance(home: Path):
    path = home / "sentinel.lock"
    handle = open(path, "a+")
    try:
        if IS_WINDOWS:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SentinelError("Another NES Sentinel is already running with this data folder.")
    try:
        yield
    finally:
        handle.close()


# ═════════════════════════════════════════════════════════════════════════════
# Commands
# ═════════════════════════════════════════════════════════════════════════════

def cmd_enroll(args, node: Node):
    if node.config_path.exists() and not args.force:
        raise SentinelError(f"Already enrolled ({node.config_path}). Use --force to enrol again; "
                            "the old identity will stop working.")
    server = check_server_url(args.server)
    node.prepare_home()
    # The key is only written once the server accepted it AND a person
    # confirmed the server's fingerprint, so a failed or refused enrolment
    # never disturbs an existing identity.
    key = Ed25519PrivateKey.generate()
    previous = dict(node.config) if node.config_path.exists() else {}
    node.config = {"server": server, "node_id": 0}
    if args.ca:
        node.config["ca_file"] = str(Path(args.ca).resolve())
    client = Client(node, server, args.ca)

    # The response is signed by the key it announces; we check that, then
    # a human checks the fingerprint out of band.
    data, raw, sig, nonce = client.call("POST", "enroll/", sign=False, payload={
        "code": args.code.strip(), "public_key": node.public_b64(key),
        "platform": f"{platform.system()} {platform.release()}"[:80], "version": VERSION})
    server_b64 = data.get("server_key", "")
    if not B64.match(server_b64 or ""):
        raise SentinelError("The server sent no publication key.")
    server_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(server_b64))
    try:
        server_key.verify(base64.b64decode(sig), f"{RESPONSE_TAG}\n{nonce}\n{sha256_hex(raw)}".encode())
    except Exception:
        raise ServerIdentityError("The enrolment answer was not signed by the key it contained.")
    fp = sha256_hex(base64.b64decode(server_b64))

    print(f"\nUN PASS server fingerprint:\n\n    {pretty_fp(fp)}\n")
    expected = re.sub(r"\s", "", (args.expect_fingerprint or "")).lower()
    if expected:
        if not fp.startswith(expected) or len(expected) < 32:
            raise SentinelError("The fingerprint does NOT match the one you were given. Do not use this server.")
    elif not args.yes:
        answer = input("Does this match the fingerprint ICT read to you? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            raise SentinelError("Not confirmed. Nothing was saved; ask ICT to revoke this enrolment.")

    node.write_key(key)
    node.config.update({
        "server": server, "node_id": int(data["node_id"]), "name": str(data.get("name", ""))[:120],
        "office_code": str(data.get("office_code", ""))[:40], "server_key": server_b64,
        "server_fingerprint": fp, "enrolled_at": iso(),
        "policy": bounded_policy(data.get("policy")),
        "alert_emails": previous.get("alert_emails", []),
        **({"email": previous["email"]} if previous.get("email") else {}),
        **({"webhook": previous["webhook"]} if previous.get("webhook") else {}),
    })
    if node.db_path.exists():
        node.db_path.rename(node.db_path.with_suffix(f".db.before-{int(time.time())}"))
    node.save_config()
    node.open_db()
    node.reseal()
    print(f"Enrolled as Sentinel #{node.config['node_id']} '{node.config['name']}'.")
    print(f"Data folder: {node.home}")
    print("Next: nes-sentinel set-email ...   (so it can raise the alarm without UN PASS)")
    print("      nes-sentinel run             (or install the service)")


def cmd_run(args, node: Node):
    node.load()
    client = Client(node)
    with single_instance(node.home):
        while True:
            try:
                report = cycle(node, client)
                log.info("cycle %s", canonical(report))
            except Exception as exc:          # never die silently in service mode
                log.exception("cycle failed: %s", exc)
            if args.once:
                return
            poll = bounded_policy(node.config.get("policy"))["poll_seconds"]
            time.sleep(poll + random.uniform(0, 30))


def cmd_verify(args, node: Node):
    node.load()
    results, bad = [], 0
    for name in args.files:
        digest = hashlib.sha256()
        with open(name, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        match = None
        for seq, envelope, body, received in node.db.execute(
                "SELECT seq, envelope, body, received_at FROM blocks WHERE kind='seal' ORDER BY seq"):
            b = json.loads(body)
            if b.get("document_sha256") == sha:
                match = {"seq": seq, "envelope": envelope, "sealed_at": b.get("sealed_at"),
                         "level": b.get("level"), "witnessed_at": received}
        results.append({"file": name, "sha256": sha, "match": match})
        bad += match is None
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            if r["match"]:
                m = r["match"]
                print(f"GENUINE   {r['file']}\n          envelope {m['envelope']}, sealed {m['sealed_at']} "
                      f"({m['level']}), witnessed here {m['witnessed_at']} — ledger block #{m['seq']}")
            else:
                print(f"UNKNOWN   {r['file']}\n          sha256 {r['sha256']}\n          This exact file was "
                      "never sealed by NES — or it was altered after sealing.")
    return 1 if bad else 0


def cmd_status(args, node: Node):
    node.load()
    seq, head = local_head(node)
    print(f"Sentinel      #{node.config.get('node_id')} {node.config.get('name')} "
          f"({node.config.get('office_code') or 'no office'})")
    print(f"Server        {node.config.get('server')}")
    print(f"Server key    {pretty_fp(node.config.get('server_fingerprint', ''))}")
    print(f"Trust         {'BROKEN — re-enrol' if node.meta('trust_broken') == '1' else 'ok'}")
    print(f"Ledger        {seq} blocks, head {head[:16]}…")
    env = node.db.execute("SELECT count(*), sum(gone), sum(last_ok=0) FROM envelopes").fetchone()
    print(f"Envelopes     {env[0]} witnessed, {env[1] or 0} gone, {env[2] or 0} with findings")
    print(f"Last contact  {node.meta('last_contact') or 'never'}")
    print(f"Alerts        {node.db.execute('SELECT count(*) FROM alerts').fetchone()[0]}")
    print(f"E-mail        {(node.config.get('email') or {}).get('host') or 'not set'}"
          f" → {', '.join(node.config.get('alert_emails') or []) or 'owners/watchers only'}")
    print(f"Webhook       {'set' if node.config.get('webhook') else 'not set'}")
    print(f"Self-check    {'ok' if selfcheck(node)['ok'] else 'FAILED'}")


def cmd_selfcheck(args, node: Node):
    node.load()
    result = selfcheck(node)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_alerts(args, node: Node):
    node.load()
    for row in node.db.execute("SELECT at, severity, kind, envelope, detail, reported, emailed "
                               "FROM alerts ORDER BY id DESC LIMIT ?", (args.limit,)):
        at, sev, kind, env, detail, rep, mail = row
        print(f"{at}  {sev:<8} {kind:<22} {env or '-':<32} server={'✓' if rep else '…'} "
              f"mail={'✓' if mail else '…'}\n    {detail}")


def cmd_set_email(args, node: Node):
    node.load()
    if args.to:
        bad = [e for e in args.to if not EMAIL.match(e)]
        if bad:
            raise SentinelError(f"Not e-mail addresses: {bad}")
        node.config["alert_emails"] = sorted(set(e.lower() for e in args.to))
    if args.host:
        if not EMAIL.match(args.sender or ""):
            raise SentinelError("--from must be an e-mail address.")
        node.config["email"] = {"host": args.host, "port": args.port, "user": args.user or "",
                                "from": args.sender, "security": args.security}
        if args.user:
            password = os.environ.get("NES_SENTINEL_SMTP_PASSWORD") or getpass.getpass("SMTP password: ")
            write_secret(node.smtp_secret, password.encode("utf-8"))
    node.save_config()
    print("Saved. Try it: nes-sentinel test-alert")


def cmd_set_webhook(args, node: Node):
    node.load()
    if args.url and not args.url.startswith("https://"):
        raise SentinelError("Webhooks must be https://")
    node.config["webhook"] = args.url or ""
    node.save_config()
    print("Saved." if args.url else "Webhook removed.")


def cmd_test_alert(args, node: Node):
    node.load()
    raise_alert(node, "test", "Test alert from this Sentinel — no action needed.",
                severity="warning", key=iso())
    print(json.dumps(dispatch_alerts(node, Client(node)), indent=2))
    node.reseal()


def cmd_export(args, node: Node):
    """A self-contained proof: every block for one envelope plus the server key."""
    node.load()
    rows = node.db.execute("SELECT seq, kind, envelope, body, prev_hash, block_hash, signature, received_at "
                           "FROM blocks WHERE envelope=? ORDER BY seq", (args.envelope,)).fetchall()
    if not rows:
        raise SentinelError("This Sentinel holds nothing for that envelope.")
    bundle = {
        "format": "nes-sentinel-evidence-v1",
        "exported_at": iso(), "sentinel": node.config.get("name"),
        "server": node.config.get("server"), "server_key": node.config.get("server_key"),
        "server_fingerprint": node.config.get("server_fingerprint"),
        "blocks": [dict(zip(["seq", "kind", "envelope", "body", "prev_hash", "block_hash", "signature",
                             "received_at"], r)) for r in rows],
        "how_to_check": "For each block: sha256('nes-sentinel-block-v1\\n'+seq+'\\n'+prev_hash+'\\n'+body) "
                        "== block_hash, and the Ed25519 signature over block_hash verifies with server_key.",
    }
    Path(args.output).write_text(json.dumps(bundle, indent=2), "utf-8")
    print(f"Wrote {len(rows)} signed blocks to {args.output}")


def cmd_check_bundle(args, node: Node):
    bundle = json.loads(Path(args.bundle).read_text("utf-8"))
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(bundle["server_key"]))
    ok = True
    for b in bundle["blocks"]:
        good = block_hash(b["seq"], b["prev_hash"], b["body"]) == b["block_hash"]
        try:
            key.verify(base64.b64decode(b["signature"]), b["block_hash"].encode())
        except Exception:
            good = False
        ok &= good
        print(f"#{b['seq']:<8} {b['kind']:<6} {'ok' if good else 'FAILED'}")
    print(f"\nServer key fingerprint {pretty_fp(sha256_hex(base64.b64decode(bundle['server_key'])))}")
    print("ALL BLOCKS VERIFY" if ok else "SOME BLOCKS DO NOT VERIFY")
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(prog="nes-sentinel", description="NES Sentinel — independent tamper witness.")
    p.add_argument("--home", type=Path, default=None, help="data folder (default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"NES Sentinel {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("enroll", help="join a UN PASS server with a one-time code")
    s.add_argument("--server", required=True)
    s.add_argument("--code", required=True)
    s.add_argument("--ca", help="extra CA bundle, if the server uses a private CA")
    s.add_argument("--expect-fingerprint", help="the fingerprint ICT gave you (skips the prompt)")
    s.add_argument("--yes", action="store_true", help="do not ask to confirm the fingerprint")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_enroll)

    s = sub.add_parser("run", help="watch continuously (what the service runs)")
    s.add_argument("--once", action="store_true", help="one pass, then exit")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("verify", help="is this exact file a document NES sealed?")
    s.add_argument("files", nargs="+")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_verify)

    sub.add_parser("status", help="what this Sentinel holds").set_defaults(func=cmd_status)
    sub.add_parser("selfcheck", help="re-verify this machine's copy").set_defaults(func=cmd_selfcheck)

    s = sub.add_parser("alerts", help="list alerts")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_alerts)

    s = sub.add_parser("set-email", help="how this Sentinel sends alarms itself")
    s.add_argument("--host"); s.add_argument("--port", type=int, default=587)
    s.add_argument("--user"); s.add_argument("--from", dest="sender")
    s.add_argument("--security", choices=["starttls", "ssl", "none"], default="starttls",
                   help="'none' is only accepted for a mail relay on localhost")
    s.add_argument("--to", nargs="*", help="this office's own alert recipients")
    s.set_defaults(func=cmd_set_email)

    s = sub.add_parser("set-webhook", help="Teams/Slack incoming webhook (https)")
    s.add_argument("url", nargs="?", default="")
    s.set_defaults(func=cmd_set_webhook)

    sub.add_parser("test-alert", help="send a test alarm through every channel").set_defaults(func=cmd_test_alert)

    s = sub.add_parser("export", help="write a portable, verifiable evidence file for one envelope")
    s.add_argument("envelope"); s.add_argument("-o", "--output", required=True)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("check-bundle", help="verify an evidence file offline")
    s.add_argument("bundle")
    s.set_defaults(func=cmd_check_bundle)
    return p


def setup_logging(node: Node, verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        node.home.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            node.home / "sentinel.log", maxBytes=5 << 20, backupCount=5, encoding="utf-8"))
    except OSError:
        pass
    if not IS_WINDOWS and Path("/dev/log").exists():
        try:
            sh = logging.handlers.SysLogHandler(address="/dev/log")
            sh.setFormatter(logging.Formatter("nes-sentinel: %(levelname)s %(message)s"))
            sh.setLevel(logging.WARNING)
            handlers.append(sh)
        except OSError:
            pass
    for h in handlers:
        if h.formatter is None:
            h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(level)


def main(argv=None) -> int:
    if not IS_WINDOWS:
        os.umask(0o077)
    args = build_parser().parse_args(argv)
    node = Node(args.home or default_home())
    setup_logging(node, args.verbose)
    try:
        return int(args.func(args, node) or 0)
    except KeyboardInterrupt:
        return 130
    except SentinelError as exc:
        print(f"nes-sentinel: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
