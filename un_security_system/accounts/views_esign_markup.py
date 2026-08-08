# accounts/views_esign_markup.py
"""
UN PASS — eSign markup API (highlight / freehand / area / strikeout / note pin
plus a Word-style comment thread on each mark).

Two access paths, one implementation:

    tokenized (no login)                internal (login required)
    ────────────────────                ─────────────────────────
    esign_markup_list    <token>        esign_markup_list_internal    <pk>
    esign_markup_add     <token>        esign_markup_add_internal     <pk>
    esign_markup_reply   <token>/<id>   esign_markup_reply_internal   <pk>/<id>
    esign_markup_resolve <token>/<id>   esign_markup_resolve_internal <pk>/<id>
    esign_markup_delete  <token>/<id>   esign_markup_delete_internal  <pk>/<id>

Every endpoint returns JSON. Requests carry a JSON body and the CSRF token in
the X-CSRFToken header — the tokenized pages are anonymous but still CSRF
protected, because the recipient link is the only credential and a forged POST
would otherwise write to the envelope in the recipient's name.
"""

import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models_esign import Envelope, EnvelopeDocument, EnvelopeRecipient
from .models_esign_markup import (
    MARKUP_COLOR_MAP,
    EnvelopeAnnotation,
    EnvelopeAnnotationReply,
    clean_geometry,
    markup_colors_json,
)
from .utils_esign import client_ip, log_event

logger = logging.getLogger(__name__)

MAX_TEXT = 4000


# ─────────────────────────────────────────────────────────────────────────────
# Access context — resolves "who is asking and what may they do"
# ─────────────────────────────────────────────────────────────────────────────

class _Ctx:
    """Everything the shared handlers need, whichever door the caller came in."""

    def __init__(self, envelope, recipient=None, user=None,
                 can_annotate=False, can_moderate=False, see_internal=False):
        self.envelope = envelope
        self.recipient = recipient
        self.user = user if (user and getattr(user, "is_authenticated", False)) else None
        self.can_annotate = can_annotate
        self.can_moderate = can_moderate
        self.see_internal = see_internal

    @property
    def viewer_key(self) -> str:
        if self.recipient:
            return f"r:{self.recipient.pk}"
        if self.user:
            return f"u:{self.user.pk}"
        return ""

    @property
    def display_name(self) -> str:
        if self.recipient:
            return self.recipient.name
        if self.user:
            return self.user.get_full_name() or self.user.username
        return "Unknown"


def _token_ctx(request, token) -> _Ctx:
    """Recipient arriving on a tokenized link."""
    from .views_esign import _access_ok, _recipient_or_404  # lazy: avoids a cycle

    recipient = _recipient_or_404(token)
    if recipient.access_code and not _access_ok(request, recipient):
        raise Http404()

    envelope = recipient.envelope
    # Markup is a PRE-signature review activity. It closes on two conditions,
    # either of which is enough:
    #
    #   1. the envelope itself is no longer in play, and
    #   2. this recipient has already had their say.
    #
    # (2) is the important one: a signature attests to the document as it stood
    # at that moment, so letting a signer keep marking it afterwards would leave
    # the audit trail claiming they annotated a document they had already
    # executed. Existing marks stay readable and resolvable — only new ones stop.
    envelope_open = envelope.status in (Envelope.STATUS_SENT, Envelope.STATUS_RETURNED)
    recipient_open = recipient.status not in (
        EnvelopeRecipient.STATUS_SIGNED,
        EnvelopeRecipient.STATUS_DECLINED,
    )

    return _Ctx(
        envelope,
        recipient=recipient,
        can_annotate=envelope_open and recipient_open,
        can_moderate=False,
        see_internal=False,
    )


def _internal_ctx(request, pk) -> _Ctx:
    """Sender / ICT / Ops arriving from inside UN PASS."""
    from .views_esign import _can_manage_envelope, _get_envelope_for_user  # lazy

    envelope = _get_envelope_for_user(request, pk)
    frozen = envelope.status in (Envelope.STATUS_VOIDED, Envelope.STATUS_EXPIRED)

    return _Ctx(
        envelope,
        user=request.user,
        can_annotate=not frozen,
        can_moderate=_can_manage_envelope(request.user, envelope),
        see_internal=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _body(request) -> dict:
    try:
        return json.loads((request.body or b"{}").decode("utf-8")) or {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _queryset(ctx: _Ctx):
    qs = (
        EnvelopeAnnotation.objects
        .filter(envelope=ctx.envelope, revision=ctx.envelope.revision)
        .select_related("document", "recipient", "author_user")
        .prefetch_related("replies", "replies__recipient", "replies__author_user")
    )
    if not ctx.see_internal:
        qs = qs.filter(is_internal=False)
    return qs


def _payload(ctx: _Ctx, extra=None) -> JsonResponse:
    items = [
        a.as_dict(viewer_key=ctx.viewer_key, can_moderate=ctx.can_moderate)
        for a in _queryset(ctx)
    ]
    data = {
        "ok": True,
        "annotations": items,
        "can_annotate": ctx.can_annotate,
        "can_internal": ctx.see_internal,
        "colors": markup_colors_json(),
        "me": ctx.display_name,
        "revision": ctx.envelope.revision,
    }
    if extra:
        data.update(extra)
    return JsonResponse(data)


def _stamp_author(obj, ctx: _Ctx):
    obj.recipient = ctx.recipient
    obj.author_user = ctx.user
    obj.author_name = ctx.display_name


def _notify_sender(request, ctx: _Ctx, annotation, text, is_reply=False):
    """
    Tell the sender a mark was left — reusing the existing comment email so the
    eSign flow keeps one notification style. Never let mail break the write.
    """
    if ctx.recipient is None:
        return  # the sender marking up their own envelope needs no email
    try:
        from . import esign_notify

        verb = "replied on" if is_reply else "marked up"
        page_ref = f"page {annotation.page}"
        esign_notify.notify_comment(
            request,
            ctx.envelope,
            ctx.display_name,
            f"[{annotation.color_label} — {verb} {page_ref}] {text}",
        )
    except Exception:
        logger.exception("eSign markup: could not queue notification for envelope %s",
                         ctx.envelope.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Core handlers
# ─────────────────────────────────────────────────────────────────────────────

def _do_list(request, ctx: _Ctx):
    return _payload(ctx)


def _do_add(request, ctx: _Ctx):
    if not ctx.can_annotate:
        return JsonResponse(
            {"ok": False, "error": "This envelope is no longer open for markup."},
            status=403,
        )

    data = _body(request)

    kind = (data.get("kind") or "").strip()
    if kind not in dict(EnvelopeAnnotation.KIND_CHOICES):
        return JsonResponse({"ok": False, "error": "Unknown markup tool."}, status=400)

    color = (data.get("color") or "").strip()
    if color not in MARKUP_COLOR_MAP:
        color = "yellow"

    try:
        document = EnvelopeDocument.objects.get(
            pk=int(data.get("document_id") or 0), envelope=ctx.envelope
        )
    except (EnvelopeDocument.DoesNotExist, TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Unknown document."}, status=400)

    try:
        page = max(1, int(data.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    if document.page_count and page > document.page_count:
        return JsonResponse({"ok": False, "error": "That page does not exist."}, status=400)

    geometry = clean_geometry(kind, data.get("geometry"))
    if not geometry:
        return JsonResponse({"ok": False, "error": "That mark had no usable shape."}, status=400)

    text = (data.get("text") or "").strip()[:MAX_TEXT]
    if not text:
        return JsonResponse({"ok": False, "error": "Please write a comment first."}, status=400)

    internal = bool(data.get("is_internal")) and ctx.see_internal

    annotation = EnvelopeAnnotation(
        envelope=ctx.envelope,
        document=document,
        page=page,
        kind=kind,
        color=color,
        geometry=geometry,
        text=text,
        is_internal=internal,
        revision=ctx.envelope.revision,
        ip=client_ip(request),
    )
    _stamp_author(annotation, ctx)
    annotation.save()

    log_event(
        ctx.envelope,
        "commented",
        request=request,
        recipient=ctx.recipient,
        actor=ctx.user,
        note=(
            f"{'[internal] ' if internal else ''}"
            f"{annotation.get_kind_display()} ({annotation.color_label}) "
            f"on page {page}: {text[:120]}"
        ),
        meta={"annotation_id": annotation.pk, "page": page, "kind": kind, "color": color},
    )

    if not internal:
        _notify_sender(request, ctx, annotation, text)

    return _payload(
        ctx,
        {"annotation": annotation.as_dict(viewer_key=ctx.viewer_key,
                                          can_moderate=ctx.can_moderate)},
    )


def _annotation_or_404(ctx: _Ctx, pk):
    qs = EnvelopeAnnotation.objects.filter(envelope=ctx.envelope)
    if not ctx.see_internal:
        qs = qs.filter(is_internal=False)
    return get_object_or_404(qs, pk=pk)


def _do_reply(request, ctx: _Ctx, pk):
    if not ctx.can_annotate:
        return JsonResponse(
            {"ok": False, "error": "This envelope is no longer open for markup."},
            status=403,
        )

    annotation = _annotation_or_404(ctx, pk)
    text = (_body(request).get("text") or "").strip()[:MAX_TEXT]
    if not text:
        return JsonResponse({"ok": False, "error": "Please write a reply first."}, status=400)

    reply = EnvelopeAnnotationReply(annotation=annotation, text=text, ip=client_ip(request))
    _stamp_author(reply, ctx)
    reply.save()

    log_event(
        ctx.envelope,
        "commented",
        request=request,
        recipient=ctx.recipient,
        actor=ctx.user,
        note=f"Reply on page {annotation.page}: {text[:140]}",
        meta={"annotation_id": annotation.pk, "reply_id": reply.pk},
    )

    if not annotation.is_internal:
        _notify_sender(request, ctx, annotation, text, is_reply=True)

    return _payload(ctx)


def _do_resolve(request, ctx: _Ctx, pk):
    annotation = _annotation_or_404(ctx, pk)

    annotation.resolved = not annotation.resolved
    annotation.resolved_at = timezone.now() if annotation.resolved else None
    annotation.resolved_by_name = ctx.display_name if annotation.resolved else ""
    annotation.save(update_fields=["resolved", "resolved_at", "resolved_by_name", "updated_at"])

    log_event(
        ctx.envelope,
        "commented",
        request=request,
        recipient=ctx.recipient,
        actor=ctx.user,
        note=(
            f"{'Resolved' if annotation.resolved else 'Reopened'} a mark on "
            f"page {annotation.page} ({annotation.color_label})"
        ),
        meta={"annotation_id": annotation.pk, "resolved": annotation.resolved},
    )
    return _payload(ctx)


def _do_delete(request, ctx: _Ctx, pk):
    annotation = _annotation_or_404(ctx, pk)

    if not (ctx.can_moderate or annotation.owner_key() == ctx.viewer_key):
        return JsonResponse(
            {"ok": False, "error": "You can only remove your own markup."}, status=403
        )

    page, label = annotation.page, annotation.color_label
    annotation.delete()

    log_event(
        ctx.envelope,
        "commented",
        request=request,
        recipient=ctx.recipient,
        actor=ctx.user,
        note=f"Removed a mark on page {page} ({label})",
    )
    return _payload(ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Tokenized entry points (recipients)
# ─────────────────────────────────────────────────────────────────────────────

@require_GET
def esign_markup_list(request, token):
    return _do_list(request, _token_ctx(request, token))


@require_POST
def esign_markup_add(request, token):
    return _do_add(request, _token_ctx(request, token))


@require_POST
def esign_markup_reply(request, token, pk):
    return _do_reply(request, _token_ctx(request, token), pk)


@require_POST
def esign_markup_resolve(request, token, pk):
    return _do_resolve(request, _token_ctx(request, token), pk)


@require_POST
def esign_markup_delete(request, token, pk):
    return _do_delete(request, _token_ctx(request, token), pk)


# ─────────────────────────────────────────────────────────────────────────────
# Internal entry points (sender / ICT / Ops)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_markup_list_internal(request, pk):
    return _do_list(request, _internal_ctx(request, pk))


@login_required
@require_POST
def esign_markup_add_internal(request, pk):
    return _do_add(request, _internal_ctx(request, pk))


@login_required
@require_POST
def esign_markup_reply_internal(request, pk, ann_id):
    return _do_reply(request, _internal_ctx(request, pk), ann_id)


@login_required
@require_POST
def esign_markup_resolve_internal(request, pk, ann_id):
    return _do_resolve(request, _internal_ctx(request, pk), ann_id)


@login_required
@require_POST
def esign_markup_delete_internal(request, pk, ann_id):
    return _do_delete(request, _internal_ctx(request, pk), ann_id)


# ─────────────────────────────────────────────────────────────────────────────
# Template helper — used by the pages that mount the markup layer
# ─────────────────────────────────────────────────────────────────────────────

def markup_context(envelope, recipient=None, user=None) -> dict:
    """
    Context the _markup.html include expects. Call it from any view that
    renders a document viewer:

        ctx.update(markup_context(envelope, recipient=recipient))
    """
    if recipient is not None:
        can = (
            envelope.status in (Envelope.STATUS_SENT, Envelope.STATUS_RETURNED)
            and recipient.status not in (
                EnvelopeRecipient.STATUS_SIGNED,
                EnvelopeRecipient.STATUS_DECLINED,
            )
        )
        return {
            "markup_can_annotate": can,
            "markup_colors": markup_colors_json(),
            "markup_scope": "token",
            "markup_can_internal": False,
        }

    frozen = envelope.status in (Envelope.STATUS_VOIDED, Envelope.STATUS_EXPIRED)
    return {
        "markup_can_annotate": not frozen,
        "markup_colors": markup_colors_json(),
        "markup_scope": "internal",
        "markup_can_internal": True,
    }
