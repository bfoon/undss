# accounts/views_esign_studio.py
"""
UN PASS — eSign Studio: the PDF workbench.

Every tool produces a file in "My files". Operations on an existing file write
a new version, so nothing is ever lost and every change can be undone. From any
file you can go straight on to signing it, sending it for signature, or
starting a workflow with it.
"""

import io
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST

from . import pdf_tools_esign as T
from .models_esign_studio import StudioFile, StudioFileVersion
from .studio_common_esign import (
    UploadError,
    accepted_ext,
    inline_pdf,
    envelope_from_pdf,
    json_body,
    max_upload_bytes,
    studio_context,
    studio_gate,
    upload_to_pdf,
)

logger = logging.getLogger(__name__)

MAX_VERSIONS = getattr(settings, "ESIGN_STUDIO_MAX_VERSIONS", 15)
MAX_MERGE_FILES = 30

#: The tool catalogue: drives the hub, each tool page and the dashboard strip.
TOOLS = [
    # key, group, label, icon, blurb, accepts, multiple
    {"key": "convert", "group": "Convert", "label": "Word, Excel, PowerPoint to PDF", "icon": "bi-file-earmark-word",
     "blurb": "Turn Office documents into PDFs.", "accepts": "office", "multiple": True},
    {"key": "images", "group": "Convert", "label": "Images to PDF", "icon": "bi-file-earmark-image",
     "blurb": "Photos and scans into one PDF.", "accepts": "image", "multiple": True},
    {"key": "to_images", "group": "Convert", "label": "PDF to images", "icon": "bi-images",
     "blurb": "Save every page as a PNG.", "accepts": "pdf", "multiple": False},
    {"key": "merge", "group": "Organise", "label": "Merge PDFs", "icon": "bi-union",
     "blurb": "Combine files in the order you choose.", "accepts": "any", "multiple": True},
    {"key": "organize", "group": "Organise", "label": "Organise pages", "icon": "bi-grid-3x3-gap",
     "blurb": "Reorder, rotate, delete, duplicate, add blank pages.", "accepts": "any", "multiple": False},
    {"key": "remove", "group": "Organise", "label": "Remove pages", "icon": "bi-file-earmark-minus",
     "blurb": "Pick the pages you don't want.", "accepts": "any", "multiple": False},
    {"key": "rotate", "group": "Organise", "label": "Rotate pages", "icon": "bi-arrow-clockwise",
     "blurb": "Turn some or all pages the right way up.", "accepts": "any", "multiple": False},
    {"key": "extract", "group": "Organise", "label": "Extract pages", "icon": "bi-file-earmark-arrow-up",
     "blurb": "Copy selected pages into a new PDF.", "accepts": "any", "multiple": False},
    {"key": "split", "group": "Organise", "label": "Split PDF", "icon": "bi-scissors",
     "blurb": "By page ranges or every few pages.", "accepts": "any", "multiple": False},
    {"key": "edit", "group": "Edit", "label": "Edit PDF", "icon": "bi-pencil-square",
     "blurb": "Add text, white-out, highlight, draw, shapes, images, ticks.", "accepts": "any", "multiple": False},
    {"key": "watermark", "group": "Edit", "label": "Watermark", "icon": "bi-droplet-half",
     "blurb": "DRAFT, CONFIDENTIAL or your own text.", "accepts": "any", "multiple": False},
    {"key": "numbers", "group": "Edit", "label": "Page numbers", "icon": "bi-123",
     "blurb": "Page 1 of 12, where you want it.", "accepts": "any", "multiple": False},
    {"key": "compress", "group": "Optimise", "label": "Compress PDF", "icon": "bi-file-zip",
     "blurb": "Shrink scans and photo-heavy files for email.", "accepts": "any", "multiple": False},
    {"key": "protect", "group": "Security", "label": "Protect with a password", "icon": "bi-lock",
     "blurb": "Download a copy that needs a password to open.", "accepts": "any", "multiple": False},
    {"key": "unlock", "group": "Security", "label": "Unlock PDF", "icon": "bi-unlock",
     "blurb": "Remove a password you know.", "accepts": "pdf", "multiple": False},
]
TOOL_BY_KEY = {t["key"]: t for t in TOOLS}
EDITOR_TOOLS = {"organize": "organize", "remove": "organize", "rotate": "organize", "extract": "organize",
                "edit": "edit", "to_images": "file"}


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

def create_studio_file(user, agency, raw, name, origin="upload", operation="Uploaded"):
    with transaction.atomic():
        sf = StudioFile.objects.create(owner=user, agency=agency, name=(name or "document.pdf")[:200], origin=origin)
        add_version(sf, raw, operation)
    return sf


def add_version(sf, raw, operation):
    """Write raw bytes as the next version, make it current, prune the oldest."""
    last = sf.versions.order_by("-number").first()
    version = StudioFileVersion(
        studio_file=sf, number=(last.number + 1) if last else 1,
        operation=(operation or "")[:160], page_count=T.page_count(raw), size=len(raw),
    )
    version.file.save(f"v{version.number}.pdf", ContentFile(raw), save=False)
    version.save()
    sf.current = version
    sf.save(update_fields=["current", "updated_at"])

    stale = list(sf.versions.order_by("-number")[MAX_VERSIONS:])
    for old in stale:
        try:
            old.file.delete(save=False)
        except Exception:  # noqa: BLE001
            pass
        old.delete()
    return version


def _own_file(request, pk):
    return get_object_or_404(StudioFile.objects.select_related("current"), pk=pk, owner=request.user)


def _pdf_response(raw, filename, inline=True):
    resp = HttpResponse(raw, content_type="application/pdf")
    disposition = "inline" if inline else "attachment"
    safe = filename.replace('"', "")
    resp["Content-Disposition"] = f'{disposition}; filename="{safe}"'
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    return resp


def _stem(name):
    return (name or "document").rsplit(".", 1)[0][:120] or "document"


# ─────────────────────────────────────────────────────────────────────────────
# Hub
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_studio(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    groups = {}
    for t in TOOLS:
        groups.setdefault(t["group"], []).append(t)
    files = StudioFile.objects.filter(owner=request.user).select_related("current")[:8]
    return render(request, "accounts/esign/studio/home.html", studio_context(
        request, "pdf",
        tool_groups=list(groups.items()),
        recent_files=files,
        file_count=StudioFile.objects.filter(owner=request.user).count(),
        accept_attr=",".join(accepted_ext()),
        max_mb=max_upload_bytes() // (1024 * 1024),
    ))


@login_required
@require_POST
def esign_studio_upload(request):
    """Drop anything on the hub: it becomes a file in the workbench."""
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    uploads = request.FILES.getlist("files")
    if not uploads:
        messages.error(request, "Choose a file to open.")
        return redirect("accounts:esign_studio")
    created = []
    for up in uploads[:MAX_MERGE_FILES]:
        try:
            raw, name = upload_to_pdf(up)
        except UploadError as exc:
            messages.error(request, str(exc))
            continue
        created.append(create_studio_file(request.user, agency, raw, name, "upload",
                                          "Uploaded" if up.name.lower().endswith(".pdf") else f"Converted from {up.name}"))
    if len(created) == 1:
        return redirect("accounts:esign_studio_file", pk=created[0].pk)
    if created:
        messages.success(request, f"{len(created)} files added to your workbench.")
    return redirect("accounts:esign_studio_files")


# ─────────────────────────────────────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def esign_studio_tool(request, tool):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    meta = TOOL_BY_KEY.get(tool)
    if not meta:
        raise Http404("Unknown tool")

    existing = None
    if request.GET.get("file") or request.POST.get("file"):
        existing = _own_file(request, request.GET.get("file") or request.POST.get("file"))

    # Page-picking tools work visually on a file in the workbench
    if existing and tool in EDITOR_TOOLS:
        return _open_visual(tool, existing)

    if request.method == "GET":
        return render(request, "accounts/esign/studio/tool.html", studio_context(
            request, "pdf", tool=meta, existing=existing, visual=tool in EDITOR_TOOLS,
            my_files=StudioFile.objects.filter(owner=request.user).select_related("current")[:50],
            accept_attr=_accept_for(meta),
            max_mb=max_upload_bytes() // (1024 * 1024),
        ))

    try:
        return _run_tool(request, agency, meta, existing)
    except (T.PdfToolError, UploadError) as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("eSign Studio: tool %s failed", tool)
        detail = f" ({type(exc).__name__}: {exc})"[:200] if request.user.is_staff else ""
        messages.error(request, "That didn't work. The error has been logged." + detail)
    url = reverse("accounts:esign_studio_tool", args=[tool])
    return redirect(f"{url}?file={existing.pk}" if existing else url)


def _accept_for(meta):
    from .studio_common_esign import IMAGE_EXT

    if meta["accepts"] == "image":
        return ",".join(IMAGE_EXT)
    if meta["accepts"] == "pdf":
        return ".pdf"
    if meta["accepts"] == "office":
        from .converters_esign import supported_upload_ext

        return ",".join(supported_upload_ext())
    return ",".join(accepted_ext())


def _open_visual(tool, sf):
    target = EDITOR_TOOLS[tool]
    if target == "edit":
        return redirect("accounts:esign_studio_edit", pk=sf.pk)
    if target == "file":
        return redirect(reverse("accounts:esign_studio_file", args=[sf.pk]) + "?images=1")
    return redirect(reverse("accounts:esign_studio_organize", args=[sf.pk]) + f"?focus={tool}")


def _single_input(request, agency, meta, existing):
    """The one PDF a tool works on: an existing file, or a fresh upload saved to the workbench."""
    if existing:
        return existing, existing.read_bytes()
    up = request.FILES.get("file_upload")
    if not up:
        raise UploadError("Choose a file.")
    allow = ("pdf",) if meta["accepts"] == "pdf" else ("pdf", "image", "office")
    if meta["key"] == "unlock":
        if up.size > max_upload_bytes():
            raise UploadError(f"{up.name} is too large.")
        return None, up.read()
    raw, name = upload_to_pdf(up, allow=allow)
    return create_studio_file(request.user, agency, raw, name, "upload", "Uploaded"), raw


def _run_tool(request, agency, meta, existing):
    key = meta["key"]
    user = request.user
    P = request.POST

    if key in ("convert", "images", "merge"):
        uploads = request.FILES.getlist("files")
        order = [s for s in (P.get("order") or "").split(",") if s != ""]
        if order and len(order) == len(uploads):
            try:
                uploads = [uploads[int(i)] for i in order]
            except (ValueError, IndexError):
                pass
        picked = [int(i) for i in P.getlist("workbench") if str(i).isdigit()]
        if not uploads and not picked:
            raise UploadError("Add at least one file.")
        if len(uploads) + len(picked) > MAX_MERGE_FILES:
            raise UploadError(f"Up to {MAX_MERGE_FILES} files at a time.")

        if key == "convert":
            made = []
            for up in uploads:
                raw, name = upload_to_pdf(up, allow=("office",))
                made.append(create_studio_file(user, agency, raw, name, "convert", f"Converted from {up.name}"))
            if len(made) == 1:
                messages.success(request, f"{made[0].name} is ready.")
                return redirect("accounts:esign_studio_file", pk=made[0].pk)
            messages.success(request, f"Converted {len(made)} documents.")
            return redirect("accounts:esign_studio_files")

        if key == "images":
            images = []
            for up in uploads:
                if up.size > max_upload_bytes():
                    raise UploadError(f"{up.name} is too large.")
                images.append((up.name, up.read()))
            raw = T.images_to_pdf(images, page=P.get("page") or "a4",
                                  margin_mm=float(P.get("margin") or 10))
            name = (P.get("name") or "").strip() or f"{_stem(uploads[0].name)}.pdf"
            sf = create_studio_file(user, agency, raw, name if name.lower().endswith(".pdf") else f"{name}.pdf",
                                    "images", f"Built from {len(images)} image(s)")
            messages.success(request, f"{sf.name} — {sf.page_count} page(s).")
            return redirect("accounts:esign_studio_file", pk=sf.pk)

        # merge
        parts = []
        for pk in picked:
            sf = _own_file(request, pk)
            parts.append((sf.name, sf.read_bytes()))
        for up in uploads:
            raw, name = upload_to_pdf(up)
            parts.append((name, raw))
        if len(parts) < 2:
            raise UploadError("Add at least two files to merge.")
        raw = T.merge([p[1] for p in parts])
        name = (P.get("name") or "").strip() or f"{_stem(parts[0][0])}-merged.pdf"
        sf = create_studio_file(user, agency, raw, name if name.lower().endswith(".pdf") else f"{name}.pdf",
                                "merge", "Merged " + ", ".join(p[0] for p in parts)[:140])
        messages.success(request, f"Merged {len(parts)} files into {sf.page_count} pages.")
        return redirect("accounts:esign_studio_file", pk=sf.pk)

    sf, raw = _single_input(request, agency, meta, existing)

    if key in EDITOR_TOOLS:
        return _open_visual(key, sf)

    if key == "split":
        parts = T.split(raw, mode=P.get("mode") or "ranges", ranges=P.get("ranges") or "",
                        every=int(P.get("every") or 1), stem=_stem(sf.name))
        made = [create_studio_file(user, agency, data, name, "split", f"Split from {sf.name}") for name, data in parts]
        messages.success(request, f"Split into {len(made)} files.")
        ids = ",".join(str(m.pk) for m in made)
        return redirect(reverse("accounts:esign_studio_files") + f"?ids={ids}")

    if key == "compress":
        level = P.get("level") if P.get("level") in T.COMPRESS_LEVELS else "balanced"
        out = T.compress(raw, level)
        if len(out) >= len(raw):
            messages.info(request, "This PDF is already about as small as it gets — nothing changed.")
            return redirect("accounts:esign_studio_file", pk=sf.pk)
        saved = 100 - int(len(out) * 100 / max(1, len(raw)))
        add_version(sf, out, f"Compressed ({level}) — {saved}% smaller")
        messages.success(request, f"Compressed: {_human(len(raw))} → {_human(len(out))} ({saved}% smaller).")
        return redirect("accounts:esign_studio_file", pk=sf.pk)

    if key == "watermark":
        out = T.watermark(raw, P.get("text") or "", size=int(P.get("size") or 54),
                          opacity=float(P.get("opacity") or 0.18), angle=int(P.get("angle") or 45),
                          color=P.get("color") or "#6B7280", position=P.get("position") or "center",
                          pages=P.get("pages") or "")
        add_version(sf, out, f"Watermark “{(P.get('text') or '')[:40]}”")
        messages.success(request, "Watermark added.")
        return redirect("accounts:esign_studio_file", pk=sf.pk)

    if key == "numbers":
        out = T.page_numbers(raw, position=P.get("position") or "bottom-center",
                             fmt=P.get("fmt") or "Page {n} of {total}", start=int(P.get("start") or 1),
                             size=int(P.get("size") or 9), skip_first=bool(P.get("skip_first")))
        add_version(sf, out, "Page numbers added")
        messages.success(request, "Page numbers added.")
        return redirect("accounts:esign_studio_file", pk=sf.pk)

    if key == "protect":
        if (P.get("password") or "") != (P.get("password2") or ""):
            raise T.PdfToolError("The two passwords don't match.")
        out = T.protect(raw, P.get("password") or "", allow_printing=bool(P.get("allow_print")))
        return _pdf_response(out, f"{_stem(sf.name)}-protected.pdf", inline=False)

    if key == "unlock":
        out = T.unlock(raw, P.get("password") or "")
        up = request.FILES.get("file_upload")
        name = f"{_stem(up.name if up else 'document')}-unlocked.pdf"
        new = create_studio_file(user, agency, out, name, "unlock", "Password removed")
        messages.success(request, "Unlocked. The copy in your workbench has no password.")
        return redirect("accounts:esign_studio_file", pk=new.pk)

    raise Http404("Unknown tool")


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return str(n)


# ─────────────────────────────────────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_studio_files(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    qs = StudioFile.objects.filter(owner=request.user).select_related("current")
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(current__operation__icontains=q))
    highlight = [int(i) for i in (request.GET.get("ids") or "").split(",") if i.isdigit()]
    if highlight:
        qs = qs.filter(pk__in=highlight)
    return render(request, "accounts/esign/studio/files.html", studio_context(
        request, "pdf", files=qs[:300], q=q, highlight=highlight,
        retention_days=getattr(settings, "ESIGN_STUDIO_RETENTION_DAYS", 90),
    ))


@login_required
@require_GET
def esign_studio_file(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    sf = _own_file(request, pk)
    from .models_esign_studio import DocumentWorkflow
    from .studio_common_esign import shared_template_q

    flows = DocumentWorkflow.objects.filter(shared_template_q(request.user), is_active=True, form__isnull=True)[:30]
    return render(request, "accounts/esign/studio/file_detail.html", studio_context(
        request, "pdf", file=sf, versions=sf.versions.all(), flows=flows,
        pdf_inline=inline_pdf(sf.read_bytes()),
        tools=[t for t in TOOLS if t["key"] not in ("convert", "images", "merge", "unlock")],
        show_images=bool(request.GET.get("images")),
    ))


@login_required
@require_GET
@xframe_options_sameorigin          # lets the viewer fall back to the browser's own PDF viewer
def esign_studio_file_pdf(request, pk):
    sf = _own_file(request, pk)
    number = request.GET.get("v")
    if number and str(number).isdigit():
        version = get_object_or_404(StudioFileVersion, studio_file=sf, number=int(number))
        handle = version.file
        handle.open("rb")
        raw = handle.read()
        handle.close()
        name = f"{_stem(sf.name)}-v{version.number}.pdf"
    else:
        raw = sf.read_bytes()
        name = sf.download_name
    if not raw:
        raise Http404("File missing")
    return _pdf_response(raw, name, inline=not request.GET.get("download"))


@login_required
@require_POST
def esign_studio_file_rename(request, pk):
    sf = _own_file(request, pk)
    name = (request.POST.get("name") or "").strip()[:200]
    if name:
        sf.name = name if name.lower().endswith(".pdf") else f"{name}.pdf"
        sf.save(update_fields=["name", "updated_at"])
        messages.success(request, "Renamed.")
    return redirect("accounts:esign_studio_file", pk=sf.pk)


@login_required
@require_POST
def esign_studio_file_delete(request, pk):
    sf = _own_file(request, pk)
    _delete_file(sf)
    messages.success(request, "File deleted.")
    return redirect("accounts:esign_studio_files")


def _delete_file(sf):
    for v in sf.versions.all():
        try:
            v.file.delete(save=False)
        except Exception:  # noqa: BLE001
            pass
    sf.delete()


@login_required
@require_POST
def esign_studio_files_bulk(request):
    ids = [int(i) for i in request.POST.getlist("ids") if str(i).isdigit()]
    files = list(StudioFile.objects.filter(owner=request.user, pk__in=ids).select_related("current"))
    if not files:
        messages.error(request, "Select at least one file.")
        return redirect("accounts:esign_studio_files")
    action = request.POST.get("action")
    if action == "delete":
        for sf in files:
            _delete_file(sf)
        messages.success(request, f"Deleted {len(files)} file(s).")
        return redirect("accounts:esign_studio_files")
    if action == "merge":
        agency, bounce = studio_gate(request)
        if bounce:
            return bounce
        order = {pk: i for i, pk in enumerate(ids)}
        files.sort(key=lambda f: order.get(f.pk, 0))
        try:
            raw = T.merge([f.read_bytes() for f in files])
        except T.PdfToolError as exc:
            messages.error(request, str(exc))
            return redirect("accounts:esign_studio_files")
        sf = create_studio_file(request.user, agency, raw, f"{_stem(files[0].name)}-merged.pdf", "merge",
                                f"Merged {len(files)} files")
        return redirect("accounts:esign_studio_file", pk=sf.pk)
    # zip
    data = T.zip_files([(f.download_name, f.read_bytes()) for f in files])
    resp = HttpResponse(data, content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="esign-studio-{timezone.now():%Y%m%d-%H%M}.zip"'
    return resp


@login_required
@require_POST
def esign_studio_file_revert(request, pk, number):
    sf = _own_file(request, pk)
    version = get_object_or_404(StudioFileVersion, studio_file=sf, number=number)
    handle = version.file
    handle.open("rb")
    raw = handle.read()
    handle.close()
    add_version(sf, raw, f"Restored version {number}")
    messages.success(request, f"Restored version {number}. The version you had is still in the history.")
    return redirect("accounts:esign_studio_file", pk=sf.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Organise pages
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_studio_organize(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    sf = _own_file(request, pk)
    raw = sf.read_bytes()
    try:
        sizes = T.page_sizes(raw)
    except T.PdfToolError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_studio_file", pk=sf.pk)
    focus = request.GET.get("focus") if request.GET.get("focus") in ("remove", "rotate", "extract", "organize") else "organize"
    return render(request, "accounts/esign/studio/organize.html", studio_context(
        request, "pdf", file=sf, focus=focus, page_sizes_json=json.dumps(sizes), pdf_inline=inline_pdf(raw),
    ))


@login_required
@require_POST
def esign_studio_organize_save(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return JsonResponse({"ok": False, "error": "eSign is not enabled."}, status=403)
    sf = _own_file(request, pk)
    body = json_body(request)
    if not isinstance(body, dict):
        return JsonResponse({"ok": False, "error": "Invalid request."}, status=400)
    plan = body.get("plan")
    if not isinstance(plan, list) or len(plan) > 2000:
        return JsonResponse({"ok": False, "error": "Invalid page plan."}, status=400)
    try:
        out = T.apply_page_plan(sf.read_bytes(), plan)
    except T.PdfToolError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    summary = (body.get("summary") or "Pages reorganised")[:160]
    if body.get("as_new"):
        new = create_studio_file(request.user, agency, out, f"{_stem(sf.name)}-extract.pdf", "extract",
                                 f"Pages extracted from {sf.name}")
        return JsonResponse({"ok": True, "redirect": reverse("accounts:esign_studio_file", args=[new.pk])})
    add_version(sf, out, summary)
    messages.success(request, f"Saved — {sf.page_count} page(s).")
    return JsonResponse({"ok": True, "redirect": reverse("accounts:esign_studio_file", args=[sf.pk])})


# ─────────────────────────────────────────────────────────────────────────────
# Edit
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_studio_edit(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    sf = _own_file(request, pk)
    raw = sf.read_bytes()
    try:
        sizes = T.page_sizes(raw)
    except T.PdfToolError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_studio_file", pk=sf.pk)
    return render(request, "accounts/esign/studio/editor.html", studio_context(
        request, "pdf", file=sf, page_sizes_json=json.dumps(sizes), pdf_inline=inline_pdf(raw),
    ))


@login_required
@require_POST
def esign_studio_edit_save(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return JsonResponse({"ok": False, "error": "eSign is not enabled."}, status=403)
    sf = _own_file(request, pk)
    if len(request.body) > 60 * 1024 * 1024:
        return JsonResponse({"ok": False, "error": "Too many edits in one save. Save in smaller batches."}, status=400)
    body = json_body(request)
    if not isinstance(body, dict):
        return JsonResponse({"ok": False, "error": "Invalid request."}, status=400)
    pages = body.get("pages") if isinstance(body.get("pages"), dict) else {}
    flattened = body.get("flattened") if isinstance(body.get("flattened"), dict) else {}
    count = sum(len(v) for v in pages.values() if isinstance(v, list))
    if not count and not flattened:
        return JsonResponse({"ok": False, "error": "Nothing to save yet."}, status=400)
    try:
        out = T.burn_edits(sf.read_bytes(), {k: v for k, v in pages.items() if isinstance(v, list)}, flattened)
    except T.PdfToolError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    label = f"Edited — {count} change(s)"
    if flattened:
        label += f", {len(flattened)} page(s) flattened"
    add_version(sf, out, label)
    messages.success(request, "Edits saved as a new version.")
    return JsonResponse({"ok": True, "redirect": reverse("accounts:esign_studio_file", args=[sf.pk])})


# ─────────────────────────────────────────────────────────────────────────────
# Onward: sign, send, workflow
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def esign_studio_to_sign(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    sf = _own_file(request, pk)
    mode = request.POST.get("mode")
    try:
        envelope = envelope_from_pdf(request, agency, sf.read_bytes(), name=sf.download_name,
                                     subject=_stem(sf.name), self_sign=(mode == "self"))
    except (UploadError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_studio_file", pk=sf.pk)
    if mode == "self":
        messages.info(request, "Your signature and date are on the last page. Drag them anywhere you like, "
                               "then choose Sign it now.")
    else:
        messages.info(request, "Add your signers, place their fields, then send.")
    return redirect("accounts:esign_prepare", pk=envelope.pk)
