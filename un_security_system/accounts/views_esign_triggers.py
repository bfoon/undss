# accounts/views_esign_triggers.py
"""
UN PASS — the screens behind "when a module finishes, start this".

  /esign/automation/triggers/                 what is set up, and what it did
  /esign/automation/triggers/new/             add one
  /esign/automation/triggers/<id>/            change one
  /esign/automation/triggers/<id>/preview/    try it against a real record
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from . import module_events, trigger_engine_esign
from . import form_formula_esign as FX
from .models_esign_studio import DocumentWorkflow, FormTemplate
from .models_esign_triggers import ModuleTrigger, ModuleTriggerLog
from .studio_common_esign import studio_context, studio_gate

logger = logging.getLogger(__name__)


def _may_manage(user):
    return bool(user.is_superuser or getattr(user, "role", "") in ("ict_focal", "admin", "manager"))


def _guard(request):
    if not _may_manage(request.user):
        raise Http404()


@login_required
@require_GET
def esign_trigger_list(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    _guard(request)

    triggers = (ModuleTrigger.objects
                .select_related("target_form", "target_workflow", "agency", "assignee")
                .order_by("event_code", "order", "id"))
    logs = (ModuleTriggerLog.objects
            .select_related("trigger", "submission", "assigned_to")[:40])

    return render(request, "accounts/esign/studio/trigger_list.html", studio_context(
        request, "forms",
        triggers=triggers,
        logs=logs,
        events_by_module=module_events.grouped(),
        event_count=len(module_events.all_events()),
    ))


def _read(request, trigger):
    trigger.name = (request.POST.get("name") or "").strip()[:150] or "Follow-on form"
    trigger.description = (request.POST.get("description") or "").strip()[:300]
    trigger.is_active = request.POST.get("is_active") == "1"
    trigger.notify = request.POST.get("notify") == "1"
    trigger.once_per_record = request.POST.get("once_per_record") == "1"
    trigger.email_subject = (request.POST.get("email_subject") or "").strip()[:200]
    trigger.email_message = (request.POST.get("email_message") or "").strip()[:2000]

    event_code = (request.POST.get("event_code") or "").strip()
    if module_events.get(event_code) is None:
        return "Choose the moment this should wait for."
    trigger.event_code = event_code

    trigger.action = (request.POST.get("action")
                      if request.POST.get("action") in dict(ModuleTrigger.ACTION_CHOICES)
                      else ModuleTrigger.ACTION_DRAFT)
    trigger.assign_to = (request.POST.get("assign_to")
                         if request.POST.get("assign_to") in dict(ModuleTrigger.WHO_CHOICES)
                         else ModuleTrigger.WHO_REQUESTER)

    form = FormTemplate.objects.filter(pk=request.POST.get("target_form") or 0).first()
    if form is None:
        return "Choose the form to start."
    trigger.target_form = form

    trigger.target_workflow = DocumentWorkflow.objects.filter(
        pk=request.POST.get("target_workflow") or 0
    ).first()

    from .models import Agency, User

    trigger.agency = Agency.objects.filter(pk=request.POST.get("agency") or 0).first()
    trigger.assignee = (User.objects.filter(pk=request.POST.get("assignee") or 0).first()
                        if trigger.assign_to == ModuleTrigger.WHO_FIXED else None)
    if trigger.assign_to == ModuleTrigger.WHO_FIXED and trigger.assignee is None:
        return "Name the person this should go to."

    mapping, problems = [], []
    for target, expr in zip(request.POST.getlist("map_target"), request.POST.getlist("map_expr")):
        target, expr = target.strip(), expr.strip()
        if not target or not expr:
            continue
        report = FX.describe(expr)
        if not report["ok"]:
            problems.append(f"{target}: {report['error']}")
            continue
        mapping.append({"target": target[:80], "expr": expr[:FX.MAX_EXPR]})
    trigger.field_map = mapping[:80]

    conditions = []
    for token, op, value in zip(request.POST.getlist("cond_token"),
                                request.POST.getlist("cond_op"),
                                request.POST.getlist("cond_value")):
        token = token.strip()
        if not token:
            continue
        conditions.append({
            "token": token[:80],
            "op": op if op in trigger_engine_esign.OPS else "eq",
            "value": value.strip()[:200],
        })
    trigger.conditions = conditions[:10]

    return "; ".join(problems)


@login_required
@require_http_methods(["GET", "POST"])
def esign_trigger_edit(request, pk=None):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    _guard(request)

    trigger = (get_object_or_404(ModuleTrigger, pk=pk) if pk
               else ModuleTrigger(created_by=request.user))

    if request.method == "POST":
        problem = _read(request, trigger)
        if problem:
            messages.error(request, problem)
        else:
            trigger.save()
            messages.success(request, f"“{trigger.name}” saved.")
            return redirect("accounts:esign_trigger_list")

    from .models import Agency, User

    event = trigger.event
    return render(request, "accounts/esign/studio/trigger_edit.html", studio_context(
        request, "forms",
        trigger=trigger,
        events_by_module=module_events.grouped(),
        event=event,
        tokens=event.token_dicts() if event else [],
        tokens_json=event.token_dicts() if event else [],
        field_map=trigger.field_map or [],
        conditions=trigger.conditions or [],
        forms=FormTemplate.objects.order_by("name").values("pk", "name"),
        workflows=DocumentWorkflow.objects.filter(is_active=True).order_by("name").values("pk", "name"),
        agencies=Agency.objects.order_by("name").values("pk", "name"),
        people=User.objects.filter(is_active=True).order_by("first_name", "username")[:400],
        action_choices=ModuleTrigger.ACTION_CHOICES,
        who_choices=ModuleTrigger.WHO_CHOICES,
        op_labels=trigger_engine_esign.OP_LABELS,
    ))


@login_required
@require_GET
def esign_trigger_event_tokens(request):
    """What a chosen event can hand over — for the mapping editor."""
    _guard(request)
    event = module_events.get(request.GET.get("event") or "")
    if event is None:
        return JsonResponse({"ok": False, "tokens": []}, status=404)
    return JsonResponse({
        "ok": True,
        "label": f"{event.module} — {event.label}",
        "description": event.description,
        "model": event.model,
        "people": sorted(event.people.keys()),
        "tokens": event.token_dicts(),
    })


@login_required
@require_GET
def esign_form_fields(request, pk):
    """The questions on the target form — for the mapping editor."""
    _guard(request)
    form = get_object_or_404(FormTemplate, pk=pk)
    fields = []
    for element in (form.schema or {}).get("elements") or []:
        if not element.get("key"):
            continue
        fields.append({
            "key": element["key"],
            "label": element.get("label") or element["key"],
            "type": element.get("type"),
            "columns": [{"key": c.get("key"), "label": c.get("label")}
                        for c in element.get("columns") or []],
        })
    return JsonResponse({"ok": True, "name": form.name, "fields": fields})


@login_required
@require_POST
def esign_trigger_preview(request, pk):
    _guard(request)
    trigger = get_object_or_404(ModuleTrigger, pk=pk)
    try:
        return JsonResponse(trigger_engine_esign.preview(trigger))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Trigger preview failed for %s", pk)
        return JsonResponse({"ok": False, "error": str(exc)[:200]})


@login_required
@require_POST
def esign_trigger_delete(request, pk):
    _guard(request)
    trigger = get_object_or_404(ModuleTrigger, pk=pk)
    name = trigger.name
    trigger.delete()
    messages.success(request, f"“{name}” removed. Anything it already started is kept.")
    return redirect("accounts:esign_trigger_list")
