# accounts/views_esign_docgen.py
"""
UN PASS — eSign Studio: the automation page of a form.

Two things live here, because they are the two questions people ask once a form
works: "who gets the signed copy, and when?" and "what else should this form
produce?"

  /esign/forms/<pk>/automation/                 both, on one page
  /esign/forms/<pk>/automation/rules/new/       add a follow-on document
  /esign/forms/<pk>/automation/rules/<id>/      change one
  /esign/documents/<id>/                        download what was produced
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from . import docgen_esign
from . import esign_delivery
from . import form_formula_esign as FX
from .models_esign_docgen import FormDocumentRule, GeneratedDocument
from .models_esign_studio import FormSubmission, FormTemplate
from .studio_common_esign import can_edit_template, can_view_submission, studio_context, studio_gate

logger = logging.getLogger(__name__)


def _form_or_404(request, pk):
    form = get_object_or_404(FormTemplate.objects.select_related("workflow"), pk=pk)
    if not can_edit_template(request.user, form):
        raise Http404()
    return form


def _target_choices(request, form):
    """Forms that can serve as the document: the owner's own, newest first."""
    return (FormTemplate.objects
            .filter(created_by=request.user)
            .exclude(pk=form.pk)
            .order_by("name")
            .values("pk", "name", "category"))


def _schema_fields(form):
    out = []
    for element in (form.schema or {}).get("elements") or []:
        key = element.get("key")
        if not key:
            continue
        out.append({
            "key": key,
            "label": element.get("label") or key,
            "type": element.get("type"),
            "columns": [{"key": c.get("key"), "label": c.get("label")}
                        for c in element.get("columns") or []],
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# The page
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET", "POST"])
def esign_form_automation(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    form = _form_or_404(request, pk)

    if request.method == "POST":
        policy = esign_delivery.clean_policy({
            "mode": request.POST.get("mode"),
            "single_message": request.POST.get("single_message") == "1",
            "include_signers": request.POST.get("include_signers") == "1",
            "include_initiator": request.POST.get("include_initiator") == "1",
            "include_participants": request.POST.get("include_participants") == "1",
            "attach_certificate": request.POST.get("attach_certificate") == "1",
            "attach_documents": request.POST.get("attach_documents") == "1",
            "extra_emails": [e.strip() for e in (request.POST.get("extra_emails") or "").replace(";", ",").split(",")],
            "message": request.POST.get("message"),
        })
        schema = dict(form.schema or {})
        schema["delivery"] = policy
        form.schema = schema
        form.version += 1
        form.save(update_fields=["schema", "version", "updated_at"])
        messages.success(request, "Saved. " + esign_delivery.describe(policy))
        return redirect("accounts:esign_form_automation", pk=form.pk)

    policy = esign_delivery.policy_for_form(form)
    rules = (FormDocumentRule.objects
             .filter(form=form)
             .select_related("target_form")
             .order_by("order", "id"))
    documents = (GeneratedDocument.objects
                 .filter(source_submission__form=form)
                 .select_related("rule", "source_submission")[:25])

    return render(request, "accounts/esign/studio/form_automation.html", studio_context(
        request, "forms",
        form=form,
        policy=policy,
        policy_summary=esign_delivery.describe(policy),
        rules=rules,
        documents=documents,
        formula_problems=FX.check_schema(form.schema or {}),
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Rules
# ─────────────────────────────────────────────────────────────────────────────

def _read_rule(request, rule, form):
    rule.name = (request.POST.get("name") or "").strip()[:150] or "Document"
    rule.description = (request.POST.get("description") or "").strip()[:300]
    rule.is_active = request.POST.get("is_active") == "1"
    rule.trigger = (request.POST.get("trigger")
                    if request.POST.get("trigger") in dict(FormDocumentRule.TRIGGER_CHOICES)
                    else FormDocumentRule.TRIGGER_COMPLETE)
    rule.node_id = (request.POST.get("node_id") or "").strip()[:40]
    rule.number_prefix = (request.POST.get("number_prefix") or "INV").strip().upper()[:16]
    rule.number_format = (request.POST.get("number_format") or "{prefix}-{year}-{sequence:04d}").strip()[:60]
    rule.send_when = (request.POST.get("send_when")
                      if request.POST.get("send_when") in dict(FormDocumentRule.SEND_CHOICES)
                      else FormDocumentRule.SEND_WITH_FINAL)
    rule.send_to_initiator = request.POST.get("send_to_initiator") == "1"
    rule.send_to_submitter = request.POST.get("send_to_submitter") == "1"
    rule.send_to_signers = request.POST.get("send_to_signers") == "1"
    rule.send_to_emails = [
        e.strip().lower() for e in (request.POST.get("send_to_emails") or "").replace(";", ",").split(",")
        if "@" in e
    ][:20]
    rule.send_to_email_fields = [
        k.strip() for k in (request.POST.get("send_to_email_fields") or "").split(",") if k.strip()
    ][:10]
    rule.email_subject = (request.POST.get("email_subject") or "").strip()[:200]
    rule.email_message = (request.POST.get("email_message") or "").strip()[:2000]
    rule.start_target_workflow = request.POST.get("start_target_workflow") == "1"

    target_id = request.POST.get("target_form")
    target = FormTemplate.objects.filter(pk=target_id or 0).first()
    if target is None:
        return "Choose which form the document is built from."
    rule.target_form = target

    field_map, problems = [], []
    for target_key, expr in zip(request.POST.getlist("map_target"), request.POST.getlist("map_expr")):
        target_key, expr = target_key.strip(), expr.strip()
        if not target_key or not expr:
            continue
        report = FX.describe(expr)
        if not report["ok"]:
            problems.append(f"{target_key}: {report['error']}")
            continue
        field_map.append({"target": target_key[:80], "expr": expr[:FX.MAX_EXPR]})
    rule.field_map = field_map[:100]

    table_map = []
    raw_tables = request.POST.get("table_map_json") or "[]"
    try:
        for entry in json.loads(raw_tables)[:10]:
            if not isinstance(entry, dict):
                continue
            target_key = str(entry.get("target") or "").strip()[:80]
            source_key = str(entry.get("from") or "").strip()[:80]
            if not target_key or not source_key:
                continue
            columns = {}
            for column, expr in (entry.get("columns") or {}).items():
                expr = str(expr or "").strip()
                if not expr:
                    continue
                report = FX.describe(expr)
                if not report["ok"]:
                    problems.append(f"{target_key}.{column}: {report['error']}")
                    continue
                columns[str(column)[:60]] = expr[:FX.MAX_EXPR]
            table_map.append({"target": target_key, "from": source_key, "columns": columns})
    except (TypeError, ValueError):
        problems.append("The table mapping could not be read.")
    rule.table_map = table_map

    return "; ".join(problems)


@login_required
@require_http_methods(["GET", "POST"])
def esign_document_rule_edit(request, pk, rule_id=None):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    form = _form_or_404(request, pk)
    rule = (get_object_or_404(FormDocumentRule, pk=rule_id, form=form)
            if rule_id else FormDocumentRule(form=form, created_by=request.user))

    if request.method == "POST":
        problem = _read_rule(request, rule, form)
        if problem:
            messages.error(request, problem)
        else:
            rule.save()
            messages.success(request, f"“{rule.name}” saved.")
            return redirect("accounts:esign_form_automation", pk=form.pk)

    steps = []
    if form.workflow_id:
        from . import workflow_engine_esign as E

        graph = E.clean_graph(form.workflow.graph)
        steps = [{"id": n["id"], "label": n.get("label") or n["type"]}
                 for n in graph.get("nodes") or []
                 if n.get("type") in ("approval", "review", "fill", "signature")]

    target = rule.target_form if rule.target_form_id else None
    return render(request, "accounts/esign/studio/form_document_rule.html", studio_context(
        request, "forms",
        form=form,
        rule=rule,
        steps=steps,
        targets=list(_target_choices(request, form)),
        source_fields=_schema_fields(form),
        target_fields=_schema_fields(target) if target else [],
        field_map=rule.field_map or [],
        table_map_json=json.dumps(rule.table_map or []),
        trigger_choices=FormDocumentRule.TRIGGER_CHOICES,
        send_choices=FormDocumentRule.SEND_CHOICES,
    ))


@login_required
@require_POST
def esign_document_rule_delete(request, pk, rule_id):
    form = _form_or_404(request, pk)
    rule = get_object_or_404(FormDocumentRule, pk=rule_id, form=form)
    name = rule.name
    rule.delete()
    messages.success(request, f"“{name}” removed. Documents already produced are kept.")
    return redirect("accounts:esign_form_automation", pk=form.pk)


@login_required
@require_GET
def esign_document_rule_fields(request, pk):
    """The questions on a chosen target form, for the mapping editor."""
    _form_or_404(request, pk)
    target = FormTemplate.objects.filter(pk=request.GET.get("target") or 0,
                                         created_by=request.user).first()
    if target is None:
        return JsonResponse({"ok": False, "fields": []}, status=404)
    return JsonResponse({"ok": True, "name": target.name, "fields": _schema_fields(target)})


@login_required
@require_POST
def esign_document_rule_preview(request, pk, rule_id):
    """What the rule would write, tried against the newest real submission."""
    form = _form_or_404(request, pk)
    rule = get_object_or_404(FormDocumentRule, pk=rule_id, form=form)
    submission = FormSubmission.objects.filter(form=form).order_by("-created_at").first()
    if submission is None:
        return JsonResponse({"ok": False, "error": "There are no submissions to try this on yet."})
    try:
        result = docgen_esign.preview(rule, submission)
    except Exception as exc:  # noqa: BLE001
        logger.exception("eSign Studio: preview failed for rule %s", rule.pk)
        return JsonResponse({"ok": False, "error": str(exc)[:200]})
    return JsonResponse({"ok": True, "reference": submission.reference,
                         "rows": result["rows"], "problems": result["problems"]})


# ─────────────────────────────────────────────────────────────────────────────
# The documents themselves
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def esign_document_generate(request, submission_pk):
    """Produce the manual documents for one submission, on request."""
    submission = get_object_or_404(FormSubmission, pk=submission_pk)
    if not can_view_submission(request.user, submission):
        raise Http404()
    made = docgen_esign.run_rules(submission, FormDocumentRule.TRIGGER_MANUAL,
                                  run=submission.runs.first(), request=request, actor=request.user)
    if not made:
        messages.info(request, "No rule on this form asks for a document on request.")
    else:
        ok = [d for d in made if d.status != GeneratedDocument.STATUS_FAILED]
        messages.success(request, f"{len(ok)} document(s) produced: "
                                  + ", ".join(d.number for d in ok if d.number))
    return redirect("accounts:esign_submission_detail", pk=submission.pk)


@login_required
@require_GET
def esign_document_download(request, document_id):
    document = get_object_or_404(
        GeneratedDocument.objects.select_related("source_submission", "rule"), pk=document_id
    )
    if not can_view_submission(request.user, document.source_submission):
        raise Http404()
    if not document.pdf:
        raise Http404()
    document.pdf.open("rb")
    response = FileResponse(document.pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{(document.number or "document")[:60]}.pdf"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
def esign_document_send(request, document_id):
    document = get_object_or_404(
        GeneratedDocument.objects.select_related("source_submission", "rule"), pk=document_id
    )
    if not can_view_submission(request.user, document.source_submission):
        raise Http404()
    sent = docgen_esign.deliver(document, request=request)
    messages.success(request, f"Sent to {sent} recipient(s)." if sent
                     else "Nobody to send it to — check the rule's recipients.")
    return redirect(request.META.get("HTTP_REFERER")
                    or reverse("accounts:esign_submission_detail", args=[document.source_submission_id]))
