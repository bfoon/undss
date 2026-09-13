"""
UN PASS — API used by the phone app.

Mobile sign-in supports username OR registered email, then a one-time code by
email, then the device is remembered for 30 days. The ordinary Django session
cookie keeps the app signed in.
"""

import json
from functools import wraps

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Asset, TrustedDevice
from .utils import create_otp_for_user, remember_device, send_otp_email

API_VERSION = "1.1"
MIN_DEVICE_ID = 16


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (
        forwarded.split(",")[0].strip()
        if forwarded
        else request.META.get("REMOTE_ADDR")
    ) or None


def _body(request):
    try:
        data = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _fail(message, status=400, **extra):
    return JsonResponse({"ok": False, "error": message, **extra}, status=status)


def api(view):
    """JSON in, JSON out — never return an HTML error page to the app."""

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception(
                "Mobile API error in %s", view.__name__
            )
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
        "initials": "".join(
            part[0]
            for part in (user.get_full_name() or user.username).split()[:2]
        ).upper(),
    }


def _device_trusted(user, device_id):
    return TrustedDevice.objects.filter(
        user=user,
        device_id=device_id,
        is_active=True,
        expires_at__gt=timezone.now(),
    ).exists()


def _authenticate_identifier(request, identifier, password):
    """Authenticate with either username or registered email."""
    identifier = (identifier or "").strip()
    if not identifier or not password:
        return None

    # Keep normal username authentication exactly as before.
    user = authenticate(request, username=identifier, password=password)
    if user is not None:
        return user

    # If it looks like an email, resolve it to the user's actual username.
    if "@" not in identifier:
        return None

    User = get_user_model()
    matches = User._default_manager.filter(email__iexact=identifier)

    # Do not guess if duplicate email addresses exist.
    if matches.count() != 1:
        return None

    candidate = matches.first()
    return authenticate(
        request,
        username=candidate.get_username(),
        password=password,
    )


def _masked_email(email):
    email = (email or "").strip()
    if "@" not in email:
        return ""

    name, _, domain = email.partition("@")
    if len(name) <= 2:
        visible = name[:1] + "•" * max(1, len(name) - 1)
    else:
        visible = name[:2] + "•" * max(1, len(name) - 2)
    return f"{visible}@{domain}"


def _create_and_send_mobile_otp(
    user,
    device_id,
    *,
    ip_address=None,
    user_agent="",
):
    """
    Create the OTP and send it synchronously.

    The phone only receives otp_required=True after Django's configured email
    backend accepts the message. If delivery fails, the just-created OTP is
    invalidated and the API returns a real error instead of a false success.
    """
    if not (user.email or "").strip():
        return None, _fail(
            "No email address is registered on this UNPASS account. Contact ICT.",
            400,
        )

    otp = create_otp_for_user(
        user,
        device_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    try:
        send_otp_email(user, getattr(otp, "code", otp))
    except Exception:
        import logging

        if hasattr(otp, "is_used"):
            otp.is_used = True
            otp.save(update_fields=["is_used"])

        logging.getLogger(__name__).exception(
            "Mobile OTP email delivery failed for user_id=%s",
            getattr(user, "pk", None),
        )

        return None, _fail(
            "UNPASS could not send the verification email. "
            "Please try again. If it continues, contact ICT.",
            503,
            email_failed=True,
        )

    return otp, None


# ─────────────────────────────────────────────────────────────────────────────
# Signing in
# ─────────────────────────────────────────────────────────────────────────────

@api
@require_GET
def ping(request):
    return JsonResponse(
        {
            "ok": True,
            "service": "UN PASS",
            "api": API_VERSION,
            "signed_in": request.user.is_authenticated,
        }
    )


@api
@require_POST
def auth_login(request):
    data = _body(request)
    if data is None:
        return _fail("The request couldn't be read.")

    identifier = str(
        data.get("identifier") or data.get("username") or ""
    ).strip()
    password = str(data.get("password") or "")
    device_id = str(data.get("device_id") or "").strip()
    device_name = str(data.get("device_name") or "")[:255]

    if not identifier or not password:
        return _fail("Enter your username or email and password.")

    if len(device_id) < MIN_DEVICE_ID:
        return _fail(
            "This app couldn't identify the device. Reinstall the app and try again."
        )

    user = _authenticate_identifier(request, identifier, password)
    if user is None:
        return _fail(
            "That username/email and password don't match.",
            401,
        )

    if not user.is_active:
        return _fail("This account is not active. Contact ICT.", 403)

    if _device_trusted(user, device_id):
        login(request, user)
        remember_device(
            user,
            device_id,
            user_agent=device_name,
            ip_address=_client_ip(request) or "",
        )
        return JsonResponse(
            {
                "ok": True,
                "otp_required": False,
                "user": _user_json(user),
                "must_change_password": bool(
                    getattr(user, "must_change_password", False)
                ),
            }
        )

    _otp, error = _create_and_send_mobile_otp(
        user,
        device_id,
        ip_address=_client_ip(request),
        user_agent=device_name,
    )
    if error is not None:
        return error

    return JsonResponse(
        {
            "ok": True,
            "otp_required": True,
            "sent_to": _masked_email(user.email),
            "expires_in_minutes": 10,
        }
    )


@api
@require_POST
def auth_verify(request):
    from .models import OneTimeCode

    data = _body(request)
    if data is None:
        return _fail("The request couldn't be read.")

    identifier = str(
        data.get("identifier") or data.get("username") or ""
    ).strip()
    password = str(data.get("password") or "")
    device_id = str(data.get("device_id") or "").strip()
    device_name = str(data.get("device_name") or "")[:255]
    code = "".join(
        ch for ch in str(data.get("code") or "") if ch.isdigit()
    )

    user = _authenticate_identifier(request, identifier, password)
    if user is None:
        return _fail("Please sign in again.", 401)

    if len(code) != 6:
        return _fail("Enter the six-digit code from your email.")

    otp = (
        OneTimeCode.objects.filter(
            user=user,
            device_id=device_id,
            code=code,
            is_used=False,
        )
        .order_by("-created_at")
        .first()
    )

    if otp is None:
        return _fail("That code isn't right. Check the latest email.")

    if otp.expires_at <= timezone.now():
        return _fail(
            "That code has expired. Ask for a new one.",
            400,
            expired=True,
        )

    otp.is_used = True
    otp.save(update_fields=["is_used"])

    login(request, user)
    remember_device(
        user,
        device_id,
        user_agent=device_name,
        ip_address=_client_ip(request) or "",
    )

    return JsonResponse(
        {
            "ok": True,
            "user": _user_json(user),
            "must_change_password": bool(
                getattr(user, "must_change_password", False)
            ),
        }
    )


@api
@require_POST
def auth_resend(request):
    data = _body(request) or {}

    identifier = str(
        data.get("identifier") or data.get("username") or ""
    ).strip()
    password = str(data.get("password") or "")
    device_id = str(data.get("device_id") or "").strip()

    user = _authenticate_identifier(request, identifier, password)
    if user is None or len(device_id) < MIN_DEVICE_ID:
        return _fail("Please sign in again.", 401)

    _otp, error = _create_and_send_mobile_otp(
        user,
        device_id,
        ip_address=_client_ip(request),
        user_agent="",
    )
    if error is not None:
        return error

    return JsonResponse(
        {
            "ok": True,
            "sent_to": _masked_email(user.email),
            "expires_in_minutes": 10,
        }
    )


@api
@require_POST
@signed_in
def auth_logout(request):
    data = _body(request) or {}
    device_id = str(data.get("device_id") or "").strip()

    if data.get("forget_device") and device_id:
        TrustedDevice.objects.filter(
            user=request.user,
            device_id=device_id,
        ).update(is_active=False)

    logout(request)
    return JsonResponse({"ok": True})


@api
@require_GET
@signed_in
def me(request):
    return JsonResponse({"ok": True, "user": _user_json(request.user)})


# ─────────────────────────────────────────────────────────────────────────────
# eSign — waiting on me
# ─────────────────────────────────────────────────────────────────────────────

@api
@require_GET
@signed_in
def inbox(request):
    from .models_esign import EnvelopeRecipient
    from .models_esign_studio import WorkflowTask

    items = []

    tasks = (
        WorkflowTask.objects.filter(user=request.user, status="pending")
        .select_related("run")[:100]
    )

    for task in tasks:
        items.append(
            {
                "kind": "task",
                "id": task.token,
                "title": task.run.subject,
                "subtitle": task.node_label or task.get_kind_display(),
                "reference": task.run.reference,
                "task_kind": task.kind,
                "due": task.due_at.date().isoformat() if task.due_at else None,
                "started_by": (
                    task.run.initiator.get_full_name()
                    if task.run.initiator
                    else ""
                ),
                "url": f"/accounts/esign/wf/t/{task.token}/",
                "can_decide": task.kind in ("approval", "review"),
            }
        )

    recipients = (
        EnvelopeRecipient.objects.filter(
            user=request.user,
            status__in=("pending", "sent"),
        )
        .select_related("envelope")[:100]
    )

    for recipient in recipients:
        if not recipient.can_sign_now():
            continue

        items.append(
            {
                "kind": "envelope",
                "id": str(recipient.token),
                "title": recipient.envelope.subject,
                "subtitle": "Needs your signature",
                "reference": recipient.envelope.envelope_id,
                "due": (
                    recipient.envelope.expires_at.date().isoformat()
                    if getattr(recipient.envelope, "expires_at", None)
                    else None
                ),
                "started_by": (
                    recipient.envelope.created_by.get_full_name()
                    if recipient.envelope.created_by
                    else ""
                ),
                "url": f"/accounts/esign/sign/{recipient.token}/",
                "can_decide": False,
            }
        )

    items.sort(key=lambda item: (item["due"] or "9999", item["title"]))
    return JsonResponse(
        {
            "ok": True,
            "count": len(items),
            "items": items,
        }
    )


@api
@require_GET
@signed_in
def task_detail(request, token):
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
        answers = [
            {"label": label, "value": value}
            for label, value in summary_rows(
                run.submission.schema,
                run.submission.values,
            )
            if value
        ]

    return JsonResponse(
        {
            "ok": True,
            "task": {
                "id": task.token,
                "title": run.subject,
                "step": task.node_label or task.get_kind_display(),
                "kind": task.kind,
                "status": task.status,
                "reference": run.reference,
                "instructions": _node_config(run, task.node_id).get(
                    "instructions", ""
                ),
                "message": run.message or "",
                "started_by": (
                    run.initiator.get_full_name() if run.initiator else ""
                ),
                "due": task.due_at.date().isoformat() if task.due_at else None,
                "answers": answers,
                "document_url": f"/accounts/esign/wf/t/{task.token}/pdf/",
                "open_url": f"/accounts/esign/wf/t/{task.token}/",
                "can_decide": task.kind in ("approval", "review"),
                "can_return": bool(
                    _node_config(run, task.node_id).get("allow_return", True)
                ),
            },
        }
    )


@api
@require_POST
@signed_in
def task_decide(request, token):
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
        return _fail(
            "This step has already been dealt with.",
            409,
            stale=True,
        )

    if action not in ("approve", "reject", "return", "acknowledge"):
        return _fail("Unknown action.")

    if action in ("reject", "return") and not comment:
        return _fail(
            "Please say why — a comment is needed to reject or return."
        )

    try:
        E.decide(task, action, comment=comment, user=request.user)
    except TypeError:
        E.decide(task, action, comment=comment)
    except E.WorkflowError as exc:
        return _fail(str(exc), 409)

    task.refresh_from_db()

    return JsonResponse(
        {
            "ok": True,
            "status": task.status,
            "message": {
                "approve": "Approved.",
                "reject": "Rejected.",
                "return": "Sent back for changes.",
                "acknowledge": "Marked as reviewed.",
            }[action],
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# Assets
# ─────────────────────────────────────────────────────────────────────────────

def _node_config(run, node_id):
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
        "holder": (
            (holder.get_full_name() or holder.username) if holder else ""
        ),
        "holder_email": getattr(holder, "email", "") if holder else "",
        "acquired": asset.acquired_at.isoformat() if asset.acquired_at else None,
        "retired": asset.retired_at.isoformat() if asset.retired_at else None,
        "detail_url": f"/accounts/assets/{asset.pk}/",
    }


def _asset_from_payload(payload, agency):
    text = (payload or "").strip()
    if not text:
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = []

    for line in lines:
        if "/assets/" in line:
            tail = line.rstrip("/").split("/assets/")[-1].split("/")[0]
            if tail.isdigit():
                asset = Asset.objects.filter(
                    pk=int(tail),
                    agency=agency,
                ).first()
                if asset:
                    return asset
        else:
            candidates.append(line)

    for value in candidates:
        asset = (
            Asset.objects.filter(
                agency=agency,
                asset_tag__iexact=value,
            ).first()
            or Asset.objects.filter(
                agency=agency,
                serial_number__iexact=value,
            ).first()
        )
        if asset:
            return asset

    return None


@api
@require_POST
@signed_in
def asset_lookup(request):
    data = _body(request) or {}
    payload = str(data.get("code") or "")
    agency = getattr(request.user, "agency", None)

    if agency is None:
        return _fail(
            "Your account isn't linked to an agency, so assets can't be looked up.",
            403,
        )

    asset = _asset_from_payload(payload, agency)
    if asset is None:
        return JsonResponse(
            {
                "ok": False,
                "found": False,
                "error": "No asset in your agency matches that code.",
                "scanned": payload[:120],
            },
            status=404,
        )

    mine = asset.current_holder_id == request.user.pk

    return JsonResponse(
        {
            "ok": True,
            "found": True,
            "asset": _asset_json(asset, request),
            "held_by_me": mine,
            "hint": (
                "This one is assigned to you."
                if mine
                else (
                    "Not assigned to anyone."
                    if not asset.current_holder_id
                    else None
                )
            ),
        }
    )


@api
@require_GET
@signed_in
def asset_search(request):
    from django.db.models import Q

    q = (request.GET.get("q") or "").strip()
    agency = getattr(request.user, "agency", None)

    if agency is None or len(q) < 2:
        return JsonResponse({"ok": True, "results": []})

    rows = (
        Asset.objects.filter(agency=agency)
        .filter(
            Q(asset_tag__icontains=q)
            | Q(serial_number__icontains=q)
            | Q(name__icontains=q)
        )
        .select_related("category", "unit", "current_holder")[:25]
    )

    return JsonResponse(
        {
            "ok": True,
            "results": [_asset_json(asset) for asset in rows],
        }
    )


@api
@require_GET
@signed_in
def my_assets(request):
    rows = (
        Asset.objects.filter(current_holder=request.user)
        .select_related("category", "unit", "current_holder")
        .order_by("name")[:200]
    )

    return JsonResponse(
        {
            "ok": True,
            "count": len(rows),
            "assets": [_asset_json(asset) for asset in rows],
        }
    )
