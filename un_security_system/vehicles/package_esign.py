"""
vehicles/package_esign.py
=========================

Bridge between the Packages/Mailroom workflow and the canonical accounts eSign
engine.  This module deliberately contains *no* signature rendering, stamping,
certificate, audit-hash or notification implementation.  Those all remain in
accounts.models_esign / accounts.views_esign / accounts.utils_esign.

A PackageDocument keeps its original package file for package provenance and
links to one eSign Envelope + EnvelopeDocument for the signing lifecycle.
"""

from __future__ import annotations

from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from accounts.esign_access import recipient_for
from accounts.models_esign import Envelope, EnvelopeDocument, EnvelopeRecipient
from accounts.utils_esign import log_event, prepare_document
from accounts.views_esign import MAX_DOC_BYTES, allowed_doc_ext, esign_send
from tenancy.services import has_all

from .models import Package, PackageDocument, PackageEvent, PackageStepLog


REQUIRED_FEATURES = ("mailroom", "mailroom_signing", "esign")


class PackageESignError(ValueError):
    """A package document could not be handed to eSign."""


def _package_agency(doc: PackageDocument):
    package = doc.step_log.package
    template = getattr(package, "flow_template", None)
    agency = getattr(template, "agency", None)
    if agency is not None:
        return agency

    uploader = getattr(doc, "uploaded_by", None)
    agency = getattr(uploader, "agency", None)
    if agency is None:
        office = getattr(uploader, "country_office", None)
        agency = getattr(office, "agency", None)
    return agency


def _actor_agency(user):
    agency = getattr(user, "agency", None)
    if agency is None:
        office = getattr(user, "country_office", None)
        agency = getattr(office, "agency", None)
    return agency


def _check_entitlement(user, agency):
    if not has_all(user, REQUIRED_FEATURES):
        raise PermissionDenied(
            "Package document signing requires Mailroom, Mailroom document signing, "
            "and eSign to be enabled for your office."
        )

    if getattr(user, "is_superuser", False):
        return

    actor_agency = _actor_agency(user)
    if agency is None or actor_agency is None or actor_agency.pk != agency.pk:
        raise PermissionDenied("This package belongs to a different agency scope.")




def _has_legacy_fields(doc: PackageDocument) -> bool:
    """True when this row already belongs to the pre-eSign package signer."""
    try:
        return doc.signature_fields.exists()
    except Exception:
        return False


def _legacy_view(name):
    # Lazy import avoids a module cycle: vehicles.views may import package helpers.
    from . import views as legacy_views
    return getattr(legacy_views, name)


def _read_package_file(doc: PackageDocument) -> tuple[bytes, str]:
    if not doc.file:
        raise PackageESignError("The package document has no stored file.")

    filename = Path(doc.filename or doc.file.name).name
    if not filename:
        filename = f"package-document-{doc.pk}.pdf"

    if not filename.lower().endswith(allowed_doc_ext()):
        allowed = ", ".join(ext.lstrip(".").upper() for ext in allowed_doc_ext())
        raise PackageESignError(f"{filename}: eSign supports {allowed} on this server.")

    doc.file.open("rb")
    try:
        raw = doc.file.read()
    finally:
        doc.file.close()

    if not raw:
        raise PackageESignError(f"{filename} is empty.")
    if len(raw) > MAX_DOC_BYTES:
        raise PackageESignError(f"{filename} is larger than the eSign 25 MB limit.")
    return raw, filename


def _ensure_package_recipient(envelope: Envelope, step_log: PackageStepLog):
    """Create the signer selected on Package Flow exactly once."""
    email = (getattr(step_log, 'signature_recipient_email', '') or '').strip()
    if not email:
        return None

    existing = envelope.recipients.filter(email__iexact=email).first()
    if existing:
        return existing

    user = getattr(step_log, 'signature_recipient_user', None)
    if user is not None and (getattr(user, 'email', '') or '').strip().lower() != email.lower():
        # The email captured on the package step is authoritative.  Avoid
        # attaching the wrong account if somebody's profile changed later.
        user = None

    name = (getattr(step_log, 'signature_recipient_name', '') or '').strip() or email
    return EnvelopeRecipient.objects.create(
        envelope=envelope,
        user=user,
        name=name[:150],
        email=email,
        role=EnvelopeRecipient.ROLE_SIGNER,
        order=1,
    )


def ensure_document_envelope(doc: PackageDocument, actor, request=None) -> Envelope:
    """Return the eSign envelope linked to ``doc``, creating it once if needed."""

    agency = _package_agency(doc)
    if agency is None:
        raise PackageESignError(
            "The package document has no agency scope. Assign a package workflow/agency first."
        )
    _check_entitlement(actor, agency)

    # Idempotent path: the document was already handed to eSign.
    if getattr(doc, "esign_envelope_id", None):
        envelope = doc.esign_envelope
        _ensure_package_recipient(envelope, doc.step_log)
        return envelope

    raw, filename = _read_package_file(doc)
    package = doc.step_log.package

    with transaction.atomic():
        envelope = Envelope.objects.create(
            agency=agency,
            subject=(
                f"Package Flow Signature — {package.tracking_code} — {doc.step_log.step_name}"
            )[:200],
            message=(
                f"This signature request is part of UN PASS Package Flow for "
                f"{package.tracking_code}. Step: {doc.step_log.step_name}. "
                "The signed PDF will return to the package record automatically."
            ),
            created_by=actor,
            enforce_order=True,
            reminders_enabled=True,
            reminder_days=3,
            reference=package.tracking_code[:120],
        )

        esign_doc = EnvelopeDocument(
            envelope=envelope,
            name=filename[:200],
            order=0,
        )
        esign_doc.file.save(filename, ContentFile(raw), save=False)
        esign_doc.save()
        prepare_document(esign_doc)
        recipient = _ensure_package_recipient(envelope, doc.step_log)

        doc.esign_envelope = envelope
        doc.esign_document = esign_doc
        # Keep the legacy package status column for old rows/UI, but do not use it
        # as the signing source of truth.  New templates read envelope.status.
        if getattr(doc, "status", None) == "uploaded":
            doc.status = "annotation_ready"
            update_fields = ["esign_envelope", "esign_document", "status"]
        else:
            update_fields = ["esign_envelope", "esign_document"]
        doc.save(update_fields=update_fields)

        log_event(
            envelope,
            "created",
            request=request,
            actor=actor,
            note=(
                f"Created from package {package.tracking_code}; "
                f"PackageDocument #{doc.pk}; "
                f"signer {getattr(recipient, 'email', '') or 'not preselected'}; "
                f"source SHA-256 {doc.file_hash or 'not recorded'}."
            ),
            meta={
                "package_id": package.pk,
                "package_tracking_code": package.tracking_code,
                "package_document_id": doc.pk,
                "package_source_hash": doc.file_hash or "",
            },
        )
        log_event(
            envelope,
            "document_added",
            request=request,
            actor=actor,
            note=f"{filename} (package document #{doc.pk})",
        )

    return envelope


def create_document_from_step_scan(step_log: PackageStepLog, actor, request=None):
    """Copy a scan uploaded while completing a package step into PackageDocument + eSign."""

    if not step_log.scan_file:
        return None

    # A retry of the package POST should not create duplicate package documents.
    existing = step_log.documents.order_by("pk").first()
    if existing is not None:
        ensure_document_envelope(existing, actor, request=request)
        return existing

    step_log.scan_file.open("rb")
    try:
        raw = step_log.scan_file.read()
    finally:
        step_log.scan_file.close()

    filename = Path(step_log.scan_file.name).name or f"package-{step_log.package_id}-scan.pdf"
    doc = PackageDocument(
        step_log=step_log,
        filename=filename,
        uploaded_by=actor,
        status="uploaded",
    )
    doc.file.save(filename, ContentFile(raw), save=False)
    doc.save()
    doc.file_hash = doc.compute_hash()
    doc.save(update_fields=["file_hash"])

    ensure_document_envelope(doc, actor, request=request)
    return doc


def complete_package_step_from_envelope(envelope: Envelope) -> bool:
    """
    Return a completed package eSign envelope to Package Flow.

    Safe to call repeatedly.  The workflow advances only while the package is
    still sitting on the exact step that created this envelope.
    """
    try:
        doc = envelope.package_document_source
    except Exception:
        return False

    if envelope.status != Envelope.STATUS_COMPLETED or not envelope.completed_pdf:
        return False

    # Keep the legacy status column useful for old templates/admin screens.
    if doc.status != 'signed':
        doc.status = 'signed'
        doc.save(update_fields=['status'])

    log = doc.step_log
    if not log.step_id:
        return False

    with transaction.atomic():
        package = Package.objects.select_for_update().get(pk=log.package_id)
        if package.current_step_id != log.step_id:
            return False

        step = log.step
        PackageEvent.objects.create(
            package=package,
            status=step.status_code[:20],
            who=log.performed_by,
            note=(log.note or f"{step.name} — eSign completed")[:255],
        )

        next_step = step.next_step()
        if next_step:
            package.current_step = next_step
            package.status = next_step.status_code
        else:
            package.current_step = None
            package.is_complete = True
        package.save(update_fields=['current_step', 'status', 'is_complete', 'last_update'])

    # Lazy import avoids a module cycle. Notifications are sent only after the
    # signed output exists and the package workflow has actually advanced.
    from .views import _send_notifications
    if log.performed_by:
        _send_notifications(package, step, log, log.performed_by)
    return True


# ---------------------------------------------------------------------------
# Compatibility views
# ---------------------------------------------------------------------------
# Keep the old vehicles:* route names so bookmarks/templates do not break, but
# hand every action to accounts eSign.

@login_required
def document_annotate(request, pk):
    doc = get_object_or_404(PackageDocument, pk=pk)
    if not getattr(doc, "esign_envelope_id", None) and _has_legacy_fields(doc):
        # An in-flight pre-cutover document keeps its original field coordinates.
        return _legacy_view("document_annotate")(request, pk)
    try:
        envelope = ensure_document_envelope(doc, request.user, request=request)
    except PackageESignError as exc:
        messages.error(request, str(exc))
        return redirect("vehicles:package_detail", pk=doc.step_log.package_id)

    if envelope.is_editable:
        messages.info(request, "Package document opened in eSign. Add recipients and place fields here.")
        return redirect("accounts:esign_prepare", pk=envelope.pk)
    return redirect("accounts:esign_envelope_detail", pk=envelope.pk)


@login_required
@require_POST
def document_send_for_signing(request, pk):
    """Legacy package send route; delegate to the canonical eSign send view."""
    doc = get_object_or_404(PackageDocument, pk=pk)
    if not getattr(doc, "esign_envelope_id", None) and _has_legacy_fields(doc):
        return _legacy_view("document_send_for_signing")(request, pk)
    try:
        envelope = ensure_document_envelope(doc, request.user, request=request)
    except PackageESignError as exc:
        messages.error(request, str(exc))
        return redirect("vehicles:package_detail", pk=doc.step_log.package_id)

    if envelope.is_editable:
        return esign_send(request, envelope.pk)
    return redirect("accounts:esign_envelope_detail", pk=envelope.pk)


@login_required
def document_sign(request, pk):
    """Legacy package signing URL -> this user's eSign tokenized signing/review URL."""
    doc = get_object_or_404(PackageDocument, pk=pk)
    envelope = getattr(doc, "esign_envelope", None)
    if envelope is None and _has_legacy_fields(doc):
        return _legacy_view("document_sign")(request, pk)
    if envelope is None:
        return document_annotate(request, pk)

    recipient = recipient_for(request.user, envelope)
    if recipient is not None:
        if (
            recipient.status == EnvelopeRecipient.STATUS_SIGNED
            or envelope.status == Envelope.STATUS_COMPLETED
        ):
            return redirect("accounts:esign_review", token=recipient.token)
        return redirect("accounts:esign_sign", token=recipient.token)

    # Sender/override users land on the internal envelope page. eSign access
    # control remains authoritative there.
    return redirect("accounts:esign_envelope_detail", pk=envelope.pk)


@login_required
def document_audit(request, pk):
    """Legacy package audit URL -> canonical eSign envelope audit/detail page."""
    doc = get_object_or_404(PackageDocument, pk=pk)
    envelope = getattr(doc, "esign_envelope", None)
    if envelope is None and _has_legacy_fields(doc):
        return _legacy_view("document_audit")(request, pk)
    if envelope is None:
        messages.info(request, "This package document has not been prepared in eSign yet.")
        return redirect("vehicles:package_detail", pk=doc.step_log.package_id)
    return redirect("accounts:esign_envelope_detail", pk=envelope.pk)


@login_required
def signature_profile(request):
    """Old package signature studio -> shared eSign signature studio."""
    return redirect("accounts:esign_signatures")


# Register Package Flow <-> eSign lifecycle synchronization when the vehicle
# URL module imports this bridge.  Kept here so an existing vehicles/apps.py
# does not need to be replaced.
from . import signals as _package_esign_signals  # noqa: E402,F401
