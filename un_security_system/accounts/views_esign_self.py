"""
accounts/views_esign_self.py
============================

Sign a document yourself, without addressing it to anyone.

Why this is not a new signing engine
------------------------------------
A self-signed document still needs everything an envelope already gives you:
the PDF conversion, the field placement, the stamping, the certificate, the
audit trail, the envelope ID printed on every page. Rebuilding any of that
alongside the existing pipeline would mean two code paths that have to stay in
step, and the second one would drift.

So a self-sign *is* an envelope — one with a single recipient, you, flagged
`is_self_sign`. What changes is the route through the interface, not the
machinery underneath. The completed PDF lands in the same place, downloads from
the same URL, and carries the same certificate as anything you send to someone
else.

The two routes
--------------
`quick`     one signature block, bottom-right of the last page, straight to
            the signing screen. For "I just need to sign this."

`place`     the normal prepare screen, so you can put a signature, initials,
            a date and text boxes wherever you want them, then sign.

Wiring
------
1. Add `is_self_sign` to Envelope (see README-self-sign.md), migrate.
2. Add the two URLs.
3. Add the small guard in `esign_send` so a self-sign envelope goes to the
   signing screen rather than the "sent, now wait" screen.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .models_esign import (
    Envelope,
    EnvelopeDocument,
    EnvelopeRecipient,
    SignatureField,
    SignatureProfile,
)
from .utils_esign import log_event, pdf_page_count, prepare_document

# These all live in views_esign, which owns the eSign upload rules. Importing
# them keeps one definition of "how big may a document be" rather than two that
# can drift apart.
from .views_esign import (
    MAX_DOC_BYTES,
    _agency_or_redirect,
    _service_enabled,
    allowed_doc_ext,
    conversion_backend_available,
    supported_upload_ext,
)

logger = logging.getLogger(__name__)

#: Where the auto-placed signature goes on the last page: bottom-right, clear
#: of a typical footer. Fractions of page width/height, origin top-left.
QUICK_SIGNATURE_BOX = {"x": 0.58, "y": 0.78, "w": 0.30, "h": 0.075}
QUICK_DATE_BOX = {"x": 0.58, "y": 0.865, "w": 0.30, "h": 0.035}


def is_self_sign(envelope) -> bool:
    """
    True for a self-signed envelope.

    Reads the flag when the column exists, and falls back to the shape of the
    envelope when it does not — one recipient, who is also the sender. That
    fallback means this module still behaves correctly if you deploy the code
    before running the migration.
    """
    flagged = getattr(envelope, "is_self_sign", None)
    if flagged is not None:
        return bool(flagged)
    rows = list(envelope.recipients.all())
    return (
        len(rows) == 1
        and rows[0].user_id
        and rows[0].user_id == envelope.created_by_id
        and rows[0].role in (EnvelopeRecipient.ROLE_SIGNER, EnvelopeRecipient.ROLE_APPROVER)
    )


def _self_recipient(envelope):
    """The sender's own recipient row."""
    return envelope.recipients.filter(user_id=envelope.created_by_id).first()


# ---------------------------------------------------------------------------
# Upload and start
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["GET", "POST"])
def esign_self_new(request):
    agency = _agency_or_redirect(request)
    if not agency:
        return redirect("accounts:profile")
    if not _service_enabled(agency, request.user):
        messages.warning(request, "eSign is not enabled for your agency or country office.")
        return redirect("accounts:profile")

    if request.method == "GET":
        converter_ok, converter_note = conversion_backend_available()
        return render(
            request,
            "accounts/esign/self_sign_new.html",
            {
                "converter_ok": converter_ok,
                "converter_note": converter_note,
                "office_ext": ", ".join(
                    e.lstrip(".").upper() for e in supported_upload_ext()
                ),
                "accept_attr": ",".join(allowed_doc_ext()),
                "has_signature": SignatureProfile.objects.filter(user=request.user).exists(),
            },
        )

    files = request.FILES.getlist("documents")
    if not files:
        messages.error(request, "Choose a document to sign.")
        return redirect("accounts:esign_self_new")

    mode = request.POST.get("mode") or "quick"
    subject = (request.POST.get("subject") or "").strip()
    if not subject:
        # The filename is almost always the right title, so don't make them type it.
        subject = files[0].name.rsplit(".", 1)[0][:200] or "Signed document"

    user = request.user
    display_name = user.get_full_name() or user.username
    email = (user.email or "").strip()
    if not email:
        messages.error(
            request,
            "Your account has no email address, and the signature record needs one. "
            "Ask ICT to add it to your profile first.",
        )
        return redirect("accounts:esign_self_new")

    try:
        with transaction.atomic():
            envelope = Envelope.objects.create(
                agency=agency,
                subject=subject,
                message="",
                created_by=user,
                enforce_order=False,
                reminders_enabled=False,      # nobody to remind
                reference=(request.POST.get("reference") or "").strip(),
                **({"is_self_sign": True} if _has_flag() else {}),
            )

            for order, f in enumerate(files):
                if f.size > MAX_DOC_BYTES:
                    raise ValueError(
                        f"{f.name} is larger than "
                        f"{MAX_DOC_BYTES // (1024 * 1024)} MB."
                    )
                if not f.name.lower().endswith(allowed_doc_ext()):
                    raise ValueError(
                        f"{f.name}: supported types here are "
                        + ", ".join(e.lstrip(".").upper() for e in allowed_doc_ext())
                        + "."
                    )
                doc = EnvelopeDocument.objects.create(
                    envelope=envelope, file=f, name=f.name[:200], order=order
                )
                prepare_document(doc)
                log_event(
                    envelope, "document_added", request=request, actor=user,
                    note=f"{doc.name}"
                    + (" (converted to PDF)" if getattr(doc, "was_converted", False) else ""),
                )

            recipient = EnvelopeRecipient.objects.create(
                envelope=envelope,
                user=user,
                name=display_name,
                email=email,
                title=getattr(user, "job_title", "") or "",
                role=EnvelopeRecipient.ROLE_SIGNER,
                order=1,
            )

            log_event(
                envelope, "created", request=request, actor=user,
                note=f"Self-signed document created with {len(files)} document(s).",
            )

            if mode == "quick":
                _place_quick_fields(envelope, recipient)

    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_self_new")
    except Exception as exc:  # noqa: BLE001
        logger.exception("eSign: self-sign creation failed")
        detail = f"{type(exc).__name__}: {exc}"[:300]
        if request.user.is_superuser or request.user.is_staff:
            messages.error(request, f"Could not start the document — {detail}")
        else:
            messages.error(
                request,
                "Could not start the document. The error has been logged — "
                "please ask ICT to check the server log.",
            )
        return redirect("accounts:esign_self_new")

    if mode == "quick":
        return _go_sign(request, envelope, recipient,
                        "Your signature block is on the last page. "
                        "Drag it if you want it elsewhere, then sign.")

    messages.success(request, "Now place your signature where you want it, then sign.")
    return redirect("accounts:esign_prepare", pk=envelope.pk)


def _has_flag() -> bool:
    """True once the is_self_sign column has been migrated in."""
    return any(f.name == "is_self_sign" for f in Envelope._meta.get_fields())


def _place_quick_fields(envelope, recipient):
    """
    Put a signature and a date on the last page of the first document.

    Last page rather than first: a signature block belongs at the end of what
    it is agreeing to. If the page count cannot be read, page 1 is the safe
    fallback — a misplaced field can be dragged, a crash cannot.
    """
    doc = envelope.documents.order_by("order", "pk").first()
    if not doc:
        return

    pages = getattr(doc, "page_count", 0) or 0
    if not pages:
        try:
            pages = pdf_page_count(doc) or 1
        except Exception:  # noqa: BLE001
            logger.warning("eSign: could not read page count for document %s", doc.pk)
            pages = 1

    SignatureField.objects.create(
        envelope=envelope, document=doc, recipient=recipient,
        kind=SignatureField.KIND_SIGNATURE, page=pages,
        label="Signature", required=True, **QUICK_SIGNATURE_BOX,
    )
    SignatureField.objects.create(
        envelope=envelope, document=doc, recipient=recipient,
        kind=SignatureField.KIND_DATE, page=pages,
        label="Date signed", required=False, **QUICK_DATE_BOX,
    )


# ---------------------------------------------------------------------------
# Going from draft straight to signing
# ---------------------------------------------------------------------------

def _go_sign(request, envelope, recipient, note=""):
    """
    Flip a self-sign draft to sent and hand the user their own signing screen.

    No invitation email: mailing yourself a link to a page you are already
    looking at is noise. The event is still logged, so the certificate reads
    the same as any other envelope.
    """
    from django.utils import timezone

    if envelope.status == Envelope.STATUS_DRAFT:
        envelope.status = Envelope.STATUS_SENT
        envelope.sent_at = timezone.now()
        envelope.save(update_fields=["status", "sent_at"])

        recipient.status = EnvelopeRecipient.STATUS_SENT
        recipient.sent_at = timezone.now()
        recipient.save(update_fields=["status", "sent_at"])

        log_event(
            envelope, "sent", request=request, actor=request.user,
            note="Self-signed — opened directly by the sender, no invitation sent.",
        )

    if note:
        messages.info(request, note)
    return redirect("accounts:esign_sign", token=recipient.token)


@login_required
@require_http_methods(["POST"])
def esign_self_finish(request, pk):
    """
    Called by the "Sign it now" button on the prepare screen.

    Validates the same way `esign_send` does — every signer needs at least one
    field — then goes to the signing screen instead of the sent screen.
    """
    from .esign_access import get_envelope_or_404

    envelope = get_envelope_or_404(request, pk, mode="manage")

    if not is_self_sign(envelope):
        return redirect("accounts:esign_send", pk=envelope.pk)

    recipient = _self_recipient(envelope)
    if recipient is None:
        messages.error(request, "This document has no signer row. Start it again.")
        return redirect("accounts:esign_dashboard")

    if envelope.status != Envelope.STATUS_DRAFT:
        return redirect("accounts:esign_sign", token=recipient.token)

    if not envelope.fields.filter(recipient=recipient).exists():
        messages.error(
            request,
            "Place at least one field — a signature, initials or a date — before signing.",
        )
        return redirect("accounts:esign_prepare", pk=envelope.pk)

    return _go_sign(request, envelope, recipient)
