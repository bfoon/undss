# accounts/mail_graph.py
"""
Send UN PASS e-mail through Microsoft 365 with Microsoft Graph and OAuth —
no mailbox password, nothing for Microsoft's SMTP basic-auth retirement to
break.

Why
---
Microsoft disables password ("basic") SMTP AUTH by default for existing
Exchange Online tenants at the end of December 2026, and will remove it for
good later. `smtp.office365.com` with EMAIL_HOST_USER / EMAIL_HOST_PASSWORD
then stops working, with "535 5.7.139" or "550 5.7.30".

This backend signs in as an app registration (client credentials) and calls
`POST /users/{sender}/sendMail`. Setup, once, in Entra ID:

1. App registrations → New registration ("UN PASS mail"). No redirect URI.
2. API permissions → Microsoft Graph → *Application* → Mail.Send → Grant
   admin consent.
3. Certificates & secrets → New client secret. Copy the value.
4. Limit it to the one mailbox (strongly recommended), in Exchange Online
   PowerShell:
       New-ApplicationAccessPolicy -AppId <client id> -PolicyScopeGroupId unpass@your.org `
         -AccessRight RestrictAccess -Description "UN PASS may only send as unpass@"

Then in .env:

    EMAIL_BACKEND=accounts.mail_graph.GraphEmailBackend
    MS_GRAPH_TENANT_ID=<directory (tenant) id>
    MS_GRAPH_CLIENT_ID=<application (client) id>
    MS_GRAPH_CLIENT_SECRET=<secret value>
    DEFAULT_FROM_EMAIL=unpass@your.org

Standard library only. Attachments up to Graph's single-request limit
(about 3 MB after encoding) are sent inline; a larger message is refused
with a clear error rather than sent without its attachment.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parseaddr

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
MAX_REQUEST = 3_500_000
TIMEOUT = 30

_token_lock = threading.Lock()
_token: dict = {}


class GraphMailError(Exception):
    pass


def _post(url: str, data: bytes, headers: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1 << 20)


def access_token() -> str:
    """Client-credentials token, cached until five minutes before it expires."""
    with _token_lock:
        if _token.get("value") and _token.get("expires", 0) > time.time() + 300:
            return _token["value"]
        tenant = settings.MS_GRAPH_TENANT_ID
        body = urllib.parse.urlencode({
            "client_id": settings.MS_GRAPH_CLIENT_ID,
            "client_secret": settings.MS_GRAPH_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode()
        status, raw = _post(TOKEN_URL.format(tenant=urllib.parse.quote(tenant, safe="")), body,
                            {"Content-Type": "application/x-www-form-urlencoded"})
        data = json.loads(raw or b"{}")
        if status != 200 or "access_token" not in data:
            raise GraphMailError(
                f"Microsoft sign-in refused ({status}): "
                f"{data.get('error_description') or data.get('error') or raw[:200]!r}")
        _token.update(value=data["access_token"], expires=time.time() + int(data.get("expires_in", 3600)))
        return _token["value"]


def _recipients(addresses) -> list[dict]:
    out = []
    for item in addresses or []:
        name, email = parseaddr(item)
        if email:
            out.append({"emailAddress": {"address": email, **({"name": name} if name else {})}})
    return out


def _payload(message) -> dict:
    html = None
    for content, mimetype in getattr(message, "alternatives", []) or []:
        if mimetype == "text/html":
            html = content
    attachments = []
    for item in message.attachments or []:
        if isinstance(item, tuple):
            filename, content, mimetype = item
            if isinstance(content, str):
                content = content.encode("utf-8")
            attachments.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename or "attachment",
                "contentType": mimetype or "application/octet-stream",
                "contentBytes": base64.b64encode(content or b"").decode("ascii"),
            })
    body = {"contentType": "HTML", "content": html} if html is not None \
        else {"contentType": "Text", "content": message.body or ""}
    msg = {
        "subject": message.subject or "",
        "body": body,
        "toRecipients": _recipients(message.to),
        "ccRecipients": _recipients(message.cc),
        "bccRecipients": _recipients(message.bcc),
        "replyTo": _recipients(message.reply_to),
    }
    if attachments:
        msg["attachments"] = attachments
    return {"message": msg, "saveToSentItems": False}


class GraphEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages) -> int:
        sent = 0
        for message in email_messages or []:
            try:
                self._send(message)
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
                logger.exception("Graph mail: could not send %r", getattr(message, "subject", ""))
        return sent

    def _send(self, message):
        sender = parseaddr(message.from_email or settings.DEFAULT_FROM_EMAIL)[1]
        if not sender:
            raise GraphMailError("No sender: set DEFAULT_FROM_EMAIL to the mailbox UN PASS sends as.")
        data = json.dumps(_payload(message)).encode("utf-8")
        if len(data) > MAX_REQUEST:
            raise GraphMailError(
                f"Message is {len(data) // 1024} KB encoded; Graph's single-request limit is about "
                "3 MB. The signed copy is still available from the link in UN PASS.")
        for attempt in (1, 2):
            status, raw = _post(
                SEND_URL.format(sender=urllib.parse.quote(sender)), data,
                {"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"})
            if status == 202:
                return
            if status == 401 and attempt == 1:
                _token.clear()
                continue
            if status in (429, 503) and attempt == 1:
                time.sleep(5)
                continue
            try:
                err = json.loads(raw).get("error", {})
                detail = f"{err.get('code')}: {err.get('message')}"
            except Exception:
                detail = raw[:200]
            raise GraphMailError(f"Graph sendMail refused ({status}) — {detail}")
