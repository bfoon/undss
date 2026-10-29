# vehicles/package_studio.py
"""
Bridge between Packages & Mailroom and eSign Flow Studio.

Nothing here re-implements workflow logic. A package that uses a Studio flow:

1. becomes a filled-in Mail docket (accounts.FormSubmission),
2. starts an ordinary Flow Studio run on that docket (workflow_engine_esign),
3. is kept in step with the run: status, "waiting on" stage, completion,
   delivery time, timeline events and in-app notifications.

Slot auto-binding
-----------------
Flows ask for "people chosen when the flow starts" by role name. The mailroom
fills the obvious ones from the package so Reception rarely has to pick anyone:

    Focal point / Agency focal       → the focal-point email on the package
    Recipient / Addressee            → the named recipient (account matched by
                                       name), the recipient email (outgoing) or
                                       the focal point (incoming fallback)
    Sender / Originator / Requester  → the sender email
    Mailroom / Logged by / Clerk     → the person logging the package
    Registry, Reception, ICT Focal   → everyone with that role in the logger's
    Point, LSA, SOC, CSM, Agency HR    country office (agency if they have none)

Anything left over is asked for on the log form.
"""

import logging
import re

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_save
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DONE_EVENTS = {"assigned", "approved", "rejected", "returned", "resubmitted", "acknowledged",
                "filled", "sent_for_signature", "signed", "declined", "blocked", "retried",
                "reassigned", "completed", "cancelled", "step"}


class MailFlowError(ValueError):
    """A package could not be started on a Flow Studio flow."""


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def studio_enabled(user) -> bool:
    try:
        from tenancy.services import has_feature

        return has_feature(user, "mailroom_studio")
    except Exception:  # noqa: BLE001 - a feature check must never 500
        logger.exception("Mail flow feature check failed")
        return False


def actor_agency(user):
    agency = getattr(user, "agency", None)
    if agency is None:
        agency = getattr(getattr(user, "country_office", None), "agency", None)
    return agency


def _norm(text):
    return re.sub(r"[^a-z]", "", (text or "").lower())


def get_link(package):
    from .models_mailflow import PackageStudioLink

    try:
        return package.studio_link
    except PackageStudioLink.DoesNotExist:
        return None
    except AttributeError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Package → plain facts → docket values
# ─────────────────────────────────────────────────────────────────────────────

FACT_KEYS = ("direction", "item_type", "description", "sender_name", "sender_type", "sender_org",
             "sender_contact", "sender_email", "destination_agency", "dest_focal_email",
             "for_recipient", "recipient_org", "recipient_address", "recipient_email", "notes")


def package_facts(source):
    """Package instance or dict (form data) → plain dict used by mail rules."""
    if isinstance(source, dict):
        return {k: str(source.get(k) or "").strip() for k in FACT_KEYS}
    return {k: str(getattr(source, k, "") or "").strip() for k in FACT_KEYS}


def docket_values(package):
    from .models import Package

    sender_label = dict(Package.SENDER_TYPES).get(package.sender_type, package.sender_type or "")
    logged = timezone.localtime(package.logged_at) if package.logged_at else timezone.localtime()
    values = {k: str(getattr(package, k, "") or "") for k in FACT_KEYS if k not in ("direction",)}
    values.update({
        "tracking_code": package.tracking_code,
        "logged_at": f"{logged:%d %b %Y, %H:%M}",
        "sender_type": sender_label,
    })
    return values


# ─────────────────────────────────────────────────────────────────────────────
# Which flows, which rule
# ─────────────────────────────────────────────────────────────────────────────

def studio_flows_for(user, direction):
    """Flows the mailroom may offer for this direction, in a stable order."""
    from accounts.models_esign_studio import DocumentWorkflow
    from accounts.studio_common_esign import shared_template_q

    from .mail_docket import docket_for
    from .models_mailflow import MailRoute

    agency = actor_agency(user)
    if agency is None:
        return []

    routed = MailRoute.objects.filter(agency=agency, direction=direction, is_active=True,
                                      workflow__is_active=True).values_list("workflow_id", flat=True)
    q = Q(pk__in=list(routed))
    docket = docket_for(agency, direction)
    if docket:
        q |= Q(form_id=docket.form_id) & shared_template_q(user)
    return list(
        DocumentWorkflow.objects.filter(q, agency=agency, is_active=True)
        .select_related("form").distinct().order_by("name")
    )


def active_routes(agency, direction):
    from .models_mailflow import MailRoute

    if agency is None:
        return MailRoute.objects.none()
    return (MailRoute.objects.filter(agency=agency, direction=direction, is_active=True,
                                     workflow__is_active=True)
            .select_related("workflow").order_by("is_fallback", "priority", "name"))


def pick_route(agency, direction, facts):
    """-> (MailRoute, "rule"|"fallback") or (None, None). Specific rules beat the fallback."""
    for route in active_routes(agency, direction):
        if route.matches(facts):
            return route, ("fallback" if route.is_fallback else "rule")
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# Slots
# ─────────────────────────────────────────────────────────────────────────────

_ALIASES = {
    "focal": {"focal", "focalpoint", "agencyfocal", "agencyfocalpoint", "focalperson"},
    "recipient": {"recipient", "addressee", "namedrecipient", "receiver"},
    "sender": {"sender", "originator", "requester", "requestor"},
    "logger": {"mailroom", "loggedby", "logger", "clerk", "mailclerk", "frontdesk", "mailroomclerk"},
}


def _role_slots():
    from .models import PackageFlowStep

    out = {}
    for code, label in PackageFlowStep.ROLE_CHOICES:
        out[_norm(code)] = code
        out[_norm(label)] = code
        out[_norm(label.split("(")[0])] = code
    out["receptionist"] = "reception"
    out["ictfocal"] = "ict_focal"
    return out


def _people_with_role(role, actor):
    from accounts.workflow_engine_esign import person_from_user

    User = get_user_model()
    qs = User.objects.filter(is_active=True, role=role)
    office_id = getattr(actor, "country_office_id", None)
    agency = actor_agency(actor)
    if office_id:
        qs = qs.filter(country_office_id=office_id)
    elif agency is not None:
        qs = qs.filter(agency=agency)
    else:
        return []
    return [person_from_user(u) for u in qs.order_by("first_name", "last_name")[:25]]


def _user_by_name(name, agency):
    User = get_user_model()
    parts = [p for p in (name or "").split() if p]
    if len(parts) < 2:
        return None
    qs = User.objects.filter(is_active=True, first_name__iexact=parts[0], last_name__iexact=" ".join(parts[1:]))
    if agency is not None:
        qs = qs.filter(Q(agency=agency) | Q(country_office__agency=agency))
    return qs.first() if qs.count() == 1 else None


def bind_slot(slot_label, facts, actor):
    """-> ([people], "how it was filled") or ([], "")."""
    from accounts.workflow_engine_esign import person_from_email, person_from_user

    key = _norm(slot_label)
    agency = actor_agency(actor)

    def by_email(email, how):
        email = (email or "").strip()
        return ([person_from_email(email)], how) if _EMAIL_RE.match(email) else ([], "")

    if key in _ALIASES["focal"]:
        return by_email(facts.get("dest_focal_email"), "Agency focal point email on the package")

    if key in _ALIASES["recipient"]:
        if facts.get("direction") == "outgoing":
            return by_email(facts.get("recipient_email"), "Recipient email on the package")
        user = _user_by_name(facts.get("for_recipient"), agency)
        if user and user.email:
            return [person_from_user(user)], f"Named recipient ({user.get_full_name()})"
        return by_email(facts.get("dest_focal_email"), "Focal point, standing in for the recipient")

    if key in _ALIASES["sender"]:
        return by_email(facts.get("sender_email"), "Sender email on the package")

    if key in _ALIASES["logger"]:
        return ([person_from_user(actor)], "The person logging the package") if actor else ([], "")

    role = _role_slots().get(key)
    if role:
        people = _people_with_role(role, actor)
        if people:
            return people, f"Everyone with the {slot_label} role in your office ({len(people)})"
    return [], ""


def slot_source(slot_label):
    """Static answer for the rules page: "package", "role" or "ask"."""
    key = _norm(slot_label)
    if any(key in names for names in _ALIASES.values()):
        return "package"
    if key in _role_slots():
        return "role"
    return "ask"


def plan_slots(workflow, facts, actor):
    """
    -> [{"key", "label", "steps", "signs", "people", "how"}]

    `people` is empty when the slot has to be filled on the form.
    """
    from accounts.workflow_engine_esign import chosen_slots, clean_graph

    out = []
    for slot in chosen_slots(clean_graph(workflow.graph)):
        people, how = bind_slot(slot["label"], facts, actor)
        if slot["signs"]:
            people = [p for p in people if p.get("email")]
        out.append({**slot, "people": people, "how": how if people else ""})
    return out


def slots_from_post(post, plan):
    """Read `slot__<key>` inputs (comma/space separated emails) for unbound slots."""
    from accounts.workflow_engine_esign import person_from_email

    chosen, missing = {}, []
    for slot in plan:
        if slot["people"]:
            chosen[slot["key"]] = slot["people"]
            continue
        raw = post.get(f"slot__{slot['key']}", "") or ""
        people = [person_from_email(e) for e in re.split(r"[,;\s]+", raw) if _EMAIL_RE.match(e)]
        if people:
            chosen[slot["key"]] = people
        else:
            missing.append(slot["label"])
    return chosen, missing


# ─────────────────────────────────────────────────────────────────────────────
# Starting a mail run
# ─────────────────────────────────────────────────────────────────────────────

def _docket_form_for(workflow, agency, direction, actor):
    from .mail_docket import ensure_docket
    from .models_mailflow import MailDocket

    if workflow.form_id and MailDocket.objects.filter(form_id=workflow.form_id).exists():
        return workflow.form
    return ensure_docket(agency, direction, actor).form


def start_mail_run(package, workflow, actor, *, slots, route=None, picked_by="manual",
                   scan=None, request=None):
    """
    Start `workflow` for `package`. Call inside transaction.atomic so a refusal
    leaves no half-logged package behind. Raises MailFlowError.
    """
    from accounts.models_esign_studio import FormSubmission
    from accounts.workflow_engine_esign import WorkflowError, start_run

    from .models_mailflow import PackageStudioLink

    agency = actor_agency(actor)
    if agency is None:
        raise MailFlowError("Your account isn't linked to an agency, so a mail flow can't start.")
    if not workflow.is_active or workflow.agency_id != agency.pk:
        raise MailFlowError("That flow isn't available to your agency.")

    form = _docket_form_for(workflow, agency, package.direction, actor)
    submission = FormSubmission.objects.create(
        form=form,
        form_name=form.name,
        schema=form.schema,
        values=docket_values(package),
        reference=package.tracking_code,
        agency=agency,
        submitted_by=actor,
        submitter_name=(actor.get_full_name() or actor.username)[:150],
        submitter_email=(actor.email or "")[:254],
    )

    who = package.sender_name if package.direction == "incoming" else (package.for_recipient or package.destination_agency)
    arrow = "from" if package.direction == "incoming" else "to"
    subject = f"📦 {package.tracking_code} · {package.item_type or 'Item'} {arrow} {who or '—'}"

    try:
        run = start_run(
            workflow, actor, agency=agency, subject=subject[:200],
            message=(package.description or "")[:2000], submission=submission,
            slots=slots, request=request,
        )
    except WorkflowError as exc:
        raise MailFlowError(str(exc)) from exc

    link = PackageStudioLink.objects.create(
        package=package, workflow=workflow, run=run, docket=submission,
        route=route, picked_by=picked_by,
    )
    if scan:
        link.scan.save(scan.name, scan, save=True)

    # Events logged while the run started fired before the link existed.
    sync_package(run.pk)
    return link


# ─────────────────────────────────────────────────────────────────────────────
# Keeping the package in step with the run
# ─────────────────────────────────────────────────────────────────────────────

def _human_open_tasks(run):
    from accounts.models_esign_studio import WorkflowTask

    return list(run.tasks.filter(status__in=WorkflowTask.OPEN).order_by("created_at"))


def _notify(package, user_id, message):
    from .models import PackageNotification

    if not user_id:
        return
    exists = PackageNotification.objects.filter(
        package=package, recipient_id=user_id, message=message, is_read=False,
    ).exists()
    if not exists:
        PackageNotification.objects.create(package=package, recipient_id=user_id, message=message[:500])


def sync_package(run_id):
    """Mirror a run onto its package. Safe to call any number of times."""
    from accounts.models_esign_studio import WorkflowRun

    from .models import PackageEvent
    from .models_mailflow import PackageStudioLink

    link = (PackageStudioLink.objects.select_related("package", "run")
            .filter(run_id=run_id).first())
    if link is None or link.run is None:
        return
    run, package = link.run, link.package
    was_complete = package.is_complete
    outgoing = package.direction == "outgoing"

    open_tasks = _human_open_tasks(run)
    labels = []
    for t in open_tasks:
        if t.node_label and t.node_label not in labels:
            labels.append(t.node_label)

    if run.status == WorkflowRun.STATUS_RUNNING:
        stage = " · ".join(labels) or "Moving through the flow"
        status = slugify(" and ".join(labels))[:60] or "in-flow"
    elif run.status == WorkflowRun.STATUS_RETURNED:
        stage, status = "Returned for changes", "returned-for-changes"
    elif run.status == WorkflowRun.STATUS_BLOCKED:
        stage, status = "Needs attention", "needs-attention"
    elif run.status == WorkflowRun.STATUS_COMPLETED:
        stage = run.outcome_note or ("Dispatched" if outgoing else "Delivered")
        status = "dispatched" if outgoing else "delivered"
    elif run.status == WorkflowRun.STATUS_REJECTED:
        stage = run.outcome_note or "Stopped"
        status = slugify(run.outcome_note or "rejected")[:60] or "rejected"
    else:
        stage, status = "Cancelled", "cancelled"

    package.status = status
    package.is_complete = not run.is_open
    fields = ["status", "is_complete", "last_update"]
    if run.status == WorkflowRun.STATUS_COMPLETED and not package.delivered_at:
        package.delivered_at = run.completed_at or timezone.now()
        package.delivered_to = (package.for_recipient or package.destination_agency or "")[:120]
        fields += ["delivered_at", "delivered_to"]
    package.save(update_fields=fields)

    link.stage = stage[:200]
    link.synced_at = timezone.now()
    link.save(update_fields=["stage", "synced_at"])

    # "It's your turn" in the package bell, for people with accounts.
    for t in open_tasks:
        if t.user_id and t.kind != "signature":
            _notify(package, t.user_id, f"Package {package.tracking_code} is ready for: '{t.node_label}'.")

    if package.is_complete and not was_complete:
        code = {"completed": "delivered", "rejected": "returned", "cancelled": "cancelled"}.get(run.status, "cancelled")
        PackageEvent.objects.create(package=package, status=code, who=run.initiator,
                                    note=f"Flow Studio {run.reference}: {stage}"[:255])
        if package.logged_by_id:
            _notify(package, package.logged_by_id,
                    f"Package {package.tracking_code}: {run.workflow_name} finished — {stage}.")


def _on_workflow_event(sender, instance, created, **kwargs):
    if not created or instance.event not in _DONE_EVENTS:
        return
    run_id = instance.run_id

    def _go():
        from .models_mailflow import PackageStudioLink

        if not PackageStudioLink.objects.filter(run_id=run_id).exists():
            return
        try:
            sync_package(run_id)
        except Exception:  # noqa: BLE001 - mail sync must never break a workflow
            logger.exception("Mail flow: could not sync package for run %s", run_id)

    transaction.on_commit(_go)


def connect_signals():
    """Called from VehiclesConfig.ready()."""
    from accounts.models_esign_studio import WorkflowEvent

    post_save.connect(_on_workflow_event, sender=WorkflowEvent,
                      dispatch_uid="vehicles_mailflow_workflow_event")


# ─────────────────────────────────────────────────────────────────────────────
# What the package page shows
# ─────────────────────────────────────────────────────────────────────────────

def _node_order(graph):
    nodes = (graph or {}).get("nodes") or []
    return sorted(nodes, key=lambda n: (n.get("x", 0), n.get("y", 0)))


def panel_context(package, user):
    """Everything the Flow Studio panel on the package page needs, or None."""
    from accounts.models_esign_studio import WorkflowTask
    from accounts.studio_common_esign import can_manage_run, can_view_run
    from accounts.workflow_engine_esign import NODE_TYPES, task_url

    link = get_link(package)
    if link is None:
        return None
    run = link.run
    ctx = {"link": link, "run": run, "package": package}
    if run is None:
        return ctx

    tasks = list(run.tasks.all().order_by("created_at"))
    latest_round = {}
    for t in tasks:
        latest_round[t.node_id] = max(latest_round.get(t.node_id, 0), t.round)

    chips = []
    for node in _node_order(run.graph):
        if node.get("type") not in ("approval", "review", "fill", "signature"):
            continue
        mine = [t for t in tasks if t.node_id == node["id"] and t.round == latest_round.get(node["id"])]
        if any(t.is_open for t in mine):
            state = "current"
        elif any(t.status in (WorkflowTask.STATUS_REJECTED, WorkflowTask.STATUS_RETURNED) for t in mine):
            state = "stopped"
        elif any(t.status in WorkflowTask.CLOSED_OK for t in mine):
            state = "done"
        else:
            state = "todo"
        meta = NODE_TYPES.get(node["type"], {})
        chips.append({
            "label": node.get("label"), "state": state,
            "icon": meta.get("icon", "bi-dot"), "color": meta.get("color", "#64748B"),
            "who": ", ".join(dict.fromkeys(t.name for t in mine if t.status != WorkflowTask.STATUS_SKIPPED))[:120],
        })

    email = (getattr(user, "email", "") or "").strip().lower()
    my_tasks = [
        {"task": t, "url": task_url(t)}
        for t in tasks
        if t.is_open and t.kind != WorkflowTask.KIND_SIGNATURE
        and (t.user_id == user.pk or (email and (t.email or "").lower() == email))
    ]

    ctx.update({
        "chips": chips,
        "progress": run.progress(),
        "events": list(run.events.order_by("-at", "-id")[:8]),
        "my_tasks": my_tasks,
        "can_open_run": can_view_run(user, run),
        "can_manage_run": can_manage_run(user, run),
        "run_url": reverse("accounts:esign_run_detail", args=[run.pk]),
        "docket_url": reverse("vehicles:package_mail_docket", args=[package.pk]),
        "scan_url": reverse("vehicles:package_mail_scan", args=[package.pk]) if link.scan else "",
        "designer_url": (reverse("accounts:esign_workflow_designer", args=[run.workflow_id])
                         if run.workflow_id and run.workflow and run.workflow.created_by_id == user.pk else ""),
    })
    return ctx
