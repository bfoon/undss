# vehicles/views_mailflow.py
"""
Packages & Mailroom ⇄ Flow Studio.

The two log pages are re-implemented here (same URL names as before) so the
flow picker can offer three kinds of route for a package:

    classic:<id>   a classic PackageFlowTemplate — unchanged behaviour
    studio:<id>    a Flow Studio flow chosen by hand
    auto           "let the mail rules decide"

Everything else on this page is the Mail flows hub for ICT focal points: the
docket, the rules, starter flows, adopting an existing Flow Studio flow, and a
test bench that shows which flow a package would take.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import package_studio as PS
from .forms import PackageLogForm, PackageOutgoingLogForm
from .forms_mailflow import MailRouteForm
from .mail_docket import create_starter_flow, docket_for, ensure_docket, missing_keys
from .models import Package, PackageEvent, PackageFlowTemplate, PackageStepLog
from .models_mailflow import MailDocket, MailRoute, PackageStudioLink
from .package_access import can_log_incoming_package, get_visible_package_or_404
from .views import _generate_tracking_code, _is_ict_focal, _user_agency

logger = logging.getLogger(__name__)

MAX_SCAN_BYTES = 15 * 1024 * 1024
SCAN_EXTS = ("pdf", "png", "jpg", "jpeg", "webp")


# ─────────────────────────────────────────────────────────────────────────────
# Log pages
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def package_log_new(request):
    if not can_log_incoming_package(request.user):
        messages.error(request, "Only Reception or Registry staff can log incoming packages.")
        return redirect("vehicles:package_list")
    return _log_package(request, "incoming")


@login_required
def package_log_outgoing(request):
    return _log_package(request, "outgoing")


def _flow_cards(flows):
    """Studio flows with the human steps the card shows."""
    cards = []
    for wf in flows:
        cards.append({
            "wf": wf,
            "steps": [n.get("label") for n in wf.step_summary()][:6],
            "on_docket": bool(wf.form_id and MailDocket.objects.filter(form_id=wf.form_id).exists()),
        })
    return cards


def _directory_emails(user):
    try:
        from accounts.studio_common_esign import directory

        return [p for p in directory(user, limit=600) if p.get("email")]
    except Exception:  # noqa: BLE001
        return []


def _valid_scan(upload):
    if not upload:
        return None, ""
    ext = (upload.name.rsplit(".", 1)[-1] if "." in upload.name else "").lower()
    if ext not in SCAN_EXTS:
        return None, "The scan must be a PDF or an image (PNG, JPG, WebP)."
    if upload.size > MAX_SCAN_BYTES:
        return None, "The scan is larger than 15 MB."
    return upload, ""


def _log_package(request, direction):
    user = request.user
    outgoing = direction == "outgoing"
    agency = _user_agency(user)

    flow_templates = (
        PackageFlowTemplate.objects.filter(is_active=True, agency=agency, direction=direction)
        .prefetch_related("steps")
    ) if agency else PackageFlowTemplate.objects.none()

    studio_on = PS.studio_enabled(user)
    studio_flows = PS.studio_flows_for(user, direction) if studio_on else []
    routes = list(PS.active_routes(agency, direction)) if studio_on else []

    initial = None
    if outgoing:
        initial = {
            "sender_name": user.get_full_name() or user.username,
            "sender_org": getattr(getattr(user, "unit", None), "name", ""),
            "sender_contact": getattr(user, "phone", ""),
            "sender_email": user.email,
            "sender_type": "individual",
        }
    form_cls = PackageOutgoingLogForm if outgoing else PackageLogForm
    form = form_cls(request.POST or None, initial=initial)

    choice = (request.POST.get("flow_choice") or "").strip()
    if not choice and request.POST.get("flow_template_id"):
        choice = f"classic:{request.POST['flow_template_id']}"
    if request.method != "POST":
        if routes:
            choice = "auto"
        elif studio_flows:
            choice = f"studio:{studio_flows[0].pk}"
        elif flow_templates:
            choice = f"classic:{flow_templates[0].pk}"

    flow_error = ""

    if request.method == "POST" and form.is_valid():
        if choice == "auto" or choice.startswith("studio:"):
            scan, scan_error = _valid_scan(request.FILES.get("mail_scan"))
            if scan_error:
                flow_error = scan_error
            elif not studio_on:
                flow_error = "Flow Studio mail flows are not switched on for your office."
            else:
                try:
                    with transaction.atomic():
                        package = _save_package(form, user, direction, default_status="logged")
                        link = _start_studio(request, package, choice, studio_flows, scan)
                except PS.MailFlowError as exc:
                    flow_error = str(exc)
                else:
                    how = {"rule": f" — picked by the rule “{link.route.name}”" if link.route else "",
                           "fallback": " — no rule matched, so the fallback flow was used"}.get(link.picked_by, "")
                    messages.success(
                        request,
                        f"{'Outgoing item' if outgoing else 'Package'} {package.tracking_code} logged and "
                        f"started on “{link.workflow.name}”{how}.",
                    )
                    return redirect("vehicles:package_detail", pk=package.pk)
        else:
            package = _save_package(form, user, direction,
                                    default_status="registered" if outgoing else "logged")
            _start_classic(package, choice, agency, direction, user, form)
            messages.success(
                request,
                f"Outgoing item {package.tracking_code} registered." if outgoing
                else f"Package {package.tracking_code} logged.",
            )
            return redirect("vehicles:package_detail", pk=package.pk)

    posted_slots = {k[6:]: v for k, v in request.POST.items() if k.startswith("slot__")}
    template = "vehicles/packages/package_log_outgoing.html" if outgoing else "vehicles/packages/package_log_new.html"
    return render(request, template, {
        "form": form,
        "flow_templates": flow_templates,
        "user_agency": agency,
        "direction": direction,
        "studio_on": studio_on,
        "studio_cards": _flow_cards(studio_flows),
        "mail_routes": routes,
        "choice": choice,
        "flow_error": flow_error,
        "posted_slots": posted_slots,
        "directory": _directory_emails(user) if studio_on else [],
        "plan_url": reverse("vehicles:package_mailflow_plan"),
        "can_manage_mailflows": studio_on and _is_ict_focal(user),
    })


def _save_package(form, user, direction, default_status):
    package = form.save(commit=False)
    package.logged_by = user
    package.tracking_code = _generate_tracking_code()
    package.direction = direction
    if direction == "outgoing":
        package.sender_type = "individual"
        if not package.dest_focal_email:
            package.dest_focal_email = getattr(package, "recipient_email", "") or ""
    package.status = default_status
    package.save()
    return package


def _start_classic(package, choice, agency, direction, user, form):
    """The original classic behaviour, kept byte-for-byte in spirit."""
    template_id = choice.split(":", 1)[1] if choice.startswith("classic:") else ""
    if template_id:
        tmpl = PackageFlowTemplate.objects.filter(
            pk=template_id, is_active=True, agency=agency, direction=direction,
        ).first()
        if tmpl:
            first_step = tmpl.first_step
            package.flow_template = tmpl
            if first_step:
                package.current_step = first_step
                package.status = first_step.status_code
            package.save(update_fields=["flow_template", "current_step", "status", "last_update"])

    if package.current_step:
        PackageStepLog.objects.create(
            package=package,
            step=package.current_step,
            step_name=package.current_step.name,
            step_order=package.current_step.order,
            performed_by=user,
            note=form.cleaned_data.get("notes", ""),
        )


def _start_studio(request, package, choice, studio_flows, scan):
    user = request.user
    agency = PS.actor_agency(user)
    facts = PS.package_facts(package)
    route, picked = None, "manual"

    if choice == "auto":
        route, picked = PS.pick_route(agency, package.direction, facts)
        if route is None:
            raise PS.MailFlowError("No mail rule matches this item and there is no fallback. "
                                   "Pick a flow by hand, or ask your ICT focal point to add a fallback rule.")
        workflow = route.workflow
    else:
        try:
            wf_id = int(choice.split(":", 1)[1])
        except (IndexError, ValueError):
            raise PS.MailFlowError("Choose a flow for this item.")
        workflow = next((wf for wf in studio_flows if wf.pk == wf_id), None)
        if workflow is None:
            raise PS.MailFlowError("That flow isn't available for this kind of item.")

    plan = PS.plan_slots(workflow, facts, user)
    chosen, missing = PS.slots_from_post(request.POST, plan)
    if missing:
        raise PS.MailFlowError(f"“{workflow.name}” needs someone for: {', '.join(missing)}.")

    link = PS.start_mail_run(package, workflow, user, slots=chosen, route=route,
                             picked_by=picked, scan=scan, request=request)
    PackageEvent.objects.create(
        package=package, status="logged", who=user,
        note=f"Started on Flow Studio “{workflow.name}” ({link.run.reference})"[:255],
    )
    return link


# ─────────────────────────────────────────────────────────────────────────────
# Live plan for the log form and the test bench
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def package_mailflow_plan(request):
    user = request.user
    direction = "outgoing" if request.GET.get("direction") == "outgoing" else "incoming"
    choice = (request.GET.get("choice") or "auto").strip()
    facts = PS.package_facts(request.GET)
    facts["direction"] = direction
    if direction == "outgoing":
        facts["sender_type"] = "individual"

    agency = PS.actor_agency(user)
    workflow, rule = None, None
    if choice == "auto":
        route, picked = PS.pick_route(agency, direction, facts)
        if route:
            workflow = route.workflow
            rule = {"name": route.name, "picked": picked, "criteria": route.criteria_summary()}
    elif choice.startswith("studio:"):
        try:
            wf_id = int(choice.split(":", 1)[1])
        except (IndexError, ValueError):
            wf_id = None
        workflow = next((wf for wf in PS.studio_flows_for(user, direction) if wf.pk == wf_id), None)

    if workflow is None:
        return JsonResponse({
            "ok": True, "flow": None, "rule": None, "slots": [],
            "message": ("No rule matches yet — keep filling in the details, or pick a flow by hand."
                        if choice == "auto" else "Choose a flow."),
        })

    plan = PS.plan_slots(workflow, facts, user)
    return JsonResponse({
        "ok": True,
        "flow": {"id": workflow.pk, "name": workflow.name,
                 "steps": [n.get("label") for n in workflow.step_summary()][:8]},
        "rule": rule,
        "slots": [{
            "key": s["key"], "label": s["label"], "steps": s["steps"], "signs": s["signs"],
            "auto": bool(s["people"]), "how": s["how"],
            "people": [p.get("name") or p.get("email") for p in s["people"]][:6],
            "more": max(0, len(s["people"]) - 6),
        } for s in plan],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Docket and scan on the package page
# ─────────────────────────────────────────────────────────────────────────────

def _read(field):
    field.open("rb")
    try:
        return field.read()
    finally:
        field.close()


@login_required
def package_mail_docket(request, pk):
    from accounts.workflow_engine_esign import run_pdf_bytes

    package = get_visible_package_or_404(request.user, pk)
    link = PS.get_link(package)
    if link is None or link.run is None:
        raise Http404()
    run = link.run
    try:
        raw = _read(run.final_pdf) if run.final_pdf else run_pdf_bytes(run)
    except Exception:  # noqa: BLE001
        logger.exception("Mail flow: docket PDF failed for package %s", package.pk)
        raw = b""
    if not raw:
        messages.error(request, "The docket couldn't be produced right now. Try again in a moment.")
        return redirect("vehicles:package_detail", pk=pk)
    resp = HttpResponse(raw, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{package.tracking_code}-docket.pdf"'
    return resp


@login_required
def package_mail_scan(request, pk):
    package = get_visible_package_or_404(request.user, pk)
    link = PS.get_link(package)
    if link is None or not link.scan:
        raise Http404()
    name = link.scan.name.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower()
    ctype = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
             "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "application/octet-stream")
    resp = HttpResponse(_read(link.scan), content_type=ctype)
    resp["Content-Disposition"] = f'inline; filename="{name}"'
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Mail flows hub (ICT focal points)
# ─────────────────────────────────────────────────────────────────────────────

def _manager_gate(request):
    if not _is_ict_focal(request.user):
        messages.error(request, "Only ICT Focal Points can manage mail flows.")
        return None, redirect("vehicles:package_list")
    agency = _user_agency(request.user)
    if agency is None:
        messages.error(request, "Your account needs an agency before you can set up mail flows.")
        return None, redirect("vehicles:package_list")
    return agency, None


def _route_view(route):
    plan = []
    if route.workflow_id:
        from accounts.workflow_engine_esign import chosen_slots, clean_graph

        for s in chosen_slots(clean_graph(route.workflow.graph)):
            plan.append({"label": s["label"], "source": PS.slot_source(s["label"])})
    return {"route": route, "criteria": route.criteria_summary(), "slots": plan,
            "packages": getattr(route, "n_packages", 0)}


@login_required
def package_mailflow_home(request):
    agency, bounce = _manager_gate(request)
    if bounce:
        return bounce
    user = request.user
    from accounts.models_esign_studio import DocumentWorkflow, WorkflowRun

    dockets = {}
    for direction in ("incoming", "outgoing"):
        d = docket_for(agency, direction)
        dockets[direction] = {
            "docket": d,
            "missing": missing_keys(d.form.schema, direction) if d else [],
            "can_design": bool(d and d.form.created_by_id == user.pk),
            "count": d.form.submissions.count() if d else 0,
        }

    routes = (MailRoute.objects.filter(agency=agency).select_related("workflow")
              .annotate(n_packages=Count("packages")))
    by_direction = {"incoming": [], "outgoing": []}
    for r in routes:
        by_direction[r.direction].append(_route_view(r))

    docket_form_ids = list(MailDocket.objects.filter(agency=agency).values_list("form_id", flat=True))
    my_flows = []
    for wf in DocumentWorkflow.objects.filter(created_by=user, agency=agency, is_active=True).order_by("-updated_at")[:30]:
        my_flows.append({
            "wf": wf,
            "on_docket": wf.form_id in docket_form_ids,
            "rules": sum(1 for r in routes if r.workflow_id == wf.pk),
        })

    since = timezone.now() - timezone.timedelta(days=30)
    links = PackageStudioLink.objects.filter(package__logged_by__agency=agency, created_at__gte=since)
    open_links = (PackageStudioLink.objects.select_related("package", "run")
                  .filter(package__logged_by__agency=agency, run__status__in=WorkflowRun.OPEN_STATUSES))
    pulse = (open_links.values("stage").annotate(n=Count("pk")).order_by("-n")[:6])
    attention = list(open_links.filter(run__status=WorkflowRun.STATUS_BLOCKED).order_by("-created_at")[:5])

    return render(request, "vehicles/packages/mailflow_home.html", {
        "agency": agency,
        "dockets": dockets,
        "routes": by_direction,
        "my_flows": my_flows,
        "stats": {
            "started": links.count(),
            "open": open_links.count(),
            "by_rule": links.filter(picked_by__in=("rule", "fallback")).count(),
            "attention": open_links.filter(run__status=WorkflowRun.STATUS_BLOCKED).count(),
        },
        "pulse": pulse,
        "attention": attention,
        "plan_url": reverse("vehicles:package_mailflow_plan"),
        "sender_types": Package.SENDER_TYPES,
    })


@login_required
@require_POST
def package_mailflow_docket(request, direction):
    agency, bounce = _manager_gate(request)
    if bounce:
        return bounce
    if direction not in ("incoming", "outgoing"):
        raise Http404()
    docket = ensure_docket(agency, direction, request.user)
    if docket.form.created_by_id == request.user.pk:
        return redirect("accounts:esign_form_designer", pk=docket.form_id)
    messages.info(request, f"The {direction} docket belongs to "
                           f"{docket.created_by.get_full_name() if docket.created_by else 'another focal point'}; "
                           "they can restyle it in the Form designer.")
    return redirect("vehicles:package_mailflow_home")


@login_required
@require_POST
def package_mailflow_starter(request, direction):
    agency, bounce = _manager_gate(request)
    if bounce:
        return bounce
    if direction not in ("incoming", "outgoing"):
        raise Http404()
    wf = create_starter_flow(agency, direction, request.user)
    if not MailRoute.objects.filter(agency=agency, direction=direction, is_fallback=True).exists():
        MailRoute.objects.create(
            agency=agency, direction=direction, workflow=wf, name=f"All other {direction} mail",
            priority=999, is_fallback=True, created_by=request.user,
        )
        messages.success(request, f"“{wf.name}” created and set as the {direction} fallback. "
                                  "Shape it in Flow Studio — every change applies to new packages.")
    else:
        messages.success(request, f"“{wf.name}” created. Add a mail rule to send packages to it.")
    return redirect("accounts:esign_workflow_designer", pk=wf.pk)


@login_required
@require_POST
def package_mailflow_adopt(request, pk):
    """Use one of my existing Flow Studio flows as a mail flow: bind it to the docket."""
    agency, bounce = _manager_gate(request)
    if bounce:
        return bounce
    from accounts.models_esign_studio import DocumentWorkflow

    wf = get_object_or_404(DocumentWorkflow, pk=pk, created_by=request.user, agency=agency, is_active=True)
    direction = "outgoing" if request.POST.get("direction") == "outgoing" else "incoming"
    docket = ensure_docket(agency, direction, request.user)

    if wf.form_id and wf.form_id != docket.form_id and not MailDocket.objects.filter(form_id=wf.form_id).exists():
        messages.warning(request, f"“{wf.name}” was designed around the form “{wf.form.name}”. "
                                  "It now runs on the mail docket — check its conditions and fill-in steps.")
    wf.form = docket.form
    if wf.share_scope == "private":
        wf.share_scope = "office" if getattr(request.user, "country_office_id", None) else "agency"
    wf.save(update_fields=["form", "share_scope", "updated_at"])
    messages.success(request, f"“{wf.name}” now runs on the {direction} mail docket and is shared with the "
                              "mailroom. Add a rule to send packages to it.")
    return redirect(f"{reverse('vehicles:package_mailflow_route_new')}?workflow={wf.pk}&direction={direction}")


def _route_form_page(request, form, agency, route=None):
    return render(request, "vehicles/packages/mailflow_route_form.html", {
        "form": form, "route": route, "agency": agency,
        "warnings": getattr(form, "docket_warnings", []),
        "plan_url": reverse("vehicles:package_mailflow_plan"),
    })


@login_required
def package_mailflow_route_new(request):
    agency, bounce = _manager_gate(request)
    if bounce:
        return bounce
    initial = {"direction": request.GET.get("direction") or "incoming", "priority": 100, "is_active": True}
    if request.GET.get("workflow"):
        initial["workflow"] = request.GET["workflow"]
    form = MailRouteForm(request.POST or None, initial=initial, agency=agency, user=request.user)
    if request.method == "POST" and form.is_valid():
        route = form.save(commit=False)
        route.agency = agency
        route.created_by = request.user
        route.save()
        messages.success(request, f"Mail rule “{route.name}” saved.")
        for w in getattr(form, "docket_warnings", [])[:2]:
            messages.warning(request, f"Flow check: {w}")
        return redirect("vehicles:package_mailflow_home")
    return _route_form_page(request, form, agency)


@login_required
def package_mailflow_route_edit(request, pk):
    agency, bounce = _manager_gate(request)
    if bounce:
        return bounce
    route = get_object_or_404(MailRoute, pk=pk, agency=agency)
    form = MailRouteForm(request.POST or None, instance=route, agency=agency, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Mail rule “{route.name}” updated.")
        return redirect("vehicles:package_mailflow_home")
    return _route_form_page(request, form, agency, route)


@login_required
@require_POST
def package_mailflow_route_toggle(request, pk):
    agency, bounce = _manager_gate(request)
    if bounce:
        return bounce
    route = get_object_or_404(MailRoute, pk=pk, agency=agency)
    route.is_active = not route.is_active
    route.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"“{route.name}” {'switched on' if route.is_active else 'paused'}.")
    return redirect("vehicles:package_mailflow_home")


@login_required
@require_POST
def package_mailflow_route_delete(request, pk):
    agency, bounce = _manager_gate(request)
    if bounce:
        return bounce
    route = get_object_or_404(MailRoute, pk=pk, agency=agency)
    name = route.name
    route.delete()
    messages.success(request, f"Mail rule “{name}” deleted. Packages already on its flow carry on.")
    return redirect("vehicles:package_mailflow_home")
