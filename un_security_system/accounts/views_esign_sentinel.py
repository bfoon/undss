# accounts/views_esign_sentinel.py
"""
UN PASS — NES Sentinel endpoints and pages.

Machine API (called by installed Sentinels, never by browsers):

    POST /accounts/esign/sentinel/api/enroll/     one-time code -> node
    GET  /accounts/esign/sentinel/api/feed/       signed ledger blocks
    POST /accounts/esign/sentinel/api/state/      live fingerprints to compare
    POST /accounts/esign/sentinel/api/alert/      report a finding
    POST /accounts/esign/sentinel/api/heartbeat/  "still here"

Pages:

    /accounts/esign/sentinel/                    ICT: nodes, codes, alerts
    /accounts/esign/<pk>/watchers/               owner: who is told

The API is CSRF-exempt because it has no cookies to forge: every call is
authenticated by an Ed25519 signature over the exact request (see
sentinel_esign.authenticate). Errors are deliberately terse.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import sentinel_esign as S

logger = logging.getLogger(__name__)


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        raise S.SentinelError(400, "Body is not JSON.")
    if not isinstance(data, dict):
        raise S.SentinelError(400, "Body must be a JSON object.")
    return data


def _api(handler):
    """Shared plumbing: size cap, auth, signed responses, terse errors."""

    @csrf_exempt
    def view(request, *args, **kwargs):
        nonce = ""
        try:
            nonce = S.request_nonce(request)
            if not S.enabled():
                raise S.SentinelError(503, "Sentinels are switched off on this server.")
            body = S.read_body(request)
            data = handler(request, body, *args, **kwargs)
            return S.signed_json({"ok": True, **data}, nonce)
        except S.SentinelError as exc:
            if nonce:
                return S.signed_json({"ok": False, "error": exc.message}, nonce, exc.status)
            from django.http import JsonResponse
            return JsonResponse({"ok": False, "error": exc.message}, status=exc.status)
        except Exception:
            logger.exception("NES Sentinel API failure")
            if nonce:
                return S.signed_json({"ok": False, "error": "Server error."}, nonce, 500)
            from django.http import JsonResponse
            return JsonResponse({"ok": False, "error": "Server error."}, status=500)

    return view


def _method(request, *allowed):
    if request.method not in allowed:
        raise S.SentinelError(405, "Method not allowed.")


def _policy() -> dict:
    from django.conf import settings

    return {
        "poll_seconds": int(getattr(settings, "ESIGN_SENTINEL_POLL_SECONDS", 300)),
        "audit_batch": min(S.STATE_MAX, int(getattr(settings, "ESIGN_SENTINEL_AUDIT_BATCH", 25))),
        "audit_every_hours": float(getattr(settings, "ESIGN_SENTINEL_AUDIT_EVERY_HOURS", 24)),
        "silent_hours": float(getattr(settings, "ESIGN_SENTINEL_SILENT_HOURS", 6)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Machine API
# ─────────────────────────────────────────────────────────────────────────────

@_api
def api_enroll(request, body):
    _method(request, "POST")
    ip = S._client_ip(request) or "unknown"
    key = f"nes:sentinel:enroll:{ip}"
    tries = cache.get(key, 0)
    if tries >= 10:
        raise S.SentinelError(429, "Too many enrolment attempts. Try again in an hour.")
    cache.set(key, tries + 1, 3600)

    data = _json(body)
    code = str(data.get("code") or "")
    public = str(data.get("public_key") or "")
    if not code or not public:
        raise S.SentinelError(400, "code and public_key are required.")
    node = S.consume_enrolment(code, public, str(data.get("platform") or ""),
                               str(data.get("version") or ""), S._client_ip(request))
    server_key = S.public_key_b64()
    return {
        "protocol": S.PROTOCOL,
        "node_id": node.pk,
        "name": node.name,
        "office_code": node.office_code,
        "server_key": server_key,
        "server_key_fingerprint": S.fingerprint(server_key),
        "head": S.ledger_head(),
        "policy": _policy(),
    }


@_api
def api_feed(request, body):
    from .models_esign_sentinel import SentinelBlock, SentinelNode

    _method(request, "GET")
    node = S.authenticate(request, body)
    try:
        after = max(0, int(request.GET.get("after", "0")))
        limit = max(1, min(S.FEED_MAX, int(request.GET.get("limit", "500"))))
    except ValueError:
        raise S.SentinelError(400, "after and limit must be integers.")
    blocks = [b.as_wire() for b in SentinelBlock.objects.filter(seq__gt=after).order_by("seq")[:limit]]
    if after > node.last_seq:
        SentinelNode.objects.filter(pk=node.pk).update(last_seq=after)
    return {"head": S.ledger_head(), "blocks": blocks,
            "server_time": timezone.now().isoformat(), "policy": _policy()}


@_api
def api_state(request, body):
    _method(request, "POST")
    S.authenticate(request, body)
    data = _json(body)
    uids = data.get("envelopes")
    if not isinstance(uids, list) or not uids:
        raise S.SentinelError(400, "envelopes must be a non-empty list.")
    if len(uids) > S.STATE_MAX:
        raise S.SentinelError(400, f"At most {S.STATE_MAX} envelopes per request.")
    clean = []
    for uid in uids:
        if not isinstance(uid, str) or not S.UID.match(uid):
            raise S.SentinelError(400, "Malformed envelope id.")
        clean.append(uid)
    return {"envelopes": S.envelope_state(clean)}


@_api
def api_alert(request, body):
    _method(request, "POST")
    node = S.authenticate(request, body)
    alert, created = S.record_alert(node, _json(body))
    return {"alert_id": alert.pk, "created": created}


@_api
def api_heartbeat(request, body):
    from .models_esign_sentinel import SentinelNode

    _method(request, "POST")
    node = S.authenticate(request, body)
    data = _json(body)
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    safe = {str(k)[:40]: v for k, v in list(stats.items())[:20]
            if isinstance(v, (int, float, str, bool)) and len(str(v)) <= 200}
    SentinelNode.objects.filter(pk=node.pk).update(
        stats=safe, version=str(data.get("version") or "")[:20],
        platform=str(data.get("platform") or node.platform)[:80],
    )
    return {"head": S.ledger_head(), "policy": _policy(),
            "server_time": timezone.now().isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# ICT page
# ─────────────────────────────────────────────────────────────────────────────

def _require_ict(user):
    from .permissions import is_ict_focal

    if not is_ict_focal(user):
        raise PermissionDenied("Only ICT focal points can manage Sentinels.")


@login_required
def sentinel_home(request):
    from .models_esign_sentinel import (
        SentinelAlert, SentinelBlock, SentinelEnrolment, SentinelNode,
    )

    _require_ict(request.user)
    new_code = None

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "code":
            name = (request.POST.get("name") or "").strip()
            if not name:
                messages.error(request, "Give the Sentinel a name, e.g. the office that will run it.")
            else:
                new_code = S.create_enrolment(
                    name, (request.POST.get("office_code") or "").strip(), request.user,
                )
        elif action == "revoke":
            node = get_object_or_404(SentinelNode, pk=request.POST.get("node"))
            node.status = SentinelNode.STATUS_REVOKED
            node.revoked_at = timezone.now()
            node.revoked_by = request.user
            node.save(update_fields=["status", "revoked_at", "revoked_by"])
            messages.success(request, f"Sentinel '{node.name}' revoked. Its key no longer works.")
            return redirect("accounts:esign_sentinel_home")
        elif action == "resolve":
            alert = get_object_or_404(SentinelAlert, pk=request.POST.get("alert"))
            note = (request.POST.get("note") or "").strip()
            if not note:
                messages.error(request, "Say what you found before closing an alert.")
            else:
                alert.resolved_at = timezone.now()
                alert.resolved_by = request.user
                alert.resolution_note = note[:2000]
                alert.save(update_fields=["resolved_at", "resolved_by", "resolution_note"])
                messages.success(request, "Alert closed.")
            return redirect("accounts:esign_sentinel_home")
        elif action == "test":
            S.server_alert("test", "Test alert sent from the Sentinel page by "
                                   f"{request.user.get_username()}.", severity="warning")
            messages.success(request, "Test alert queued — ICT will receive it within two minutes.")
            return redirect("accounts:esign_sentinel_home")
        else:
            return HttpResponseNotAllowed(["GET", "POST"])

    try:
        server_key = S.public_key_b64()
        server_fp = S.pretty_fingerprint(S.fingerprint(server_key))
    except Exception as exc:
        server_key, server_fp = "", ""
        messages.warning(request, str(getattr(exc, "message", exc)))

    from django.conf import settings
    silent_after = timezone.now() - timedelta(
        hours=float(getattr(settings, "ESIGN_SENTINEL_SILENT_HOURS", 6)))
    nodes = list(SentinelNode.objects.all())
    for node in nodes:
        node.is_silent = node.is_active and (not node.last_seen_at or node.last_seen_at < silent_after)

    context = {
        "enabled": S.enabled(),
        "server_fp": server_fp,
        "server_url": request.build_absolute_uri("/").rstrip("/"),
        "head": S.ledger_head(),
        "block_count": SentinelBlock.objects.count(),
        "nodes": nodes,
        "codes": SentinelEnrolment.objects.filter(used_at__isnull=True,
                                                  expires_at__gt=timezone.now())[:20],
        "alerts": SentinelAlert.objects.filter(resolved_at__isnull=True)
                  .select_related("node")[:100],
        "closed": SentinelAlert.objects.filter(resolved_at__isnull=False)
                  .select_related("node", "resolved_by")[:20],
        "new_code": new_code,
    }
    return render(request, "accounts/esign/sentinel.html", context)


# ─────────────────────────────────────────────────────────────────────────────
# Owner page: watchers
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def envelope_watchers(request, pk):
    from .esign_access import can_view, is_owner
    from .models_esign import Envelope
    from .models_esign_sentinel import EnvelopeWatcher

    envelope = get_object_or_404(Envelope, pk=pk)
    if not can_view(request.user, envelope):
        raise PermissionDenied
    may_edit = is_owner(request.user, envelope) or request.user.is_superuser

    if request.method == "POST":
        if not may_edit:
            raise PermissionDenied("Only the owner of this document chooses its watchers.")
        action = request.POST.get("action")
        if action == "add":
            ident = (request.POST.get("who") or "").strip()
            User = get_user_model()
            user = (User.objects.filter(email__iexact=ident, is_active=True).first()
                    or User.objects.filter(username__iexact=ident, is_active=True).first())
            if user is None:
                messages.error(request, "No active user with that e-mail or username.")
            elif not user.email:
                messages.error(request, "That user has no e-mail address, so could not be told.")
            elif user.pk == envelope.created_by_id:
                messages.info(request, "The owner is always told; no need to add them.")
            elif EnvelopeWatcher.objects.filter(envelope=envelope).count() >= 20:
                messages.error(request, "A document can have at most 20 watchers.")
            else:
                _, made = EnvelopeWatcher.objects.get_or_create(
                    envelope=envelope, user=user, defaults={"added_by": request.user})
                if made:
                    messages.success(request, f"{user.get_full_name() or user.email} will be "
                                              "told if this document is tampered with.")
        elif action == "remove":
            EnvelopeWatcher.objects.filter(envelope=envelope, pk=request.POST.get("watcher")).delete()
            messages.success(request, "Watcher removed.")
        return redirect("accounts:esign_envelope_watchers", pk=envelope.pk)

    from .models_esign_sentinel import SentinelAlert, SentinelBlock, SentinelNode
    context = {
        "envelope": envelope,
        "watchers": EnvelopeWatcher.objects.filter(envelope=envelope).select_related("user", "added_by"),
        "may_edit": may_edit,
        "blocks": SentinelBlock.objects.filter(envelope_uid=envelope.envelope_id).count(),
        "alerts": SentinelAlert.objects.filter(envelope_uid=envelope.envelope_id).select_related("node")[:20],
        "nodes": SentinelNode.objects.filter(status=SentinelNode.STATUS_ACTIVE).count(),
    }
    return render(request, "accounts/esign/watchers.html", context)
