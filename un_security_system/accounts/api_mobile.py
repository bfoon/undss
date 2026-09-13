"""
UN PASS — API used by the phone app.

Mobile sign-in supports username OR registered email, then a one-time code by
email, then the device is remembered for 30 days. The ordinary Django session
cookie keeps the app signed in.
"""

import json
from functools import wraps

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.http import JsonResponse, QueryDict
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


# ─────────────────────────────────────────────────────────────────────────────
# Reusing the website's own views
#
# Signing and form submission are long, careful pieces of work: consent, field
# placement, the audit trail, notifications, starting the workflow. Rather than
# write a second version for the phone that could drift out of step, the API
# hands the same request objects to the very same view functions and turns
# their answer into JSON.
# ─────────────────────────────────────────────────────────────────────────────

def _as_form_post(request, values):
    """Point this request at a form-style POST, so a website view can handle it."""
    post = QueryDict(mutable=True)
    for key, value in values.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                post.appendlist(key, str(item))
        elif value is not None:
            post[key] = str(value)
    request.POST = post
    request.method = "POST"
    return request


def _messages_from(request):
    """Collect anything the view put in the message framework, for the app to show."""
    try:
        from django.contrib.messages import get_messages

        return [{"level": m.level_tag, "text": str(m)} for m in get_messages(request)]
    except Exception:  # noqa: BLE001
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Envelopes
# ─────────────────────────────────────────────────────────────────────────────

def _envelope_json(env, recipient=None):
    return {
        "id": env.pk,
        "envelope_id": env.envelope_id,
        "subject": env.subject,
        "message": env.message or "",
        "status": env.status,
        "status_label": env.get_status_display(),
        "created_by": env.created_by.get_full_name() if env.created_by else "",
        "created": env.created_at.isoformat() if env.created_at else None,
        "expires": env.expires_at.date().isoformat() if getattr(env, "expires_at", None) else None,
        "documents": [{"id": d.pk, "name": d.name, "pages": getattr(d, "page_count", 0) or 0}
                      for d in env.documents.all()],
        "recipients": [{"name": r.name, "email": r.email, "role": r.role,
                        "status": r.status, "order": r.order,
                        "is_me": recipient is not None and r.pk == recipient.pk}
                       for r in env.recipients.all()],
        "my_token": str(recipient.token) if recipient else None,
    }


@api
@require_GET
@signed_in
def envelopes(request):
    """Every envelope this person can see, newest first."""
    from .views_esign import _esign_visible_envelopes

    status = (request.GET.get("status") or "").strip()
    q = (request.GET.get("q") or "").strip()
    rows = _esign_visible_envelopes(request.user).select_related("created_by").prefetch_related("recipients")
    if status:
        rows = rows.filter(status=status)
    if q:
        from django.db.models import Q

        rows = rows.filter(Q(subject__icontains=q) | Q(envelope_id__icontains=q))
    rows = rows.order_by("-created_at")[:100]
    from .models_esign import EnvelopeRecipient

    mine = {r.envelope_id: r for r in
            EnvelopeRecipient.objects.filter(user=request.user, envelope__in=[e.pk for e in rows])}
    return JsonResponse({"ok": True, "count": len(rows),
                         "envelopes": [_envelope_json(e, mine.get(e.pk)) for e in rows]})


@api
@require_GET
@signed_in
def envelope_detail(request, pk):
    from .models_esign import Envelope, EnvelopeRecipient
    from .views_esign import _esign_visible_envelopes

    env = _esign_visible_envelopes(request.user).filter(pk=pk).first()
    if env is None:
        return _fail("That envelope no longer exists, or you can't see it.", 404)
    me = EnvelopeRecipient.objects.filter(envelope=env, user=request.user).first()
    data = _envelope_json(env, me)
    data["events"] = [{"event": e.event, "note": e.note,
                       "at": e.at.isoformat() if e.at else None}
                      for e in env.events.order_by("-at")[:40]]
    data["can_sign"] = bool(me and me.is_signing_role and me.can_sign_now()
                            and me.status not in ("signed", "declined"))
    data["final_url"] = f"/accounts/esign/envelope/{env.pk}/download/"
    return JsonResponse({"ok": True, "envelope": data})


@api
@require_GET
def sign_sheet(request, token):
    """
    What the app needs to sign: the documents, and the fields placed for this
    person. Reachable by the emailed link, so no sign-in is required.
    """
    from .models_esign import EnvelopeRecipient

    recipient = EnvelopeRecipient.objects.filter(token=token).select_related("envelope").first()
    if recipient is None:
        return _fail("That signing link is not valid.", 404)
    env = recipient.envelope
    if recipient.access_code:
        return _fail("This envelope needs an access code. Open it in the browser.", 409,
                     needs_browser=True, url=f"/accounts/esign/sign/{token}/")
    if recipient.status in ("signed", "declined") or env.status == "completed":
        return JsonResponse({"ok": True, "already_done": True, "status": recipient.status,
                             "envelope": _envelope_json(env, recipient)})
    if not recipient.can_sign_now():
        return _fail("It is not your turn yet — you will be emailed when it is.", 409)

    fields = list(env.fields.filter(recipient=recipient).values(
        "id", "document_id", "kind", "page", "x", "y", "w", "h", "required", "label"))
    return JsonResponse({"ok": True, "already_done": False,
                         "envelope": _envelope_json(env, recipient),
                         "consent_needed": True,
                         "fields": fields,
                         "documents": [{"id": d.pk, "name": d.name,
                                        "url": f"/accounts/esign/t/{token}/doc/{d.pk}/"}
                                       for d in env.documents.all()]})


@api
@require_POST
def envelope_sign(request, token):
    """
    Sign from the phone. The drawn signature arrives as a data URL and is handed
    to the website's own handler, so the audit trail, the stamping and the
    notifications are identical to signing in a browser.
    """
    from .models_esign import EnvelopeRecipient
    from .views_esign import _handle_sign_submit

    data = _body(request) or {}
    recipient = EnvelopeRecipient.objects.filter(token=token).select_related("envelope").first()
    if recipient is None:
        return _fail("That signing link is not valid.", 404)
    if recipient.status in ("signed", "declined"):
        return _fail("You have already responded to this envelope.", 409, stale=True)
    signature = str(data.get("signature") or "")
    if not signature.startswith("data:image/"):
        return _fail("Draw your signature before signing.")
    if not data.get("consent"):
        return _fail("You need to accept the electronic record consent to sign.")

    # The website's handler only fills fields that appear in the payload, so
    # build an entry for every one of this person's fields: the drawn signature
    # where a signature is wanted, and the obvious details filled in for them.
    supplied = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    from .models_esign import SignatureField

    user = recipient.user
    auto = {
        SignatureField.KIND_DATE: timezone.localdate().strftime("%d %b %Y"),
        SignatureField.KIND_NAME: recipient.name or (user.get_full_name() if user else ""),
        SignatureField.KIND_EMAIL: recipient.email or (getattr(user, "email", "") if user else ""),
        SignatureField.KIND_TITLE: getattr(user, "role", "") if user else "",
    }
    payload = {}
    for field in recipient.envelope.fields.filter(recipient=recipient):
        fid = str(field.id)
        if fid in supplied:
            payload[fid] = supplied[fid]
        elif field.kind in (SignatureField.KIND_SIGNATURE, SignatureField.KIND_INITIALS):
            payload[fid] = ""                      # falls through to the drawing below
        else:
            payload[fid] = auto.get(field.kind, "")

    values = {
        "consent": "1",
        "signature_data": signature,
        "initials_data": str(data.get("initials") or signature),
        "fields_payload": json.dumps(payload),
    }
    if data.get("save_signature"):
        values["save_signature"] = "1"
    _as_form_post(request, values)
    _handle_sign_submit(request, recipient)
    recipient.refresh_from_db()
    if recipient.status != "signed":
        notes = _messages_from(request)
        return _fail(notes[0]["text"] if notes else "The signature could not be recorded.", 400)
    return JsonResponse({"ok": True, "status": recipient.status,
                         "message": "Signed. Everyone will be notified."})


@api
@require_POST
def envelope_decline(request, token):
    from .models_esign import EnvelopeRecipient
    from .views_esign import esign_decline

    data = _body(request) or {}
    reason = str(data.get("reason") or "").strip()
    if not reason:
        return _fail("Please say why you are declining.")
    recipient = EnvelopeRecipient.objects.filter(token=token).first()
    if recipient is None:
        return _fail("That link is not valid.", 404)
    _as_form_post(request, {"reason": reason})
    esign_decline(request, token)
    recipient.refresh_from_db()
    return JsonResponse({"ok": recipient.status == "declined", "status": recipient.status,
                         "message": "Declined. The sender has been told."})


# ─────────────────────────────────────────────────────────────────────────────
# Forms
# ─────────────────────────────────────────────────────────────────────────────

@api
@require_GET
@signed_in
def forms(request):
    """Forms this person can fill in, and their own forms."""
    from .models_esign_studio import FormSubmission, FormTemplate
    from .studio_common_esign import shared_template_q

    mine = FormTemplate.objects.filter(created_by=request.user).order_by("name")[:100]
    fillable = (FormTemplate.objects.filter(shared_template_q(request.user), is_published=True)
                .order_by("name")[:100])

    def row(f):
        return {"id": f.pk, "name": f.name, "description": f.description or "",
                "category": f.category or "", "published": f.is_published,
                "reference_prefix": f.reference_prefix,
                "workflow": f.workflow.name if f.workflow_id else ""}

    subs = (FormSubmission.objects.filter(submitted_by=request.user)
            .select_related("form").order_by("-created_at")[:50])
    return JsonResponse({"ok": True,
                         "fillable": [row(f) for f in fillable],
                         "mine": [row(f) for f in mine],
                         "submissions": [{"id": s.pk, "reference": s.reference,
                                          "form": s.form_name, "status": s.status,
                                          "status_label": s.get_status_display(),
                                          "created": s.created_at.isoformat() if s.created_at else None}
                                         for s in subs]})


@api
@require_GET
@signed_in
def form_schema(request, pk):
    """
    The form itself, ready for the app to draw: the questions, the section
    rules, and who fills each part in.
    """
    from .form_pdf_esign import resolved_schema
    from .models_esign_studio import FormTemplate
    from .studio_common_esign import shared_template_q

    form = (FormTemplate.objects.filter(shared_template_q(request.user), pk=pk).first()
            or FormTemplate.objects.filter(created_by=request.user, pk=pk).first())
    if form is None:
        return _fail("That form no longer exists, or it isn't shared with you.", 404)
    schema = resolved_schema(form.schema or {})
    prefill = {}
    for el in schema.get("elements") or []:
        key, source = el.get("key"), el.get("prefill")
        if not key or not source:
            continue
        office = getattr(request.user, "country_office", None)
        agency = getattr(request.user, "agency", None) or getattr(office, "agency", None)
        prefill[key] = {
            "user.full_name": request.user.get_full_name() or request.user.username,
            "user.email": request.user.email,
            "user.job_title": getattr(request.user, "role", "") or "",
            "user.agency": getattr(agency, "name", "") or "",
            "user.office": getattr(office, "name", "") or "",
            "today": timezone.localdate().isoformat(),
        }.get(source, "")
    return JsonResponse({"ok": True, "form": {
        "id": form.pk, "name": form.name, "description": form.description or "",
        "schema": schema, "prefill": prefill,
        "workflow": form.workflow.name if form.workflow_id else "",
        "slots": _slots_for(form),
    }})


def _slots_for_graph(graph):
    """Roles whoever starts a run must pick people for."""
    from . import workflow_engine_esign as E

    try:
        return list(E.chosen_slots(E.clean_graph(graph or {})))
    except Exception:  # noqa: BLE001
        return []


def _slots_for(form):
    """The same, for the flow a form starts."""
    return _slots_for_graph(form.workflow.graph) if form.workflow_id else []


@api
@require_POST
@signed_in
def form_submit(request, pk):
    """
    Submit a form. Handed straight to the website's fill view, so validation,
    the reference number, the PDF and the workflow all behave identically.
    """
    from .models_esign_studio import FormSubmission, FormTemplate
    from .studio_common_esign import shared_template_q
    from .views_esign_forms import _fill

    form = FormTemplate.objects.filter(shared_template_q(request.user), pk=pk, is_published=True).first()
    if form is None:
        return _fail("That form no longer exists, or it isn't shared with you.", 404)
    data = _body(request) or {}
    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    columns = {el["key"]: [c["key"] for c in el.get("columns") or []]
               for el in (form.schema or {}).get("elements") or []
               if el.get("type") == "table" and el.get("key")}
    post = {}
    for key, value in values.items():
        if key in columns and isinstance(value, list):
            # a table: one entry per cell, plus the row count, as the page posts it
            post[f"f_{key}__rows"] = len(value)
            for row_no, row in enumerate(value):
                if not isinstance(row, dict):
                    continue
                for column in columns[key]:
                    post[f"f_{key}__{row_no}__{column}"] = row.get(column, "")
        else:
            post[f"f_{key}"] = value          # a list here is a tick-list, sent as repeats
    for key, value in (data.get("slots") or {}).items():
        post[f"slot_{key}"] = value
    before = FormSubmission.objects.filter(form=form).count()

    _as_form_post(request, post)
    _fill(request, form, public=False)
    made = (FormSubmission.objects.filter(form=form, submitted_by=request.user)
            .order_by("-created_at").first())
    if FormSubmission.objects.filter(form=form).count() == before or made is None:
        notes = [m["text"] for m in _messages_from(request)]
        return _fail(notes[0] if notes else "Some answers need fixing. Check the form and try again.", 400,
                     notes=notes)
    return JsonResponse({"ok": True, "reference": made.reference, "submission": made.pk,
                         "message": f"Sent. Your reference is {made.reference}."})


@api
@require_GET
@signed_in
def submission_detail(request, pk):
    from .form_pdf_esign import summary_rows
    from .models_esign_studio import FormSubmission

    sub = FormSubmission.objects.filter(pk=pk).select_related("form").first()
    if sub is None or (sub.submitted_by_id != request.user.pk and sub.form.created_by_id != request.user.pk):
        return _fail("That submission no longer exists, or you can't see it.", 404)
    runs = [{"reference": r.reference, "status": r.status, "subject": r.subject}
            for r in sub.runs.all()]
    return JsonResponse({"ok": True, "submission": {
        "id": sub.pk, "reference": sub.reference, "form": sub.form_name,
        "status": sub.status, "status_label": sub.get_status_display(),
        "created": sub.created_at.isoformat() if sub.created_at else None,
        "answers": [{"label": label, "value": value} for label, value in summary_rows(sub.schema, sub.values)],
        "runs": runs,
        "pdf_url": f"/accounts/esign/submissions/{sub.pk}/pdf/",
    }})


# ─────────────────────────────────────────────────────────────────────────────
# Flows and runs
# ─────────────────────────────────────────────────────────────────────────────

@api
@require_GET
@signed_in
def flows(request):
    from .models_esign_studio import DocumentWorkflow
    from .studio_common_esign import shared_template_q

    rows = (DocumentWorkflow.objects.filter(shared_template_q(request.user), is_active=True)
            .select_related("form").order_by("name")[:100])
    return JsonResponse({"ok": True, "flows": [{
        "id": f.pk, "name": f.name, "description": f.description or "",
        "steps": len([n for n in (f.graph or {}).get("nodes") or []
                      if n.get("type") not in ("start", "end")]),
        "form": f.form.name if f.form_id else "",
        "form_id": f.form_id,
        "mine": f.created_by_id == request.user.pk,
        "slots": _slots_for_graph(f.graph),
    } for f in rows]})


@api
@require_GET
@signed_in
def runs(request):
    from .models_esign_studio import WorkflowRun
    from .studio_common_esign import visible_runs

    status = (request.GET.get("status") or "").strip()
    rows = visible_runs(request.user).select_related("initiator", "workflow")
    if status == "open":
        rows = rows.filter(status__in=WorkflowRun.OPEN_STATUSES)
    elif status:
        rows = rows.filter(status=status)
    rows = rows.order_by("-started_at")[:100]
    return JsonResponse({"ok": True, "runs": [{
        "id": r.pk, "reference": r.reference, "subject": r.subject,
        "status": r.status, "status_label": r.get_status_display(),
        "flow": r.workflow_name, "started_by": r.initiator.get_full_name() if r.initiator else "",
        "started": r.started_at.isoformat() if r.started_at else None,
    } for r in rows]})


@api
@require_GET
@signed_in
def run_detail(request, pk):
    from .models_esign_studio import WorkflowRun
    from .studio_common_esign import visible_runs

    run = visible_runs(request.user).filter(pk=pk).select_related("initiator").first()
    if run is None:
        return _fail("That run no longer exists, or you can't see it.", 404)
    steps = [{"step": t.node_label or t.get_kind_display(), "kind": t.kind,
              "who": t.name or (t.user.get_full_name() if t.user else ""),
              "status": t.status, "comment": t.comment or "",
              "decided": t.decided_at.isoformat() if t.decided_at else None}
             for t in run.tasks.select_related("user").order_by("created_at")]
    return JsonResponse({"ok": True, "run": {
        "id": run.pk, "reference": run.reference, "subject": run.subject,
        "status": run.status, "status_label": run.get_status_display(),
        "flow": run.workflow_name,
        "started_by": run.initiator.get_full_name() if run.initiator else "",
        "started": run.started_at.isoformat() if run.started_at else None,
        "steps": steps,
        "events": [{"event": e.event, "note": e.note,
                    "at": e.at.isoformat() if e.at else None}
                   for e in run.events.order_by("-at")[:40]],
        "document_url": f"/accounts/esign/runs/{run.pk}/pdf/final/",
        "can_cancel": run.initiator_id == request.user.pk and run.status in WorkflowRun.OPEN_STATUSES,
    }})


@api
@require_POST
@signed_in
def run_cancel(request, pk):
    from . import workflow_engine_esign as E
    from .models_esign_studio import WorkflowRun
    from .studio_common_esign import visible_runs

    run = visible_runs(request.user).filter(pk=pk).first()
    if run is None:
        return _fail("That run no longer exists.", 404)
    if run.initiator_id != request.user.pk:
        return _fail("Only the person who started a run can cancel it.", 403)
    if run.status not in WorkflowRun.OPEN_STATUSES:
        return _fail("This run has already finished.", 409)
    reason = str((_body(request) or {}).get("reason") or "").strip()
    E.cancel_run(run, request.user, reason=reason)
    run.refresh_from_db()
    return JsonResponse({"ok": True, "status": run.status, "message": "The run has been cancelled."})
