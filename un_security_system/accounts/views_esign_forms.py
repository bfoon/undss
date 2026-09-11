# accounts/views_esign_forms.py
"""
UN PASS — eSign Studio: the form builder.

A form can be filled by signed-in colleagues it is shared with, or by anyone
with its link. A submission can start the form's workflow automatically, or be
downloaded, signed by the submitter, sent for signature, or started on any flow
afterwards.
"""

import csv
import json
import logging

from django.contrib import messages
from django.core.exceptions import RequestDataTooBig, TooManyFieldsSent
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import form_pdf_esign as F
from . import workflow_engine_esign as E
from .models_esign_studio import DocumentWorkflow, FormSubmission, FormTemplate
from .studio_common_esign import (
    UploadError,
    asset_urls,
    can_edit_template,
    can_use_template,
    can_view_submission,
    client_ip,
    directory,
    envelope_from_pdf,
    inline_pdf,
    json_body,
    read_people,
    shared_template_q,
    studio_context,
    studio_gate,
    visible_submissions,
)
from .studio_library_esign import FORM_TEMPLATES, form_template
from .utils_esign import esign_brand

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# List and create
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_forms(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    user = request.user
    forms = (FormTemplate.objects.filter(shared_template_q(user)).select_related("created_by", "workflow")
             .annotate(submission_total=Count("submissions")))
    tab = request.GET.get("tab") or "forms"
    return render(request, "accounts/esign/studio/forms.html", studio_context(
        request, "forms",
        tab=tab if tab in ("forms", "fill", "submissions") else "forms",
        my_forms=[f for f in forms if f.created_by_id == user.id],
        fillable=[f for f in forms if f.is_published],
        submissions=visible_submissions(user).select_related("form", "submitted_by")[:200],
        templates=FORM_TEMPLATES,
    ))


@login_required
@require_POST
def esign_form_new(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    tpl = form_template(request.POST.get("template") or "blank")
    schema = F.clean_schema(tpl["schema"])
    prefix = "".join(ch for ch in (tpl["key"] if tpl["key"] != "blank" else "FRM").upper() if ch.isalnum())[:6]
    form = FormTemplate.objects.create(
        agency=agency, created_by=request.user, office_id=getattr(request.user, "country_office_id", None),
        name=tpl["name"] if tpl["key"] != "blank" else "Untitled form",
        description=tpl["description"] if tpl["key"] != "blank" else "",
        category=tpl["category"], schema=schema, reference_prefix=prefix or "FRM",
    )
    return redirect("accounts:esign_form_designer", pk=form.pk)


@login_required
@require_POST
def esign_form_duplicate(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    src = get_object_or_404(FormTemplate, pk=pk)
    if not can_use_template(request.user, src):
        raise Http404()
    copy = FormTemplate.objects.create(
        agency=agency, created_by=request.user, office_id=getattr(request.user, "country_office_id", None),
        name=f"Copy of {src.name}"[:150], description=src.description, category=src.category,
        schema=src.schema, reference_prefix=src.reference_prefix, submit_message=src.submit_message,
    )
    messages.success(request, "Duplicated. This copy is yours to change.")
    return redirect("accounts:esign_form_designer", pk=copy.pk)


@login_required
@require_POST
def esign_form_delete(request, pk):
    form = get_object_or_404(FormTemplate, pk=pk, created_by=request.user)
    if form.submissions.exists():
        form.is_published = False
        form.save(update_fields=["is_published", "updated_at"])
        messages.success(request, "Form unpublished. Its submissions are kept; delete them first to remove the form.")
    else:
        form.delete()
        messages.success(request, "Form deleted.")
    return redirect("accounts:esign_forms")


# ─────────────────────────────────────────────────────────────────────────────
# Designer
# ─────────────────────────────────────────────────────────────────────────────

def _flow_steps(workflow):
    """Steps a field can be 'filled in at' / a signature 'signed at'."""
    if not workflow:
        return []
    nodes = sorted(E.clean_graph(workflow.graph)["nodes"], key=lambda n: (n["x"], n["y"]))
    return [{"id": n["id"], "label": n["label"], "type": n["type"]}
            for n in nodes if n["type"] in ("fill", "signature")]


@login_required
@require_GET
def esign_form_designer(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    form = get_object_or_404(FormTemplate.objects.select_related("workflow"), pk=pk)
    if not can_edit_template(request.user, form):
        if can_use_template(request.user, form):
            messages.info(request, "Only the owner can change this form. Duplicate it to make your own version.")
            return redirect("accounts:esign_forms")
        raise Http404()
    flows = DocumentWorkflow.objects.filter(created_by=request.user, is_active=True).order_by("name")
    return render(request, "accounts/esign/studio/form_designer.html", studio_context(
        request, "forms",
        form=form,
        payload=json.dumps({
            "id": form.pk, "name": form.name, "description": form.description, "category": form.category,
            "schema": F.clean_schema(form.schema), "workflow_id": form.workflow_id,
            "share_scope": form.share_scope, "access": form.access, "is_published": form.is_published,
            "owner_sees_submissions": form.owner_sees_submissions,
            "reference_prefix": form.reference_prefix, "submit_message": form.submit_message,
            "version": form.version,
        }),
        config_json=json.dumps({
            "prefill": F.PREFILL_KEYS,
            "widths": F.WIDTHS,
            "templates": [{"key": t["key"], "name": t["name"], "icon": t["icon"], "category": t["category"],
                           "description": t["description"], "schema": F.clean_schema(t["schema"])}
                          for t in FORM_TEMPLATES],
            "flows": [{"id": w.pk, "name": w.name, "steps": _flow_steps(w)} for w in flows],
            "save_url": reverse("accounts:esign_form_save", args=[form.pk]),
            "preview_url": reverse("accounts:esign_form_preview_pdf", args=[form.pk]),
            "fill_url": reverse("accounts:esign_form_fill", args=[form.pk]),
            "public_url": request.build_absolute_uri(reverse("accounts:esign_form_public", args=[form.public_token])),
            "flow_new_url": reverse("accounts:esign_workflow_new"),
            "designer_url": reverse("accounts:esign_workflow_designer", args=[0]),
        }),
    ))


@login_required
@require_POST
def esign_form_save(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return JsonResponse({"ok": False, "error": "eSign is not enabled."}, status=403)
    form = get_object_or_404(FormTemplate, pk=pk, created_by=request.user)
    body, problem = json_body(request, "form")
    if problem:
        return problem
    if str(body.get("version") or "") not in ("", str(form.version)):
        return JsonResponse({"ok": False, "conflict": True,
                             "error": "This form was saved from another window. Reload to see the latest version."},
                            status=409)

    schema = F.clean_schema(body.get("schema"))
    workflow = None
    if body.get("workflow_id"):
        workflow = DocumentWorkflow.objects.filter(pk=body["workflow_id"], created_by=request.user,
                                                   is_active=True).first()
    form.name = (body.get("name") or "").strip()[:150] or form.name
    form.description = (body.get("description") or "")[:2000]
    form.category = (body.get("category") or "")[:60]
    form.schema = schema
    form.workflow = workflow
    form.share_scope = body.get("share_scope") if body.get("share_scope") in ("private", "office", "agency") else "office"
    form.access = "link" if body.get("access") == "link" else "internal"
    form.is_published = bool(body.get("is_published"))
    form.owner_sees_submissions = bool(body.get("owner_sees_submissions", True))
    form.reference_prefix = "".join(ch for ch in (body.get("reference_prefix") or "FRM").upper()
                                    if ch.isalnum() or ch == "-")[:16] or "FRM"
    form.submit_message = (body.get("submit_message") or "")[:300]
    form.office_id = getattr(request.user, "country_office_id", None)
    form.version += 1
    form.save()

    warnings = []
    if workflow:
        report = E.validate_graph(E.clean_graph(workflow.graph), schema)
        warnings = [w["text"] for w in report["errors"] + report["warnings"]]
    return JsonResponse({"ok": True, "version": form.version, "schema": schema, "flow_warnings": warnings,
                         "saved_at": timezone.localtime().strftime("%H:%M")})


@login_required
@require_http_methods(["GET", "POST"])
@xframe_options_sameorigin
def esign_form_preview_pdf(request, pk):
    """GET: the saved form as a blank printable PDF. POST: preview unsaved changes."""
    form = get_object_or_404(FormTemplate, pk=pk)
    if not can_use_template(request.user, form):
        raise Http404()
    schema = form.schema
    if request.method == "POST":
        body, problem = json_body(request, "form")
        if problem:
            return problem
        if body.get("schema"):
            schema = F.clean_schema(body["schema"])
    pdf, _ = F.render_form_pdf(F.clean_schema(schema), {}, reference=f"{form.reference_prefix}-PREVIEW", blank=True)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{form.name[:60]}-blank.pdf"'
    resp["Cache-Control"] = "private, no-store"
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Fill
# ─────────────────────────────────────────────────────────────────────────────

def _new_reference(form):
    year = timezone.localdate().year
    prefix = form.reference_prefix or "FRM"
    count = FormSubmission.objects.filter(reference__startswith=f"{prefix}-{year}-").count()
    return f"{prefix}-{year}-{count + 1:04d}"


def _save_submission(form, values, *, user, name, email, agency, ip):
    for attempt in range(6):
        ref = _new_reference(form)
        if attempt:
            ref = f"{ref}-{attempt}"
        try:
            with transaction.atomic():
                return FormSubmission.objects.create(
                    form=form, form_name=form.name, schema=form.schema, values=values, reference=ref,
                    agency=agency, submitted_by=user, submitter_name=name[:150], submitter_email=email[:254], ip=ip,
                )
        except IntegrityError:
            continue
    raise IntegrityError("Could not allocate a reference number.")


def _fill(request, form, *, public):
    user = request.user if request.user.is_authenticated else None
    schema = F.clean_schema(form.schema)
    workflow = form.workflow if (form.workflow_id and form.workflow.is_active) else None
    graph = E.clean_graph(workflow.graph) if workflow else None
    slots = E.chosen_slots(graph) if graph else []
    test_mode = bool(user and form.created_by_id == user.id and not form.is_published)

    ctx = {
        **asset_urls(),
        "base_template": "base.html" if user else "accounts/esign/studio/public_base.html",
        "form": form, "schema": schema, "public": public, "workflow": workflow, "slots": slots,
        "directory_json": json.dumps(directory(user)) if (user and not public) else "[]",
        "values": F.initial_values(schema, user), "errors": {}, "test_mode": test_mode,
        "studio_section": "forms",
    }

    if request.method == "GET":
        return render(request, "accounts/esign/studio/form_fill.html", ctx)

    try:
        request.POST
    except (TooManyFieldsSent, RequestDataTooBig):
        messages.error(request, "Your answers were too large for the server to accept in one submission, so "
                                "they weren't saved. Please shorten the longest tables or texts and try again.")
        return render(request, "accounts/esign/studio/form_fill.html", ctx, status=413)

    if request.POST.get("website"):          # honeypot on public links
        return redirect(request.path)

    values, errors = F.read_values(schema, request.POST, existing={}, scope="submitter")
    name = (request.POST.get("submitter_name") or "").strip() if public and not user else (
        user.get_full_name() or user.username)
    email = (request.POST.get("submitter_email") or "").strip().lower() if public and not user else (user.email or "")
    if public and not user:
        if not name:
            errors["__name"] = "Enter your name."
        if not email or "@" not in email:
            errors["__email"] = "Enter your email address so you can be sent a copy."

    chosen = {}
    for s in slots:
        people = read_people(request.POST.get(f"slot_{s['key']}"))
        if not people:
            errors[f"__slot_{s['key']}"] = f"Choose who is your {s['label']}."
        chosen[s["key"]] = people

    if errors:
        ctx.update(values=values, errors=errors, posted_slots={k: request.POST.get(f"slot_{k}") or "[]"
                                                               for k in [s["key"] for s in slots]})
        messages.error(request, "Please check the highlighted answers.")
        return render(request, "accounts/esign/studio/form_fill.html", ctx)

    agency = form.agency
    sub = _save_submission(form, values, user=user, name=name, email=email, agency=agency, ip=client_ip(request))

    run = None
    if workflow:
        initiator = user or form.created_by
        try:
            run = E.start_run(workflow, initiator, agency=agency, subject=f"{sub.title} — {sub.reference}",
                              submission=sub, slots=chosen, request=request)
            if not user:
                E.log_run(run, "step", note=f"Submitted through the public link by {name} <{email}>.")
        except E.WorkflowError as exc:
            logger.warning("eSign Studio: form %s submission %s could not start its flow: %s", form.pk, sub.pk, exc)
            messages.warning(request, f"Your form was saved as {sub.reference}, but its workflow couldn't start: {exc}")

    try:
        from .asset_email import send_email_async

        if email:
            pdf, _ = F.render_form_pdf(sub.schema, sub.values, reference=sub.reference,
                                       submitted_at=sub.created_at, submitter=sub.submitter_name)
            send_email_async(
                subject=f"Received: {sub.title} ({sub.reference})",
                to_emails=[email],
                html_template="accounts/esign/email/workflow_update.html",
                context={"brand": esign_brand(),
                         "subject": f"Received: {sub.title}", "subject_line": sub.title,
                         "headline": "We received your form", "reference": sub.reference,
                         "flow_name": workflow.name if workflow else form.name, "recipient_name": name,
                         "detail": form.submit_message, "summary": [r for r in F.summary_rows(sub.schema, sub.values) if r[1]][:12],
                         "run_link": request.build_absolute_uri(reverse("accounts:esign_submission_detail", args=[sub.pk]))
                         if user else request.build_absolute_uri(request.path),
                         "action_label": "View your submission" if user else "Open the form",
                         "attached": True, "tone": "success", "now": timezone.now()},
                attachments=[(f"{sub.reference}.pdf", pdf, "application/pdf")],
            )
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: receipt email failed for submission %s", sub.pk)

    if user:
        messages.success(request, form.submit_message or f"Submitted — reference {sub.reference}.")
        if run:
            return redirect("accounts:esign_run_detail", pk=run.pk)
        return redirect("accounts:esign_submission_detail", pk=sub.pk)
    return render(request, "accounts/esign/studio/form_done.html", {
        **asset_urls(), "base_template": "accounts/esign/studio/public_base.html",
        "form": form, "sub": sub, "run": run,
    })


@login_required
@require_http_methods(["GET", "POST"])
def esign_form_fill(request, pk):
    form = get_object_or_404(FormTemplate.objects.select_related("workflow"), pk=pk)
    owner = form.created_by_id == request.user.id
    if not (owner or (form.is_published and can_use_template(request.user, form))):
        raise Http404()
    return _fill(request, form, public=False)


@require_http_methods(["GET", "POST"])
def esign_form_public(request, token):
    form = get_object_or_404(FormTemplate.objects.select_related("workflow"), public_token=token)
    if not (form.is_published and form.access == FormTemplate.ACCESS_LINK):
        return render(request, "accounts/esign/studio/form_done.html", {
            **asset_urls(), "base_template": "accounts/esign/studio/public_base.html",
            "form": form, "closed": True,
        }, status=404)
    return _fill(request, form, public=True)


# ─────────────────────────────────────────────────────────────────────────────
# Submissions
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_form_submissions(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    form = get_object_or_404(FormTemplate, pk=pk, created_by=request.user)
    if not form.owner_sees_submissions:
        messages.info(request, "This form is set so that you don't see its submissions.")
        return redirect("accounts:esign_form_designer", pk=form.pk)
    subs = form.submissions.select_related("submitted_by").prefetch_related("runs")
    schema = F.clean_schema(form.schema)
    inputs = [el for el in schema["elements"] if el["type"] in F.INPUT_TYPES]

    if request.GET.get("format") == "csv":
        resp = HttpResponse(content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{form.reference_prefix}-submissions.csv"'
        resp.write("\ufeff")
        w = csv.writer(resp)
        w.writerow(["Reference", "Submitted", "Submitted by", "Email", "Status"] + [el["label"] for el in inputs])
        for s in subs:
            row = [s.reference, timezone.localtime(s.created_at).strftime("%Y-%m-%d %H:%M"), s.submitter_name,
                   s.submitter_email, s.get_status_display()]
            for el in inputs:
                v = s.values.get(el["key"])
                if el["type"] == "table" and isinstance(v, list):
                    row.append(" | ".join("; ".join(f"{c['label']}: {r.get(c['key'], '')}" for c in el["columns"])
                                          for r in v))
                else:
                    row.append(F.display_value(el, v))
            w.writerow(row)
        return resp

    columns = [el for el in inputs if el["type"] != "table"][:5]
    return render(request, "accounts/esign/studio/submissions.html", studio_context(
        request, "forms", form=form, submissions=subs[:500], columns=columns,
    ))


def _sub_or_404(request, pk):
    sub = get_object_or_404(FormSubmission.objects.select_related("form", "submitted_by"), pk=pk)
    if not can_view_submission(request.user, sub):
        raise Http404()
    return sub


@login_required
@require_GET
def esign_submission_detail(request, pk):
    sub = _sub_or_404(request, pk)
    from .studio_common_esign import shared_template_q

    runs = list(sub.runs.select_related("workflow").all())
    flows = DocumentWorkflow.objects.filter(shared_template_q(request.user), is_active=True)[:40]
    schema = sub.schema
    try:
        pdf, _ = F.render_form_pdf(schema, sub.values, reference=sub.reference, submitted_at=sub.created_at,
                                   submitter=sub.submitter_name, status_label=sub.get_status_display())
    except Exception:  # noqa: BLE001 - the answers tab still works; the PDF tab fetches by URL
        logger.exception("eSign Studio: could not render submission %s for inline preview", sub.pk)
        pdf = None
    return render(request, "accounts/esign/studio/submission_detail.html", studio_context(
        request, "forms", sub=sub, runs=runs, flows=flows, schema=schema,
        directory_json=json.dumps(directory(request.user)),
        pdf_inline=inline_pdf(pdf),
        summary=F.summary_rows(schema, sub.values),
        tables=[(el, sub.values.get(el["key"]) or []) for el in schema.get("elements") or [] if el["type"] == "table"],
        is_owner=bool(sub.form and sub.form.created_by_id == request.user.id),
        is_submitter=sub.submitted_by_id == request.user.id,
        live_run=next((r for r in runs if r.is_open), None),
    ))


@login_required
@require_GET
@xframe_options_sameorigin          # lets the viewer fall back to the browser's own PDF viewer
def esign_submission_pdf(request, pk):
    sub = _sub_or_404(request, pk)
    pdf, _ = F.render_form_pdf(sub.schema, sub.values, reference=sub.reference, submitted_at=sub.created_at,
                               submitter=sub.submitter_name, status_label=sub.get_status_display())
    resp = HttpResponse(pdf, content_type="application/pdf")
    disp = "attachment" if request.GET.get("download") else "inline"
    resp["Content-Disposition"] = f'{disp}; filename="{sub.reference}.pdf"'
    resp["Cache-Control"] = "private, no-store"
    return resp


@login_required
@require_POST
def esign_submission_action(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    sub = _sub_or_404(request, pk)
    action = request.POST.get("action")
    pdf, boxes = F.render_form_pdf(sub.schema, sub.values, reference=sub.reference, submitted_at=sub.created_at,
                                   submitter=sub.submitter_name)
    name = f"{sub.reference}.pdf"

    if action in ("self_sign", "send"):
        mine = [b for b in boxes if (b.get("role") or "").lower() in ("requester", "submitter", "initiator")] or boxes
        try:
            env = envelope_from_pdf(request, agency, pdf, name=name, subject=f"{sub.title} — {sub.reference}",
                                    self_sign=(action == "self_sign"), reference=sub.reference,
                                    signature_boxes=mine if action == "self_sign" else None)
        except (UploadError, ValueError) as exc:
            messages.error(request, str(exc))
            return redirect("accounts:esign_submission_detail", pk=sub.pk)
        if action == "self_sign":
            messages.info(request, "Your signature is in the form's signature box. Move it if you need to, then sign.")
        else:
            messages.info(request, "Add your signers and place their fields, then send.")
        return redirect("accounts:esign_prepare", pk=env.pk)

    if action == "studio":
        from .views_esign_studio import create_studio_file

        sf = create_studio_file(request.user, agency, pdf, name, "form", f"From form submission {sub.reference}")
        messages.success(request, "Copied to your PDF workbench.")
        return redirect("accounts:esign_studio_file", pk=sf.pk)

    if action == "flow":
        wf = get_object_or_404(DocumentWorkflow, pk=request.POST.get("workflow"), is_active=True)
        if not can_use_template(request.user, wf):
            raise Http404()
        if sub.runs.filter(status__in=("running", "returned", "blocked")).exists():
            messages.error(request, "This submission is already in a workflow.")
            return redirect("accounts:esign_submission_detail", pk=sub.pk)
        slots = E.chosen_slots(E.clean_graph(wf.graph))
        chosen = {s["key"]: read_people(request.POST.get(f"slot_{s['key']}")) for s in slots}
        try:
            run = E.start_run(wf, request.user, agency=agency, subject=f"{sub.title} — {sub.reference}",
                              submission=sub, slots=chosen, request=request)
        except E.WorkflowError as exc:
            messages.error(request, str(exc))
            return redirect("accounts:esign_submission_detail", pk=sub.pk)
        messages.success(request, f"Started {run.reference}.")
        return redirect("accounts:esign_run_detail", pk=run.pk)

    raise Http404()


@login_required
@require_GET
def esign_flow_slots(request, pk):
    """Roles a flow asks for at start — used by 'Start a workflow' pickers."""
    wf = get_object_or_404(DocumentWorkflow, pk=pk, is_active=True)
    if not can_use_template(request.user, wf):
        raise Http404()
    return JsonResponse({"slots": E.chosen_slots(E.clean_graph(wf.graph))})
