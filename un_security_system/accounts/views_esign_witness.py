# accounts/views_esign_witness.py
"""
UN PASS — NES witnessing endpoints.

Two machine endpoints and one page.

    POST /accounts/esign/witness/receive/   a peer deposits an attestation
    GET  /accounts/esign/witness/lookup/    a peer asks what we hold
    GET  /accounts/esign/witness/           status page for ICT

Security notes, because the first two are reachable by other offices:

* **Bearer token** identifies which peer is calling, compared in constant time
  so the endpoint cannot be used as a timing oracle to recover a token.
* **The token is not the authority.** Whether we believe a statement depends
  on its detached CMS signature matching the certificate registered for that
  peer. A leaked token lets someone talk to us; it does not let them say
  anything we will store.
* **A peer may only speak for itself** — a statement whose `origin_code` is
  not the calling peer's code is rejected and raises a critical alert, because
  the only reason to try it is to forge for another office.
* **Bounded input.** The body is read up to a fixed ceiling and parsed as
  JSON; attestations are a few hundred bytes.
* **No enumeration.** Lookup answers only for an exact envelope id, never a
  list, so the endpoint cannot be walked to discover what documents exist.
"""

from __future__ import annotations

import hmac
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import witness_esign as W

logger = logging.getLogger(__name__)

MAX_BODY = 64 * 1024


def _bearer(request) -> str:
    header = request.META.get("HTTP_AUTHORIZATION", "") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _peer_for_request(request):
    """
    Identify the calling peer by its inbound token.

    Every active peer's token is compared, in constant time, and the loop is
    not short-circuited on a match so the work done does not depend on which
    peer called.
    """
    from .models_esign_witness import WitnessPeer

    presented = _bearer(request)
    if not presented:
        return None

    found = None
    for peer in WitnessPeer.objects.filter(active=True):
        if not peer.inbound_token:
            continue
        if hmac.compare_digest(str(peer.inbound_token), presented):
            found = peer
    return found


@csrf_exempt
@require_POST
def witness_receive(request):
    """Accept one attestation from a participating office."""
    peer = _peer_for_request(request)
    if peer is None:
        logger.warning(
            "NES witness: rejected a deposit with an unknown token from %s",
            request.META.get("REMOTE_ADDR", "?"),
        )
        return JsonResponse({"ok": False, "error": "Unauthorised."}, status=401)

    raw = request.body[:MAX_BODY + 1]
    if len(raw) > MAX_BODY:
        return JsonResponse(
            {"ok": False, "error": "Payload too large."}, status=413
        )

    try:
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except Exception:
        return JsonResponse(
            {"ok": False, "error": "Malformed JSON."}, status=400
        )

    result = W.receive_statement(payload, peer)
    if not result["ok"]:
        return JsonResponse(
            {"ok": False, "error": result["detail"]}, status=400
        )

    return JsonResponse({
        "ok": True,
        "stored": result["stored"],
        "detail": result["detail"],
    })


@require_GET
def witness_lookup(request):
    """
    Report what we hold for one envelope.

    Exact-match only. There is no listing endpoint on purpose: a peer should
    be able to confirm a document it already knows about, not browse ours.
    """
    peer = _peer_for_request(request)
    if peer is None:
        return JsonResponse({"ok": False, "error": "Unauthorised."}, status=401)

    uid = str(request.GET.get("envelope_uid") or "").strip()[:64]
    origin = str(request.GET.get("origin_code") or "").strip()[:32]
    if not uid:
        return JsonResponse(
            {"ok": False, "error": "envelope_uid is required."}, status=400
        )

    attestations = W.local_attestations(uid, origin)
    return JsonResponse({
        "ok": True,
        "envelope_uid": uid,
        "count": len(attestations),
        "attestations": attestations,
    })


@login_required
@require_GET
def witness_status(request):
    """Status page: peers, delivery backlog, and outstanding alerts."""
    from .models_esign_witness import (
        WitnessAlert, WitnessDelivery, WitnessPeer, WitnessRecord,
    )

    peers = list(WitnessPeer.objects.order_by("code"))
    for peer in peers:
        peer.pending_count = WitnessDelivery.objects.filter(
            peer=peer, status=WitnessDelivery.STATUS_PENDING
        ).count()
        peer.failed_count = WitnessDelivery.objects.filter(
            peer=peer, status=WitnessDelivery.STATUS_FAILED
        ).count()
        peer.held_count = WitnessRecord.objects.filter(received_from=peer).count()

    context = {
        "enabled": W.witnessing_enabled(),
        "office_code": W.office_code(),
        "peers": peers,
        "alert_recipients": W.alert_recipients(),
        "records_ours": WitnessRecord.objects.filter(
            received_from__isnull=True
        ).count(),
        "records_held": WitnessRecord.objects.filter(
            received_from__isnull=False
        ).count(),
        "pending": WitnessDelivery.objects.filter(
            status=WitnessDelivery.STATUS_PENDING
        ).count(),
        "failed": WitnessDelivery.objects.filter(
            status=WitnessDelivery.STATUS_FAILED
        ).count(),
        "alerts": WitnessAlert.objects.filter(resolved=False).order_by(
            "-severity", "-raised_at"
        )[:50],
        "alert_count": WitnessAlert.objects.filter(resolved=False).count(),
    }
    return render(request, "accounts/esign/witness_status.html", context)
