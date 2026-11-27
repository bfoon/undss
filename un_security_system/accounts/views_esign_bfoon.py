# accounts/views_esign_bfoon.py
"""
BFoon endpoints. Same access rules as the documents themselves:

    /accounts/esign/s/<token>/bfoon/text/   recipient link (access code honoured)
    /accounts/esign/s/<token>/bfoon/ask/
    /accounts/esign/<pk>/bfoon/text/        signed-in party to the envelope
    /accounts/esign/<pk>/bfoon/ask/
"""
from __future__ import annotations

import json
import logging

from django.http import Http404, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import bfoon_esign as B

logger = logging.getLogger(__name__)


def _by_token(request, token):
    from .models_esign import Envelope
    from .views_esign import _access_ok, _recipient_or_404

    recipient = _recipient_or_404(token)
    if recipient.access_code and not _access_ok(request, recipient):
        raise Http404
    if recipient.envelope.status in (Envelope.STATUS_VOIDED, Envelope.STATUS_DRAFT):
        raise Http404
    return recipient.envelope, f"t:{recipient.pk}"


def _by_pk(request, pk):
    from .esign_access import get_envelope_or_404

    if not request.user.is_authenticated:
        raise Http404
    return get_envelope_or_404(request, pk, mode="view"), f"u:{request.user.pk}"


def _json(data, status=200):
    response = JsonResponse(data, status=status)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _text(envelope):
    data = B.document_text(envelope)
    return _json({"ok": True, "docs": data["docs"], "config": B.public_config()})


def _ask(request, envelope, who):
    try:
        body = json.loads(request.body.decode("utf-8") or "{}") if len(request.body) <= 8192 else {}
    except (UnicodeDecodeError, ValueError):
        body = {}
    mode = body.get("mode") if body.get("mode") in ("summary", "question", "page") else "summary"
    page = body.get("page") if isinstance(body.get("page"), dict) else None
    if not B.allow(who):
        return _json({"ok": False, "error": "BFoon needs a breather — try again in a little while."}, 429)
    try:
        result = B.ask(envelope, mode=mode, question=str(body.get("question") or ""), page=page)
    except Exception:
        logger.exception("BFoon: ask failed for envelope %s", envelope.pk)
        return _json({"ok": False, "error": "Something went wrong reading this document."}, 500)
    return _json({"ok": True, **result})


@require_GET
def bfoon_text_token(request, token):
    envelope, _who = _by_token(request, token)
    return _text(envelope)


@require_POST
def bfoon_ask_token(request, token):
    envelope, who = _by_token(request, token)
    return _ask(request, envelope, who)


@require_GET
def bfoon_text(request, pk):
    envelope, _who = _by_pk(request, pk)
    return _text(envelope)


@require_POST
def bfoon_ask(request, pk):
    envelope, who = _by_pk(request, pk)
    return _ask(request, envelope, who)


@require_GET
def bfoon_search(request):
    """Search everything this person may see: titles, people, files and the words inside."""
    if not request.user.is_authenticated:
        raise Http404
    if not B.allow(f"s:{request.user.pk}", per_hour=600):
        return _json({"ok": False, "error": "Too many searches — give it a minute."}, 429)
    data = B.search_documents(request.user, request.GET.get("q", ""))
    return _json({"ok": True, **data})


@require_GET
def bfoon_recent(request):
    if not request.user.is_authenticated:
        raise Http404
    return _json({"ok": True, "results": B.recent(request.user), "config": B.public_config()})
