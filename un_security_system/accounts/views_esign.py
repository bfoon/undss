# accounts/views_esign.py
"""
UN PASS — eSign views.

Internal (login required)          Public / tokenized (no login)
────────────────────────           ─────────────────────────────
esign_dashboard                    esign_sign          <token>
esign_new                          esign_decline       <token>
esign_prepare / esign_fields_save  esign_review        <token>
esign_recipient_add / _remove      esign_token_document<token>/<doc>
esign_recipients_reorder           esign_token_download<token>/<kind>
esign_send / esign_void
esign_remind / esign_resend
esign_envelope_detail
esign_document_file / esign_download
esign_signatures (+ save/delete/default)
"""

import base64
import io
import json
import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.validators import validate_email
from django.conf import settings
from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import esign_notify
from .models import Asset
from .models_esign import (
    Envelope,
    EnvelopeComment,
    EnvelopeDocument,
    EnvelopeRecipient,
    SignatureField,
    SignatureProfile,
)
try:
    from .converters_esign import conversion_backend_available, supported_upload_ext
except ImportError:  # older converters_esign.py — degrade instead of breaking the URLconf
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "accounts.converters_esign is out of date — Office uploads disabled. "
        "Replace it with the current version to restore DOCX support."
    )

    def supported_upload_ext():
        return ()

    def conversion_backend_available():
        return False, (
            "accounts/converters_esign.py is an older version and does not "
            "expose supported_upload_ext(). Replace the file to enable Office "
            "uploads. PDF and image uploads are unaffected."
        )
from .utils_esign import (
    build_final_pdf,
    client_ip,
    prepare_document,
    decode_signature_data_url,
    document_pdf_bytes,
    envelope_is_expired,
    finalize_envelope,
    log_event,
    pdf_page_count,
    pdf_page_sizes,
)
from .view_asset_management import _is_ict, _is_ops_manager, _managed_unit_ids
from .esign_access import (
    can_manage as _esign_can_manage,
    can_sign as _esign_can_sign,
    can_view as _esign_can_view,
    get_envelope_or_404 as _esign_get_envelope,
    inbox_rows as _esign_inbox_rows,
    is_owner as _esign_is_owner,
    recipient_for as _esign_recipient_for,
    visible_envelopes as _esign_visible_envelopes,
    visible_recipients as _esign_visible_recipients,
)

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

#: Documents at or under this size are embedded in the page as base64 so the
#: browser never makes a second request for them. Set ESIGN_INLINE_MAX_BYTES=0
#: to disable and always fetch by URL.
def _inline_limit() -> int:
    return int(getattr(settings, "ESIGN_INLINE_MAX_BYTES", 8 * 1024 * 1024))


def _document_payload(doc, url, raw=None):
    """
    Build the JSON entry the viewers consume. Includes the PDF bytes inline
    when they're small enough — that removes the separate HTTP request, which
    is the single most fragile part of rendering (CSP connect-src, proxies,
    browser extensions, aborted streaming responses).
    """
    entry = {
        "id": doc.id,
        "name": str(doc),
        "page_count": doc.page_count or 1,
        "url": url,
    }

    limit = _inline_limit()
    if limit <= 0:
        return entry

    try:
        if raw is None:
            raw = document_pdf_bytes(doc)
        if raw and raw[:5] == b"%PDF-" and len(raw) <= limit:
            entry["data"] = base64.b64encode(raw).decode("ascii")
        elif raw:
            logger.info(
                "eSign: document %s is %d bytes — above the inline limit, "
                "the viewer will fetch it by URL.", doc.pk, len(raw)
            )
    except Exception:
        logger.exception("eSign: could not inline document %s", getattr(doc, "pk", "?"))

    return entry


def _agency_or_redirect(request):
    """Resolve the agency directly or through the user's country office."""
    user = request.user
    agency = getattr(user, "agency", None)
    if agency is None:
        office = getattr(user, "country_office", None)
        agency = getattr(office, "agency", None)
    if not agency:
        messages.error(request, "You are not assigned to an agency or country office.")
        return None
    return agency


def _service_enabled(agency, user) -> bool:
    """Return the resolved tenancy entitlement for eSign.

    `agency` is kept in the signature for backward compatibility with the
    existing callers, but entitlement resolution now comes exclusively from
    tenancy. That means country-office overrides, agency grants, expiry and
    catalogue defaults all resolve in one place.
    """
    from tenancy.services import has_feature

    return has_feature(user, "esign")


def _can_manage_envelope(user, envelope) -> bool:
    """
    Kept under its old name so existing callers keep working, but the meaning
    is now correct: "manage" is the sender's authority, not everyone who can
    see the envelope. Use _esign_can_view for read access.
    """
    return _esign_can_manage(user, envelope)


def _get_envelope_for_user(request, pk, editable=False):
    """
    Fetch an envelope and enforce access.

    editable=True  -> the sender only
    editable=False -> any party to the envelope

    There is no agency filter any more. Access follows participation, which is
    what lets a recipient in a linked office open an envelope sent to them.
    """
    return _esign_get_envelope(request, pk, mode="manage" if editable else "view")


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def esign_dashboard(request):
    agency = _agency_or_redirect(request)
    if not agency:
        return redirect("accounts:profile")
    if not _service_enabled(agency, request.user):
        messages.warning(request, "eSign is not enabled for your agency or country office.")
        return redirect("accounts:profile")

    user = request.user
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    # Participation decides visibility — not agency membership, and not the
    # asset-administration roles. See accounts/esign_access.py.
    base = _esign_visible_envelopes(user).select_related("created_by")

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

    my_recipient_rows = _esign_inbox_rows(user)

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
        messages.warning(request, "eSign is not enabled for your agency or country office.")
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
            # Convert now, once. Raises if it fails, so the whole envelope is
            # rolled back and the sender is told why — rather than the signer
            # meeting the error later.
            prepare_document(doc)
            log_event(
                envelope,
                "document_added",
                request=request,
                actor=request.user,
                note=f"{doc.name}"
                + (" (converted to PDF)" if getattr(doc, "was_converted", False) else ""),
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
        # Expected, actionable problems: unsupported type, oversize, no converter
        messages.error(request, str(exc))
        return redirect(request.get_full_path())

    except (ProgrammingError, OperationalError) as exc:
        # Almost always a missing migration for the eSign tables/columns
        logger.exception("eSign: database schema error creating envelope")
        first = str(exc).strip().splitlines()[0][:200]
        messages.error(
            request,
            "The eSign database schema is out of date. Run "
            "`python manage.py makemigrations accounts && python manage.py migrate` "
            f"and try again. Database said: {first}",
        )
        return redirect(request.get_full_path())

    except Exception as exc:  # noqa: BLE001
        logger.exception("eSign: envelope creation failed")
        detail = f"{type(exc).__name__}: {exc}"[:300]
        if settings.DEBUG or request.user.is_superuser or request.user.is_staff:
            messages.error(request, f"The envelope could not be created — {detail}")
        else:
            messages.error(
                request,
                "The envelope could not be created. The error has been logged — "
                "please ask ICT to check the server log.",
            )
        return redirect(request.get_full_path())

    messages.success(request, "Draft created. Now place the fields on the document.")
    return redirect("accounts:esign_prepare", pk=envelope.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Prepare (field placement)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def esign_prepare(request, pk):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    if not envelope.is_editable:
        messages.info(
            request,
            "This envelope has already been sent — fields are locked. "
            "Use Rework if you need to change it.",
        )
        return redirect("accounts:esign_envelope_detail", pk=envelope.pk)

    documents = []
    for d in envelope.documents.all():
        entry = _document_payload(
            d, reverse("accounts:esign_document_file", args=[envelope.pk, d.id])
        )
        entry["sizes"] = pdf_page_sizes(d)
        documents.append(entry)

    signers = list(envelope.signers().values("id", "name", "email", "role", "order"))
    existing = list(
        envelope.fields.values(
            "id", "document_id", "recipient_id", "kind", "page", "x", "y", "w", "h", "required", "label"
        )
    )

    from .converters_esign import conversion_backend_available as _cba

    return render(
        request,
        "accounts/esign/envelope_prepare.html",
        {
            "envelope": envelope,
            "accept_attr": ",".join(allowed_doc_ext()),
            "converter_ok": _cba()[0],
            "documents_json": json.dumps(documents),
            "signers_json": json.dumps(signers, default=str),
            "fields_json": json.dumps(existing, default=str),
            "field_kinds": SignatureField.KIND_CHOICES,
            "colleagues": User.objects.filter(
                agency=envelope.agency, is_active=True
            ).order_by("first_name", "username")[:500],
            "signing_roles": [
                (EnvelopeRecipient.ROLE_SIGNER, "Needs to sign"),
                (EnvelopeRecipient.ROLE_APPROVER, "Needs to approve"),
            ],
        },
    )


@login_required
@require_POST
def esign_fields_save(request, pk):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    if not envelope.is_editable:
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
# Recipients on a draft: reorder / remove
#
# Both endpoints are JSON and draft-only. They are driven from the "Fields per
# signer" panel on the prepare screen, so the sender can fix the party list
# without going back to the new-envelope form.
# ─────────────────────────────────────────────────────────────────────────────

def _signer_rows(envelope):
    """The same shape the prepare screen is seeded with, so the browser can
    swap its signer list wholesale after a change."""
    return list(envelope.signers().values("id", "name", "email", "role", "order"))


def _renumber_recipients(envelope):
    """Signers take 1..N in their current order; observers follow after them."""
    n = 0
    for r in envelope.signers():
        n += 1
        if r.order != n:
            r.order = n
            r.save(update_fields=["order"])
    for r in envelope.observers():
        n += 1
        if r.order != n:
            r.order = n
            r.save(update_fields=["order"])


def _recipients_editable(envelope):
    """None if the recipient list may still be changed, else the reason why not."""
    if not envelope.is_editable:
        return (
            "This envelope has already been sent — the recipients are locked. "
            "Use Rework if you need to change them."
        )
    return None


@login_required
@require_POST
def esign_recipient_add(request, pk):
    """
    Add one signer or approver to a draft, at the end of the signing order.

    Body: {"name": "...", "email": "...", "role": "signer",
           "title": "...", "access_code": ""}

    Kept to signing roles on purpose — this is driven from the field-placement
    panel, and a CC has no fields to place. Copies and viewers are still set on
    the new-envelope form.
    """
    envelope = _get_envelope_for_user(request, pk, editable=True)
    locked = _recipients_editable(envelope)
    if locked:
        return JsonResponse({"ok": False, "error": locked}, status=400)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Malformed payload."}, status=400)

    email = (payload.get("email") or "").strip()
    if not email:
        return JsonResponse({"ok": False, "error": "An email address is required."}, status=400)
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse(
            {"ok": False, "error": f"{email} is not a valid email address."}, status=400
        )

    if envelope.recipients.filter(email__iexact=email).exists():
        return JsonResponse(
            {"ok": False, "error": f"{email} is already on this envelope."}, status=400
        )

    role = payload.get("role") or EnvelopeRecipient.ROLE_SIGNER
    if role not in (EnvelopeRecipient.ROLE_SIGNER, EnvelopeRecipient.ROLE_APPROVER):
        role = EnvelopeRecipient.ROLE_SIGNER

    matched = User.objects.filter(agency=envelope.agency, email__iexact=email).first()
    name = (payload.get("name") or "").strip()
    if not name:
        name = (
            (matched.get_full_name() or matched.username).strip()
            if matched
            else email.split("@")[0]
        )
    title = (payload.get("title") or "").strip() or (
        getattr(matched, "job_title", "") or "" if matched else ""
    )

    with transaction.atomic():
        recipient = EnvelopeRecipient.objects.create(
            envelope=envelope,
            user=matched,
            name=name[:150],
            email=email,
            title=title[:120],
            role=role,
            order=envelope.recipients.count() + 1,
            access_code=(payload.get("access_code") or "").strip()[:32],
        )
        _renumber_recipients(envelope)

    log_event(
        envelope, "recipient_changed", request=request, actor=request.user,
        note=f"Added {recipient.name} <{recipient.email}> as {recipient.get_role_display().lower()}",
    )
    recipient.refresh_from_db(fields=["order"])
    return JsonResponse(
        {"ok": True, "added_id": recipient.pk, "signers": _signer_rows(envelope)}
    )


@login_required
@require_POST
def esign_recipients_reorder(request, pk):
    """
    Set the signing order from a list of recipient ids.

    Body: {"order": [12, 9, 14]}

    Ids that don't belong to this envelope are ignored; signers the browser
    left out keep their place at the end, so a stale page can never silently
    drop somebody.
    """
    envelope = _get_envelope_for_user(request, pk, editable=True)
    locked = _recipients_editable(envelope)
    if locked:
        return JsonResponse({"ok": False, "error": locked}, status=400)

    try:
        payload = json.loads(request.body.decode("utf-8"))
        wanted = [int(i) for i in payload.get("order", [])]
    except Exception:
        return JsonResponse({"ok": False, "error": "Malformed payload."}, status=400)

    current = list(envelope.signers())
    by_id = {r.id: r for r in current}

    seen, ordered = set(), []
    for rid in wanted:
        r = by_id.get(rid)
        if r is not None and rid not in seen:
            seen.add(rid)
            ordered.append(r)
    for r in current:
        if r.id not in seen:
            ordered.append(r)

    if not ordered:
        return JsonResponse(
            {"ok": False, "error": "There are no signers to reorder."}, status=400
        )

    with transaction.atomic():
        for i, r in enumerate(ordered, start=1):
            if r.order != i:
                r.order = i
                r.save(update_fields=["order"])
        _renumber_recipients(envelope)

    log_event(
        envelope, "recipient_changed", request=request, actor=request.user,
        note="Signing order: "
             + ", ".join(f"{i}. {r.name}" for i, r in enumerate(ordered, start=1)),
    )
    return JsonResponse({"ok": True, "signers": _signer_rows(envelope)})


@login_required
@require_POST
def esign_recipient_remove(request, pk, recipient_id):
    """
    Drop one recipient from a draft. Any fields placed for them go too — they
    would have nobody to fill them.
    """
    envelope = _get_envelope_for_user(request, pk, editable=True)
    locked = _recipients_editable(envelope)
    if locked:
        return JsonResponse({"ok": False, "error": locked}, status=400)

    recipient = get_object_or_404(EnvelopeRecipient, pk=recipient_id, envelope=envelope)

    if recipient.is_signing_role and envelope.signers().count() <= 1:
        return JsonResponse(
            {
                "ok": False,
                "error": "An envelope needs at least one person to sign or approve. "
                         "Add another signer first, then remove this one.",
            },
            status=400,
        )

    name = recipient.name
    role = recipient.get_role_display()
    field_count = recipient.fields.count()

    with transaction.atomic():
        recipient.delete()
        _renumber_recipients(envelope)

    log_event(
        envelope, "recipient_changed", request=request, actor=request.user,
        note=f"Removed {name} ({role})"
             + (f" and {field_count} field(s) placed for them" if field_count else ""),
    )
    return JsonResponse(
        {
            "ok": True,
            "removed_id": recipient.pk,
            "fields_removed": field_count,
            "signers": _signer_rows(envelope),
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# Document page editing (draft only): rotate, delete, reorder, add, remove
# ─────────────────────────────────────────────────────────────────────────────

def _rotate_field_coords(f, deg):
    """
    Rotate a field's normalised box to follow a page rotation.

    Coordinates are fractions of the page with the origin at the top-left,
    x running right and y running down. Rotating the page clockwise moves the
    box and swaps its width and height.
    """
    deg = deg % 360
    if deg == 0:
        return

    x, y, w, h = f.x, f.y, f.w, f.h
    if deg == 90:
        f.x, f.y, f.w, f.h = 1.0 - (y + h), x, h, w
    elif deg == 180:
        f.x, f.y = 1.0 - (x + w), 1.0 - (y + h)
    elif deg == 270:
        f.x, f.y, f.w, f.h = y, 1.0 - (x + w), h, w

    f.x = max(0.0, min(1.0, f.x))
    f.y = max(0.0, min(1.0, f.y))


def _editable_draft(request, pk):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    if not envelope.is_editable:
        raise PermissionDenied(
            "Documents can only be edited while the envelope is a draft or has "
            "been returned for changes."
        )
    return envelope


@login_required
@require_POST
def esign_document_pages(request, pk, doc_id):
    """
    Apply a whole page plan to one document in a single atomic operation.

    Body: {"pages": [{"src": 0, "rotate": 90}, {"src": 2, "rotate": 0}, ...]}

    `src` is the zero-based page index in the document as it stands now. Pages
    left out of the list are deleted; the order of the list becomes the new page
    order. Signature fields follow their page — fields on a deleted page are
    removed, and rotated pages carry their fields around with them.
    """
    from pypdf import PdfReader, PdfWriter

    envelope = _editable_draft(request, pk)
    doc = get_object_or_404(EnvelopeDocument, pk=doc_id, envelope=envelope)

    try:
        plan = json.loads(request.body.decode("utf-8")).get("pages", [])
    except Exception:
        return JsonResponse({"ok": False, "error": "Malformed payload."}, status=400)

    if not isinstance(plan, list) or not plan:
        return JsonResponse(
            {"ok": False, "error": "A document must keep at least one page."}, status=400
        )

    try:
        raw = document_pdf_bytes(doc)
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:
        logger.exception("eSign: cannot read document %s for editing", doc.pk)
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    total = len(reader.pages)
    writer = PdfWriter()

    # src page index (0-based) -> new page number (1-based), plus its rotation
    mapping = {}
    for new_index, entry in enumerate(plan, start=1):
        try:
            src = int(entry.get("src"))
            rotate = int(entry.get("rotate") or 0) % 360
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Invalid page entry."}, status=400)

        if src < 0 or src >= total:
            return JsonResponse(
                {"ok": False, "error": f"Page {src + 1} does not exist."}, status=400
            )
        if rotate not in (0, 90, 180, 270):
            return JsonResponse({"ok": False, "error": "Rotation must be 0/90/180/270."}, status=400)

        page = reader.pages[src]
        if rotate:
            page.rotate(rotate)
        writer.add_page(page)
        mapping.setdefault(src, (new_index, rotate))

    out = io.BytesIO()
    writer.write(out)
    new_bytes = out.getvalue()

    with transaction.atomic():
        stem = (doc.name or "document").rsplit(".", 1)[0][:120]
        doc.converted_pdf.save(f"{stem}.pdf", ContentFile(new_bytes), save=False)
        doc.page_count = len(plan)
        doc.save(update_fields=["converted_pdf", "page_count"])

        removed = 0
        for f in list(doc.fields.all()):
            entry = mapping.get(f.page - 1)
            if entry is None:
                f.delete()
                removed += 1
                continue
            new_page, rotate = entry
            f.page = new_page
            _rotate_field_coords(f, rotate)
            f.save(update_fields=["page", "x", "y", "w", "h"])

    log_event(
        envelope,
        "document_added",
        request=request,
        actor=request.user,
        note=(
            f"Pages edited on {doc}: {total} → {len(plan)} page(s)"
            + (f", {removed} field(s) removed with deleted pages" if removed else "")
        ),
    )

    return JsonResponse({"ok": True, "page_count": len(plan), "fields_removed": removed})


@login_required
@require_POST
def esign_document_add(request, pk):
    """Append more documents to a draft envelope."""
    envelope = _editable_draft(request, pk)

    files = request.FILES.getlist("documents")
    if not files:
        messages.error(request, "Choose at least one file to add.")
        return redirect("accounts:esign_prepare", pk=envelope.pk)

    start = (envelope.documents.count() or 0)
    added = 0

    for offset, f in enumerate(files):
        if f.size > MAX_DOC_BYTES:
            messages.error(request, f"{f.name} is larger than 25 MB.")
            continue
        if not f.name.lower().endswith(allowed_doc_ext()):
            messages.error(
                request,
                f"{f.name}: supported types here are "
                + ", ".join(e.lstrip(".").upper() for e in allowed_doc_ext())
                + ".",
            )
            continue
        try:
            doc = EnvelopeDocument.objects.create(
                envelope=envelope, file=f, name=f.name[:200], order=start + offset
            )
            try:
                prepare_document(doc)
            except Exception:
                doc.delete()          # don't leave an unreadable document behind
                raise
            added += 1
            log_event(
                envelope, "document_added", request=request, actor=request.user,
                note=f"Added {doc.name} ({doc.page_count} page(s))",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("eSign: failed to add document to envelope %s", envelope.pk)
            messages.error(request, f"{f.name}: {exc}")

    if added:
        messages.success(request, f"{added} document(s) added.")
    return redirect("accounts:esign_prepare", pk=envelope.pk)


@login_required
@require_POST
def esign_document_remove(request, pk, doc_id):
    """Remove a whole document (and its fields) from a draft envelope."""
    envelope = _editable_draft(request, pk)
    doc = get_object_or_404(EnvelopeDocument, pk=doc_id, envelope=envelope)

    if envelope.documents.count() <= 1:
        messages.error(request, "An envelope must keep at least one document.")
        return redirect("accounts:esign_prepare", pk=envelope.pk)

    name = str(doc)
    field_count = doc.fields.count()
    doc.delete()

    for order, d in enumerate(envelope.documents.all()):
        if d.order != order:
            d.order = order
            d.save(update_fields=["order"])

    log_event(
        envelope, "document_added", request=request, actor=request.user,
        note=f"Removed {name}" + (f" and {field_count} field(s) on it" if field_count else ""),
    )
    messages.success(request, f"Removed {name}.")
    return redirect("accounts:esign_prepare", pk=envelope.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Send / manage
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def esign_send(request, pk):
    envelope = _get_envelope_for_user(request, pk, editable=True)
    if not envelope.is_editable:
        messages.info(request, "This envelope has already been sent.")
        return redirect("accounts:esign_envelope_detail", pk=envelope.pk)

    signers = list(envelope.signers())
    if not signers:
        messages.error(request, "Add at least one signer before sending.")
        return redirect("accounts:esign_prepare", pk=envelope.pk)

    unreadable = []
    for d in envelope.documents.all():
        try:
            raw = document_pdf_bytes(d)
            if not raw or raw[:5] != b"%PDF-":
                unreadable.append(str(d))
        except Exception as exc:  # noqa: BLE001
            logger.warning("eSign: document %s unreadable at send time: %s", d.pk, exc)
            unreadable.append(f"{d} — {exc}")

    if unreadable:
        messages.error(
            request,
            "These documents can't be prepared for signature yet: "
            + "; ".join(unreadable[:3])
            + ". Fix or re-upload them before sending.",
        )
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
        note=(f"Sent to {len(signers)} signer(s); "
              f"{envelope.observers().count()} copy recipient(s)"
              + (f" — revision {envelope.revision}." if envelope.revision > 1 else ".")),
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

    # A self-signed envelope has nobody to wait for, so hand the sender their
    # own signing screen rather than the "sent, now wait" screen.
    #
    # Imported here, not at the top of the file: views_esign_self imports
    # MAX_DOC_BYTES and the agency helpers from this module, so a module-level
    # import would be a cycle and the URLConf would fail to load.
    from .views_esign_self import is_self_sign

    if is_self_sign(envelope):
        return redirect("accounts:esign_sign", token=signers[0].token)

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
            "comments": envelope.comments.select_related("recipient", "author_user").all(),
            "progress": envelope.progress(),
            "can_rework": (
                envelope.created_by_id == request.user.id or request.user.is_superuser
            ) and envelope.status in (
                Envelope.STATUS_SENT, Envelope.STATUS_RETURNED,
                Envelope.STATUS_DECLINED, Envelope.STATUS_EXPIRED,
            ),
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



@login_required
@require_POST
def esign_envelope_delete(request, pk):
    """
    Permanently delete a DRAFT envelope and everything attached to it.

    Drafts only, on purpose. Once an envelope has been sent, recipients hold
    links and have received emails — the audit trail is a record of what
    happened, not a working file. Sent envelopes are voided (which notifies
    everyone and keeps the history) rather than deleted.
    """
    envelope = _get_envelope_for_user(request, pk, editable=True)

    if envelope.status != Envelope.STATUS_DRAFT:
        messages.error(
            request,
            "Only drafts can be deleted. This envelope has already been sent — "
            "void it instead, which notifies every party and preserves the audit trail.",
        )
        return redirect("accounts:esign_envelope_detail", pk=envelope.pk)

    subject = envelope.subject
    doc_count = envelope.documents.count()

    # Django removes the rows but never the files — clear them up explicitly.
    stored_files = []
    for d in envelope.documents.all():
        for handle in (d.file, getattr(d, "converted_pdf", None)):
            if handle:
                stored_files.append(handle)
    for f in envelope.fields.all():
        if f.image:
            stored_files.append(f.image)

    with transaction.atomic():
        envelope.delete()

    removed = 0
    for handle in stored_files:
        try:
            handle.delete(save=False)
            removed += 1
        except Exception:
            logger.warning("eSign: could not delete stored file %s", getattr(handle, "name", "?"))

    logger.info(
        "eSign: %s deleted draft '%s' (%d document(s), %d file(s) removed)",
        request.user, subject, doc_count, removed,
    )
    messages.success(request, f"Draft deleted: {subject}")
    return redirect("accounts:esign_dashboard")



# ─────────────────────────────────────────────────────────────────────────────
# Return for changes · rework · comments · duplicate
# ─────────────────────────────────────────────────────────────────────────────

@require_POST
def esign_return(request, token):
    """
    A signer sends the envelope BACK to the sender instead of declining.

    Declining kills the envelope; returning parks it so the sender can fix the
    document and send it round again — the normal outcome when a serial number
    is wrong or a clause needs changing.
    """
    recipient = _recipient_or_404(token)
    envelope = recipient.envelope

    if recipient.access_code and not _access_ok(request, recipient):
        raise Http404()
    if not recipient.is_signing_role or envelope.status != Envelope.STATUS_SENT:
        raise Http404()

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Please say what needs changing.")
        return redirect("accounts:esign_sign", token=token)

    with transaction.atomic():
        recipient.status = EnvelopeRecipient.STATUS_RETURNED
        recipient.save(update_fields=["status"])

        envelope.status = Envelope.STATUS_RETURNED
        envelope.returned_at = timezone.now()
        envelope.return_reason = reason
        envelope.returned_by = recipient
        envelope.save(update_fields=["status", "returned_at", "return_reason", "returned_by"])

        EnvelopeComment.objects.create(
            envelope=envelope,
            recipient=recipient,
            author_name=recipient.name,
            text=reason,
            revision=envelope.revision,
            ip=client_ip(request),
        )

    log_event(
        envelope, "returned", request=request, recipient=recipient,
        note=f"{recipient.name} returned the document for changes: {reason[:160]}",
    )
    esign_notify.notify_returned(request, recipient)

    messages.success(request, "Returned to the sender. They will be notified.")
    return render(request, "accounts/esign/returned.html",
                  {"envelope": envelope, "recipient": recipient, "reason": reason})


@login_required
@require_POST
def esign_rework(request, pk):
    """
    Reopen a sent / returned / declined envelope for editing.

    Signatures already collected are cleared, deliberately: they attested to a
    document that is about to change. The audit trail keeps the record that they
    happened, and the revision number goes up so the history stays readable.
    """
    envelope = _get_envelope_for_user(request, pk, editable=True)

    if envelope.status not in (
        Envelope.STATUS_SENT, Envelope.STATUS_RETURNED,
        Envelope.STATUS_DECLINED, Envelope.STATUS_EXPIRED,
    ):
        messages.error(request, "Only an envelope that is out, returned, declined or expired can be reworked.")
        return redirect("accounts:esign_envelope_detail", pk=envelope.pk)

    note = (request.POST.get("note") or "").strip()
    cleared = 0

    with transaction.atomic():
        for f in envelope.fields.all():
            if f.is_filled:
                cleared += 1
            if f.image:
                f.image.delete(save=False)
            f.value = ""
            f.filled_at = None
            f.save(update_fields=["image", "value", "filled_at"])

        for r in envelope.recipients.all():
            r.status = EnvelopeRecipient.STATUS_PENDING
            r.signed_at = None
            r.viewed_at = None
            r.declined_at = None
            r.decline_reason = ""
            r.sent_at = None
            r.signed_ip = None
            r.consent_accepted = False
            r.save()

        envelope.revision += 1
        envelope.status = Envelope.STATUS_DRAFT
        envelope.sent_at = None
        envelope.completed_at = None
        envelope.returned_at = None
        envelope.save(update_fields=[
            "revision", "status", "sent_at", "completed_at", "returned_at",
        ])

        if note:
            EnvelopeComment.objects.create(
                envelope=envelope, author_user=request.user,
                author_name=request.user.get_full_name() or request.user.username,
                text=note, is_internal=False, revision=envelope.revision,
                ip=client_ip(request),
            )

    log_event(
        envelope, "reworked", request=request, actor=request.user,
        note=(f"Reopened as revision {envelope.revision}; {cleared} signature(s)/value(s) cleared."
              + (f" Note: {note[:120]}" if note else "")),
        meta={"revision": envelope.revision, "cleared": cleared},
    )

    messages.success(
        request,
        f"Envelope reopened as revision {envelope.revision}. "
        + (f"{cleared} previously collected entry(ies) were cleared. " if cleared else "")
        + "Edit the document or fields, then send again.",
    )
    return redirect("accounts:esign_prepare", pk=envelope.pk)


@require_POST
def esign_comment(request, token):
    """A recipient leaves a comment without signing, declining or returning."""
    recipient = _recipient_or_404(token)
    envelope = recipient.envelope

    if recipient.access_code and not _access_ok(request, recipient):
        raise Http404()

    text = (request.POST.get("text") or "").strip()
    if not text:
        messages.error(request, "Please write a comment first.")
        return redirect("accounts:esign_sign", token=token)

    EnvelopeComment.objects.create(
        envelope=envelope,
        recipient=recipient,
        author_name=recipient.name,
        text=text[:4000],
        revision=envelope.revision,
        ip=client_ip(request),
    )
    log_event(
        envelope, "commented", request=request, recipient=recipient,
        note=f"{recipient.name}: {text[:160]}",
    )
    esign_notify.notify_comment(request, envelope, recipient.name, text)

    messages.success(request, "Comment sent to the sender.")
    return redirect(
        "accounts:esign_sign" if recipient.can_sign_now() else "accounts:esign_review",
        token=token,
    )


@login_required
@require_POST
def esign_comment_internal(request, pk):
    """Sender-side comment. Internal ones are never shown to recipients."""
    envelope = _get_envelope_for_user(request, pk)

    text = (request.POST.get("text") or "").strip()
    if not text:
        messages.error(request, "Please write a comment first.")
        return redirect("accounts:esign_envelope_detail", pk=envelope.pk)

    internal = bool(request.POST.get("internal"))
    EnvelopeComment.objects.create(
        envelope=envelope,
        author_user=request.user,
        author_name=request.user.get_full_name() or request.user.username,
        text=text[:4000],
        is_internal=internal,
        revision=envelope.revision,
        ip=client_ip(request),
    )
    log_event(
        envelope, "commented", request=request, actor=request.user,
        note=("[internal] " if internal else "") + text[:160],
    )
    messages.success(request, "Comment added." if internal else "Comment added and shared with recipients.")
    return redirect("accounts:esign_envelope_detail", pk=envelope.pk)


@login_required
@require_POST
def esign_duplicate(request, pk):
    """
    Copy an envelope into a fresh draft.

    Recipients, routing and options always come across — that is the reusable
    part, the "flow". Documents and their field layout are optional: tick the
    box to reuse the same form, leave it clear to attach a new document and
    place fields yourself.
    """
    source = _get_envelope_for_user(request, pk)
    agency = getattr(request.user, "agency", None)
    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return redirect("accounts:esign_dashboard")

    include_docs = bool(request.POST.get("include_documents"))
    subject = (request.POST.get("subject") or "").strip() or f"Copy of {source.subject}"

    with transaction.atomic():
        new = Envelope.objects.create(
            agency=agency,
            subject=subject[:200],
            message=source.message,
            created_by=request.user,
            enforce_order=source.enforce_order,
            reminders_enabled=source.reminders_enabled,
            reminder_days=source.reminder_days,
            reference=source.reference,
            asset=source.asset,
            duplicated_from=source,
        )

        recipient_map = {}
        for r in source.recipients.all().order_by("order", "id"):
            copy = EnvelopeRecipient.objects.create(
                envelope=new,
                user=r.user,
                name=r.name,
                email=r.email,
                title=r.title,
                role=r.role,
                order=r.order,
                access_code=r.access_code,
            )
            recipient_map[r.id] = copy

        copied_docs = 0
        copied_fields = 0
        if include_docs:
            for d in source.documents.all():
                new_doc = EnvelopeDocument(
                    envelope=new, name=d.name, order=d.order, page_count=d.page_count
                )
                try:
                    d.file.open("rb")
                    new_doc.file.save(
                        d.file.name.rsplit("/", 1)[-1], ContentFile(d.file.read()), save=False
                    )
                    d.file.close()
                except Exception:
                    logger.exception("eSign: could not copy document %s", d.pk)
                    continue
                new_doc.save()
                copied_docs += 1

                for f in d.fields.all():
                    target = recipient_map.get(f.recipient_id)
                    if not target:
                        continue
                    SignatureField.objects.create(
                        envelope=new, document=new_doc, recipient=target,
                        kind=f.kind, label=f.label, page=f.page,
                        x=f.x, y=f.y, w=f.w, h=f.h, required=f.required,
                    )
                    copied_fields += 1

    log_event(
        new, "duplicated", request=request, actor=request.user,
        note=(f"Duplicated from {source.short_id} — "
              f"{len(recipient_map)} recipient(s), {copied_docs} document(s), "
              f"{copied_fields} field(s) copied."),
        meta={"source": source.envelope_id},
    )
    log_event(
        source, "duplicated", request=request, actor=request.user,
        note=f"Used as the template for {new.short_id}",
    )

    if include_docs and copied_docs:
        messages.success(
            request,
            f"Duplicated with {copied_docs} document(s) and {copied_fields} field(s). "
            "Review and send.",
        )
        return redirect("accounts:esign_prepare", pk=new.pk)

    messages.success(
        request,
        f"Flow duplicated with {len(recipient_map)} recipient(s). Add your document to continue.",
    )
    return redirect("accounts:esign_prepare", pk=new.pk)


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

    if envelope.status == Envelope.STATUS_RETURNED:
        return render(request, "accounts/esign/returned.html",
                      {"envelope": envelope, "recipient": recipient,
                       "reason": envelope.return_reason})

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
        _document_payload(
            d, reverse("accounts:esign_token_document", args=[token, d.id])
        )
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
            "comments": envelope.comments.filter(is_internal=False).select_related("recipient"),
            "fonts": ESIGN_FONTS,
            "default_name": recipient.name,
            "default_initials": _initials_from(recipient.name),
        },
    )


def _initials_from(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    return "".join(p[0].upper() for p in parts[:3]) or "NA"


SAVED_REF = "saved:"


def _saved_signature_content(ref: str, recipient, want_initials=False):
    """
    Resolve a `saved:<profile_id>` reference to real PNG bytes.

    A saved signature reaches the browser as a media URL, not a data: URL, so it
    can't be decoded like a freshly drawn one. The client sends this reference
    instead and we read the stored file — which also means a signer can never
    apply someone else's saved signature by editing the payload.
    """
    try:
        profile_id = int(str(ref)[len(SAVED_REF):].split(":")[0])
    except (TypeError, ValueError):
        return None

    if not recipient.user_id:
        return None

    prof = SignatureProfile.objects.filter(id=profile_id, user_id=recipient.user_id).first()
    if not prof:
        logger.warning(
            "eSign: recipient %s referenced signature %s that isn't theirs",
            recipient.pk, profile_id,
        )
        return None

    src = prof.initials_image if (want_initials and prof.initials_image) else prof.image
    if not src:
        return None

    try:
        src.open("rb")
        data = src.read()
        src.close()
        return ContentFile(data) if data else None
    except Exception:
        logger.exception("eSign: could not read saved signature %s", profile_id)
        return None


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
    signature_errors = []
    now = timezone.now()

    for fid, raw in payload.items():
        f = by_id.get(str(fid))
        if not f:
            continue

        if f.kind in (SignatureField.KIND_SIGNATURE, SignatureField.KIND_INITIALS):
            wants_initials = f.kind == SignatureField.KIND_INITIALS
            ref = raw if isinstance(raw, str) else ""
            if not ref:
                ref = init_data if wants_initials else sig_data

            if ref.startswith(SAVED_REF):
                content = _saved_signature_content(ref, recipient, wants_initials)
            else:
                content = decode_signature_data_url(ref)

            if content:
                f.image.save(f"field-{f.id}.png", content, save=False)
                f.filled_at = now
                f.save()
            else:
                # Don't fail silently — this used to loop the signer back with
                # a generic "complete all required fields" and no way forward.
                signature_errors.append(f.label or f.get_kind_display())
                logger.warning(
                    "eSign: unusable signature payload for field %s (recipient %s, ref %r)",
                    f.id, recipient.pk, (ref or "")[:40],
                )
            continue

        if f.kind == SignatureField.KIND_CHECKBOX:
            f.value = "1" if str(raw) in ("1", "true", "True", "on") else ""
        else:
            f.value = str(raw or "")[:2000]
        f.filled_at = now if f.value else None
        f.save(update_fields=["value", "filled_at"])

    if signature_errors:
        messages.error(
            request,
            "Your signature image could not be read for: "
            + ", ".join(signature_errors[:4])
            + ". Please adopt the signature again and re-apply it.",
        )
        return redirect("accounts:esign_sign", token=recipient.token)

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
    if save_signature and recipient.user_id and not sig_data.startswith(SAVED_REF):
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

    documents = []
    if envelope.status == Envelope.STATUS_COMPLETED and envelope.completed_pdf:
        # One merged, stamped PDF replaces the individual source documents.
        first = envelope.documents.first()
        if first:
            raw = None
            try:
                envelope.completed_pdf.open("rb")
                raw = envelope.completed_pdf.read()
                envelope.completed_pdf.close()
            except Exception:
                logger.exception("eSign: could not read completed PDF for %s", envelope.pk)
            documents = [
                _document_payload(
                    first,
                    reverse("accounts:esign_token_document", args=[token, first.id]),
                    raw=raw,
                )
            ]
    if not documents:
        documents = [
            _document_payload(
                d, reverse("accounts:esign_token_document", args=[token, d.id])
            )
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
            "comments": envelope.comments.filter(is_internal=False).select_related("recipient"),
            "show_audit": True,
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
    profiles = SignatureProfile.objects.filter(user=request.user)

    return render(
        request,
        "accounts/esign/signature_studio.html",
        {
            "signatures": profiles,
            # same shape the signing page uses, so the pad's "Saved" tab works here too
            "saved_signatures": [
                {
                    "id": p.id,
                    "label": p.label or p.get_kind_display(),
                    "url": p.image.url if p.image else "",
                    "initials": p.initials_image.url if p.initials_image else "",
                    "is_default": p.is_default,
                }
                for p in profiles
                if p.image
            ],
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