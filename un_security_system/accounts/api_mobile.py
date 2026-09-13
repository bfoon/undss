"""
UN PASS — the API the phone app talks to.

Deliberately small and dependency-free: plain Django views returning JSON, so
nothing new has to be installed and no model or migration is added.

Sign-in mirrors the website exactly — password, then a one-time code by email,
then the device is remembered for 30 days — by calling the very same helpers
(`create_otp_for_user`, `send_otp_email_async`, `remember_device`) and the same
`OneTimeCode` and `TrustedDevice` tables. A phone that has been remembered
skips the code next time, just as a browser does.

The session cookie Django already issues is what keeps the app signed in, so
there are no API keys to store or leak. Every endpoint answers JSON, including
its errors, so the app never has to parse an HTML error page.
"""

import json
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Asset, TrustedDevice
from .utils import create_otp_for_user, remember_device, send_otp_email_async

API_VERSION = "1.0"
MIN_DEVICE_ID = 16          # the app sends a hash; anything shorter isn't a fingerprint


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")) or None


def _body(request):
    try:
        data = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _fail(message, status=400, **extra):
    return JsonResponse({"ok": False, "error": message, **extra}, status=status)


def api(view):
    """JSON in, JSON out — and never an HTML error page, whatever goes wrong."""

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except Exception:  # noqa: BLE001 - the app must always get JSON back
            import logging

            logging.getLogger(__name__).exception("Mobile API error in %s", view.__name__)
            return _fail("Something went wrong on the server. Please try again.", 500)

    return wrapper


def signed_in(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _fail("Please sign in again.", 401, signed_out=True)
        return view(request, *args, **kwargs)

    return wrapper


def _user_json(user):
    office = getattr(user, "country_office", None)
    agency = getattr(user, "agency", None) or getattr(office, "agency", None)
    return {
        "id": user.pk,
        "name": user.get_full_name() or user.username,
        "username": user.username,
        "email": user.email,
        "role": getattr(user, "role", "") or "",
        "agency": getattr(agency, "name", "") or "",
        "office": getattr(office, "name", "") or "",
        "initials": "".join(p[0] for p in (user.get_full_name() or user.username).split()[:2]).upper(),
    }


def _device_trusted(user, device_id):
    return TrustedDevice.objects.filter(
        user=user, device_id=device_id, is_active=True, expires_at__gt=timezone.now()
    ).exists()


# ─────────────────────────────────────────────────────────────────────────────
# Signing in
# ─────────────────────────────────────────────────────────────────────────────

@api
@require_GET
def ping(request):
    """Lets the app check the address it was given before anyone types a password."""
    return JsonResponse({"ok": True, "service": "UN PASS", "api": API_VERSION,
                         "signed_in": request.user.is_authenticated})


@api
@require_POST
def auth_login(request):
    """
    Step one: username and password, plus this phone's fingerprint.

    Answers either `otp_required` — a code has been emailed — or, for a phone
    already remembered, signs straight in.
    """
    data = _body(request)
    if data is None:
        return _fail("The request couldn't be read.")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    device_id = str(data.get("device_id") or "").strip()
    device_name = str(data.get("device_name") or "")[:255]
    if not username or not password:
        return _fail("Enter your username and password.")
    if len(device_id) < MIN_DEVICE_ID:
        return _fail("This app couldn't identify the device. Reinstall the app and try again.")

    user = authenticate(request, username=username, password=password)
    if user is None:
        return _fail("That username and password don't match.", 401)
    if not user.is_active:
        return _fail("This account is not active. Contact ICT.", 403)

    if _device_trusted(user, device_id):
        login(request, user)
        remember_device(user, device_id, user_agent=device_name, ip_address=_client_ip(request) or "")
        return JsonResponse({"ok": True, "otp_required": False, "user": _user_json(user),
                             "must_change_password": bool(getattr(user, "must_change_password", False))})

    code = create_otp_for_user(user, device_id, ip_address=_client_ip(request), user_agent=device_name)
    send_otp_email_async(user, getattr(code, "code", code))
    masked = user.email
    if "@" in masked:
        name, _, domain = masked.partition("@")
        masked = (name[:2] + "•" * max(1, len(name) - 2)) + "@" + domain
    return JsonResponse({"ok": True, "otp_required": True, "sent_to": masked,
                         "expires_in_minutes": 10})


@api
@require_POST
def auth_verify(request):
    """Step two: the six-digit code. The phone is then remembered for 30 days."""
    from .models import OneTimeCode

    data = _body(request)
    if data is None:
        return _fail("The request couldn't be read.")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    device_id = str(data.get("device_id") or "").strip()
    device_name = str(data.get("device_name") or "")[:255]
    code = "".join(ch for ch in str(data.get("code") or "") if ch.isdigit())

    user = authenticate(request, username=username, password=password)
    if user is None:
        return _fail("Please sign in again.", 401)
    if len(code) != 6:
        return _fail("Enter the six-digit code from your email.")

    otp = (OneTimeCode.objects
           .filter(user=user, device_id=device_id, code=code, is_used=False)
           .order_by("-created_at").first())
    if otp is None:
        return _fail("That code isn't right. Check the latest email.")
    if otp.expires_at <= timezone.now():
        return _fail("That code has expired. Ask for a new one.", 400, expired=True)

    otp.is_used = True
    otp.save(update_fields=["is_used"])
    login(request, user)
    remember_device(user, device_id, user_agent=device_name, ip_address=_client_ip(request) or "")
    return JsonResponse({"ok": True, "user": _user_json(user),
                         "must_change_password": bool(getattr(user, "must_change_password", False))})


@api
@require_POST
def auth_resend(request):
    data = _body(request) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    device_id = str(data.get("device_id") or "").strip()
    user = authenticate(request, username=username, password=password)
    if user is None or len(device_id) < MIN_DEVICE_ID:
        return _fail("Please sign in again.", 401)
    code = create_otp_for_user(user, device_id, ip_address=_client_ip(request), user_agent="")
    send_otp_email_async(user, getattr(code, "code", code))
    return JsonResponse({"ok": True})


@api
@require_POST
@signed_in
def auth_logout(request):
    """
    Signs out. `forget_device` also drops the trust on this phone, so the next
    sign-in needs a fresh code — what you want on a handset being handed over.
    """
    data = _body(request) or {}
    device_id = str(data.get("device_id") or "").strip()
    if data.get("forget_device") and device_id:
        TrustedDevice.objects.filter(user=request.user, device_id=device_id).update(is_active=False)
    logout(request)
    return JsonResponse({"ok": True})


@api
@require_GET
@signed_in
def me(request):
    return JsonResponse({"ok": True, "user": _user_json(request.user)})


# ─────────────────────────────────────────────────────────────────────────────
# eSign — what is waiting for me
# ─────────────────────────────────────────────────────────────────────────────

@api
@require_GET
@signed_in
def inbox(request):
    """Everything waiting on this person: workflow steps and envelopes to sign."""
    from .models_esign import EnvelopeRecipient
    from .models_esign_studio import WorkflowTask

    items = []
    tasks = (WorkflowTask.objects
             .filter(user=request.user, status="pending")
             .select_related("run")[:100])
    for t in tasks:
        items.append({
            "kind": "task", "id": t.token, "title": t.run.subject,
            "subtitle": t.node_label or t.get_kind_display(),
            "reference": t.run.reference, "task_kind": t.kind,
            "due": t.due_at.date().isoformat() if t.due_at else None,
            "started_by": (t.run.initiator.get_full_name() if t.run.initiator else ""),
            "url": f"/accounts/esign/wf/t/{t.token}/",
            "can_decide": t.kind in ("approval", "review"),
        })

    rows = (EnvelopeRecipient.objects
            .filter(user=request.user, status__in=("pending", "sent"))
            .select_related("envelope")[:100])
    for r in rows:
        if not r.can_sign_now():
            continue
        items.append({
            "kind": "envelope", "id": str(r.token), "title": r.envelope.subject,
            "subtitle": "Needs your signature", "reference": r.envelope.envelope_id,
            "due": r.envelope.expires_at.date().isoformat() if getattr(r.envelope, "expires_at", None) else None,
            "started_by": (r.envelope.created_by.get_full_name() if r.envelope.created_by else ""),
            "url": f"/accounts/esign/sign/{r.token}/", "can_decide": False,
        })
    items.sort(key=lambda x: (x["due"] or "9999", x["title"]))
    return JsonResponse({"ok": True, "count": len(items), "items": items})


@api
@require_GET
@signed_in
def task_detail(request, token):
    """A step in full: the instructions and the answers so far, ready to read on a phone."""
    from .form_pdf_esign import summary_rows
    from .models_esign_studio import WorkflowTask

    task = WorkflowTask.objects.filter(token=token).select_related("run").first()
    if task is None:
        return _fail("That item no longer exists.", 404)
    if task.user_id not in (None, request.user.pk):
        return _fail("This task belongs to someone else.", 403)

    run = task.run
    answers = []
    if run.submission_id:
        answers = [{"label": label, "value": value}
                   for label, value in summary_rows(run.submission.schema, run.submission.values) if value]
    return JsonResponse({"ok": True, "task": {
        "id": task.token, "title": run.subject, "step": task.node_label or task.get_kind_display(),
        "kind": task.kind, "status": task.status, "reference": run.reference,
        "instructions": _node_config(run, task.node_id).get("instructions", ""),
        "message": run.message or "", "started_by": run.initiator.get_full_name() if run.initiator else "",
        "due": task.due_at.date().isoformat() if task.due_at else None,
        "answers": answers,
        "document_url": f"/accounts/esign/wf/t/{task.token}/pdf/",
        "open_url": f"/accounts/esign/wf/t/{task.token}/",
        "can_decide": task.kind in ("approval", "review"),
        "can_return": bool(_node_config(run, task.node_id).get("allow_return", True)),
    }})


@api
@require_POST
@signed_in
def task_decide(request, token):
    """Approve, reject, return for changes, or acknowledge — the same engine as the website."""
    from . import workflow_engine_esign as E
    from .models_esign_studio import WorkflowTask

    data = _body(request) or {}
    action = str(data.get("action") or "").strip()
    comment = str(data.get("comment") or "").strip()[:2000]
    task = WorkflowTask.objects.filter(token=token).select_related("run").first()
    if task is None:
        return _fail("That item no longer exists.", 404)
    if task.user_id not in (None, request.user.pk):
        return _fail("This task belongs to someone else.", 403)
    if task.status != "pending":
        return _fail("This step has already been dealt with.", 409, stale=True)
    if action not in ("approve", "reject", "return", "acknowledge"):
        return _fail("Unknown action.")
    if action in ("reject", "return") and not comment:
        return _fail("Please say why — a comment is needed to reject or return.")

    try:
        E.decide(task, action, comment=comment, user=request.user)
    except TypeError:                       # older signature without `user`
        E.decide(task, action, comment=comment)
    except E.WorkflowError as exc:
        return _fail(str(exc), 409)
    task.refresh_from_db()
    return JsonResponse({"ok": True, "status": task.status,
                         "message": {"approve": "Approved.", "reject": "Rejected.",
                                     "return": "Sent back for changes.",
                                     "acknowledge": "Marked as reviewed."}[action]})


# ─────────────────────────────────────────────────────────────────────────────
# Assets — scan and verify
# ─────────────────────────────────────────────────────────────────────────────

def _node_config(run, node_id):
    """The step's settings from the run's own snapshot of the flow."""
    for node in (run.graph or {}).get("nodes") or []:
        if node.get("id") == node_id:
            return node.get("config") or {}
    return {}


def _asset_json(asset, request=None):
    holder = asset.current_holder
    return {
        "id": asset.pk,
        "name": asset.name,
        "tag": asset.asset_tag or "",
        "serial": asset.serial_number or "",
        "status": asset.status,
        "status_label": asset.get_status_display(),
        "category": getattr(asset.category, "name", "") or "",
        "unit": getattr(asset.unit, "name", "") or "",
        "agency": getattr(asset.agency, "name", "") or "",
        "holder": (holder.get_full_name() or holder.username) if holder else "",
        "holder_email": getattr(holder, "email", "") if holder else "",
        "acquired": asset.acquired_at.isoformat() if asset.acquired_at else None,
        "retired": asset.retired_at.isoformat() if asset.retired_at else None,
        "detail_url": f"/accounts/assets/{asset.pk}/",
    }


def _asset_from_payload(payload, agency):
    """
    Find the asset a scanned code refers to.

    The printed label holds the asset tag on the first line and a link to the
    asset's page on the second, so accept either — and a bare serial number
    too, for labels that only carry one.
    """
    text = (payload or "").strip()
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = []
    for line in lines:
        if "/assets/" in line:
            tail = line.rstrip("/").split("/assets/")[-1].split("/")[0]
            if tail.isdigit():
                asset = Asset.objects.filter(pk=int(tail), agency=agency).first()
                if asset:
                    return asset
        else:
            candidates.append(line)
    for value in candidates:
        asset = (Asset.objects.filter(agency=agency, asset_tag__iexact=value).first()
                 or Asset.objects.filter(agency=agency, serial_number__iexact=value).first())
        if asset:
            return asset
    return None


@api
@require_POST
@signed_in
def asset_lookup(request):
    """What did I just scan? Answers with the asset, or says plainly that it's unknown."""
    data = _body(request) or {}
    payload = str(data.get("code") or "")
    agency = getattr(request.user, "agency", None)
    if agency is None:
        return _fail("Your account isn't linked to an agency, so assets can't be looked up.", 403)
    asset = _asset_from_payload(payload, agency)
    if asset is None:
        return JsonResponse({"ok": False, "found": False,
                             "error": "No asset in your agency matches that code.",
                             "scanned": payload[:120]}, status=404)
    mine = asset.current_holder_id == request.user.pk
    return JsonResponse({"ok": True, "found": True, "asset": _asset_json(asset, request),
                         "held_by_me": mine,
                         "hint": "This one is assigned to you." if mine
                                 else ("Not assigned to anyone." if not asset.current_holder_id else None)})


@api
@require_GET
@signed_in
def asset_search(request):
    """A fallback for a damaged or missing label: search by tag, serial or name."""
    q = (request.GET.get("q") or "").strip()
    agency = getattr(request.user, "agency", None)
    if agency is None or len(q) < 2:
        return JsonResponse({"ok": True, "results": []})
    from django.db.models import Q

    rows = (Asset.objects.filter(agency=agency)
            .filter(Q(asset_tag__icontains=q) | Q(serial_number__icontains=q) | Q(name__icontains=q))
            .select_related("category", "unit", "current_holder")[:25])
    return JsonResponse({"ok": True, "results": [_asset_json(a) for a in rows]})


@api
@require_GET
@signed_in
def my_assets(request):
    """The assets this person is holding — the list to work through during a count."""
    rows = (Asset.objects.filter(current_holder=request.user)
            .select_related("category", "unit", "current_holder").order_by("name")[:200])
    return JsonResponse({"ok": True, "count": len(rows), "assets": [_asset_json(a) for a in rows]})
