# accounts/views_esign_agents.py
"""
UN PASS — NES Studio: Agentic Flows pages.

Designing and running agents needs a login, NES, and the "NES Agentic flows"
module (tenancy catalogue: esign_agents). A Human check link works like a
workflow task link: it carries its own token, so a reviewer outside the
platform can still decide.
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import agent_ai_esign as A
from . import agent_docs_esign as D
from . import agent_engine_esign as E
from . import agent_policy_esign as PO
from . import agent_portability_esign as P
from .agent_library_esign import AGENT_TEMPLATES, agent_template
from .models_esign_agents import AgentFlow, AgentFlowShare, AgentRun, AgentStep, AgentSubscription
from .studio_common_esign import directory, json_body, max_upload_bytes, studio_context, studio_gate

logger = logging.getLogger(__name__)

TEXT_EXT = (".txt", ".md", ".csv", ".html", ".htm", ".json")


def _gate(request, as_json=False):
    """NES gate, then the Agentic flows module itself."""
    agency, bounce = studio_gate(request)
    if bounce:
        if as_json:
            return None, JsonResponse({"ok": False, "error": "NES is not enabled for you."}, status=403)
        return None, bounce
    try:
        from tenancy.services import has_feature

        on = has_feature(request.user, "esign_agents")
    except Exception:  # noqa: BLE001
        on = True
    if not on:
        if as_json:
            return None, JsonResponse({"ok": False, "error": "Agentic flows are switched off for your office."}, status=403)
        messages.warning(request, "Agentic flows are switched off for your country office.")
        return None, redirect("accounts:esign_workflows")
    return agency, None


def _flow_or_404(request, pk, edit=False):
    flow = get_object_or_404(AgentFlow.objects.select_related("created_by"), pk=pk, is_active=True)
    ok = E.can_edit_flow(request.user, flow) if edit else E.can_use_flow(request.user, flow)
    if not ok:
        raise Http404()
    return flow


def _my_email(user):
    return (getattr(user, "email", "") or "").strip().lower()


def _my_checks(user):
    email = _my_email(user)
    if not email:
        return AgentStep.objects.none()
    return (AgentStep.objects.filter(status=AgentStep.STATUS_WAITING, run__status=AgentRun.STATUS_WAITING,
                                     assignees__icontains=email)
            .select_related("run").order_by("started_at"))


def _bind_email_shares(user):
    """A share made to someone's email becomes theirs the first time they look."""
    email = _my_email(user)
    if email:
        AgentFlowShare.objects.filter(user__isnull=True, email__iexact=email).update(user=user)


# ─────────────────────────────────────────────────────────────────────────────
# Home
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_agents(request):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    user = request.user
    _bind_email_shares(user)
    tab = request.GET.get("tab") or "mine"

    mine = list(AgentFlow.objects.filter(created_by=user, is_active=True)
                .annotate(run_total=Count("runs", distinct=True), share_total=Count("shares", distinct=True)))
    subs = {s.flow_id: s for s in AgentSubscription.objects.filter(user=user)}
    shared = list(E.usable_flows(user).exclude(created_by=user).select_related("created_by")[:200])
    for f in shared:
        f.my_sub = subs.get(f.pk)
        f.i_can_edit = E.can_edit_flow(user, f)

    runs = E.visible_runs(user).select_related("flow", "run_as", "envelope")
    status = request.GET.get("status") or ""
    if status == "open":
        runs = runs.filter(status__in=AgentRun.OPEN_STATUSES)
    elif status:
        runs = runs.filter(status=status)
    q = (request.GET.get("q") or "").strip()
    if q:
        runs = runs.filter(Q(flow_name__icontains=q) | Q(reference__icontains=q) | Q(document_name__icontains=q)
                           | Q(envelope__subject__icontains=q))
    week = timezone.now() - timezone.timedelta(days=7)
    checks = list(_my_checks(user)[:50])

    return render(request, "accounts/esign/agents/home.html", studio_context(
        request, "agents",
        tab=tab if tab in ("mine", "shared", "runs", "gallery") else "mine",
        mine=mine, shared=shared, runs=runs[:200], checks=checks,
        status=status, q=q, run_statuses=AgentRun.STATUS_CHOICES,
        templates=AGENT_TEMPLATES, card_types=E.CARD_TYPES, ai=A.info(),
        counts={
            "mine": len(mine),
            "automations": sum(1 for f in mine if f.is_automatic and f.is_enabled)
                           + sum(1 for f in shared if f.my_sub and f.my_sub.is_active and f.is_enabled),
            "runs_week": E.visible_runs(user).filter(created_at__gte=week).count(),
            "checks": len(checks),
            "shared": len(shared),
        },
    ))


@login_required
@require_POST
def esign_agent_new(request):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    graph, name, description = None, "", ""
    raw_graph = request.POST.get("graph") or ""
    if raw_graph:
        try:
            graph = E.clean_graph(json.loads(raw_graph))
            name = (request.POST.get("compose_name") or "").strip()
            description = (request.POST.get("description") or "")[:2000]
        except ValueError:
            graph = None
    if graph is None or not graph["nodes"]:
        tpl = agent_template(request.POST.get("template") or "blank")
        graph, name, description = E.clean_graph(tpl["graph"]), tpl["name"] if tpl["key"] != "blank" else "", \
            tpl["description"] if tpl["key"] != "blank" else ""
    flow = AgentFlow.objects.create(
        agency=agency, created_by=request.user, office_id=getattr(request.user, "country_office_id", None),
        name=((request.POST.get("name") or "").strip() or name or "New agent")[:150],
        description=description, graph=graph, trigger=E.trigger_of(graph),
    )
    return redirect("accounts:esign_agent_design", pk=flow.pk)


@login_required
@require_POST
def esign_agent_compose(request):
    agency, bounce = _gate(request, as_json=True)
    if bounce:
        return bounce
    body, problem = json_body(request, "flow")
    if problem:
        return problem
    try:
        graph, how, name = E.compose(body.get("description") or "")
    except E.AgentError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "graph": graph, "how": how, "name": name, "report": E.validate_graph(graph)})


# ─────────────────────────────────────────────────────────────────────────────
# Designer
# ─────────────────────────────────────────────────────────────────────────────

def _share_rows(flow):
    rows = []
    for s in flow.shares.select_related("user"):
        if s.user_id:
            rows.append({"user_id": s.user_id, "name": s.user.get_full_name() or s.user.username,
                         "email": s.user.email or "", "role": s.role})
        else:
            rows.append({"email": s.email, "name": s.email, "role": s.role})
    return rows


def _workflow_choices(user):
    """Flow Studio workflows a Policy card may start (document flows only, not form-driven ones)."""
    try:
        from .models_esign_studio import DocumentWorkflow
        from .studio_common_esign import shared_template_q
        from .workflow_engine_esign import chosen_slots, clean_graph

        out = []
        for wf in DocumentWorkflow.objects.filter(shared_template_q(user), is_active=True).order_by("name")[:200]:
            slots = chosen_slots(clean_graph(wf.graph))
            out.append({"id": wf.pk, "name": wf.name, "roles": [x["label"] for x in slots], "form": bool(wf.form_id)})
        return out
    except Exception:  # noqa: BLE001 - the designer still opens without the list
        logger.exception("Agentic flows: could not list workflows")
        return []


def _config(request, flow):
    return {
        "card_types": E.CARD_TYPES, "groups": E.GROUPS, "port_labels": E.PORT_LABELS, "triggers": E.TRIGGERS,
        "languages": E.LANGUAGES, "lengths": E.SUMMARY_LENGTHS, "styles": E.SUMMARY_STYLES,
        "ops": E.CONDITION_OPS, "redact_kinds": E.REDACT_KINDS, "note_colors": E.NOTE_COLORS,
        "targets": {k: v[0] for k, v in D.TARGETS.items()},
        "directory": directory(request.user),
        "templates": [{"key": t["key"], "name": t["name"], "icon": t["icon"], "description": t["description"],
                       "graph": E.clean_graph(t["graph"])} for t in AGENT_TEMPLATES],
        "ai": A.info(),
        "policy": {"severities": PO.SEVERITIES, "kinds": PO.KINDS, "ops": PO.OPS, "outcomes": PO.OUTCOMES,
                   "actions": PO.ACTIONS, "rule_then": PO.RULE_THEN},
        "agents": [{"id": f.pk, "name": f.name} for f in E.usable_flows(request.user).exclude(pk=flow.pk).order_by("name")[:200]],
        "workflows": _workflow_choices(request.user),
        "is_owner": flow.created_by_id == request.user.id,
        "owner_name": (flow.created_by.get_full_name() or flow.created_by.username) if flow.created_by_id else "",
        "urls": {
            "save": reverse("accounts:esign_agent_save", args=[flow.pk]),
            "share": reverse("accounts:esign_agent_share", args=[flow.pk]),
            "compose": reverse("accounts:esign_agent_compose"),
            "export": reverse("accounts:esign_agent_export", args=[flow.pk]),
            "import": reverse("accounts:esign_agent_import"),
            "run": reverse("accounts:esign_agent_run_new", args=[flow.pk]),
            "toggle": reverse("accounts:esign_agent_toggle", args=[flow.pk]),
            "policy_build": reverse("accounts:esign_agent_policy_build"),
            "policy_library": reverse("accounts:esign_agent_policy_library"),
            "extract": reverse("accounts:esign_agent_extract"),
            "home": reverse("accounts:esign_agents"),
        },
    }


@login_required
@require_GET
def esign_agent_design(request, pk):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    flow = get_object_or_404(AgentFlow.objects.select_related("created_by"), pk=pk, is_active=True)
    if not E.can_edit_flow(request.user, flow):
        if E.can_use_flow(request.user, flow):
            messages.info(request, "You can run this agent but not change it. Make your own copy to change it.")
            return redirect(reverse("accounts:esign_agents") + "?tab=shared")
        raise Http404()
    graph = E.clean_graph(flow.graph)
    return render(request, "accounts/esign/agents/designer.html", studio_context(
        request, "agents", flow=flow,
        payload=json.dumps({
            "id": flow.pk, "name": flow.name, "description": flow.description, "graph": graph,
            "version": flow.version, "is_enabled": flow.is_enabled, "share_scope": flow.share_scope,
            "shares": _share_rows(flow), "report": E.validate_graph(graph),
        }),
        config_json=json.dumps(_config(request, flow)),
    ))


@login_required
@require_POST
def esign_agent_save(request, pk):
    agency, bounce = _gate(request, as_json=True)
    if bounce:
        return bounce
    flow = get_object_or_404(AgentFlow, pk=pk, is_active=True)
    if not E.can_edit_flow(request.user, flow):
        return JsonResponse({"ok": False, "error": "You can't change this agent."}, status=403)
    body, problem = json_body(request, "flow")
    if problem:
        return problem
    if str(body.get("version") or "") not in ("", str(flow.version)):
        return JsonResponse({"ok": False, "conflict": True,
                             "error": "Someone saved this agent from another window. Reload to see the latest version."},
                            status=409)
    graph = E.clean_graph(body.get("graph"))
    report = E.validate_graph(graph)
    flow.name = (body.get("name") or "").strip()[:150] or flow.name
    flow.description = (body.get("description") or "")[:2000]
    flow.graph = graph
    flow.trigger = E.trigger_of(graph)
    if "is_enabled" in body:
        flow.is_enabled = bool(body["is_enabled"])
    if not flow.is_automatic or report["errors"]:
        flow.is_enabled = False            # an automation with problems never runs unattended
    flow.version += 1
    flow.save()
    return JsonResponse({"ok": True, "version": flow.version, "graph": graph, "report": report,
                         "is_enabled": flow.is_enabled, "trigger": flow.trigger,
                         "saved_at": timezone.localtime().strftime("%H:%M")})


@login_required
@require_POST
def esign_agent_toggle(request, pk):
    agency, bounce = _gate(request, as_json=True)
    if bounce:
        return bounce
    flow = get_object_or_404(AgentFlow, pk=pk, is_active=True)
    wants_json = "application/json" in (request.headers.get("Accept") or "") or request.content_type == "application/json"
    if not E.can_edit_flow(request.user, flow):
        raise Http404()
    report = E.validate_graph(E.clean_graph(flow.graph))
    on = request.POST.get("on", "1") == "1"
    if wants_json:
        body, _ = json_body(request, "flow")
        on = bool((body or {}).get("on"))
    error = ""
    if on and not flow.is_automatic:
        error = "This agent only runs when someone starts it, so there is nothing to switch on."
    elif on and report["errors"]:
        error = "Fix the problems in the designer before switching it on."
    else:
        flow.is_enabled = on
        flow.save(update_fields=["is_enabled", "updated_at"])
    if wants_json:
        return JsonResponse({"ok": not error, "error": error, "is_enabled": flow.is_enabled}, status=400 if error else 200)
    if error:
        messages.error(request, error)
    else:
        messages.success(request, f"“{flow.name}” is {'on — it runs by itself' if on else 'off'}.")
    return redirect(request.POST.get("next") or reverse("accounts:esign_agents"))


@login_required
@require_POST
def esign_agent_share(request, pk):
    agency, bounce = _gate(request, as_json=True)
    if bounce:
        return bounce
    flow = get_object_or_404(AgentFlow, pk=pk, created_by=request.user, is_active=True)
    body, problem = json_body(request, "flow")
    if problem:
        return problem
    scope = body.get("share_scope")
    if scope not in dict(AgentFlow.SHARE_CHOICES):
        scope = AgentFlow.SHARE_PRIVATE
    User = get_user_model()
    wanted = {}
    for p in list(body.get("people") or [])[:200]:
        if not isinstance(p, dict):
            continue
        role = "edit" if p.get("role") == "edit" else "use"
        if p.get("user_id"):
            u = User.objects.filter(pk=p["user_id"], is_active=True).first()
            if u and u.pk != request.user.id:
                wanted[("u", u.pk)] = (u, (u.email or "").lower(), role)
        elif p.get("email"):
            email = E._emails([p["email"]], 1)
            if email:
                u = User.objects.filter(email__iexact=email[0], is_active=True).first()
                if u and u.pk == request.user.id:
                    continue
                key = ("u", u.pk) if u else ("e", email[0])
                wanted[key] = (u, email[0], role)
    with transaction.atomic():
        existing = {("u", s.user_id) if s.user_id else ("e", s.email.lower()): s for s in flow.shares.all()}
        added = []
        for key, (u, email, role) in wanted.items():
            s = existing.pop(key, None)
            if s:
                if s.role != role:
                    s.role = role
                    s.save(update_fields=["role"])
            else:
                AgentFlowShare.objects.create(flow=flow, user=u, email=email, role=role, shared_by=request.user)
                added.append({"email": email, "name": (u.get_full_name() or u.username) if u else email})
        for s in existing.values():
            s.delete()
        flow.share_scope = scope
        flow.office_id = getattr(request.user, "country_office_id", None) or flow.office_id
        flow.save(update_fields=["share_scope", "office_id", "updated_at"])
    if added and body.get("notify", True):
        E.notify_shared(flow, request.user, added)
    return JsonResponse({"ok": True, "share_scope": flow.share_scope, "shares": _share_rows(flow), "notified": len(added)})


@login_required
@require_POST
def esign_agent_subscribe(request, pk):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    flow = _flow_or_404(request, pk)
    on = request.POST.get("on", "1") == "1"
    if on and not flow.is_automatic:
        messages.error(request, "That agent only runs when someone starts it.")
    else:
        sub, _ = AgentSubscription.objects.get_or_create(flow=flow, user=request.user)
        sub.is_active = on
        sub.save(update_fields=["is_active"])
        if on and not flow.is_enabled:
            messages.warning(request, f"You're signed up to “{flow.name}”, but its owner has it switched off at the moment.")
        else:
            messages.success(request, f"“{flow.name}” {'now runs on your documents' if on else 'no longer runs on your documents'}.")
    return redirect(reverse("accounts:esign_agents") + "?tab=shared")


@login_required
@require_POST
def esign_agent_duplicate(request, pk):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    src = _flow_or_404(request, pk)
    graph = E.clean_graph(src.graph)
    if src.created_by_id != request.user.id:
        graph, _todo = P.sanitize(graph)        # someone else's people and webhooks stay theirs
    copy = AgentFlow.objects.create(
        agency=agency, created_by=request.user, office_id=getattr(request.user, "country_office_id", None),
        name=f"Copy of {src.name}"[:150], description=src.description, graph=graph, trigger=E.trigger_of(graph),
    )
    messages.success(request, "Copied. This one is yours to change.")
    return redirect("accounts:esign_agent_design", pk=copy.pk)


@login_required
@require_POST
def esign_agent_delete(request, pk):
    flow = get_object_or_404(AgentFlow, pk=pk, created_by=request.user)
    if flow.runs.exists():
        flow.is_active = False
        flow.is_enabled = False
        flow.save(update_fields=["is_active", "is_enabled", "updated_at"])
        messages.success(request, "Agent archived. Its runs and their results are kept.")
    else:
        flow.delete()
        messages.success(request, "Agent deleted.")
    return redirect("accounts:esign_agents")


# ─────────────────────────────────────────────────────────────────────────────
# Policies: build a rulebook from a file, read a file, reuse another agent's
# ─────────────────────────────────────────────────────────────────────────────

POLICY_EXT = (".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".html", ".htm")
MAX_POLICY_BYTES = 15 * 1024 * 1024


def _read_upload(upload):
    name = upload.name or "document"
    if not name.lower().endswith(POLICY_EXT):
        raise D.DocError(f"{name}: upload a PDF, Word (.docx/.doc), text, Markdown or web page file.")
    if upload.size > MAX_POLICY_BYTES:
        raise D.DocError(f"{name} is larger than {MAX_POLICY_BYTES // (1024 * 1024)} MB.")
    text = D.read_text(upload.read(), name)
    if not text.strip():
        raise D.DocError(f"{name} has no readable text — if it is a scan, paste the text instead.")
    return name, text


@login_required
@require_POST
def esign_agent_policy_build(request):
    """Upload / paste / link -> a rulebook. The file is read here and then thrown away."""
    agency, bounce = _gate(request, as_json=True)
    if bounce:
        return bounce
    try:
        kind, url = "paste", ""
        if request.FILES.get("file"):
            name, text = _read_upload(request.FILES["file"])
            kind = "upload"
        elif (request.POST.get("url") or "").strip():
            url = request.POST["url"].strip()
            page = D.fetch_url(url)
            name, text, kind = page["title"], page["text"], "link"
        else:
            name, text = (request.POST.get("name") or "Pasted policy")[:200], (request.POST.get("text") or "")
        cfg = PO.build(text, name=name, hint=(request.POST.get("hint") or "")[:1000], user=request.user, kind=kind, url=url)
    except (D.DocError, ValueError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    diff = None
    try:
        old = json.loads(request.POST.get("current") or "[]")
        if isinstance(old, list) and old:
            diff = PO.merge([PO.clean_rule(r, i) for i, r in enumerate(old)], cfg["rules"])
    except ValueError:
        pass
    return JsonResponse({"ok": True, "policy": cfg, "diff": diff, "how": cfg["origin"]["how"]})


@login_required
@require_POST
def esign_agent_extract(request):
    """A file -> its text, for a Knowledge link's reference text. Nothing is stored."""
    agency, bounce = _gate(request, as_json=True)
    if bounce:
        return bounce
    if not request.FILES.get("file"):
        return JsonResponse({"ok": False, "error": "Choose a file."}, status=400)
    try:
        name, text = _read_upload(request.FILES["file"])
    except D.DocError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "name": name, "text": text[:20000], "chars": len(text), "cut": len(text) > 20000})


def _policy_cards(user, exclude_flow=None):
    out = []
    for f in E.usable_flows(user).select_related("created_by")[:300]:
        for n in (f.graph or {}).get("nodes") or []:
            if n.get("type") != "policy" or not (n.get("config") or {}).get("rules"):
                continue
            c = n["config"]
            out.append({"key": f"{f.pk}:{n['id']}", "flow": f.name, "flow_id": f.pk, "title": c.get("title") or n.get("label"),
                        "version": c.get("version") or "", "rules": len(c.get("rules") or []),
                        "owner": (f.created_by.get_full_name() or f.created_by.username) if f.created_by_id else "",
                        "mine": f.created_by_id == user.id, "same_flow": f.pk == exclude_flow})
    return out


@login_required
@require_GET
def esign_agent_policy_library(request):
    agency, bounce = _gate(request, as_json=True)
    if bounce:
        return bounce
    key = request.GET.get("key") or ""
    if not key:
        return JsonResponse({"ok": True, "policies": _policy_cards(request.user)})
    try:
        flow_id, node_id = key.split(":", 1)
        flow = E.usable_flows(request.user).get(pk=int(flow_id))
    except (ValueError, AgentFlow.DoesNotExist):
        return JsonResponse({"ok": False, "error": "That policy is no longer available to you."}, status=404)
    node = next((n for n in (flow.graph or {}).get("nodes") or [] if n.get("id") == node_id and n.get("type") == "policy"), None)
    if not node:
        return JsonResponse({"ok": False, "error": "That policy is no longer available to you."}, status=404)
    g, _todo = P.sanitize({"nodes": [node], "edges": []}) if flow.created_by_id != request.user.id else ({"nodes": [node]}, [])
    cfg = PO.clean_config(g["nodes"][0]["config"])
    return JsonResponse({"ok": True, "policy": cfg, "from": flow.name})


# ─────────────────────────────────────────────────────────────────────────────
# Export / import
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def esign_agent_export(request, pk):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    flow = _flow_or_404(request, pk)
    raw = P.dumps(flow, exported_by=request.user)
    resp = HttpResponse(raw, content_type="application/json; charset=utf-8")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in flow.name)[:70].strip("-") or "agent"
    resp["Content-Disposition"] = f'attachment; filename="{safe}{P.EXTENSION}"'
    return resp


@login_required
@require_http_methods(["GET", "POST"])
def esign_agent_import(request):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    if request.method == "GET":
        return render(request, "accounts/esign/agents/import.html", studio_context(request, "agents"))

    raw = None
    if request.FILES.get("package"):
        upload = request.FILES["package"]
        if upload.size > P.MAX_PACKAGE_BYTES:
            messages.error(request, "That file is too large to be an agent.")
            return redirect("accounts:esign_agent_import")
        raw = upload.read()
    elif (request.POST.get("pasted") or "").strip():
        raw = request.POST["pasted"].strip().encode("utf-8")
    if not raw:
        messages.error(request, "Choose a .unpassagent file, or paste its contents.")
        return redirect("accounts:esign_agent_import")
    try:
        pkg = P.loads(raw)
    except P.AgentPackageError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_agent_import")

    if request.POST.get("confirm") != "1":
        return render(request, "accounts/esign/agents/import.html", studio_context(
            request, "agents", preview=pkg, raw=raw.decode("utf-8", "replace"),
            graph_json=json.dumps(pkg["graph"]), card_types=E.CARD_TYPES,
            trigger_label=E.TRIGGERS.get(pkg["trigger"], pkg["trigger"])))

    flow = AgentFlow.objects.create(
        agency=agency, created_by=request.user, office_id=getattr(request.user, "country_office_id", None),
        name=(request.POST.get("name") or "").strip()[:150] or pkg["name"], description=pkg["description"],
        graph=pkg["graph"], trigger=pkg["trigger"], is_enabled=False,
        imported_from=(f"Exported by {pkg['exported_by']}" if pkg["exported_by"] else "Imported file")[:200],
    )
    messages.success(request, "Agent imported. It is private to you and switched off until you check it — "
                              + ("then " + "; ".join(pkg["needs"][:3]) + "." if pkg["needs"] else "review its cards first."))
    return redirect("accounts:esign_agent_design", pk=flow.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Running
# ─────────────────────────────────────────────────────────────────────────────

def _my_envelopes(user):
    from .models_esign import Envelope

    q = Q(created_by=user) | Q(recipients__user=user)
    if _my_email(user):
        q |= Q(recipients__email__iexact=_my_email(user))
    return (Envelope.objects.filter(q).exclude(status="draft").distinct().order_by("-created_at")[:25])


@login_required
@require_http_methods(["GET", "POST"])
def esign_agent_run_new(request, pk):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    flow = _flow_or_404(request, pk)
    from .models_esign_studio import StudioFile

    my_files = StudioFile.objects.filter(owner=request.user).select_related("current")[:40]
    envelopes = _my_envelopes(request.user)
    report = E.validate_graph(E.clean_graph(flow.graph))

    def page(**extra):
        return render(request, "accounts/esign/agents/run_new.html", studio_context(
            request, "agents", flow=flow, report=report, my_files=my_files, envelopes=envelopes, ai=A.info(),
            graph_json=json.dumps(E.clean_graph(flow.graph)), card_types=E.CARD_TYPES,
            max_mb=max_upload_bytes() // (1024 * 1024), **extra))

    if request.method == "GET":
        return page()

    P_ = request.POST
    raw, name, text, envelope = None, "", (P_.get("text") or "").strip(), None
    try:
        source = P_.get("source") or "upload"
        if source == "upload":
            up = request.FILES.get("document")
            if not up:
                raise E.AgentError("Choose a file to upload.")
            if up.size > max_upload_bytes():
                raise E.AgentError(f"{up.name} is larger than {max_upload_bytes() // (1024 * 1024)} MB.")
            raw, name = up.read(), up.name[:200]
        elif source == "studio":
            sf = StudioFile.objects.filter(pk=P_.get("studio_file") or 0, owner=request.user).first()
            if not sf:
                raise E.AgentError("Choose one of your PDF tools files.")
            raw, name = sf.read_bytes(), sf.download_name
        elif source == "envelope":
            envelope = next((e for e in envelopes if str(e.pk) == str(P_.get("envelope"))), None)
            if not envelope:
                raise E.AgentError("Choose one of your envelopes.")
            raw = E._envelope_pdf(envelope)
            name = f"{envelope.subject[:150]}.pdf"
        elif source == "paste":
            if not text:
                raise E.AgentError("Paste some text for the agent to work on.")
        else:
            raise E.AgentError("Choose what the agent should work on.")
        if source != "paste":
            text = ""
        dry = P_.get("dry_run") == "1"
        trigger = flow.trigger if envelope else AgentFlow.TRIGGER_MANUAL
        run = E.start_run(flow, request.user, agency=agency, trigger=trigger, document=raw, document_name=name,
                          text=text, envelope=envelope, is_test=dry, dispatch_now=False)
        if dry:
            run.context = {**(run.context or {}), "dry_run": True}
            run.save(update_fields=["context"])
        E.dispatch(run)
    except E.AgentError as exc:
        messages.error(request, str(exc))
        return page(posted=P_)
    return redirect("accounts:esign_agent_run", pk=run.pk)


def _run_or_404(request, pk):
    run = get_object_or_404(AgentRun.objects.select_related("flow", "run_as", "envelope"), pk=pk)
    if not E.can_view_run(request.user, run):
        raise Http404()
    return run


def _states(run):
    states = {}
    for s in run.steps.all():
        states[s.node_id] = {"done": "done", "failed": "rejected", "waiting": "active", "running": "active",
                             "skipped": "skipped"}.get(s.status, "done")
    return states


def _steps_ctx(run, user):
    steps = list(run.steps.all())
    for s in steps:
        s.card = E.CARD_TYPES.get(s.kind, {})
        s.fields = (s.output_data or {}).get("fields") if isinstance((s.output_data or {}).get("fields"), dict) else None
        s.issues = (s.output_data or {}).get("issues") or []
        s.sources = (s.output_data or {}).get("sources") or []
        s.policy = (s.output_data or {}) if s.kind == "policy" and (s.output_data or {}).get("outcome") else None
        s.can_decide = s.status == "waiting" and (
            E.can_manage_run(user, run) or _my_email(user) in [p.get("email") for p in s.assignees or []])
    return {"run": run, "steps": steps}


@login_required
@require_GET
def esign_agent_run(request, pk):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    run = _run_or_404(request, pk)
    ctx = _steps_ctx(run, request.user)
    return render(request, "accounts/esign/agents/run_detail.html", studio_context(
        request, "agents", **ctx, can_manage=E.can_manage_run(request.user, run),
        graph_json=json.dumps(run.graph), states_json=json.dumps(_states(run)), card_types=E.CARD_TYPES,
        port_labels=E.PORT_LABELS, dry_run=bool((run.context or {}).get("dry_run")),
        emails=(run.context or {}).get("emails") or [],
    ))


@login_required
@require_GET
def esign_agent_run_status(request, pk):
    run = _run_or_404(request, pk)
    html = render_to_string("accounts/esign/agents/_steps.html", _steps_ctx(run, request.user), request=request)
    return JsonResponse({"status": run.status, "label": run.get_status_display(), "color": run.status_color,
                         "states": _states(run), "html": html, "error": run.error,
                         "open": run.is_open, "tokens": run.tokens_in + run.tokens_out})


@login_required
@require_POST
def esign_agent_run_cancel(request, pk):
    run = _run_or_404(request, pk)
    if not E.can_manage_run(request.user, run):
        raise Http404()
    try:
        E.cancel(run, request.user)
        messages.success(request, "Run cancelled.")
    except E.AgentError as exc:
        messages.error(request, str(exc))
    return redirect("accounts:esign_agent_run", pk=run.pk)


@login_required
@require_POST
def esign_agent_run_rerun(request, pk):
    agency, bounce = _gate(request)
    if bounce:
        return bounce
    run = _run_or_404(request, pk)
    if not E.can_manage_run(request.user, run):
        raise Http404()
    try:
        new = E.rerun(run, request.user)
    except E.AgentError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_agent_run", pk=run.pk)
    return redirect("accounts:esign_agent_run", pk=new.pk)


def _file_response(handle, name, inline=False):
    handle.open("rb")
    try:
        raw = handle.read()
    finally:
        handle.close()
    ext = D._ext(name)
    mime = D.TARGETS.get(ext, ("", "application/octet-stream"))[1]
    if ext == "pdf":
        mime = "application/pdf"
    resp = HttpResponse(raw, content_type=mime)
    safe = name.replace('"', "").replace("\n", " ")
    resp["Content-Disposition"] = f'{"inline" if inline else "attachment"}; filename="{safe}"'
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    return resp


@login_required
@require_GET
def esign_agent_step_file(request, pk, step_id):
    run = _run_or_404(request, pk)
    step = get_object_or_404(AgentStep, pk=step_id, run=run)
    if not step.output_file:
        raise Http404()
    return _file_response(step.output_file, step.output_file_name or "file",
                          inline=bool(request.GET.get("view")) and step.output_file_name.lower().endswith(".pdf"))


@login_required
@require_GET
def esign_agent_run_document(request, pk):
    run = _run_or_404(request, pk)
    if not run.document:
        raise Http404()
    return _file_response(run.document, run.document_name or "document")


# ─────────────────────────────────────────────────────────────────────────────
# Human check (token)
# ─────────────────────────────────────────────────────────────────────────────

@require_http_methods(["GET", "POST"])
def esign_agent_check(request, token):
    step = get_object_or_404(AgentStep.objects.select_related("run", "run__flow", "run__run_as", "run__envelope"), token=token)
    run = step.run
    node = run.node(step.node_id) or {}
    cfg = node.get("config") or {}
    authed = request.user.is_authenticated
    if request.method == "POST":
        action = request.POST.get("action")
        if action not in ("approve", "reject"):
            messages.error(request, "Choose Approve or Reject.")
        else:
            name = ""
            if not authed:
                email = (request.GET.get("as") or "").lower()
                who = next((p for p in step.assignees or [] if p.get("email") == email), None) or (step.assignees or [{}])[0]
                name = who.get("name") or who.get("email") or "Reviewer"
            try:
                E.decide(step, action == "approve", user=request.user if authed else None, name=name,
                         comment=request.POST.get("comment") or "",
                         edited_text=request.POST.get("text") if cfg.get("allow_edit", True) else None)
                messages.success(request, "Thank you — the agent " + ("carries on." if action == "approve" else "takes the Rejected path."))
            except E.AgentError as exc:
                messages.error(request, str(exc))
        return redirect("accounts:esign_agent_check", token=token)
    earlier = [(o.get("label"), o.get("text")) for k, o in (run.context or {}).get("outputs", {}).items()
               if o.get("text") and k != step.node_id and o.get("type") in E.CONTENT_TYPES][:6]
    return render(request, "accounts/esign/agents/check.html", {
        **studio_context(request, "agents"),
        "base_template": "base.html" if authed else "accounts/esign/studio/public_base.html",
        "step": step, "run": run, "node": node, "cfg": cfg, "earlier": earlier,
        "can_open_run": authed and E.can_view_run(request.user, run),
        "has_document": bool(run.document),
    })
