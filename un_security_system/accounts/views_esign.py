# accounts/views_esign.py
"""
UN PASS — eSign views.

Internal (login required)          Public / tokenized (no login)
────────────────────────           ─────────────────────────────
esign_dashboard                    esign_sign          <token>
esign_new                          esign_decline       <token>
esign_prepare / esign_fields_save  esign_review        <token>
esign_send / esign_void            esign_token_document<token>/<doc>
esign_remind / esign_resend        esign_token_download<token>/<kind>
esign_envelope_detail
esign_document_file / esign_download
esign_signatures (+ save/delete/default)
"""

import io
import json
import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import esign_notify
from .models import AgencyServiceConfig, Asset
from .models_esign import (
    Envelope,
    EnvelopeDocument,
    EnvelopeRecipient,
    SignatureField,
    SignatureProfile,
)
from .converters_esign import conversion_backend_available, supported_upload_ext
from .utils_esign import (
    build_final_pdf,
    decode_signature_data_url,
    document_pdf_bytes,
    envelope_is_expired,
    finalize_envelope,
    log_event,
    pdf_page_count,
    pdf_page_sizes,
)
from .view_asset_management import _is_ict, _is_ops_manager, _managed_unit_ids

User = get_user_model()
logger = logging.getLogger(__name__)

MAX_DOC_BYTES = 25 * 1024 * 1024
BASE_DOC_EXT = (".pdf", ".png", ".jpg", ".jpeg")


def allowed_doc_ext() -> tuple:
    """PDF/images always, plus whatever the installed converter can handle."""
    return BASE_DOC_EXT + tuple(supported_upload_ext())

# Keep in sync with SignatureProfile.FONT_CHOICES and the CSS in sign.html
ESIGN_FONTS = [
    {"key": "dancing", "name": "Dancing Script", "css": "'Dancing Script', cursive"},
    {"key": "greatvibes", "name": "Great Vibes", "css": "'Great Vibes', cursive"},
    {"key": "sacramento", "name": "Sacramento", "css": "'Sacramento', cursive"},
    {"key": "allura", "name": "Allura", "css": "'Allura', cursive"},
    {"key": "caveat", "name": "Caveat", "css": "'Caveat', cursive"},
    {"key": "homemade", "name": "Homemade Apple", "css": "'Homemade Apple', cursive"},
    {"key": "parisienne", "name": "Parisienne", "css": "'Parisienne', cursive"},
    {"key": "cedarville", "name": "Cedarville Cursive", "css": "'Cedarville Cursive', cursive"},
]


# ─────────────────────────────────────────────────────────────────────────────
# Guards
# ─────────────────────────────────────────────────────────────────────────────

def _agency_or_redirect(request):
    agency = getattr(request.user, "agency", None)
    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return None
    return agency


def _service_enabled(agency, user) -> bool:
    svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
    if user.is_superuser:
        return True
    # eSign rides on the Asset Management service flag unless you add a
    # dedicated `esign_enabled` field to AgencyServiceConfig.
    return bool(getattr(svc, "esign_enabled", svc.asset_mgmt_enabled))


def _can_manage_envelope(user, envelope) -> bool:
    if user.is_superuser:
        return True
    if envelope.created_by_id == user.id:
        return True
    agency = envelope.agency
    if _is_ict(user, agency) or _is_ops_manager(user, agency):
        return True
    return envelope.recipients.filter(user_id=user.id).exists()


def _get_envelope_for_user(request, pk, editable=False):
    envelope = get_object_or_404(
        Envelope.objects.select_related("agency", "created_by"),
        pk=pk,
        agency=getattr(request.user, "agency", None) or -1,
    )
    if not _can_manage_envelope(request.user, envelope):
        raise PermissionDenied("You do not have access to this envelope.")
    if editable and not (
        request.user.is_superuser or envelope.created_by_id == request.user.id
    ):
        raise PermissionDenied("Only the sender can edit this envelope.")
    return envelope


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def esign_dashboard(request):
    agency = _agency_or_redirect(request)
    if not agency:
        return redirect("accounts:profile")
    if not _service_enabled(agency, request.user):
        messages.warning(request, "eSign is not enabled for your agency.")
        return redirect("accounts:profile")

    user = request.user
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    base = Envelope.objects.filter(agency=agency).select_related("created_by")

    if not (user.is_superuser or _is_ict(user, agency) or _is_ops_manager(user, agency)):
        base = base.filter(
            Q(created_by=user) | Q(recipients__user=user) | Q(recipients__email__iexact=user.email)
        ).distinct()

    if q:
        base = base.filter(
            Q(subject__icontains=q)
            | Q(envelope_id__icontains=q)
            | Q(reference__icontains=q)
            | Q(recipients__name__icontains=q)
            | Q(recipients__email__icontains=q)
        ).distinct()
    if status:
        base = base.filter(status=status)

    my_recipient_rows = EnvelopeRecipient.objects.filter(
        Q(user=user) | Q(email__iexact=user.email),
        envelope__agency=agency,
        envelope__status=Envelope.STATUS_SENT,
        role__in=[EnvelopeRecipient.ROLE_SIGNER, EnvelopeRecipient.ROLE_APPROVER],
    ).exclude(
        status__in=[EnvelopeRecipient.STATUS_SIGNED, EnvelopeRecipient.STATUS_DECLINED]
    ).select_related("envelope", "envelope__created_by")

    action_required = [r for r in my_recipient_rows if r.can_sign_now()]
    waiting_on_others = [r for r in my_recipient_rows if not r.can_sign_now()]

    stats = {
        "action_required": len(action_required),
        "sent": base.filter(status=Envelope.STATUS_SENT).count(),
        "completed": base.filter(status=Envelope.STATUS_COMPLETED).count(),
        "drafts": base.filter(status=Envelope.STATUS_DRAFT, created_by=user).count(),
    }

    return render(
        request,
        "accounts/esign/dashboard.html",
        {
            "envelopes": base.prefetch_related("recipients")[:200],
            "action_required": action_required,
            "waiting_on_others": waiting_on_others,
            "stats": stats,
            "q": q,
            "status": status,
            "status_choices": Envelope.STATUS_CHOICES,
            "my_signatures": SignatureProfile.objects.filter(user=user),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Create
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def esign_new(request):
    agency = _agency_or_redirect(request)
    if not agency:
        return redirect("accounts:profile")
    if not _service_enabled(agency, request.user):
        messages.warning(request, "eSign is not enabled for your agency.")
        return redirect("accounts:profile")

    asset = None
    asset_id = request.GET.get("asset")
    if asset_id:
        asset = Asset.objects.filter(agency=agency, id=asset_id).first()

    if request.method == "GET":
        converter_ok, converter_note = conversion_backend_available()
        return render(
            request,
            "accounts/esign/envelope_new.html",
            {
                "asset": asset,
                "converter_ok": converter_ok,
                "converter_note": converter_note,
                "office_ext": ", ".join(
                    e.lstrip(".").upper() for e in supported_upload_ext()
                ),
                "accept_attr": ",".join(allowed_doc_ext()),
                "colleagues": User.objects.filter(agency=agency, is_active=True)
                .order_by("first_name", "username")[:500],
                "role_choices": EnvelopeRecipient.ROLE_CHOICES,
            },
        )

    subject = (request.POST.get("subject") or "").strip()
    if not subject:
        messages.error(request, "Please give the envelope a subject.")
        return redirect(request.get_full_path())

    files = request.FILES.getlist("documents")
    if not files:
        messages.error(request, "Attach at least one document (PDF, PNG or JPG).")
        return redirect(request.get_full_path())

    names = request.POST.getlist("recipient_name")
    emails = request.POST.getlist("recipient_email")
    roles = request.POST.getlist("recipient_role")
    titles = request.POST.getlist("recipient_title")
    codes = request.POST.getlist("recipient_access_code")

    parsed = []
    for i, email in enumerate(emails):
        email = (email or "").strip()
        if not email:
            continue
        parsed.append(
            {
                "name": (names[i] if i < len(names) else "").strip() or email.split("@")[0],
                "email": email,
                "role": (roles[i] if i < len(roles) else EnvelopeRecipient.ROLE_SIGNER),
                "title": (titles[i] if i < len(titles) else "").strip(),
                "access_code": (codes[i] if i < len(codes) else "").strip(),
            }
        )

    if not any(p["role"] in (EnvelopeRecipient.ROLE_SIGNER, EnvelopeRecipient.ROLE_APPROVER) for p in parsed):
        messages.error(request, "Add at least one recipient who needs to sign or approve.")
        return redirect(request.get_full_path())

    expires_raw = (request.POST.get("expires_at") or "").strip()
    expires_at = None
    if expires_raw:
        try:
            expires_at = timezone.make_aware(
                timezone.datetime.strptime(expires_raw, "%Y-%m-%d")
            ) + timezone.timedelta(hours=23, minutes=59)
        except Exception:
            expires_at = None

    try:
        asset_pk = int(request.POST.get("asset_id") or 0)
    except (TypeError, ValueError):
        asset_pk = 0

    try:
      with transaction.atomic():
        envelope = Envelope.objects.create(
            agency=agency,
            subject=subject,
            message=(request.POST.get("message") or "").strip(),
            created_by=request.user,
            enforce_order=bool(request.POST.get("enforce_order")),
            reminders_enabled=bool(request.POST.get("reminders_enabled")),
            reminder_days=int(request.POST.get("reminder_days") or 3),
            expires_at=expires_at,
            reference=(request.POST.get("reference") or "").strip(),
            asset=Asset.objects.filter(agency=agency, id=asset_pk).first() if asset_pk else None,
        )

        for order, f in enumerate(files):
            if f.size > MAX_DOC_BYTES:
                raise ValueError(f"{f.name} is larger than 25 MB.")
            if not f.name.lower().endswith(allowed_doc_ext()):
                raise ValueError(
                    f"{f.name}: supported types here are "
                    + ", ".join(e.lstrip(".").upper() for e in allowed_doc_ext())
                    + "."
                )
            doc = EnvelopeDocument.objects.create(
                envelope=envelope, file=f, name=f.name[:200], order=order
            )
            # Convert now, once — never on the signer's request.
            doc.page_count = pdf_page_count(doc)
            doc.save(update_fields=["page_count"])
            log_event(
                envelope,
                "document_added",
                request=request,
                actor=request.user,
                note=f"{doc.name}"
                + (" (converted to PDF)" if doc.was_converted else ""),
            )

        for order, p in enumerate(parsed, start=1):
            matched = User.objects.filter(agency=agency, email__iexact=p["email"]).first()
            EnvelopeRecipient.objects.create(
                envelope=envelope,
                user=matched,
                name=p["name"],
                email=p["email"],
                title=p["title"] or (getattr(matched, "job_title", "") or ""),
                role=p["role"],
                order=order,
                access_code=p["access_code"],
            )

        log_event(
            envelope,
            "created",
            request=request,
            actor=request.user,
            note=f"Envelope created with {len(files)} document(s) and {len(parsed)} recipient(s).",
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(request.get_full_path())
    except Exception:
        messages.error(request, "The envelope could not be created. Please check the files and try again.")
        return redirect(request.get_full_path())

    messages.success(request, "Draft created. Now place the fields on the document.")
    return redirect("accounts:esign_prepare", pk=envelope.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Prepare (field placement)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def esign_prepare(request, pk):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    if envelope.status != Envelope.STATUS_DRAFT:
        messages.info(request, "This envelope has already been sent — fields are locked.")
        return redirect("accounts:esign_envelope_detail", pk=envelope.pk)

    documents = []
    for d in envelope.documents.all():
        documents.append(
            {
                "id": d.id,
                "name": str(d),
                "page_count": d.page_count or 1,
                "sizes": pdf_page_sizes(d),
                "url": reverse("accounts:esign_document_file", args=[envelope.pk, d.id]),
            }
        )

    signers = list(envelope.signers().values("id", "name", "email", "role", "order"))
    existing = list(
        envelope.fields.values(
            "id", "document_id", "recipient_id", "kind", "page", "x", "y", "w", "h", "required", "label"
        )
    )

    return render(
        request,
        "accounts/esign/envelope_prepare.html",
        {
            "envelope": envelope,
            "documents_json": json.dumps(documents),
            "signers_json": json.dumps(signers, default=str),
            "fields_json": json.dumps(existing, default=str),
            "field_kinds": SignatureField.KIND_CHOICES,
        },
    )


@login_required
@require_POST
def esign_fields_save(request, pk):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    if envelope.status != Envelope.STATUS_DRAFT:
        return JsonResponse({"ok": False, "error": "Envelope is locked."}, status=400)

    try:
        payload = json.loads(request.body.decode("utf-8"))
        rows = payload.get("fields", [])
    except Exception:
        return JsonResponse({"ok": False, "error": "Malformed payload."}, status=400)

    valid_docs = set(envelope.documents.values_list("id", flat=True))
    valid_recipients = set(envelope.signers().values_list("id", flat=True))
    valid_kinds = {k for k, _ in SignatureField.KIND_CHOICES}

    with transaction.atomic():
        envelope.fields.all().delete()
        created = 0
        for r in rows:
            try:
                doc_id = int(r.get("document_id"))
                rec_id = int(r.get("recipient_id"))
                kind = str(r.get("kind"))
                if doc_id not in valid_docs or rec_id not in valid_recipients or kind not in valid_kinds:
                    continue
                SignatureField.objects.create(
                    envelope=envelope,
                    document_id=doc_id,
                    recipient_id=rec_id,
                    kind=kind,
                    label=(r.get("label") or "")[:80],
                    page=max(1, int(r.get("page") or 1)),
                    x=_clamp(r.get("x")),
                    y=_clamp(r.get("y")),
                    w=_clamp(r.get("w"), lo=0.01),
                    h=_clamp(r.get("h"), lo=0.008),
                    required=bool(r.get("required", True)),
                )
                created += 1
            except Exception:
                continue

    log_event(
        envelope, "field_placed", request=request, actor=request.user,
        note=f"{created} field(s) placed.",
    )
    return JsonResponse({"ok": True, "count": created})


def _clamp(v, lo=0.0, hi=1.0):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return lo


# ─────────────────────────────────────────────────────────────────────────────
# Send / manage
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def esign_send(request, pk):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    if envelope.status != Envelope.STATUS_DRAFT:
        messages.info(request, "This envelope has already been sent.")
        return redirect("accounts:esign_envelope_detail", pk=envelope.pk)

    signers = list(envelope.signers())
    if not signers:
        messages.error(request, "Add at least one signer before sending.")
        return redirect("accounts:esign_prepare", pk=envelope.pk)

    missing = [s.name for s in signers if not envelope.fields.filter(recipient=s).exists()]
    if missing:
        messages.error(
            request,
            "These recipients have no fields placed yet: " + ", ".join(missing),
        )
        return redirect("accounts:esign_prepare", pk=envelope.pk)

    envelope.status = Envelope.STATUS_SENT
    envelope.sent_at = timezone.now()
    envelope.save(update_fields=["status", "sent_at"])

    log_event(
        envelope, "sent", request=request, actor=request.user,
        note=f"Sent to {len(signers)} signer(s); "
             f"{envelope.observers().count()} copy recipient(s).",
    )

    for i, s in enumerate(signers):
        first = (i == 0) or not envelope.enforce_order
        if first:
            s.status = EnvelopeRecipient.STATUS_SENT
            s.sent_at = timezone.now()
            s.save(update_fields=["status", "sent_at"])
            esign_notify.notify_invite(request, s, is_turn=True)
        elif not envelope.enforce_order:
            esign_notify.notify_invite(request, s, is_turn=True)

    for v in envelope.observers().filter(role=EnvelopeRecipient.ROLE_VIEWER):
        v.status = EnvelopeRecipient.STATUS_SENT
        v.sent_at = timezone.now()
        v.save(update_fields=["status", "sent_at"])
        esign_notify.notify_viewer(request, v)

    messages.success(request, f"Sent. Envelope ID {envelope.short_id}.")
    return redirect("accounts:esign_envelope_detail", pk=envelope.pk)


@login_required
def esign_envelope_detail(request, pk):
    envelope = _get_envelope_for_user(request, pk)

    if envelope_is_expired(envelope):
        envelope.status = Envelope.STATUS_EXPIRED
        envelope.save(update_fields=["status"])

    me = envelope.recipients.filter(
        Q(user=request.user) | Q(email__iexact=request.user.email)
    ).first()

    return render(
        request,
        "accounts/esign/envelope_detail.html",
        {
            "envelope": envelope,
            "documents": envelope.documents.all(),
            "signers": envelope.signers(),
            "observers": envelope.observers(),
            "events": envelope.events.select_related("recipient", "actor").all(),
            "progress": envelope.progress(),
            "is_sender": envelope.created_by_id == request.user.id or request.user.is_superuser,
            "my_recipient": me,
            "can_sign_now": bool(me and me.can_sign_now()),
        },
    )


@login_required
@require_POST
def esign_remind(request, pk):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    if envelope.status != Envelope.STATUS_SENT:
        messages.info(request, "Reminders only apply to envelopes out for signature.")
        return redirect("accounts:esign_envelope_detail", pk=envelope.pk)

    targets = [s for s in envelope.signers() if s.can_sign_now()] or list(
        envelope.signers().exclude(status=EnvelopeRecipient.STATUS_SIGNED)
    )
    for t in targets:
        esign_notify.notify_reminder(request, t)

    envelope.last_reminded_at = timezone.now()
    envelope.save(update_fields=["last_reminded_at"])
    messages.success(request, f"Reminder sent to {len(targets)} recipient(s).")
    return redirect("accounts:esign_envelope_detail", pk=envelope.pk)


@login_required
@require_POST
def esign_resend(request, pk, recipient_id):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    recipient = get_object_or_404(EnvelopeRecipient, pk=recipient_id, envelope=envelope)

    if recipient.is_signing_role:
        esign_notify.notify_invite(request, recipient, is_turn=recipient.can_sign_now())
    else:
        esign_notify.notify_viewer(request, recipient)

    log_event(envelope, "resent", request=request, actor=request.user, recipient=recipient,
              note=f"Link resent to {recipient.email}")
    messages.success(request, f"Link resent to {recipient.email}.")
    return redirect("accounts:esign_envelope_detail", pk=envelope.pk)


@login_required
@require_POST
def esign_void(request, pk):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    if envelope.status not in (Envelope.STATUS_DRAFT, Envelope.STATUS_SENT):
        messages.info(request, "This envelope can no longer be voided.")
        return redirect("accounts:esign_envelope_detail", pk=envelope.pk)

    reason = (request.POST.get("reason") or "").strip()
    envelope.status = Envelope.STATUS_VOIDED
    envelope.voided_at = timezone.now()
    envelope.void_reason = reason
    envelope.save(update_fields=["status", "voided_at", "void_reason"])

    log_event(envelope, "voided", request=request, actor=request.user, note=reason or "Voided by sender")
    esign_notify.notify_voided(request, envelope)
    messages.success(request, "Envelope voided and all parties notified.")
    return redirect("accounts:esign_envelope_detail", pk=envelope.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Files (internal)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_document_file(request, pk, doc_id):
    envelope = _get_envelope_for_user(request, pk)
    doc = get_object_or_404(EnvelopeDocument, pk=doc_id, envelope=envelope)
    return _pdf_response(doc, f"{envelope.envelope_id}-{doc_id}.pdf")


@login_required
@require_GET
def esign_download(request, pk, kind):
    envelope = _get_envelope_for_user(request, pk)
    return _download_output(request, envelope, kind, actor=request.user)


def _pdf_response(doc, filename):
    """Serve a document as PDF. Failures return a readable reason, not a bare 404."""
    try:
        raw = document_pdf_bytes(doc)
    except ValueError as exc:
        # Conversion refused the file (unsupported type, no converter installed)
        logger.warning("eSign: cannot render document %s: %s", doc.pk, exc)
        return HttpResponse(str(exc), status=415, content_type="text/plain")
    except FileNotFoundError:
        logger.error("eSign: file missing on disk for document %s (%s)", doc.pk, doc.file)
        return HttpResponse(
            "The stored file is missing from the media volume.",
            status=404,
            content_type="text/plain",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("eSign: unexpected error rendering document %s", doc.pk)
        return HttpResponse(
            f"The document could not be prepared: {type(exc).__name__}: {exc}",
            status=500,
            content_type="text/plain",
        )

    if not raw or raw[:5] != b"%PDF-":
        return HttpResponse(
            "The stored file is not a valid PDF.", status=422, content_type="text/plain"
        )
    resp = FileResponse(io.BytesIO(raw), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{filename}"'
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


def _download_output(request, envelope, kind, actor=None, recipient=None):
    if kind == "certificate":
        f = envelope.certificate_pdf
        name = f"{envelope.envelope_id}-certificate.pdf"
    else:
        f = envelope.completed_pdf
        name = f"{envelope.envelope_id}-signed.pdf"

    if not f:
        if envelope.status != Envelope.STATUS_COMPLETED:
            raise Http404("Not available until the envelope is completed.")
        raise Http404("File not generated.")

    log_event(envelope, "downloaded", request=request, actor=actor, recipient=recipient,
              note=name)
    f.open("rb")
    resp = FileResponse(f, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Tokenized recipient access
# ─────────────────────────────────────────────────────────────────────────────

def _recipient_or_404(token):
    return get_object_or_404(
        EnvelopeRecipient.objects.select_related("envelope", "envelope__agency"),
        token=token,
    )


def _access_ok(request, recipient) -> bool:
    if not recipient.access_code:
        return True
    return request.session.get(f"esign_ac_{recipient.token}") == "ok"


def esign_sign(request, token):
    recipient = _recipient_or_404(token)
    envelope = recipient.envelope

    if envelope_is_expired(envelope):
        envelope.status = Envelope.STATUS_EXPIRED
        envelope.save(update_fields=["status"])

    # Access code gate
    if recipient.access_code and not _access_ok(request, recipient):
        if request.method == "POST" and request.POST.get("access_code") is not None:
            if (request.POST.get("access_code") or "").strip() == recipient.access_code:
                request.session[f"esign_ac_{recipient.token}"] = "ok"
                return redirect("accounts:esign_sign", token=token)
            messages.error(request, "Incorrect access code.")
        return render(request, "accounts/esign/access_code.html",
                      {"recipient": recipient, "envelope": envelope})

    if not recipient.is_signing_role:
        return redirect("accounts:esign_review", token=token)

    if envelope.status in (Envelope.STATUS_VOIDED, Envelope.STATUS_EXPIRED):
        return render(request, "accounts/esign/unavailable.html",
                      {"envelope": envelope, "recipient": recipient})

    if recipient.status == EnvelopeRecipient.STATUS_SIGNED or envelope.status == Envelope.STATUS_COMPLETED:
        return redirect("accounts:esign_review", token=token)

    if recipient.status == EnvelopeRecipient.STATUS_DECLINED:
        return render(request, "accounts/esign/unavailable.html",
                      {"envelope": envelope, "recipient": recipient, "declined": True})

    if not recipient.can_sign_now():
        return render(request, "accounts/esign/waiting_turn.html",
                      {"envelope": envelope, "recipient": recipient,
                       "next_signer": envelope.next_pending_signer()})

    # ---------------- POST: submit signature ----------------
    if request.method == "POST":
        return _handle_sign_submit(request, recipient)

    # ---------------- GET ----------------
    if recipient.status != EnvelopeRecipient.STATUS_VIEWED:
        recipient.status = EnvelopeRecipient.STATUS_VIEWED
        recipient.viewed_at = recipient.viewed_at or timezone.now()
        recipient.save(update_fields=["status", "viewed_at"])
        log_event(envelope, "viewed", request=request, recipient=recipient,
                  note=f"{recipient.name} opened the document.")
        esign_notify.notify_sender_viewed(request, recipient)

    documents = [
        {
            "id": d.id,
            "name": str(d),
            "page_count": d.page_count or 1,
            "url": reverse("accounts:esign_token_document", args=[token, d.id]),
        }
        for d in envelope.documents.all()
    ]
    fields = list(
        envelope.fields.filter(recipient=recipient).values(
            "id", "document_id", "kind", "page", "x", "y", "w", "h", "required", "label"
        )
    )
    others = list(
        envelope.fields.exclude(recipient=recipient).values(
            "id", "document_id", "kind", "page", "x", "y", "w", "h"
        )
    )

    saved = []
    if recipient.user_id:
        saved = [
            {"id": s.id, "label": s.label or s.get_kind_display(),
             "url": s.image.url if s.image else "",
             "initials": s.initials_image.url if s.initials_image else "",
             "is_default": s.is_default}
            for s in SignatureProfile.objects.filter(user_id=recipient.user_id)
        ]

    return render(
        request,
        "accounts/esign/sign.html",
        {
            "envelope": envelope,
            "recipient": recipient,
            "documents_json": json.dumps(documents),
            "fields_json": json.dumps(fields, default=str),
            "others_json": json.dumps(others, default=str),
            "saved_signatures": saved,
            "fonts": ESIGN_FONTS,
            "default_name": recipient.name,
            "default_initials": _initials_from(recipient.name),
        },
    )


def _initials_from(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    return "".join(p[0].upper() for p in parts[:3]) or "NA"


@transaction.atomic
def _handle_sign_submit(request, recipient):
    envelope = recipient.envelope

    if not request.POST.get("consent"):
        messages.error(request, "You must accept the electronic record consent to sign.")
        return redirect("accounts:esign_sign", token=recipient.token)

    try:
        payload = json.loads(request.POST.get("fields_payload") or "{}")
    except Exception:
        payload = {}

    my_fields = list(envelope.fields.filter(recipient=recipient))
    by_id = {str(f.id): f for f in my_fields}

    save_signature = bool(request.POST.get("save_signature"))
    sig_data = request.POST.get("signature_data") or ""
    init_data = request.POST.get("initials_data") or ""

    missing = []
    now = timezone.now()

    for fid, raw in payload.items():
        f = by_id.get(str(fid))
        if not f:
            continue

        if f.kind in (SignatureField.KIND_SIGNATURE, SignatureField.KIND_INITIALS):
            data_url = raw if isinstance(raw, str) else ""
            if not data_url:
                data_url = sig_data if f.kind == SignatureField.KIND_SIGNATURE else init_data
            content = decode_signature_data_url(data_url)
            if content:
                f.image.save(f"field-{f.id}.png", content, save=False)
                f.filled_at = now
                f.save()
            continue

        if f.kind == SignatureField.KIND_CHECKBOX:
            f.value = "1" if str(raw) in ("1", "true", "True", "on") else ""
        else:
            f.value = str(raw or "")[:2000]
        f.filled_at = now if f.value else None
        f.save(update_fields=["value", "filled_at"])

    for f in envelope.fields.filter(recipient=recipient):
        if f.required and not f.is_filled:
            missing.append(f.label or f.get_kind_display())

    if missing:
        messages.error(request, "Please complete all required fields: " + ", ".join(missing[:6]))
        return redirect("accounts:esign_sign", token=recipient.token)

    recipient.status = EnvelopeRecipient.STATUS_SIGNED
    recipient.signed_at = now
    recipient.consent_accepted = True
    from .utils_esign import client_ip, user_agent

    recipient.signed_ip = client_ip(request)
    recipient.signed_user_agent = user_agent(request)
    recipient.save()

    log_event(envelope, "consent", request=request, recipient=recipient,
              note="Consent to use electronic records and signatures accepted.")
    log_event(
        envelope,
        "signed" if recipient.role == EnvelopeRecipient.ROLE_SIGNER else "approved",
        request=request,
        recipient=recipient,
        note=f"{recipient.name} signed. Recipient token {recipient.short_token}.",
        meta={"fields": len(my_fields)},
    )

    # Persist the signature for re-use
    if save_signature and recipient.user_id:
        content = decode_signature_data_url(sig_data)
        if content:
            prof = SignatureProfile(
                user_id=recipient.user_id,
                label=request.POST.get("signature_label") or "My signature",
                kind=request.POST.get("signature_kind") or SignatureProfile.KIND_DRAWN,
                typed_text=request.POST.get("typed_text") or "",
                initials_text=request.POST.get("typed_initials") or "",
                font_key=request.POST.get("font_key") or "dancing",
                is_default=True,
            )
            prof.image.save(f"sig-{recipient.user_id}.png", content, save=False)
            init_content = decode_signature_data_url(init_data)
            if init_content:
                prof.initials_image.save(f"init-{recipient.user_id}.png", init_content, save=False)
            prof.save()

    esign_notify.notify_sender_signed(request, recipient)

    if envelope.all_signed():
        finalize_envelope(envelope, request=request)
        esign_notify.notify_completed(request, envelope)
    else:
        nxt = envelope.next_pending_signer()
        if nxt and envelope.enforce_order:
            nxt.status = EnvelopeRecipient.STATUS_SENT
            nxt.sent_at = timezone.now()
            nxt.save(update_fields=["status", "sent_at"])
            esign_notify.notify_turn(request, nxt)

    messages.success(request, "Signed. Thank you.")
    return redirect("accounts:esign_review", token=recipient.token)


@require_POST
def esign_decline(request, token):
    recipient = _recipient_or_404(token)
    envelope = recipient.envelope

    if not recipient.is_signing_role or recipient.status == EnvelopeRecipient.STATUS_SIGNED:
        raise Http404()

    reason = (request.POST.get("reason") or "").strip()
    recipient.status = EnvelopeRecipient.STATUS_DECLINED
    recipient.declined_at = timezone.now()
    recipient.decline_reason = reason
    recipient.save(update_fields=["status", "declined_at", "decline_reason"])

    envelope.status = Envelope.STATUS_DECLINED
    envelope.save(update_fields=["status"])

    log_event(envelope, "declined", request=request, recipient=recipient,
              note=reason or "Declined to sign.")
    esign_notify.notify_declined(request, recipient)

    return render(request, "accounts/esign/unavailable.html",
                  {"envelope": envelope, "recipient": recipient, "declined": True})


def esign_review(request, token):
    """Read-only view for signers who are done, and for CC / BCC / viewers."""
    recipient = _recipient_or_404(token)
    envelope = recipient.envelope

    if recipient.access_code and not _access_ok(request, recipient):
        if request.method == "POST":
            if (request.POST.get("access_code") or "").strip() == recipient.access_code:
                request.session[f"esign_ac_{recipient.token}"] = "ok"
                return redirect("accounts:esign_review", token=token)
            messages.error(request, "Incorrect access code.")
        return render(request, "accounts/esign/access_code.html",
                      {"recipient": recipient, "envelope": envelope})

    if recipient.status in (EnvelopeRecipient.STATUS_PENDING, EnvelopeRecipient.STATUS_SENT):
        recipient.status = EnvelopeRecipient.STATUS_VIEWED
        recipient.viewed_at = recipient.viewed_at or timezone.now()
        recipient.save(update_fields=["status", "viewed_at"])
        log_event(envelope, "viewed", request=request, recipient=recipient,
                  note=f"{recipient.name} ({recipient.get_role_display()}) opened the document.")

    documents = [
        {"id": d.id, "name": str(d), "page_count": d.page_count or 1,
         "url": reverse("accounts:esign_token_document", args=[token, d.id])}
        for d in envelope.documents.all()
    ]

    return render(
        request,
        "accounts/esign/review.html",
        {
            "envelope": envelope,
            "recipient": recipient,
            "documents_json": json.dumps(documents),
            "signers": envelope.signers(),
            "events": envelope.events.select_related("recipient").all(),
            "show_audit": recipient.role != EnvelopeRecipient.ROLE_BCC or True,
        },
    )


@require_GET
def esign_token_document(request, token, doc_id):
    recipient = _recipient_or_404(token)
    if recipient.access_code and not _access_ok(request, recipient):
        raise Http404()

    envelope = recipient.envelope

    # Once complete, everyone with a token sees the stamped version.
    if envelope.status == Envelope.STATUS_COMPLETED and envelope.completed_pdf:
        envelope.completed_pdf.open("rb")
        resp = FileResponse(envelope.completed_pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{envelope.envelope_id}-signed.pdf"'
        return resp

    doc = get_object_or_404(EnvelopeDocument, pk=doc_id, envelope=envelope)
    return _pdf_response(doc, f"{envelope.envelope_id}-{doc_id}.pdf")


@require_GET
def esign_token_download(request, token, kind):
    recipient = _recipient_or_404(token)
    if recipient.access_code and not _access_ok(request, recipient):
        raise Http404()
    return _download_output(request, recipient.envelope, kind, recipient=recipient)


@require_GET
def esign_preview(request, pk):
    """Sender-side preview of exactly what will be stamped (pre-completion)."""
    envelope = _get_envelope_for_user(request, pk)
    raw = build_final_pdf(envelope)
    resp = FileResponse(io.BytesIO(raw), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{envelope.envelope_id}-preview.pdf"'
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Signature studio (saved signatures)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def esign_signatures(request):
    return render(
        request,
        "accounts/esign/signature_studio.html",
        {
            "signatures": SignatureProfile.objects.filter(user=request.user),
            "fonts": ESIGN_FONTS,
            "default_name": request.user.get_full_name() or request.user.username,
            "default_initials": _initials_from(request.user.get_full_name() or request.user.username),
        },
    )


@login_required
@require_POST
def esign_signature_save(request):
    """Accepts JSON or form POST. Used by the studio and by the signing page."""
    if request.content_type and "application/json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            return JsonResponse({"ok": False, "error": "Malformed payload."}, status=400)
    else:
        data = request.POST

    content = decode_signature_data_url(data.get("signature_data") or "")
    if not content:
        return JsonResponse({"ok": False, "error": "No usable signature image."}, status=400)

    prof = SignatureProfile(
        user=request.user,
        label=(data.get("label") or "My signature")[:60],
        kind=data.get("kind") or SignatureProfile.KIND_DRAWN,
        typed_text=(data.get("typed_text") or "")[:120],
        initials_text=(data.get("typed_initials") or "")[:12],
        font_key=(data.get("font_key") or "dancing")[:24],
        is_default=True,
    )
    prof.image.save(f"sig-{request.user.id}.png", content, save=False)

    init = decode_signature_data_url(data.get("initials_data") or "")
    if init:
        prof.initials_image.save(f"init-{request.user.id}.png", init, save=False)
    prof.save()

    return JsonResponse(
        {
            "ok": True,
            "id": prof.id,
            "url": prof.image.url,
            "initials": prof.initials_image.url if prof.initials_image else "",
            "label": prof.label,
        }
    )


@login_required
@require_POST
def esign_signature_delete(request, pk):
    prof = get_object_or_404(SignatureProfile, pk=pk, user=request.user)
    prof.delete()
    messages.success(request, "Signature deleted.")
    return redirect("accounts:esign_signatures")


@login_required
@require_POST
def esign_signature_default(request, pk):
    prof = get_object_or_404(SignatureProfile, pk=pk, user=request.user)
    prof.is_default = True
    prof.save()
    messages.success(request, "Default signature updated.")
    return redirect("accounts:esign_signatures")
