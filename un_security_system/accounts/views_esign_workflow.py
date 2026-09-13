# accounts/views_esign_workflow.py
"""
UN PASS — eSign Studio: document workflows.

Designing a flow needs a login and the eSign entitlement. Acting on a task does
not: like an eSign signing link, the task link carries its own token, so an
approver outside the platform can still decide.
"""

import json
import logging

from django.contrib import messages
from django.core.exceptions import RequestDataTooBig, TooManyFieldsSent
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import workflow_engine_esign as E
from .models_esign_studio import DocumentWorkflow, FormTemplate, StudioFile, WorkflowRun, WorkflowTask
from .studio_common_esign import (
    UploadError,
    accepted_ext,
    asset_urls,
    can_edit_template,
    can_manage_run,
    can_use_template,
    can_view_run,
    directory,
    inline_pdf,
    json_body,
    read_people,
    shared_template_q,
    studio_context,
    studio_gate,
    upload_to_pdf,
    visible_runs,
)
from .studio_library_esign import FLOW_TEMPLATES, flow_template

from .workflow_portability_esign import FlowPackageError, dumps_workflow_package, loads_package
from .form_logic_esign import annotate_states

logger = logging.getLogger(__name__)


def _my_open_tasks(user):
    q = Q(user=user)
    if (user.email or "").strip():
        q |= Q(email__iexact=user.email.strip(), user__isnull=True)
    return (WorkflowTask.objects.filter(q, status=WorkflowTask.STATUS_PENDING,
                                        run__status__in=WorkflowRun.OPEN_STATUSES)
            .select_related("run", "run__initiator").order_by("due_at", "created_at"))


# ─────────────────────────────────────────────────────────────────────────────
# List
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_workflows(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    user = request.user
    tab = request.GET.get("tab") or "tasks"

    flows = (DocumentWorkflow.objects.filter(shared_template_q(user), is_active=True)
             .select_related("created_by", "form").annotate(run_total=Count("runs")))
    runs = visible_runs(user).select_related("initiator", "workflow")
    status = request.GET.get("status") or ""
    if status == "open":
        runs = runs.filter(status__in=WorkflowRun.OPEN_STATUSES)
    elif status:
        runs = runs.filter(status=status)
    q = (request.GET.get("q") or "").strip()
    if q:
        runs = runs.filter(Q(subject__icontains=q) | Q(reference__icontains=q) | Q(workflow_name__icontains=q))

    file_pk = request.GET.get("file")
    launch_file = StudioFile.objects.filter(pk=file_pk, owner=user).first() if file_pk else None
    tasks = list(_my_open_tasks(user)[:100])

    return render(request, "accounts/esign/studio/workflows.html", studio_context(
        request, "flows",
        tab=tab if tab in ("tasks", "runs", "flows") else "tasks",
        tasks=tasks,
        runs=runs[:200],
        my_flows=[f for f in flows if f.created_by_id == user.id],
        shared_flows=[f for f in flows if f.created_by_id != user.id],
        templates=FLOW_TEMPLATES,
        node_types=E.NODE_TYPES,
        launch_file=launch_file,
        status=status, q=q,
        run_statuses=WorkflowRun.STATUS_CHOICES,
        counts={
            "tasks": len(tasks),
            "open_runs": visible_runs(user).filter(status__in=WorkflowRun.OPEN_STATUSES).count(),
            "flows": len(flows),
        },
    ))


@login_required
@require_POST
def esign_workflow_new(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    tpl = flow_template(request.POST.get("template") or "blank")
    form = None
    if request.POST.get("form"):
        form = get_object_or_404(FormTemplate, pk=request.POST.get("form"), created_by=request.user)
    wf = DocumentWorkflow.objects.create(
        agency=agency, created_by=request.user, office_id=getattr(request.user, "country_office_id", None),
        name=(request.POST.get("name") or "").strip()[:150] or (f"{form.name} flow" if form else tpl["name"]),
        description=tpl["description"] if tpl["key"] != "blank" else "",
        graph=E.clean_graph(tpl["graph"]), form=form,
    )
    if form and request.POST.get("attach"):
        form.workflow = wf
        form.save(update_fields=["workflow", "updated_at"])
    return redirect("accounts:esign_workflow_designer", pk=wf.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Designer
# ─────────────────────────────────────────────────────────────────────────────

def _form_fields(form):
    if not form:
        return []
    from .form_pdf_esign import INPUT_TYPES

    return [{"key": el["key"], "label": el.get("label") or el["key"], "type": el["type"],
             "options": el.get("options") or []}
            for el in (form.schema or {}).get("elements") or [] if el["type"] in INPUT_TYPES]


def _form_sections(form):
    """
    The form's sections, so a Fill-in step can claim a whole one at a time:
    [{id, title, fill_by, fields:[label], overrides}].
    """
    from .form_pdf_esign import sections_of

    if not form:
        return []
    out = []
    for sec in sections_of(form.schema or {}):
        head = sec["heading"]
        out.append({"id": head["id"] if head else "", "title": head["text"] if head else "Before the first heading",
                    "fill_by": sec["fill_by"], "overrides": sec["overrides"],
                    "fields": [el.get("label") or el["key"] for el in sec["fields"]]})
    return out


def _rule_catalog(form, graph):
    """
    Everything a Condition in this flow can check: the form's answers, plus the
    computed values — table totals, days between dates, the requester, the run
    and the comments left at earlier steps.
    """
    from .esign_condition_engine import catalog

    if not form:
        return []
    steps = {n["id"]: n["label"] for n in (graph or {}).get("nodes") or []
             if n["type"] in ("approval", "review", "fill")}
    return catalog(form.schema or {}, workflow=True, node_labels=steps)


@login_required
@require_GET
def esign_workflow_designer(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    wf = get_object_or_404(DocumentWorkflow, pk=pk)
    if not can_edit_template(request.user, wf):
        if can_use_template(request.user, wf):
            messages.info(request, "Only the owner can change this flow. Duplicate it to make your own version.")
            return redirect(reverse("accounts:esign_workflows") + "?tab=flows")
        raise Http404()
    my_forms = FormTemplate.objects.filter(created_by=request.user).order_by("name")
    graph = E.clean_graph(wf.graph)
    return render(request, "accounts/esign/studio/designer.html", studio_context(
        request, "flows",
        workflow=wf,
        payload=json.dumps({
            "id": wf.pk,
            "name": wf.name,
            "description": wf.description,
            "graph": graph,
            "share_scope": wf.share_scope,
            "monitor_runs": wf.monitor_runs,
            "form_id": wf.form_id,
            "version": wf.version,
            "report": E.validate_graph(graph, wf.form.schema if wf.form_id else None),
        }),
        config_json=json.dumps({
            "node_types": E.NODE_TYPES,
            "port_labels": E.PORT_LABELS,
            "condition_ops": E.CONDITION_OPS,
            "assign_modes": E.ASSIGN_MODES,
            "directory": directory(request.user),
            "forms": [{"id": f.pk, "name": f.name, "fields": _form_fields(f),
                       "catalog": _rule_catalog(f, graph), "sections": _form_sections(f)} for f in my_forms],
            "templates": [{"key": t["key"], "name": t["name"], "icon": t["icon"], "description": t["description"],
                           "graph": E.clean_graph(t["graph"])} for t in FLOW_TEMPLATES],
            "save_url": reverse("accounts:esign_workflow_save", args=[wf.pk]),
            "section_step_url": reverse("accounts:esign_form_section_step", args=[wf.form_id]) if wf.form_id else "",
            "launch_url": reverse("accounts:esign_workflow_launch", args=[wf.pk]),
            "export_url": reverse("accounts:esign_workflow_export", args=[wf.pk]),
            "import_url": reverse("accounts:esign_workflow_import"),
        }),
    ))


@login_required
@require_POST
def esign_workflow_save(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return JsonResponse({"ok": False, "error": "eSign is not enabled."}, status=403)
    wf = get_object_or_404(DocumentWorkflow, pk=pk, created_by=request.user)
    body, problem = json_body(request, "flow")
    if problem:
        return problem
    if str(body.get("version") or "") not in ("", str(wf.version)):
        return JsonResponse({"ok": False, "conflict": True,
                             "error": "This flow was saved from another window. Reload to see the latest version."},
                            status=409)

    graph = E.clean_graph(body.get("graph"))
    form = None
    if body.get("form_id"):
        form = FormTemplate.objects.filter(pk=body["form_id"], created_by=request.user).first()

    wf.name = (body.get("name") or "").strip()[:150] or wf.name
    wf.description = (body.get("description") or "")[:2000]
    wf.graph = graph
    wf.form = form
    wf.share_scope = body.get("share_scope") if body.get("share_scope") in ("private", "office", "agency") else "private"
    wf.monitor_runs = bool(body.get("monitor_runs", True))
    wf.office_id = getattr(request.user, "country_office_id", None)
    wf.version += 1
    wf.save()
    return JsonResponse({"ok": True, "version": wf.version, "graph": graph,
                         "report": E.validate_graph(graph, form.schema if form else None),
                         "saved_at": timezone.localtime().strftime("%H:%M")})


@login_required
@require_POST
def esign_workflow_duplicate(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    src = get_object_or_404(DocumentWorkflow, pk=pk)
    if not can_use_template(request.user, src):
        raise Http404()
    copy = DocumentWorkflow.objects.create(
        agency=agency, created_by=request.user, office_id=getattr(request.user, "country_office_id", None),
        name=f"Copy of {src.name}"[:150], description=src.description, graph=src.graph,
        form=src.form if (src.form_id and src.form.created_by_id == request.user.id) else None,
    )
    messages.success(request, "Duplicated. This copy is yours to change.")
    return redirect("accounts:esign_workflow_designer", pk=copy.pk)


@login_required
@require_POST
def esign_workflow_delete(request, pk):
    wf = get_object_or_404(DocumentWorkflow, pk=pk, created_by=request.user)
    if wf.runs.exists():
        wf.is_active = False
        wf.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "Flow archived. Its runs and their records are kept.")
    else:
        wf.delete()
        messages.success(request, "Flow deleted.")
    return redirect(reverse("accounts:esign_workflows") + "?tab=flows")



@login_required
@require_GET
def esign_workflow_export(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    wf = get_object_or_404(DocumentWorkflow.objects.select_related("form"), pk=pk)
    if not can_use_template(request.user, wf):
        raise Http404()
    raw = dumps_workflow_package(
        wf, include_form=request.GET.get("form", "1") != "0"
    )
    resp = HttpResponse(raw, content_type="application/json; charset=utf-8")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in wf.name)[:70] or "flow"
    resp["Content-Disposition"] = f'attachment; filename="{safe}.unpassflow"'
    return resp


@login_required
@require_http_methods(["GET", "POST"])
def esign_workflow_import(request):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    if request.method == "GET":
        return render(
            request,
            "accounts/esign/studio/import_flow.html",
            studio_context(request, "flows"),
        )

    upload = request.FILES.get("package")
    if not upload:
        messages.error(request, "Choose a .unpassflow file.")
        return redirect("accounts:esign_workflow_import")
    try:
        package = loads_package(upload.read())
    except FlowPackageError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_workflow_import")

    imported_form = None
    if package.get("form"):
        f = package["form"]
        imported_form = FormTemplate.objects.create(
            agency=agency,
            created_by=request.user,
            office_id=getattr(request.user, "country_office_id", None),
            name=f["name"],
            description=f["description"],
            category=f["category"],
            schema=f["schema"],
            reference_prefix=f["reference_prefix"],
            submit_message=f["submit_message"],
            share_scope="private",
            is_published=False,
        )

    wf = DocumentWorkflow.objects.create(
        agency=agency,
        created_by=request.user,
        office_id=getattr(request.user, "country_office_id", None),
        name=package["name"],
        description=package["description"],
        graph=package["graph"],
        form=imported_form,
        share_scope="private",
        monitor_runs=package["monitor_runs"],
    )
    messages.success(
        request,
        "Flow imported. Review people, roles and conditions before using it.",
    )
    return redirect("accounts:esign_workflow_designer", pk=wf.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Launch
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET", "POST"])
def esign_workflow_launch(request, pk):
    agency, bounce = studio_gate(request)
    if bounce:
        return bounce
    wf = get_object_or_404(DocumentWorkflow, pk=pk, is_active=True)
    if not can_use_template(request.user, wf):
        raise Http404()

    linked_form = FormTemplate.objects.filter(workflow=wf).first() or wf.form
    if linked_form and linked_form.is_published and can_use_template(request.user, linked_form):
        messages.info(request, f"“{wf.name}” runs from the form “{linked_form.name}”. Fill it in to start.")
        return redirect("accounts:esign_form_fill", pk=linked_form.pk)

    graph = E.clean_graph(wf.graph)
    report = E.validate_graph(graph)
    slots = E.chosen_slots(graph)
    my_files = StudioFile.objects.filter(owner=request.user).select_related("current")[:60]
    preset_file = request.GET.get("file") or request.POST.get("studio_file") or ""

    def page(**extra):
        return render(request, "accounts/esign/studio/launch.html", studio_context(
            request, "flows", workflow=wf, graph_json=json.dumps(graph), report=report, slots=slots,
            my_files=my_files, preset_file=str(preset_file),
            directory_json=json.dumps(directory(request.user)),
            node_types_json=json.dumps(E.NODE_TYPES), port_labels_json=json.dumps(E.PORT_LABELS),
            accept_attr=",".join(accepted_ext()), **extra))

    if request.method == "GET":
        return page()

    P = request.POST
    subject = (P.get("subject") or "").strip()[:200]
    raw, name = None, ""
    try:
        if P.get("studio_file"):
            sf = get_object_or_404(StudioFile, pk=P["studio_file"], owner=request.user)
            raw, name = sf.read_bytes(), sf.download_name
        elif request.FILES.get("document"):
            raw, name = upload_to_pdf(request.FILES["document"])
        chosen = {}
        for s in slots:
            chosen[s["key"]] = read_people(P.get(f"slot_{s['key']}"))
        if not subject:
            subject = name.rsplit(".", 1)[0] if name else wf.name
        run = E.start_run(wf, request.user, agency=agency, subject=subject, message=(P.get("message") or "")[:2000],
                          pdf_bytes=raw, document_name=name, slots=chosen, request=request)
    except (UploadError, E.WorkflowError) as exc:
        messages.error(request, str(exc))
        return page(posted=P)
    messages.success(request, f"Started {run.reference}.")
    return redirect("accounts:esign_run_detail", pk=run.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Runs
# ─────────────────────────────────────────────────────────────────────────────

def _run_or_404(request, pk):
    run = get_object_or_404(WorkflowRun.objects.select_related("initiator", "workflow", "submission"), pk=pk)
    if not can_view_run(request.user, run):
        raise Http404()
    return run


def _node_states(run):
    """{node_id: state} for colouring the read-only map."""
    states = {}
    for t in run.tasks.all():
        cur = states.get(t.node_id)
        if t.is_open:
            states[t.node_id] = "active"
        elif cur != "active":
            states[t.node_id] = {"approved": "done", "done": "done", "rejected": "rejected",
                                 "returned": "returned"}.get(t.status, cur or "done")
    for e in run.events.filter(event__in=("step", "condition", "notified")).exclude(node_id=""):
        states.setdefault(e.node_id, "done")
    if run.status == WorkflowRun.STATUS_BLOCKED and run.context.get("blocked_node"):
        states[run.context["blocked_node"]] = "blocked"
    for n in run.graph.get("nodes") or []:
        if n["type"] == "start":
            states[n["id"]] = "done"
    return states


@login_required
@require_GET
def esign_run_detail(request, pk):
    run = _run_or_404(request, pk)
    manage = can_manage_run(request.user, run)
    tasks = list(run.tasks.select_related("user", "envelope").all())
    from .utils_esign import esign_brand

    summary = []
    if run.submission_id:
        from .form_pdf_esign import summary_rows

        summary = summary_rows(run.submission.schema, run.submission.values)
    blocked_node = run.node(run.context.get("blocked_node") or "") if run.status == WorkflowRun.STATUS_BLOCKED else None
    try:
        shown = _read_field(run.final_pdf) if run.final_pdf else E.run_pdf_bytes(run)
    except Exception:  # noqa: BLE001 - the page still works; the viewer fetches by URL
        logger.exception("eSign Studio: could not read run %s document for inline preview", run.pk)
        shown = None
    return render(request, "accounts/esign/studio/run_detail.html", studio_context(
        request, "flows",
        run=run, tasks=tasks, events=run.events.select_related("actor").all(),
        can_manage=manage and run.is_open, progress=run.progress(), summary=summary,
        my_task=next((t for t in tasks if t.is_open and t.status == WorkflowTask.STATUS_PENDING
                      and (t.user_id == request.user.id
                           or (not t.user_id and t.email and t.email.lower() == (request.user.email or "").lower()))), None),
        blocked_node=blocked_node,
        graph_json=json.dumps(run.graph), states_json=json.dumps(_node_states(run)),
        node_types_json=json.dumps(E.NODE_TYPES), port_labels_json=json.dumps(E.PORT_LABELS),
        directory_json=json.dumps(directory(request.user)) if manage else "[]",
        returned=run.context.get("returned") or {},
        pdf_inline=inline_pdf(shown),
    ))


def _read_field(handle):
    if not handle:
        return None
    handle.open("rb")
    try:
        return handle.read()
    finally:
        handle.close()


@login_required
@require_GET
@xframe_options_sameorigin          # lets the viewer fall back to the browser's own PDF viewer
def esign_run_pdf(request, pk, kind):
    run = _run_or_404(request, pk)
    if kind == "final":
        if not run.final_pdf:
            raise Http404()
        handle = run.final_pdf
        handle.open("rb")
        raw = handle.read()
        handle.close()
        name = f"{run.reference}-final.pdf"
    else:
        raw = E.run_pdf_bytes(run)
        name = f"{run.reference}.pdf"
    if not raw:
        raise Http404()
    if request.GET.get("download"):
        E.log_run(run, "downloaded", request=request, actor=request.user, note=f"Downloaded the {kind} PDF.")
    resp = HttpResponse(raw, content_type="application/pdf")
    resp["Content-Disposition"] = f'{"attachment" if request.GET.get("download") else "inline"}; filename="{name}"'
    resp["Cache-Control"] = "private, no-store"
    return resp


@login_required
@require_POST
def esign_run_cancel(request, pk):
    run = _run_or_404(request, pk)
    if not can_manage_run(request.user, run):
        raise Http404()
    try:
        E.cancel_run(run, request.user, reason=(request.POST.get("reason") or "").strip()[:300], request=request)
        messages.success(request, "Run cancelled. Anyone waiting has been told.")
    except E.WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("accounts:esign_run_detail", pk=run.pk)


@login_required
@require_POST
def esign_run_reassign(request, pk, task_id):
    run = _run_or_404(request, pk)
    if not can_manage_run(request.user, run):
        raise Http404()
    task = get_object_or_404(WorkflowTask, pk=task_id, run=run)
    people = read_people(request.POST.get("people"))
    try:
        new = E.reassign_task(task, people[0] if people else None, request.user, request=request)
        messages.success(request, f"“{task.node_label}” is now with {new.name}.")
    except E.WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("accounts:esign_run_detail", pk=run.pk)


@login_required
@require_POST
def esign_run_retry(request, pk):
    run = _run_or_404(request, pk)
    if not can_manage_run(request.user, run):
        raise Http404()
    try:
        E.retry_blocked(run, read_people(request.POST.get("people")), request.user, request=request)
        run.refresh_from_db()
        if run.status == WorkflowRun.STATUS_BLOCKED:
            messages.error(request, "It stopped again: " + run.block_reason)
        else:
            messages.success(request, "The step was retried and the run is moving again.")
    except E.WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("accounts:esign_run_detail", pk=run.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Task page (token)
# ─────────────────────────────────────────────────────────────────────────────

def _task_or_404(token):
    return get_object_or_404(WorkflowTask.objects.select_related("run", "run__initiator", "run__submission",
                                                                 "envelope"), token=token)


@require_http_methods(["GET", "POST"])
def esign_wf_task(request, token):
    task = _task_or_404(token)
    run = task.run
    node = run.node(task.node_id) or {}
    cfg = node.get("config") or {}
    authed = request.user.is_authenticated
    base_template = "base.html" if authed else "accounts/esign/studio/public_base.html"

    from .form_pdf_esign import read_values, summary_rows

    sub = run.submission if run.submission_id else None
    scope = None
    if sub:
        if task.kind == WorkflowTask.KIND_FILL:
            scope = task.node_id
        elif task.kind == WorkflowTask.KIND_RESUBMIT and not run.context.get("frozen"):
            scope = "submitter"

    errors = {}
    values = dict(sub.values) if sub else {}

    if request.method == "POST":
        try:
            request.POST
        except (TooManyFieldsSent, RequestDataTooBig):
            messages.error(request, "Your answers were too large for the server to accept in one go, so nothing was "
                                    "saved. Please shorten the longest tables or texts and try again.")
            return redirect("accounts:esign_wf_task", token=token)
        action = request.POST.get("action") or ""
        comment = request.POST.get("comment") or ""
        actor = request.user if authed else None
        try:
            posted_values = None
            if scope:
                posted_values, errors = read_values(sub.schema, request.POST, existing={}, scope=scope)
                values.update(posted_values)
                if errors and action in ("submit", "resubmit"):
                    raise E.WorkflowError("Please fix the highlighted fields.")
            if task.kind == WorkflowTask.KIND_RESUBMIT and action == "resubmit":
                if sub and scope and posted_values is not None:
                    sub.values = values
                    if sub.pdf:
                        sub.pdf.delete(save=False)
                    sub.save(update_fields=["values", "pdf", "updated_at"])
                elif request.FILES.get("document"):
                    raw, name = upload_to_pdf(request.FILES["document"])
                    E.set_run_document(run, raw, name)
                    E.log_run(run, "step", request=request, actor=actor, actor_name=task.name,
                              note=f"Replaced the document with {name}.")
            E.decide(task, action, request=request, comment=comment, values=posted_values, actor=actor)
            return redirect("accounts:esign_wf_task", token=token)
        except (E.WorkflowError, UploadError) as exc:
            messages.error(request, str(exc))

    # Embed the document unless the page shows an editable form instead of it.
    pdf_inline = ""
    if not (sub and task.is_open and scope):
        try:
            pdf_inline = inline_pdf(E.run_pdf_bytes(run))
        except Exception:  # noqa: BLE001 - the viewer falls back to fetching by URL
            logger.exception("eSign Studio: could not inline task %s document", task.pk)

    if request.method == "GET" and task.is_open:
        seen_key = f"esign_wf_seen_{task.pk}"
        if not request.session.get(seen_key):
            request.session[seen_key] = True
            E.log_run(run, "viewed", request=request, task=task,
                      actor=request.user if authed else None, actor_name=task.name,
                      note=f"{task.name} opened “{task.node_label}”.")

    return render(request, "accounts/esign/studio/task.html", {
        **asset_urls(),
        "base_template": base_template,
        "task": task, "run": run, "node": node, "cfg": cfg,
        "sub": sub,
        "schema": annotate_states(sub.schema, values, scope=scope) if sub else None,
        "values": values, "errors": errors, "scope": scope,
        "summary": summary_rows(sub.schema, sub.values) if sub else [],
        "actions": E.ACTIONS.get(task.kind, ()),
        "allow_return": cfg.get("allow_return", True),
        "history": run.tasks.exclude(pk=task.pk).exclude(status__in=(WorkflowTask.STATUS_PENDING,)).order_by("decided_at", "created_at"),
        "returned": run.context.get("returned") or {},
        "can_open_run": authed and can_view_run(request.user, run),
        "prepare_url": reverse("accounts:esign_prepare", args=[task.envelope_id]) if task.envelope_id else "",
        "studio_section": "flows",
        "pdf_inline": pdf_inline,
    })


@require_GET
@xframe_options_sameorigin          # lets the viewer fall back to the browser's own PDF viewer
def esign_wf_task_pdf(request, token):
    task = _task_or_404(token)
    raw = E.run_pdf_bytes(task.run)
    if not raw:
        raise Http404()
    resp = HttpResponse(raw, content_type="application/pdf")
    disposition = "attachment" if request.GET.get("download") else "inline"
    resp["Content-Disposition"] = f'{disposition}; filename="{task.run.reference}.pdf"'
    resp["Cache-Control"] = "private, no-store"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp
