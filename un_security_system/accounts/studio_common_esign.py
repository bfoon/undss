# accounts/studio_common_esign.py
"""
UN PASS — eSign Studio shared helpers for the three view modules.

Keeps the rules in one place:
  * who may use the studio          (same gate as eSign: agency + tenancy flag)
  * who can see a flow or a form    (owner, or shared with office / agency)
  * who can see a run / submission  (participation, like esign_access.py)
  * turning any upload into a PDF   (same converter and limits as eSign)
  * turning a PDF into an envelope  (same models and helpers as eSign)
"""

import json
import logging

from django.conf import settings
from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect

logger = logging.getLogger(__name__)

#: Front-end libraries ship with the app (accounts/static/accounts/esign/vendor)
#: and are served by your own static files — no CDN. Browsers then never need
#: a third-party origin, which is what CSP policies, proxies and offline
#: networks most often block. Each can still be pointed elsewhere in settings.
ASSETS = {
    "pdfjs_url": ("ESIGN_PDFJS_URL", "accounts/esign/vendor/pdfjs/pdf.min.js"),
    "pdfjs_worker_url": ("ESIGN_PDFJS_WORKER_URL", "accounts/esign/vendor/pdfjs/pdf.worker.min.js"),
    "jszip_url": ("ESIGN_JSZIP_URL", "accounts/esign/vendor/jszip/jszip.min.js"),
    "bootstrap_css_url": ("ESIGN_BOOTSTRAP_CSS_URL", "accounts/esign/vendor/bootstrap/bootstrap.min.css"),
    "bootstrap_js_url": ("ESIGN_BOOTSTRAP_JS_URL", "accounts/esign/vendor/bootstrap/bootstrap.bundle.min.js"),
    "icons_css_url": ("ESIGN_ICONS_CSS_URL", "accounts/esign/vendor/bootstrap-icons/bootstrap-icons.min.css"),
}

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")


def asset_urls():
    """Front-end library URLs: the bundled static copies unless a setting overrides one."""
    from django.templatetags.static import static

    out = {}
    for key, (setting, path) in ASSETS.items():
        override = getattr(settings, setting, "")
        out[key] = override or static(path)
    return out


def inline_pdf(raw):
    """
    The PDF as base64 for embedding in the page, or "" when it is missing or
    above ESIGN_INLINE_MAX_BYTES (the same limit eSign's own viewers use).

    Embedding removes the separate request for the document, which is the
    step most likely to fail in the field: CSP connect-src, proxies, browser
    extensions and interrupted downloads. Larger files are fetched by URL.
    """
    import base64

    limit = int(getattr(settings, "ESIGN_INLINE_MAX_BYTES", 8 * 1024 * 1024))
    if not raw or limit <= 0 or raw[:5] != b"%PDF-" or len(raw) > limit:
        return ""
    return base64.b64encode(raw).decode("ascii")


def studio_context(request, section, **extra):
    from .utils_esign import esign_brand

    ctx = {"studio_section": section, "brand": esign_brand(), **asset_urls()}
    ctx.update(extra)
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Gate
# ─────────────────────────────────────────────────────────────────────────────

def studio_gate(request):
    """
    -> (agency, None) when the person may use the studio,
       (None, redirect_response) otherwise. Same rule as the eSign dashboard.
    """
    from .views_esign import _agency_or_redirect, _service_enabled

    agency = _agency_or_redirect(request)
    if not agency:
        return None, redirect("accounts:profile")
    if not _service_enabled(agency, request.user):
        messages.warning(request, "eSign is not enabled for your agency or country office.")
        return None, redirect("accounts:profile")
    return agency, None


#: Largest JSON body each save accepts. Django's DATA_UPLOAD_MAX_MEMORY_SIZE
#: (2.5 MB by default) is a site-wide guard for form posts; these saves carry
#: page images and pictures legitimately, so each sets its own ceiling.
BODY_LIMITS = {
    "flow": 2 * 1024 * 1024,         # a flow graph is a few KB
    "organize": 1 * 1024 * 1024,     # a page plan
    "form": 12 * 1024 * 1024,        # schema with logos and images (≤ ~400 KB each)
    "edit": 40 * 1024 * 1024,        # annotations, pictures, flattened page images
}


def _mb(n):
    return f"{n / (1024 * 1024):.1f}".rstrip("0").rstrip(".")


def json_body(request, kind="flow"):
    """
    -> (payload, None) on success, or (None, JsonResponse) to return as-is.

    Reads the request stream directly with a per-save limit rather than through
    `request.body`, which raises RequestDataTooBig above DATA_UPLOAD_MAX_MEMORY_SIZE
    and makes Django answer with an HTML 400 page the browser can't read.
    Failures are always JSON with a sentence the person can act on.
    """
    from django.http import JsonResponse

    limit = int(getattr(settings, f"ESIGN_STUDIO_MAX_{kind.upper()}_BYTES", BODY_LIMITS.get(kind, BODY_LIMITS["flow"])))
    too_big = JsonResponse({
        "ok": False, "too_large": True,
        "error": (f"This is too large to save in one go (the limit is {_mb(limit)} MB). "
                  + ("Save fewer pages at a time, or untick “Make cover-ups permanent” for pages without cover-ups."
                     if kind == "edit" else "Use smaller images, or fewer of them." if kind == "form"
                     else "Please reload the page and try again.")),
    }, status=413)
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        return None, too_big
    try:
        if hasattr(request, "_body"):          # already read by middleware
            raw = request._body
        else:
            raw = request.read(limit + 1)
    except Exception:  # noqa: BLE001 - client disconnected mid-upload
        return None, JsonResponse({"ok": False, "error": "The request was interrupted. Please try again."}, status=400)
    if len(raw) > limit:
        return None, too_big
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return None, JsonResponse({"ok": False, "error": "The request couldn't be read. Please reload the page and try again."},
                                  status=400)
    return payload, None


def client_ip(request):
    from .utils_esign import client_ip as _ip

    return _ip(request)


# ─────────────────────────────────────────────────────────────────────────────
# Sharing and visibility
# ─────────────────────────────────────────────────────────────────────────────

def user_agency_id(user):
    agency_id = getattr(user, "agency_id", None)
    if not agency_id:
        office = getattr(user, "country_office", None)
        agency_id = getattr(office, "agency_id", None)
    return agency_id


def shared_template_q(user):
    """Flows and forms a person may use: their own, plus those shared with them."""
    q = Q(created_by=user)
    agency_id = user_agency_id(user)
    office_id = getattr(user, "country_office_id", None)
    if agency_id:
        q |= Q(share_scope="agency", agency_id=agency_id)
    if office_id:
        q |= Q(share_scope="office", office_id=office_id)
    return q


def can_use_template(user, obj) -> bool:
    if obj.created_by_id == user.id:
        return True
    if obj.share_scope == "agency" and obj.agency_id == user_agency_id(user):
        return True
    office_id = getattr(user, "country_office_id", None)
    return bool(obj.share_scope == "office" and office_id and obj.office_id == office_id)


def can_edit_template(user, obj) -> bool:
    return obj.created_by_id == user.id


def _email(user):
    return (getattr(user, "email", "") or "").strip()


def run_participant_q(user):
    q = Q(initiator=user) | Q(tasks__user=user) | Q(workflow__created_by=user, workflow__monitor_runs=True)
    email = _email(user)
    if email:
        q |= Q(tasks__email__iexact=email)
    return q


def visible_runs(user):
    from .models_esign_studio import WorkflowRun

    if not getattr(user, "is_authenticated", False):
        return WorkflowRun.objects.none()
    return WorkflowRun.objects.filter(run_participant_q(user)).distinct()


def can_view_run(user, run) -> bool:
    return visible_runs(user).filter(pk=run.pk).exists()


def can_manage_run(user, run) -> bool:
    """Cancel, reassign, retry: the initiator, or the flow's owner when they monitor runs."""
    if run.initiator_id == user.id:
        return True
    wf = run.workflow
    return bool(wf and wf.created_by_id == user.id and wf.monitor_runs)


def visible_submissions(user):
    from .models_esign_studio import FormSubmission

    if not getattr(user, "is_authenticated", False):
        return FormSubmission.objects.none()
    q = Q(submitted_by=user) | Q(form__created_by=user, form__owner_sees_submissions=True)
    q |= Q(runs__in=visible_runs(user))
    return FormSubmission.objects.filter(q).distinct()


def can_view_submission(user, sub) -> bool:
    return visible_submissions(user).filter(pk=sub.pk).exists()


def directory(user, limit=600):
    """[{id, name, email, title}] from the same tenancy directory eSign uses."""
    try:
        from .views_esign import _recipient_directory

        qs = _recipient_directory(user).order_by("first_name", "last_name", "username")[:limit]
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: directory unavailable")
        return []
    return [
        {"id": u.pk, "name": u.get_full_name() or u.username, "email": u.email or "",
         "title": getattr(u, "job_title", "") or ""}
        for u in qs
    ]


def read_people(raw_json):
    """Picker payload (JSON string or list) -> clean people via the engine."""
    from .workflow_engine_esign import people_from_payload

    if isinstance(raw_json, str):
        try:
            raw_json = json.loads(raw_json or "[]")
        except ValueError:
            raw_json = []
    return people_from_payload(raw_json if isinstance(raw_json, list) else [])


# ─────────────────────────────────────────────────────────────────────────────
# Uploads → PDF
# ─────────────────────────────────────────────────────────────────────────────

class UploadError(ValueError):
    pass


def max_upload_bytes():
    from .views_esign import MAX_DOC_BYTES

    return MAX_DOC_BYTES


def accepted_ext():
    from .views_esign import allowed_doc_ext

    return tuple(sorted(set(allowed_doc_ext()) | set(IMAGE_EXT)))


def upload_to_pdf(upload, allow=("pdf", "image", "office")):
    """
    One uploaded file -> (pdf_bytes, pdf_name). Raises UploadError with a
    sentence the person can act on.
    """
    from .converters_esign import ConversionError, convert_to_pdf, is_office_file
    from .pdf_tools_esign import PdfToolError, images_to_pdf, is_pdf

    name = upload.name or "document"
    lower = name.lower()
    limit = max_upload_bytes()
    if upload.size > limit:
        raise UploadError(f"{name} is larger than {limit // (1024 * 1024)} MB.")
    raw = upload.read()
    stem = name.rsplit(".", 1)[0][:150] or "document"

    if lower.endswith(".pdf"):
        if "pdf" not in allow:
            raise UploadError(f"{name}: this tool doesn't take PDFs.")
        if not is_pdf(raw):
            raise UploadError(f"{name} has a .pdf name but isn't a PDF.")
        return raw, f"{stem}.pdf"

    if lower.endswith(IMAGE_EXT):
        if "image" not in allow:
            raise UploadError(f"{name}: this tool doesn't take images.")
        try:
            return images_to_pdf([(name, raw)]), f"{stem}.pdf"
        except PdfToolError as exc:
            raise UploadError(str(exc))

    if is_office_file(name):
        if "office" not in allow:
            raise UploadError(f"{name}: this tool doesn't take Office documents.")
        try:
            pdf = convert_to_pdf(raw, name)
        except ConversionError as exc:
            raise UploadError(f"{name} could not be converted: {exc}")
        if not is_pdf(pdf):
            raise UploadError(f"{name} could not be converted to PDF.")
        return pdf, f"{stem}.pdf"

    raise UploadError(
        f"{name}: supported types are " + ", ".join(e.lstrip(".").upper() for e in accepted_ext()) + "."
    )


# ─────────────────────────────────────────────────────────────────────────────
# PDF → eSign envelope
# ─────────────────────────────────────────────────────────────────────────────

def envelope_from_pdf(request, agency, raw, *, name, subject, self_sign=False, reference="",
                      signature_boxes=None):
    """
    Create a draft envelope around a PDF and return it; the caller redirects to
    esign_prepare, the same screen every other envelope goes through.

    self_sign=True adds the sender as the only signer. If `signature_boxes` come
    from a form render, the first box gets their signature and date; otherwise
    the quick-sign block goes on the last page exactly like "Sign a document".
    """
    from .models_esign import Envelope, EnvelopeDocument, EnvelopeRecipient, SignatureField
    from .utils_esign import log_event, prepare_document
    from .views_esign_self import _has_flag, _place_quick_fields

    user = request.user
    with transaction.atomic():
        envelope = Envelope.objects.create(
            agency=agency,
            subject=(subject or name or "Document")[:200],
            message="",
            created_by=user,
            enforce_order=not self_sign,
            reminders_enabled=not self_sign,
            reference=(reference or "")[:100],
            **({"is_self_sign": True} if (self_sign and _has_flag()) else {}),
        )
        doc = EnvelopeDocument.objects.create(
            envelope=envelope, name=(name or "document.pdf")[:200], order=0,
            file=ContentFile(raw, name=(name or "document.pdf")),
        )
        prepare_document(doc)
        log_event(envelope, "document_added", request=request, actor=user, note=f"{doc.name} (from eSign Studio)")

        if self_sign:
            email = (user.email or "").strip()
            if not email:
                raise UploadError("Your account has no email address, and the signature record needs one. "
                                  "Ask ICT to add it to your profile first.")
            rec = EnvelopeRecipient.objects.create(
                envelope=envelope, user=user, name=user.get_full_name() or user.username, email=email,
                title=getattr(user, "job_title", "") or "", role=EnvelopeRecipient.ROLE_SIGNER, order=1,
            )
            box = (signature_boxes or [None])[0]
            if box:
                x, y, w, h = box["sig"]
                SignatureField.objects.create(envelope=envelope, document=doc, recipient=rec,
                                              kind=SignatureField.KIND_SIGNATURE, page=box["page"],
                                              x=x, y=y, w=w, h=h, label="Signature", required=True)
                if box.get("date"):
                    dx, dy, dw, dh = box["date"]
                    SignatureField.objects.create(envelope=envelope, document=doc, recipient=rec,
                                                  kind=SignatureField.KIND_DATE, page=box["page"],
                                                  x=dx, y=dy - dh * 0.2, w=dw, h=dh * 1.4,
                                                  label="Date signed", required=False)
            else:
                _place_quick_fields(envelope, rec)
            note = "Self-signed document created from eSign Studio."
        else:
            note = "Draft envelope created from eSign Studio."
        log_event(envelope, "created", request=request, actor=user, note=note)
    return envelope
