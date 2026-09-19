# accounts/workflow_engine_esign.py
"""
UN PASS — eSign Studio workflow engine.

A flow is a graph. A run moves through it by *entering* nodes and *leaving* them
through a port:

    start        leaves by `next` immediately
    approval     creates a task per person; leaves by `approved` or `rejected`
    review       creates a task per person; leaves by `next` once acknowledged
    fill         someone completes their part of the form; leaves by `next`
    signature    creates an eSign envelope; leaves by `signed` or `declined`
    condition    checks a set of rules; leaves by `yes` or `no`
    route        checks each path's rules in order; leaves by the first match
                 (or by every match), otherwise by `otherwise`
    notify       emails people; leaves by `next`
    parallel     leaves by every `next` edge at once
    join         waits until every incoming branch has arrived
    end          finishes the branch; outcome `completed` or `rejected`

Rules that keep it predictable
------------------------------
* An unconnected `rejected` / `declined` port ends the run as rejected.
  Any other unconnected port simply ends that branch.
* A run is complete when no branch has work left.
* "All must approve": one rejection rejects the step.
  "Any one": the first approval moves on; the step rejects only when everyone has.
* Returning for changes pauses the whole run and hands it back to the initiator.
  When they resubmit, the steps that were open start again in a new round —
  decisions from the earlier round never count twice.
* After a signature step, the document is frozen. Later form edits are kept in
  the workflow record, but a signed PDF is never re-rendered underneath a
  signature.
"""

import io
import logging
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import esign_condition_engine as C

logger = logging.getLogger(__name__)

HUMAN_TYPES = ("approval", "review", "fill", "signature")
MAX_NODES = 80
MAX_AUTOMATIC_STEPS = 400

#: Shared with the designer (serialised into the page) so both sides agree.
NODE_TYPES = {
    "start": {"label": "Start", "icon": "bi-play-fill", "color": "#0F766E",
              "ports": ["next"], "hint": "Where every run begins."},
    "approval": {"label": "Approval", "icon": "bi-patch-check", "color": "#D97706",
                 "ports": ["approved", "rejected"], "hint": "People approve, reject or return for changes."},
    "review": {"label": "Review", "icon": "bi-eye", "color": "#475569",
               "ports": ["next"], "hint": "People read and acknowledge. No approve or reject."},
    "fill": {"label": "Fill in", "icon": "bi-input-cursor-text", "color": "#2563EB",
             "ports": ["next"], "hint": "Someone completes the part of the form assigned to this step."},
    "signature": {"label": "Signature", "icon": "bi-pen", "color": "#009EDB",
                  "ports": ["signed", "declined"], "hint": "Sends an eSign envelope and waits for it."},
    "route": {"label": "Route", "icon": "bi-signpost-2", "color": "#C026D3",
              "ports": [], "hint": "Several paths, each with its own rules, plus an Otherwise path."},
    "condition": {"label": "Condition", "icon": "bi-signpost-split", "color": "#7C3AED",
                  "ports": ["yes", "no"], "hint": "Take one path or the other based on a form answer."},
    "notify": {"label": "Notify", "icon": "bi-bell", "color": "#0891B2",
               "ports": ["next"], "hint": "Email people and carry on without waiting."},
    "parallel": {"label": "Parallel", "icon": "bi-diagram-3", "color": "#64748B",
                 "ports": ["next"], "hint": "Start several branches at the same time."},
    "join": {"label": "Join", "icon": "bi-bezier2", "color": "#64748B",
             "ports": ["next"], "hint": "Wait for every incoming branch before continuing."},
    "end": {"label": "End", "icon": "bi-flag-fill", "color": "#16A34A",
            "ports": [], "hint": "Finish. Mark the outcome as completed or rejected."},
}

MAX_BRANCHES = 8


def ports_for(node):
    """A node's exits. A Route has one per path it defines, plus Otherwise."""
    if (node or {}).get("type") == "route":
        return [b["id"] for b in ((node.get("config") or {}).get("branches") or [])] + ["otherwise"]
    return NODE_TYPES.get((node or {}).get("type"), {}).get("ports", [])


def port_label(node, port):
    if (node or {}).get("type") == "route":
        for b in (node.get("config") or {}).get("branches") or []:
            if b["id"] == port:
                return b["label"]
    return PORT_LABELS.get(port, port)


PORT_LABELS = {
    "next": "Next", "approved": "Approved", "rejected": "Rejected", "signed": "Signed",
    "declined": "Declined", "yes": "Yes", "no": "No", "otherwise": "Otherwise",
}

CONDITION_OPS = C.CONDITION_OPS

ASSIGN_MODES = {
    "people": "Specific people",
    "chosen": "Chosen when the flow starts",
    "initiator": "The person who started it",
    "field": "An email address from the form",
}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class WorkflowError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Graph cleaning and validation
# ─────────────────────────────────────────────────────────────────────────────

def _s(v, n):
    return str(v if v is not None else "").strip()[:n]


def _num(v, default=0.0, lo=-5000.0, hi=20000.0):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _clean_assign(a):
    a = a if isinstance(a, dict) else {}
    mode = a.get("mode") if a.get("mode") in ASSIGN_MODES else "chosen"
    users = []
    for u in (a.get("users") or [])[:25]:
        try:
            users.append(int(u))
        except (TypeError, ValueError):
            continue
    emails = [e for e in (_s(x, 254).lower() for x in (a.get("emails") or [])[:25]) if _EMAIL_RE.match(e)]
    return {
        "mode": mode,
        "users": users,
        "emails": emails,
        "slot": _s(a.get("slot"), 60),
        "field": _s(a.get("field"), 40),
    }


def clean_graph(graph):
    graph = graph if isinstance(graph, dict) else {}
    nodes, ids = [], set()
    for n in (graph.get("nodes") or [])[:MAX_NODES]:
        if not isinstance(n, dict) or n.get("type") not in NODE_TYPES:
            continue
        nid = _s(n.get("id"), 40)
        if not _ID_RE.match(nid) or nid in ids:
            continue
        ids.add(nid)
        t = n["type"]
        cfg_in = n.get("config") if isinstance(n.get("config"), dict) else {}
        cfg = {}
        if t in ("approval", "review", "fill", "signature", "notify"):
            cfg["assign"] = _clean_assign(cfg_in.get("assign"))
        if t in ("approval", "review", "fill"):
            default_rule = "any" if t == "fill" else "all"
            cfg["rule"] = cfg_in.get("rule") if cfg_in.get("rule") in ("all", "any") else default_rule
            cfg["instructions"] = _s(cfg_in.get("instructions"), 1000)
            cfg["due_days"] = int(_num(cfg_in.get("due_days"), 0, 0, 90))
            cfg["allow_return"] = bool(cfg_in.get("allow_return", True))
        if t == "signature":
            cfg["order"] = "parallel" if cfg_in.get("order") == "parallel" else "sequential"
            cfg["placement"] = "manual" if cfg_in.get("placement") == "manual" else "auto"
            cfg["message"] = _s(cfg_in.get("message"), 1000)
        if t == "route":
            branches, seen_ids = [], set()
            for i, b in enumerate((cfg_in.get("branches") or [])[:MAX_BRANCHES]):
                if not isinstance(b, dict):
                    continue
                bid = _s(b.get("id"), 20)
                if not re.match(r"^b[A-Za-z0-9_]{1,19}$", bid) or bid in seen_ids:
                    bid = f"b{i + 1}"
                    while bid in seen_ids:
                        bid += "x"
                seen_ids.add(bid)
                branches.append({"id": bid, "label": _s(b.get("label"), 40) or f"Path {i + 1}",
                                 "condition": C.clean_tree(b.get("condition"))})
            cfg["branches"] = branches
            cfg["mode"] = "all" if cfg_in.get("mode") == "all" else "first"
        if t == "condition":
            # Keep the legacy three fields so existing flows continue to work.
            cfg["field"] = _s(cfg_in.get("field"), 40)
            cfg["op"] = cfg_in.get("op") if cfg_in.get("op") in CONDITION_OPS else "eq"
            cfg["value"] = _s(cfg_in.get("value"), 200)
            if cfg_in.get("condition"):
                cfg["condition"] = C.clean_tree(cfg_in.get("condition"))
        if t == "notify":
            cfg["message"] = _s(cfg_in.get("message"), 1000)
            cfg["attach_pdf"] = bool(cfg_in.get("attach_pdf", True))
        if t == "end":
            cfg["outcome"] = "rejected" if cfg_in.get("outcome") == "rejected" else "completed"
        nodes.append({
            "id": nid, "type": t,
            "label": _s(n.get("label"), 120) or NODE_TYPES[t]["label"],
            "x": round(_num(n.get("x")), 1), "y": round(_num(n.get("y")), 1),
            "config": cfg,
        })

    types = {n["id"]: n["type"] for n in nodes}
    cleaned_by_id = {n["id"]: n for n in nodes}
    edges, seen, eids = [], set(), set()
    for e in (graph.get("edges") or [])[:MAX_NODES * 4]:
        if not isinstance(e, dict):
            continue
        a, b, port = _s(e.get("from"), 40), _s(e.get("to"), 40), _s(e.get("port"), 20)
        if a not in types or b not in types or a == b:
            continue
        if port not in ports_for(cleaned_by_id.get(a)) or types[b] == "start":
            continue
        key = (a, port, b)
        if key in seen:
            continue
        seen.add(key)
        eid = _s(e.get("id"), 40)
        if not _ID_RE.match(eid) or eid in eids:
            eid = f"e{len(edges) + 1}_{a}_{b}"[:40]
        eids.add(eid)
        edges.append({"id": eid, "from": a, "port": port, "to": b})

    return {"nodes": nodes, "edges": edges}


def validate_graph(graph, form_schema=None):
    """
    -> {"errors": [...], "warnings": [...]}; each item {"node": id|None, "text": str}

    Errors stop a flow from being started. Warnings are worth reading but the
    flow still runs.
    """
    errors, warnings = [], []
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    by_id = {n["id"]: n for n in nodes}
    out_edges, in_edges = {}, {}
    for e in edges:
        out_edges.setdefault(e["from"], []).append(e)
        in_edges.setdefault(e["to"], []).append(e)

    form_keys = {}
    if form_schema:
        from .form_pdf_esign import INPUT_TYPES

        form_keys = {el["key"]: el for el in form_schema.get("elements") or [] if el["type"] in INPUT_TYPES}

    starts = [n for n in nodes if n["type"] == "start"]
    ends = [n for n in nodes if n["type"] == "end"]
    if len(starts) != 1:
        errors.append({"node": None, "text": "A flow needs exactly one Start." if not starts
                       else "A flow can only have one Start."})
    if not ends:
        errors.append({"node": None, "text": "Add at least one End."})

    for n in nodes:
        nid, t, cfg, name = n["id"], n["type"], n["config"], n["label"]
        outs = out_edges.get(nid, [])
        ins = in_edges.get(nid, [])

        if t != "start" and not ins:
            errors.append({"node": nid, "text": f"“{name}” has nothing leading into it, so it can never run."})
        if t == "start" and not outs:
            errors.append({"node": nid, "text": "Connect Start to the first step."})

        if t in ("approval", "review", "fill", "signature", "notify"):
            a = cfg.get("assign") or {}
            mode = a.get("mode")
            if mode == "people" and not (a.get("users") or a.get("emails")):
                errors.append({"node": nid, "text": f"Choose who handles “{name}”."})
            if mode == "chosen" and not a.get("slot"):
                errors.append({"node": nid, "text": f"Name the role for “{name}”, e.g. Supervisor."})
            if mode == "field":
                if not a.get("field"):
                    errors.append({"node": nid, "text": f"Pick the form field holding the email for “{name}”."})
                elif form_schema is not None and a["field"] not in form_keys:
                    errors.append({"node": nid, "text": f"“{name}” reads a form field that doesn't exist: {a['field']}."})
                elif form_schema is None:
                    warnings.append({"node": nid, "text": f"“{name}” is assigned from a form field; it only works when the flow runs from a form."})

        if t in ("approval", "review", "fill", "signature", "condition", "route", "notify", "parallel", "join") and not outs:
            if t in ("approval", "signature"):
                warnings.append({"node": nid, "text": f"“{name}” has no next step; when it finishes the run ends there."})
            elif t != "end":
                warnings.append({"node": nid, "text": f"“{name}” has no next step; the branch ends there."})

        if t == "route":
            branches = cfg.get("branches") or []
            if not branches:
                errors.append({"node": nid, "text": f"Add at least one path to “{name}”."})
            known = {item["field"] for item in C.catalog(form_schema, workflow=True,
                                                         node_labels={m["id"]: m["label"] for m in nodes})} if form_schema is not None else set()
            ports = {e["port"] for e in outs}
            for b in branches:
                refs = C.referenced_fields(b["condition"])
                if not refs:
                    errors.append({"node": nid, "text": f"“{name}”: the path “{b['label']}” has no rules."})
                elif form_schema is not None:
                    missing = [k for k in refs if k not in form_keys and k not in known]
                    if missing:
                        errors.append({"node": nid, "text": f"“{name}” (path “{b['label']}”) checks fields that don't exist: {', '.join(missing)}."})
                if b["id"] not in ports:
                    warnings.append({"node": nid, "text": f"“{name}”: the path “{b['label']}” isn't connected, so a run that matches it stops there."})
            if "otherwise" not in ports:
                warnings.append({"node": nid, "text": f"“{name}” has no Otherwise path; a run matching nothing stops there."})
            if cfg.get("mode") == "all" and len(branches) > 1:
                warnings.append({"node": nid, "text": f"“{name}” can take several paths at once — bring them back together with a Join if later steps should wait for all of them."})
            if form_schema is None:
                warnings.append({"node": nid, "text": f"“{name}” reads form answers; without a form every run takes Otherwise."})

        if t == "condition":
            tree = cfg.get("condition")
            refs = C.referenced_fields(tree) if tree else ([cfg.get("field")] if cfg.get("field") else [])
            if not refs:
                errors.append({"node": nid, "text": f"Choose at least one form field for “{name}”."})
            elif form_schema is not None:
                # Computed values (@sum:, @days:, @who:, @run:, @step:) are worked
                # out at run time, so check the questions they are built from.
                known = {item["field"] for item in C.catalog(form_schema, workflow=True,
                                                             node_labels={n["id"]: n["label"] for n in nodes})}
                missing = [key for key in refs if key not in form_keys and key not in known]
                if missing:
                    errors.append({"node": nid, "text": f"“{name}” checks fields that don't exist: {', '.join(missing)}."})
            elif form_schema is None:
                warnings.append({"node": nid, "text": f"“{name}” checks form fields; without a form it always takes the No path."})
            ports = {e["port"] for e in outs}
            for p in ("yes", "no"):
                if p not in ports:
                    warnings.append({"node": nid, "text": f"“{name}” has no {p.title()} path; that answer ends the branch."})

        if t == "fill" and form_schema is not None:
            from .form_pdf_esign import fields_for_step

            collected = fields_for_step(form_schema, nid)
            if not collected:
                warnings.append({"node": nid, "text": f"No questions are assigned to “{name}” yet — give it a section, or set “Filled in at” on individual questions."})
        if t == "fill" and form_schema is None:
            warnings.append({"node": nid, "text": f"“{name}” needs a form; on a plain document it works like a review."})

        if t == "join" and len(ins) < 2:
            warnings.append({"node": nid, "text": f"“{name}” joins fewer than two branches."})
        if t == "parallel" and len(outs) < 2:
            warnings.append({"node": nid, "text": f"“{name}” starts fewer than two branches."})

    # Reachability
    if len(starts) == 1:
        seen, stack = set(), [starts[0]["id"]]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(e["to"] for e in out_edges.get(cur, []))
        for n in nodes:
            if n["id"] not in seen and in_edges.get(n["id"]):
                errors.append({"node": n["id"], "text": f"“{n['label']}” can't be reached from Start."})
        if ends and not any(e["id"] in seen for e in ends):
            errors.append({"node": None, "text": "No End can be reached from Start."})

    # De-duplicate identical messages (a node can trip the same rule twice)
    def dedupe(items):
        out, keys = [], set()
        for i in items:
            k = (i["node"], i["text"])
            if k not in keys:
                keys.add(k)
                out.append(i)
        return out

    return {"errors": dedupe(errors), "warnings": dedupe(warnings)}


def chosen_slots(graph):
    """Distinct 'chosen when the flow starts' roles, in drawing order."""
    slots, seen = [], set()
    nodes = sorted(graph.get("nodes") or [], key=lambda n: (n.get("x", 0), n.get("y", 0)))
    for n in nodes:
        a = (n.get("config") or {}).get("assign") or {}
        if a.get("mode") == "chosen" and a.get("slot"):
            key = a["slot"].strip().lower()
            if key not in seen:
                seen.add(key)
                slots.append({"key": key, "label": a["slot"].strip(),
                              "steps": [m["label"] for m in nodes
                                        if ((m.get("config") or {}).get("assign") or {}).get("mode") == "chosen"
                                        and ((m.get("config") or {}).get("assign") or {}).get("slot", "").strip().lower() == key],
                              "signs": any(m["type"] == "signature" for m in nodes
                                           if ((m.get("config") or {}).get("assign") or {}).get("slot", "").strip().lower() == key)})
    return slots


# ─────────────────────────────────────────────────────────────────────────────
# Audit
# ─────────────────────────────────────────────────────────────────────────────

def _client_ip(request):
    from .utils_esign import client_ip

    return client_ip(request)


def log_run(run, event, *, request=None, task=None, actor=None, actor_name="", node_id="", note="", meta=None):
    from .models_esign_studio import WorkflowEvent

    try:
        if actor is not None and not getattr(actor, "is_authenticated", False):
            actor = None
        return WorkflowEvent.objects.create(
            run=run, task=task, actor=actor,
            actor_name=(actor_name or (actor.get_full_name() or actor.username if actor else ""))[:150],
            event=event, node_id=node_id or (task.node_id if task else ""),
            note=(note or "")[:300], ip=_client_ip(request), meta=meta or {},
        )
    except Exception:  # noqa: BLE001 - audit must never break a flow
        logger.exception("eSign Studio: could not log %s on run %s", event, getattr(run, "pk", "?"))
        return None


def _bump(*user_ids):
    try:
        from .context_processors_esign import bump_esign_badge

        for uid in user_ids:
            bump_esign_badge(uid)
    except Exception:  # noqa: BLE001
        pass


def task_url(task, request=None):
    path = reverse("accounts:esign_wf_task", args=[task.token])
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    return f"{base}{path}" if base else path


def run_url(run, request=None):
    path = reverse("accounts:esign_run_detail", args=[run.pk])
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    return f"{base}{path}" if base else path


# ─────────────────────────────────────────────────────────────────────────────
# People
# ─────────────────────────────────────────────────────────────────────────────

def person_from_user(user):
    return {"user_id": user.pk, "name": (user.get_full_name() or user.username)[:150],
            "email": (user.email or "").strip().lower(), "title": getattr(user, "job_title", "") or ""}


def person_from_email(email):
    User = get_user_model()
    email = (email or "").strip().lower()
    user = User.objects.filter(email__iexact=email, is_active=True).first() if email else None
    if user:
        return person_from_user(user)
    return {"user_id": None, "name": email, "email": email, "title": ""}


def people_from_payload(items):
    """Browser picker payload [{user_id}|{email}] -> people. Unknown ids are dropped."""
    User = get_user_model()
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("user_id"):
            try:
                u = User.objects.filter(pk=int(item["user_id"]), is_active=True).first()
            except (TypeError, ValueError):
                u = None
            if u:
                out.append(person_from_user(u))
        elif _EMAIL_RE.match(_s(item.get("email"), 254)):
            out.append(person_from_email(item["email"]))
    return _dedupe_people(out)


def _dedupe_people(people):
    out, keys = [], set()
    for p in people:
        k = ("u", p["user_id"]) if p.get("user_id") else ("e", (p.get("email") or "").lower())
        if k in keys or k == ("e", ""):
            continue
        keys.add(k)
        out.append(p)
    return out


def resolve_people(run, node):
    cfg = node.get("config") or {}
    overrides = (run.context.get("overrides") or {}).get(node["id"])
    if overrides:
        return _dedupe_people(overrides)

    a = cfg.get("assign") or {}
    mode = a.get("mode")
    User = get_user_model()
    people = []

    if mode == "initiator":
        if run.initiator:
            people.append(person_from_user(run.initiator))
    elif mode == "chosen":
        key = (a.get("slot") or "").strip().lower()
        people.extend((run.context.get("slots") or {}).get(key) or [])
    elif mode == "field":
        values = run.submission.values if run.submission_id else {}
        raw = str(values.get(a.get("field") or "") or "")
        for email in re.split(r"[,;\s]+", raw):
            if _EMAIL_RE.match(email):
                people.append(person_from_email(email))
    else:
        for u in User.objects.filter(pk__in=a.get("users") or [], is_active=True):
            people.append(person_from_user(u))
        for email in a.get("emails") or []:
            people.append(person_from_email(email))

    return _dedupe_people(people)


# ─────────────────────────────────────────────────────────────────────────────
# Documents
# ─────────────────────────────────────────────────────────────────────────────

def _read(handle):
    if not handle:
        return b""
    handle.open("rb")
    try:
        return handle.read()
    finally:
        handle.close()


def _workflow_primary_envelope_id(run):
    context = run.context if isinstance(run.context, dict) else {}
    saved = str(context.get("primary_envelope_id") or "").strip()
    if saved:
        return saved

    try:
        task = (
            run.tasks.filter(envelope__isnull=False)
            .select_related("envelope")
            .order_by("created_at", "id")
            .first()
        )
        if task and task.envelope:
            return task.envelope.envelope_id
    except Exception:
        logger.exception(
            "eSign Studio: could not resolve primary envelope for run %s",
            run.pk,
        )
    return ""


def _stamp_workflow_form_pdf(raw, run, *, envelope_id="", status_label=""):
    if not raw or not run.submission_id:
        return raw

    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.lib import colors
        from reportlab.pdfgen import canvas as rl_canvas

        reader = PdfReader(io.BytesIO(raw))
        writer = PdfWriter()

        submission = run.submission
        schema = submission.schema or {}
        header = schema.get("header") or {}

        submitted_at = getattr(submission, "created_at", None)
        submitted_stamp = ""
        if submitted_at:
            submitted_stamp = timezone.localtime(submitted_at).strftime(
                "%d %b %Y %H:%M"
            )

        for index, page in enumerate(reader.pages):
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)

            overlay_buf = io.BytesIO()
            c = rl_canvas.Canvas(overlay_buf, pagesize=(width, height))

            if envelope_id:
                c.setFillColor(colors.white)
                c.rect(
                    max(20, width - 270),
                    height - 25,
                    min(235, width - 40),
                    16,
                    stroke=0,
                    fill=1,
                )
                c.setFillColor(colors.HexColor("#64748B"))
                c.setFont("Courier", 6.5)
                c.drawRightString(
                    width - 36,
                    height - 16,
                    f"Envelope ID: {envelope_id}",
                )

            if index == 0 and status_label:
                band_h = 58.0 + (14.0 if header.get("subtitle") else 0.0)
                meta_y = height - 36.0 + 14.0 - band_h - 13.0

                c.setFillColor(colors.white)
                c.rect(
                    max(250, width - 300),
                    meta_y - 6,
                    min(265, width - 285),
                    16,
                    stroke=0,
                    fill=1,
                )

                right = status_label
                if submitted_stamp:
                    right = f"{right}   |   {submitted_stamp}"

                c.setFillColor(colors.HexColor("#64748B"))
                c.setFont("Helvetica-Bold", 7.8)
                c.drawRightString(width - 42, meta_y, right)

            c.save()
            overlay_buf.seek(0)

            overlay_reader = PdfReader(overlay_buf)
            if overlay_reader.pages:
                page.merge_page(overlay_reader.pages[0])

            writer.add_page(page)

        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    except Exception:
        logger.exception(
            "eSign Studio: could not stamp workflow PDF metadata for run %s",
            run.pk,
        )
        return raw


def run_pdf_bytes(run, status_label=""):
    if run.submission_id and not run.context.get("frozen"):
        from .form_pdf_esign import render_form_pdf

        sub = run.submission
        pdf, boxes = render_form_pdf(
            sub.schema,
            sub.values,
            reference=sub.reference,
            submitted_at=sub.created_at,
            submitter=sub.submitter_name,
            status_label=status_label or run.get_status_display(),
        )
        run._form_boxes = boxes
        return pdf

    raw = _read(run.document)

    if run.submission_id and raw:
        raw = _stamp_workflow_form_pdf(
            raw,
            run,
            envelope_id=_workflow_primary_envelope_id(run),
            status_label=status_label or run.get_status_display(),
        )

    return raw



def set_run_document(run, raw, name=None, save=True):
    name = name or run.document_name or f"{run.reference}.pdf"
    if run.document:
        try:
            run.document.delete(save=False)
        except Exception:  # noqa: BLE001
            pass
    run.document.save(f"{run.reference}.pdf", ContentFile(raw), save=False)
    run.document_name = name[:200]
    if save:
        run.save(update_fields=["document", "document_name", "updated_at"])


# ─────────────────────────────────────────────────────────────────────────────
# Starting
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def start_run(workflow, initiator, *, agency, subject, message="", pdf_bytes=None,
              document_name="", submission=None, slots=None, request=None):
    from .models_esign_studio import FormSubmission, WorkflowRun

    graph = clean_graph(workflow.graph)
    schema = submission.schema if submission else (workflow.form.schema if workflow.form_id else None)
    report = validate_graph(graph, schema if submission else None)
    if report["errors"]:
        raise WorkflowError("This flow can't start yet: " + report["errors"][0]["text"])
    if not submission and not pdf_bytes:
        raise WorkflowError("Attach a document to send through the flow.")

    needed = chosen_slots(graph)
    slots = slots or {}
    missing = [s["label"] for s in needed if not slots.get(s["key"])]
    if missing:
        raise WorkflowError("Choose someone for: " + ", ".join(missing) + ".")
    for s in needed:
        if s["signs"] and any(not p.get("email") for p in slots[s["key"]]):
            raise WorkflowError(f"Everyone chosen as {s['label']} signs, so they need an email address.")

    run = WorkflowRun.objects.create(
        workflow=workflow, workflow_name=workflow.name, workflow_version=workflow.version,
        graph=graph, agency=agency, subject=subject[:200], message=message,
        initiator=initiator, submission=submission,
        context={"slots": slots, "rounds": {}, "joins": {}, "overrides": {}},
    )
    # Freeze how signed copies will be handed out, so changing the setting
    # later never changes a run that is already under way.
    try:
        from . import esign_delivery

        esign_delivery.freeze_policy(
            run,
            form=submission.form if (submission and submission.form_id) else None,
            workflow=workflow,
        )
        run.save(update_fields=["context"])
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: could not record the copy policy for run %s", run.pk)

    if pdf_bytes:
        set_run_document(run, pdf_bytes, document_name or f"{subject}.pdf")
    if submission:
        submission.status = FormSubmission.STATUS_IN_FLOW
        submission.save(update_fields=["status", "updated_at"])

    log_run(run, "started", request=request, actor=initiator,
            note=f"Started “{workflow.name}” (version {workflow.version}).",
            meta={"slots": {k: [p.get("email") or p.get("name") for p in v] for k, v in slots.items()}})

    start = next(n for n in graph["nodes"] if n["type"] == "start")
    _drive(run, lambda: _enter(run, start, request))
    return run


# ─────────────────────────────────────────────────────────────────────────────
# Moving through the graph
# ─────────────────────────────────────────────────────────────────────────────

def _out(run, node_id, port):
    return [e for e in run.graph.get("edges") or [] if e["from"] == node_id and e["port"] == port]


def _drive(run, fn):
    """Run one step of the engine, then settle: complete the run if nothing is left."""
    run._auto_steps = 0
    try:
        fn()
    except WorkflowError as exc:
        _block(run, str(exc))
    run.save()
    _settle(run)


def _settle(run):
    from .models_esign_studio import WorkflowRun, WorkflowTask

    run.refresh_from_db()
    if run.status != WorkflowRun.STATUS_RUNNING:
        return
    if run.tasks.filter(status__in=WorkflowTask.OPEN).exists():
        return
    joins = {k: v for k, v in (run.context.get("joins") or {}).items() if v}
    if joins:
        log_run(run, "step", note="Finished with branches still waiting at a Join — check the flow design.",
                meta={"joins": joins})
    finish_run(run, WorkflowRun.STATUS_COMPLETED)


def _block(run, reason, node_id=""):
    from .models_esign_studio import WorkflowRun

    node_id = node_id or getattr(run, "_current_node", "")
    run.status = WorkflowRun.STATUS_BLOCKED
    run.block_reason = reason[:1000]
    run.context["blocked_node"] = node_id
    run.save()
    log_run(run, "blocked", node_id=node_id, note=reason)
    try:
        from . import workflow_notify_esign as notify

        notify.run_blocked(run)
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: could not notify blocked run %s", run.pk)


def _enter(run, node, request=None):
    from .models_esign_studio import WorkflowRun

    if run.status != WorkflowRun.STATUS_RUNNING:
        return
    run._auto_steps = getattr(run, "_auto_steps", 0) + 1
    if run._auto_steps > MAX_AUTOMATIC_STEPS:
        raise WorkflowError("The flow keeps looping without reaching a person or an End.")

    t = node["type"]
    run._current_node = node["id"]          # so a failure here can be retried at this step
    if t == "start":
        return _leave(run, node, "next", request)

    if t == "end":
        if (node.get("config") or {}).get("outcome") == "rejected":
            log_run(run, "step", node_id=node["id"], note=f"Reached “{node['label']}”.")
            return finish_run(run, WorkflowRun.STATUS_REJECTED, note=node["label"])
        log_run(run, "step", node_id=node["id"], note=f"Branch reached “{node['label']}”.")
        return

    if t == "condition":
        return _leave(run, node, "yes" if evaluate_condition(run, node) else "no", request)

    if t == "route":
        return _enter_route(run, node, request)

    if t == "notify":
        people = resolve_people(run, node)
        from . import workflow_notify_esign as notify

        sent = notify.notify_step(run, node, people, request=request)
        log_run(run, "notified", node_id=node["id"],
                note=f"“{node['label']}” emailed {sent} person(s).")
        return _leave(run, node, "next", request)

    if t == "parallel":
        log_run(run, "step", node_id=node["id"], note=f"Started {len(_out(run, node['id'], 'next'))} branches.")
        return _leave(run, node, "next", request)

    if t == "join":
        incoming = len([e for e in run.graph.get("edges") or [] if e["to"] == node["id"]])
        joins = run.context.setdefault("joins", {})
        joins[node["id"]] = joins.get(node["id"], 0) + 1
        if joins[node["id"]] < max(1, incoming):
            log_run(run, "step", node_id=node["id"],
                    note=f"“{node['label']}”: {joins[node['id']]} of {incoming} branches arrived.")
            return
        joins[node["id"]] = 0
        log_run(run, "step", node_id=node["id"], note=f"“{node['label']}”: all branches arrived.")
        return _leave(run, node, "next", request)

    if t in ("approval", "review", "fill"):
        return _create_human_tasks(run, node, request)

    if t == "signature":
        return _start_signature(run, node, request)


def _leave(run, node, port, request=None):
    from .models_esign_studio import WorkflowRun

    if run.status != WorkflowRun.STATUS_RUNNING:
        return
    edges = _out(run, node["id"], port)
    if not edges:
        if port in ("rejected", "declined"):
            return finish_run(run, WorkflowRun.STATUS_REJECTED,
                              note=f"{node['label']}: {port_label(node, port).lower()}")
        return
    targets = {n["id"]: n for n in run.graph.get("nodes") or []}
    for e in edges:
        target = targets.get(e["to"])
        if target is not None:
            _enter(run, target, request)


def _next_round(run, node_id):
    rounds = run.context.setdefault("rounds", {})
    rounds[node_id] = int(rounds.get(node_id, 0)) + 1
    return rounds[node_id]


def rule_values(run):
    """
    What a rule can read inside a run: the form's answers, plus the computed
    values — table totals, days between dates, the requester, this run's
    history and the comments left at earlier steps.
    """
    sub = run.submission if run.submission_id else None
    u = run.initiator
    email = (getattr(u, "email", "") or "").strip()
    office = getattr(u, "country_office", None)
    agency = getattr(u, "agency", None) or getattr(office, "agency", None)
    unit = getattr(u, "unit", None)
    who = {
        "name": (u.get_full_name() or u.username) if u else "",
        "email": email,
        "email_domain": email.split("@")[-1].lower() if "@" in email else "",
        "job_title": getattr(u, "role", "") or "",          # this deployment stores the role, not a job title
        "role": getattr(u, "role", "") or "",
        "unit": getattr(unit, "name", "") or "",
        "agency": getattr(agency, "name", "") or "",
        "office": getattr(office, "name", "") or "",
    }
    started = getattr(run, "started_at", None) or getattr(run, "created_at", None)
    run_info = {
        "returns": run.tasks.filter(status="returned").count(),
        "days_open": max(0, (timezone.now() - started).days) if started else 0,
        "pages": 0,
    }
    comments = {}
    for t in run.tasks.exclude(comment="").order_by("decided_at", "created_at"):
        comments[t.node_id] = t.comment
    return C.derive_values(sub.values if sub else {}, sub.schema if sub else None,
                           who=who, run=run_info, steps=comments, today=timezone.localdate())


def _enter_route(run, node, request=None):
    """Take the first path whose rules match — or every matching path — else Otherwise."""
    from .models_esign_studio import WorkflowRun

    cfg = node.get("config") or {}
    branches = cfg.get("branches") or []
    values = rule_values(run)
    matched = [b for b in branches if (b["condition"].get("rules") or []) and C.evaluate_tree(values, b["condition"])]
    if cfg.get("mode") != "all":
        matched = matched[:1]
    if not matched:
        log_run(run, "condition", node_id=node["id"], note=f"{node['label']}: nothing matched — took Otherwise.")
        return _leave(run, node, "otherwise", request)

    log_run(run, "condition", node_id=node["id"],
            note=f"{node['label']}: took " + ", ".join(f"“{b['label']}”" for b in matched) + ".")
    # Several paths can lead to the same step; enter each step only once.
    targets = {n["id"]: n for n in run.graph.get("nodes") or []}
    order = []
    for b in matched:
        for e in _out(run, node["id"], b["id"]):
            if e["to"] not in order and e["to"] in targets:
                order.append(e["to"])
    for target_id in order:
        if run.status != WorkflowRun.STATUS_RUNNING:
            break
        _enter(run, targets[target_id], request)
    return None


def evaluate_condition(run, node):
    cfg = node.get("config") or {}
    values = run.submission.values if run.submission_id else {}
    if cfg.get("condition"):
        resolved = rule_values(run)
        if "@run:pages" in C.referenced_fields(cfg["condition"]):
            try:
                from .pdf_tools_esign import page_count

                resolved["@run:pages"] = page_count(run_pdf_bytes(run) or b"")
            except Exception:  # noqa: BLE001 - an unreadable document counts as no pages
                logger.exception("eSign Studio: could not count pages for run %s", run.pk)
        result = C.evaluate_tree(resolved, cfg["condition"])
        schema = run.submission.schema if run.submission_id else None
        labels = {n["id"]: n["label"] for n in (run.graph or {}).get("nodes") or []}
        log_run(
            run, "condition", node_id=node["id"],
            note=f"{node['label']}: {C.describe_tree_labelled(cfg['condition'], schema, labels)} → {'Yes' if result else 'No'}",
        )
        return result

    raw = values.get(cfg.get("field") or "")
    op, target = cfg.get("op", "eq"), (cfg.get("value") or "").strip()

    if isinstance(raw, list):
        text = ", ".join(str(v) for v in raw)
        is_empty = not raw
    else:
        text = str(raw if raw is not None else "").strip()
        is_empty = text == ""

    def as_num(v):
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None

    if op == "empty":
        result = is_empty
    elif op == "not_empty":
        result = not is_empty
    elif op in ("gt", "gte", "lt", "lte"):
        a, b = as_num(text), as_num(target)
        result = a is not None and b is not None and {
            "gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    elif op == "contains":
        result = target.lower() in text.lower() if target else False
    elif op == "neq":
        result = text.lower() != target.lower()
    else:
        a, b = as_num(text), as_num(target)
        result = (a == b) if (a is not None and b is not None) else text.lower() == target.lower()

    log_run(run, "condition", node_id=node["id"],
            note=f"{node['label']}: {cfg.get('field')} {CONDITION_OPS.get(op, op)} "
                 f"{target if op not in ('empty', 'not_empty') else ''} → {'Yes' if result else 'No'}".strip())
    return result


def _create_human_tasks(run, node, request=None):
    from .models_esign_studio import WorkflowTask

    people = resolve_people(run, node)
    if not people and (node["config"].get("assign") or {}).get("mode") == "field" and run.initiator:
        log_run(run, "assigned", node_id=node["id"],
                note=f"No valid email in the form for “{node['label']}” — assigned to the initiator instead.")
        people = [person_from_user(run.initiator)]
    if not people:
        raise WorkflowError(f"Nobody could be found to handle “{node['label']}”. Choose someone and retry.")

    cfg = node["config"]
    rnd = _next_round(run, node["id"])
    due = timezone.now() + timezone.timedelta(days=cfg["due_days"]) if cfg.get("due_days") else None
    kind = {"approval": WorkflowTask.KIND_APPROVAL, "review": WorkflowTask.KIND_REVIEW,
            "fill": WorkflowTask.KIND_FILL}[node["type"]]

    from . import workflow_notify_esign as notify

    created = []
    for p in people:
        task = WorkflowTask.objects.create(
            run=run, node_id=node["id"], node_label=node["label"], kind=kind, round=rnd,
            user_id=p.get("user_id"), name=p["name"] or p["email"], email=p.get("email") or "",
            due_at=due,
        )
        created.append(task)
        notify.task_assigned(task, request=request)
    log_run(run, "assigned", node_id=node["id"],
            note=f"“{node['label']}” assigned to " + ", ".join(t.name for t in created)
                 + (f" (all must {'approve' if kind == 'approval' else 'act'})" if len(created) > 1 and cfg.get("rule") == "all" else "")
                 + (" (any one)" if len(created) > 1 and cfg.get("rule") == "any" else ""))
    _bump(*[t.user_id for t in created if t.user_id])


# ─────────────────────────────────────────────────────────────────────────────
# Decisions
# ─────────────────────────────────────────────────────────────────────────────

ACTIONS = {
    "approval": ("approve", "reject", "return"),
    "review": ("acknowledge", "return"),
    "fill": ("submit", "return"),
    "resubmit": ("resubmit", "cancel"),
    "prepare": (),
    "signature": (),
}


def decide(task, action, *, request=None, comment="", values=None, actor=None):
    """
    Apply one person's decision. Locks the run row so two approvers pressing
    at the same moment are processed one after the other.
    """
    from .models_esign_studio import WorkflowRun, WorkflowTask

    with transaction.atomic():
        run = WorkflowRun.objects.select_for_update().get(pk=task.run_id)
        task = WorkflowTask.objects.select_for_update().get(pk=task.pk)

        if not task.is_open:
            raise WorkflowError("This task has already been completed.")
        if action not in ACTIONS.get(task.kind, ()):
            raise WorkflowError("That action isn't available on this task.")
        if run.status != WorkflowRun.STATUS_RUNNING and task.kind != WorkflowTask.KIND_RESUBMIT:
            raise WorkflowError("This run is not waiting on anyone at the moment.")

        node = run.node(task.node_id)
        if node is None and task.kind != WorkflowTask.KIND_RESUBMIT:
            raise WorkflowError("This step no longer exists in the run.")
        cfg = (node or {}).get("config") or {}
        comment = (comment or "").strip()[:4000]
        now = timezone.now()
        ip = _client_ip(request)

        if action in ("reject", "return") and not comment:
            raise WorkflowError("Add a comment so the initiator knows why.")
        if action == "return" and not cfg.get("allow_return", True):
            raise WorkflowError("This step doesn't allow returning for changes.")

        if action == "return":
            _return_for_changes(run, task, node, comment, request, actor)
            return run

        if task.kind == WorkflowTask.KIND_RESUBMIT:
            return _resubmit(run, task, action, comment, values, request, actor)

        if task.kind == WorkflowTask.KIND_FILL and run.submission_id:
            sub = run.submission
            merged = dict(sub.values)
            merged.update(values or {})
            sub.values = merged
            if sub.pdf:
                sub.pdf.delete(save=False)
            sub.save(update_fields=["values", "pdf", "updated_at"])

        status = {
            "approve": WorkflowTask.STATUS_APPROVED,
            "reject": WorkflowTask.STATUS_REJECTED,
            "acknowledge": WorkflowTask.STATUS_DONE,
            "submit": WorkflowTask.STATUS_DONE,
        }[action]
        task.status = status
        task.comment = comment
        task.decided_at = now
        task.decided_ip = ip
        task.save(update_fields=["status", "comment", "decided_at", "decided_ip"])

        event = {"approve": "approved", "reject": "rejected", "acknowledge": "acknowledged", "submit": "filled"}[action]
        log_run(run, event, request=request, task=task, actor=actor, actor_name=task.name,
                note=f"{task.name} — {node['label']}" + (f": {comment[:200]}" if comment else ""))
        _bump(task.user_id)

        siblings = list(run.tasks.filter(node_id=task.node_id, round=task.round))
        open_ = [t for t in siblings if t.is_open]
        rule = cfg.get("rule", "all")

        port = None
        if task.kind == WorkflowTask.KIND_APPROVAL:
            if action == "approve":
                if rule == "any" or not open_:
                    port = "approved"
            else:
                if rule == "all":
                    port = "rejected"
                elif not open_ and not any(t.status == WorkflowTask.STATUS_APPROVED for t in siblings):
                    port = "rejected"
        else:
            if rule == "any" or not open_:
                port = "next"

        if port:
            for t in open_:
                t.status = WorkflowTask.STATUS_SKIPPED
                t.save(update_fields=["status"])
                _bump(t.user_id)
            run._auto_steps = 0
            try:
                _leave(run, node, port, request)
            except WorkflowError as exc:
                _block(run, str(exc))
            run.save()

    if run.submission_id and action in ("approve", "acknowledge", "submit"):
        try:
            from . import docgen_esign
            from .models_esign_docgen import FormDocumentRule

            docgen_esign.run_rules(run.submission, FormDocumentRule.TRIGGER_STEP,
                                   run=run, node_id=task.node_id, request=request, actor=actor)
        except Exception:  # noqa: BLE001
            logger.exception("eSign Studio: step documents failed for run %s", run.pk)

    try:
        from . import workflow_notify_esign as notify

        if action == "reject" and run.status == WorkflowRun.STATUS_REJECTED:
            pass  # finish_run already told the initiator
        elif action in ("approve", "reject", "acknowledge", "submit"):
            notify.decision_made(run, task)
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: could not send decision email for task %s", task.pk)

    _settle(run)
    return run


def _return_for_changes(run, task, node, comment, request, actor):
    from .models_esign_studio import WorkflowRun, WorkflowTask

    now = timezone.now()
    task.status = WorkflowTask.STATUS_RETURNED
    task.comment = comment
    task.decided_at = now
    task.decided_ip = _client_ip(request)
    task.save(update_fields=["status", "comment", "decided_at", "decided_ip"])

    reopen = []
    for t in run.tasks.filter(status=WorkflowTask.STATUS_PENDING).exclude(pk=task.pk):
        if t.kind in (WorkflowTask.KIND_APPROVAL, WorkflowTask.KIND_REVIEW, WorkflowTask.KIND_FILL):
            if t.node_id not in reopen:
                reopen.append(t.node_id)
            t.status = WorkflowTask.STATUS_CANCELLED
            t.save(update_fields=["status"])
            _bump(t.user_id)
    if task.node_id not in reopen:
        reopen.insert(0, task.node_id)

    run.status = WorkflowRun.STATUS_RETURNED
    run.context["returned"] = {"node": task.node_id, "by": task.name, "reason": comment,
                               "reopen": reopen, "at": now.isoformat()}
    run.save()

    log_run(run, "returned", request=request, task=task, actor=actor, actor_name=task.name,
            note=f"{task.name} returned “{node['label']}”: {comment[:200]}")

    initiator = run.initiator
    resubmit = WorkflowTask.objects.create(
        run=run, node_id=task.node_id, node_label="Update and resubmit", kind=WorkflowTask.KIND_RESUBMIT,
        round=task.round, user=initiator,
        name=(initiator.get_full_name() or initiator.username) if initiator else "Initiator",
        email=(initiator.email or "") if initiator else "",
    )
    _bump(task.user_id, resubmit.user_id)
    try:
        from . import workflow_notify_esign as notify

        notify.returned(run, task, resubmit, request=request)
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: could not send return email for run %s", run.pk)


def _resubmit(run, task, action, comment, values, request, actor):
    from .models_esign_studio import WorkflowRun, WorkflowTask

    now = timezone.now()
    if action == "cancel":
        task.status = WorkflowTask.STATUS_CANCELLED
        task.decided_at = now
        task.comment = comment
        task.save(update_fields=["status", "decided_at", "comment"])
        cancel_run(run, actor, reason=comment or "Cancelled instead of resubmitting.", request=request)
        return run

    task.status = WorkflowTask.STATUS_DONE
    task.comment = comment
    task.decided_at = now
    task.decided_ip = _client_ip(request)
    task.save(update_fields=["status", "comment", "decided_at", "decided_ip"])

    returned = run.context.pop("returned", {}) or {}
    run.status = WorkflowRun.STATUS_RUNNING
    run.save()
    log_run(run, "resubmitted", request=request, task=task, actor=actor, actor_name=task.name,
            note="Resubmitted" + (f": {comment[:200]}" if comment else "."))
    _bump(task.user_id)

    targets = {n["id"]: n for n in run.graph.get("nodes") or []}
    run._auto_steps = 0
    try:
        for node_id in returned.get("reopen") or [task.node_id]:
            if node_id in targets:
                _enter(run, targets[node_id], request)
    except WorkflowError as exc:
        _block(run, str(exc))
    run.save()
    return run


# ─────────────────────────────────────────────────────────────────────────────
# Signature steps → eSign envelopes
# ─────────────────────────────────────────────────────────────────────────────

def _start_signature(run, node, request=None):
    from .models_esign import Envelope, EnvelopeDocument, EnvelopeRecipient, SignatureField
    from .models_esign_studio import WorkflowTask
    from .pdf_tools_esign import merge, page_count, page_sizes, signature_page
    from .utils_esign import log_event, prepare_document

    cfg = node["config"]
    people = [p for p in resolve_people(run, node)]
    without_email = [p["name"] for p in people if not p.get("email")]
    people = [p for p in people if p.get("email")]
    if without_email:
        log_run(run, "step", node_id=node["id"],
                note="Skipped for signing (no email address): " + ", ".join(without_email))
    if not people:
        raise WorkflowError(f"Nobody with an email address could be found to sign at “{node['label']}”.")

    rnd = _next_round(run, node["id"])
    raw = run_pdf_bytes(run)
    boxes = getattr(run, "_form_boxes", None)
    if boxes is None:
        boxes = run.context.get("form_boxes") or []
    if not raw:
        raise WorkflowError("The run has no document to sign.")

    # Match people to boxes the form designer drew for this step
    used = set(run.context.get("used_boxes") or [])
    slot = ((cfg.get("assign") or {}).get("slot") or "").strip().lower()
    mode = (cfg.get("assign") or {}).get("mode")
    candidates = [b for b in boxes if b["element_id"] not in used and b.get("step") == node["id"]]
    candidates += [
        b for b in boxes
        if b["element_id"] not in used and not b.get("step") and (
            (slot and (b.get("role") or "").strip().lower() == slot)
            or (mode == "initiator" and (b.get("role") or "").strip().lower() in ("requester", "initiator", "submitter"))
        )
    ]
    assignments, extra = [], []
    for i, person in enumerate(people):
        if i < len(candidates):
            assignments.append((person, candidates[i]))
            used.add(candidates[i]["element_id"])
        else:
            extra.append(person)

    # A form-backed workflow must remain the form the owner designed.
    # Never manufacture a new signature page when a signature box is missing.
    if extra and run.submission_id:
        names = ", ".join(p.get("name") or p.get("email") or "Signer" for p in extra)
        raise WorkflowError(
            f"“{node['label']}” has no matching signature box for: {names}. "
            "Open the form designer, add or assign the required signature box(es) "
            "to this signature step, save the form, then retry."
        )

    pages_before = page_count(raw)
    if extra:
        size = (page_sizes(raw) or [None])[-1]
        size = size if size and size[0] < size[1] else None
        sig_pdf, sig_boxes = signature_page(
            [{"name": p["name"], "title": p.get("title") or "", "step": node["label"]} for p in extra],
            subtitle=run.subject, reference=run.reference, page_size=size,
        )
        raw = merge([raw, sig_pdf])
        for person, b in zip(extra, sig_boxes):
            assignments.append((person, {"element_id": None, "page": pages_before + b["page"],
                                         "sig": b["sig"], "date": b["date"], "_appended": True}))

    with transaction.atomic():
        envelope = Envelope.objects.create(
            agency=run.agency,
            subject=f"{run.subject} — {node['label']}"[:200],
            message=cfg.get("message") or run.message or "",
            created_by=run.initiator,
            enforce_order=cfg.get("order") != "parallel",
            reminders_enabled=True,
            reference=run.reference,
        )

        if run.submission_id:
            if not isinstance(run.context, dict):
                run.context = {}

            primary_id = (
                run.context.get("primary_envelope_id")
                or envelope.envelope_id
            )

            if not run.context.get("primary_envelope_id"):
                run.context["primary_envelope_id"] = primary_id
                run.save(update_fields=["context"])

            raw = _stamp_workflow_form_pdf(
                raw,
                run,
                envelope_id=primary_id,
                status_label=run.get_status_display(),
            )

        doc = EnvelopeDocument.objects.create(
            envelope=envelope, name=(run.document_name or f"{run.subject}.pdf")[:200], order=0,
            file=ContentFile(raw, name=f"{run.reference}-{node['id']}.pdf"),
        )
        prepare_document(doc)

        placed = 0
        for order, (person, box) in enumerate(assignments, start=1):
            rec = EnvelopeRecipient.objects.create(
                envelope=envelope, user_id=person.get("user_id"), name=person["name"][:150],
                email=person["email"], title=(person.get("title") or "")[:120],
                role=EnvelopeRecipient.ROLE_SIGNER, order=order,
            )
            if cfg.get("placement") == "manual":
                continue
            x, y, w, h = box["sig"]
            SignatureField.objects.create(
                envelope=envelope, document=doc, recipient=rec, kind=SignatureField.KIND_SIGNATURE,
                page=box["page"], x=x, y=y, w=w, h=h, label="Signature", required=True,
            )
            placed += 1
            if box.get("date"):
                dx, dy, dw, dh = box["date"]
                SignatureField.objects.create(
                    envelope=envelope, document=doc, recipient=rec, kind=SignatureField.KIND_DATE,
                    page=box["page"], x=dx, y=dy - dh * 0.2, w=dw, h=dh * 1.4,
                    label="Date signed", required=False,
                )

        log_event(envelope, "created", request=request, actor=run.initiator,
                  note=f"Created by workflow {run.reference}, step “{node['label']}”, "
                       f"with {len(assignments)} signer(s).")

        run.context["used_boxes"] = sorted(u for u in used if u)
        if boxes and not run.context.get("form_boxes"):
            run.context["form_boxes"] = boxes

        if cfg.get("placement") == "manual" and run.initiator:
            task = WorkflowTask.objects.create(
                run=run, node_id=node["id"], node_label=node["label"], kind=WorkflowTask.KIND_PREPARE,
                round=rnd, user=run.initiator, envelope=envelope,
                name=run.initiator.get_full_name() or run.initiator.username,
                email=run.initiator.email or "",
            )
            log_run(run, "assigned", task=task, node_id=node["id"],
                    note=f"“{node['label']}”: place the signature fields, then send.")
            _bump(run.initiator_id)
            try:
                from . import workflow_notify_esign as notify

                notify.task_assigned(task, request=request)
            except Exception:  # noqa: BLE001
                pass
            return

        WorkflowTask.objects.create(
            run=run, node_id=node["id"], node_label=node["label"], kind=WorkflowTask.KIND_SIGNATURE,
            round=rnd, status=WorkflowTask.STATUS_WAITING, envelope=envelope,
            name=", ".join(p["name"] for p, _ in assignments)[:150],
            email="",
        )
        send_envelope(envelope, request=request)
        log_run(run, "sent_for_signature", node_id=node["id"],
                note=f"“{node['label']}” sent to " + ", ".join(p["name"] for p, _ in assignments)
                     + f" — envelope {envelope.short_id}.",
                meta={"envelope": envelope.envelope_id})


def send_envelope(envelope, request=None):
    """The same steps as esign_send, without the redirects."""
    from . import esign_notify
    from .models_esign import Envelope, EnvelopeRecipient
    from .utils_esign import log_event

    signers = list(envelope.signers())
    envelope.status = Envelope.STATUS_SENT
    envelope.sent_at = timezone.now()
    envelope.save(update_fields=["status", "sent_at"])
    log_event(envelope, "sent", request=request, actor=envelope.created_by,
              note=f"Sent by workflow to {len(signers)} signer(s).")
    for i, s in enumerate(signers):
        if i == 0 or not envelope.enforce_order:
            s.status = EnvelopeRecipient.STATUS_SENT
            s.sent_at = timezone.now()
            s.save(update_fields=["status", "sent_at"])
            esign_notify.notify_invite(request, s, is_turn=True)
    try:
        from .context_processors_esign import bump_envelope_badges

        bump_envelope_badges(envelope)
    except Exception:  # noqa: BLE001
        pass


def on_envelope_status(envelope_id, status):
    """Called after commit whenever an envelope linked to a workflow changes status."""
    from .models_esign import Envelope
    from .models_esign_studio import WorkflowRun, WorkflowTask
    from .utils_esign import build_final_pdf

    envelope = Envelope.objects.filter(pk=envelope_id).first()
    if envelope is None:
        return

    for task in WorkflowTask.objects.select_related("run").filter(envelope_id=envelope_id,
                                                                  status__in=WorkflowTask.OPEN):
        run = task.run
        if run.status not in (WorkflowRun.STATUS_RUNNING, WorkflowRun.STATUS_RETURNED):
            continue
        node = run.node(task.node_id)
        if node is None:
            continue

        try:
            if task.kind == WorkflowTask.KIND_PREPARE and status == Envelope.STATUS_SENT:
                task.status = WorkflowTask.STATUS_DONE
                task.decided_at = timezone.now()
                task.save(update_fields=["status", "decided_at"])
                WorkflowTask.objects.create(
                    run=run, node_id=task.node_id, node_label=task.node_label,
                    kind=WorkflowTask.KIND_SIGNATURE, round=task.round,
                    status=WorkflowTask.STATUS_WAITING, envelope=envelope,
                    name=", ".join(r.name for r in envelope.signers())[:150],
                )
                log_run(run, "sent_for_signature", node_id=task.node_id,
                        note=f"“{task.node_label}” sent for signature — envelope {envelope.short_id}.")
                _bump(task.user_id)
                continue

            if task.kind != WorkflowTask.KIND_SIGNATURE:
                continue

            if status == Envelope.STATUS_COMPLETED:
                signed = _read(envelope.completed_pdf) if envelope.completed_pdf else b""
                if not signed:
                    signed = build_final_pdf(envelope)
                with transaction.atomic():
                    run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
                    task.status = WorkflowTask.STATUS_DONE
                    task.decided_at = timezone.now()
                    task.save(update_fields=["status", "decided_at"])
                    set_run_document(run, signed, save=False)
                    run.context["frozen"] = True
                    run.save()
                    log_run(run, "signed", task=task, node_id=task.node_id,
                            note=f"“{task.node_label}” — every signature collected (envelope {envelope.short_id}).")
                    _drive(run, lambda: _leave(run, node, "signed"))

            elif status in (Envelope.STATUS_DECLINED, Envelope.STATUS_VOIDED, Envelope.STATUS_EXPIRED):
                reason = {Envelope.STATUS_DECLINED: "declined", Envelope.STATUS_VOIDED: "voided",
                          Envelope.STATUS_EXPIRED: "expired"}[status]
                with transaction.atomic():
                    run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
                    task.status = WorkflowTask.STATUS_REJECTED
                    task.decided_at = timezone.now()
                    task.save(update_fields=["status", "decided_at"])
                    log_run(run, "declined", task=task, node_id=task.node_id,
                            note=f"“{task.node_label}”: envelope {envelope.short_id} was {reason}.")
                    _drive(run, lambda: _leave(run, node, "declined"))

            elif status == Envelope.STATUS_RETURNED:
                log_run(run, "step", task=task, node_id=task.node_id,
                        note=f"A signer returned envelope {envelope.short_id} for changes; the sender reworks it in eSign.")
        except Exception:  # noqa: BLE001
            logger.exception("eSign Studio: could not advance run %s from envelope %s", run.pk, envelope_id)


# ─────────────────────────────────────────────────────────────────────────────
# Owner / initiator actions
# ─────────────────────────────────────────────────────────────────────────────

def cancel_run(run, actor, reason="", request=None):
    from .models_esign import Envelope
    from .models_esign_studio import FormSubmission, WorkflowRun, WorkflowTask
    from .utils_esign import log_event

    if not run.is_open:
        raise WorkflowError("This run has already finished.")
    for t in run.tasks.filter(status__in=WorkflowTask.OPEN):
        t.status = WorkflowTask.STATUS_CANCELLED
        t.save(update_fields=["status"])
        _bump(t.user_id)
        env = t.envelope
        if env and env.status in (Envelope.STATUS_SENT, Envelope.STATUS_DRAFT, Envelope.STATUS_RETURNED):
            env.status = Envelope.STATUS_VOIDED
            env.voided_at = timezone.now()
            env.void_reason = f"Workflow {run.reference} was cancelled."
            env.save(update_fields=["status", "voided_at", "void_reason"])
            log_event(env, "voided", request=request, actor=actor, note=env.void_reason)
            try:
                from . import esign_notify

                esign_notify.notify_voided(request, env)
            except Exception:  # noqa: BLE001
                pass
    run.status = WorkflowRun.STATUS_CANCELLED
    run.completed_at = timezone.now()
    run.outcome_note = (reason or "Cancelled")[:300]
    run.save()
    if run.submission_id:
        FormSubmission.objects.filter(pk=run.submission_id).update(status=FormSubmission.STATUS_CANCELLED)
    log_run(run, "cancelled", request=request, actor=actor, note=reason or "Cancelled.")
    try:
        from . import workflow_notify_esign as notify

        notify.run_finished(run)
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: cancel email failed for run %s", run.pk)


def reassign_task(task, person, actor, request=None):
    from .models_esign_studio import WorkflowTask

    if task.status != WorkflowTask.STATUS_PENDING or task.kind in (WorkflowTask.KIND_SIGNATURE,):
        raise WorkflowError("Only a task that is waiting for someone can be reassigned.")
    if not person:
        raise WorkflowError("Choose who should take this over.")
    old = task.name
    new = WorkflowTask.objects.create(
        run=task.run, node_id=task.node_id, node_label=task.node_label, kind=task.kind, round=task.round,
        user_id=person.get("user_id"), name=person["name"] or person["email"], email=person.get("email") or "",
        due_at=task.due_at, envelope=task.envelope,
    )
    task.status = WorkflowTask.STATUS_CANCELLED
    task.comment = f"Reassigned to {new.name}"
    task.save(update_fields=["status", "comment"])
    log_run(task.run, "reassigned", request=request, task=new, actor=actor,
            note=f"“{task.node_label}” moved from {old} to {new.name}.")
    _bump(task.user_id, new.user_id)
    from . import workflow_notify_esign as notify

    notify.task_assigned(new, request=request)
    return new


def retry_blocked(run, people, actor, request=None):
    from .models_esign_studio import WorkflowRun

    if run.status != WorkflowRun.STATUS_BLOCKED:
        raise WorkflowError("This run doesn't need attention.")
    node_id = run.context.get("blocked_node") or ""
    node = run.node(node_id) if node_id else None
    if node is None:
        raise WorkflowError("The step that stopped can't be found. Cancel the run and start again.")
    if people:
        run.context.setdefault("overrides", {})[node_id] = people
    run.status = WorkflowRun.STATUS_RUNNING
    run.block_reason = ""
    run.context.pop("blocked_node", None)
    run.save()
    log_run(run, "retried", request=request, actor=actor, node_id=node_id,
            note=f"Retried “{node['label']}”" + (" with " + ", ".join(p["name"] for p in people) if people else "") + ".")
    _drive(run, lambda: _enter(run, node, request))
    return run


# ─────────────────────────────────────────────────────────────────────────────
# Finishing
# ─────────────────────────────────────────────────────────────────────────────

def finish_run(run, status, note=""):
    from .models_esign_studio import FormSubmission, WorkflowRun, WorkflowTask

    if not run.is_open:
        return
    for t in run.tasks.filter(status=WorkflowTask.STATUS_PENDING):
        t.status = WorkflowTask.STATUS_SKIPPED
        t.save(update_fields=["status"])
        _bump(t.user_id)

    run.status = status
    run.completed_at = timezone.now()
    run.outcome_note = (note or "")[:300]
    run.save()

    try:
        base = run_pdf_bytes(run, status_label=run.get_status_display())
        record = build_run_record_pdf(run)
        from .pdf_tools_esign import merge

        final = merge([base, record]) if base else record
        run.final_pdf.save(f"{run.reference}-final.pdf", ContentFile(final), save=False)
        run.save(update_fields=["final_pdf"])
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: could not build final PDF for run %s", run.pk)

    if run.submission_id:
        FormSubmission.objects.filter(pk=run.submission_id).update(
            status=FormSubmission.STATUS_COMPLETED if status == WorkflowRun.STATUS_COMPLETED
            else FormSubmission.STATUS_REJECTED)

    if run.submission_id and status == WorkflowRun.STATUS_COMPLETED:
        try:
            from . import docgen_esign
            from .models_esign_docgen import FormDocumentRule

            docgen_esign.run_rules(run.submission, FormDocumentRule.TRIGGER_COMPLETE, run=run)
        except Exception:  # noqa: BLE001
            logger.exception("eSign Studio: follow-on documents failed for run %s", run.pk)

    log_run(run, "completed" if status == WorkflowRun.STATUS_COMPLETED else "rejected",
            note=("Completed." if status == WorkflowRun.STATUS_COMPLETED else f"Rejected — {note}")[:300])
    _bump(run.initiator_id)
    try:
        from . import workflow_notify_esign as notify

        notify.run_finished(run)
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: finish email failed for run %s", run.pk)


def build_run_record_pdf(run) -> bytes:
    """The workflow's Certificate of Completion: who did what, when, from where."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from .utils_esign import esign_brand

    un_dark = colors.HexColor("#005A8B")
    grey = colors.HexColor("#6B7280")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=18 * mm, bottomMargin=16 * mm,
                            title=f"Workflow record — {run.reference}")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=16, textColor=un_dark, spaceAfter=2, alignment=0)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=8, textColor=grey)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=10.5, textColor=un_dark, spaceBefore=10, spaceAfter=4)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=7.2, leading=9, textColor=colors.HexColor("#1F2937"))

    def esc(v):
        return str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def when(dt):
        return timezone.localtime(dt).strftime("%d %b %Y %H:%M") if dt else "—"

    grid = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF6FC")),
        ("TEXTCOLOR", (0, 0), (-1, 0), un_dark),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])

    story = [Paragraph("Workflow record", h1),
             Paragraph(f"{esc(esign_brand())} — the route this document took and every decision on it", sub),
             Spacer(1, 8)]
    initiator = (run.initiator.get_full_name() or run.initiator.username) if run.initiator else "—"
    summary = [
        ["Reference", run.reference], ["Subject", Paragraph(esc(run.subject), small)],
        ["Flow", f"{run.workflow_name} (version {run.workflow_version})"],
        ["Outcome", run.get_status_display() + (f" — {run.outcome_note}" if run.outcome_note else "")],
        ["Started by", initiator], ["Started", when(run.started_at)], ["Finished", when(run.completed_at)],
    ]
    if run.submission_id:
        summary.append(["Form", f"{run.submission.form_name} ({run.submission.reference})"])
    t = Table(summary, colWidths=[34 * mm, 144 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.2),
        ("TEXTCOLOR", (0, 0), (0, -1), un_dark), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#E5E7EB")),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)

    story.append(Paragraph("Steps and decisions", h2))
    rows = [["Step", "Person", "Decision", "When", "IP", "Comment"]]
    for task in run.tasks.select_related("envelope").all():
        detail = task.comment
        if task.envelope_id:
            detail = f"Envelope {task.envelope.envelope_id}" + (f" — {detail}" if detail else "")
        rows.append([
            Paragraph(esc(task.node_label), small), Paragraph(esc(task.name), small),
            task.get_status_display(), when(task.decided_at), task.decided_ip or "—",
            Paragraph(esc(detail or "—"), small),
        ])
    st = Table(rows, colWidths=[32 * mm, 32 * mm, 22 * mm, 24 * mm, 20 * mm, 48 * mm], repeatRows=1)
    st.setStyle(grid)
    story.append(st)

    if run.submission_id:
        from .form_pdf_esign import summary_rows

        values = summary_rows(run.submission.schema, run.submission.values, for_pdf=True)
        if values:
            story.append(Paragraph("Final form answers", h2))
            vt = Table([["Field", "Answer"]] + [[Paragraph(esc(k), small), Paragraph(esc(v or "—"), small)]
                                                for k, v in values],
                       colWidths=[56 * mm, 122 * mm], repeatRows=1)
            vt.setStyle(grid)
            story.append(vt)

    story.append(Paragraph("Audit trail", h2))
    rows = [["Timestamp", "Event", "By", "IP", "Detail"]]
    for e in run.events.select_related("actor").all():
        rows.append([
            timezone.localtime(e.at).strftime("%d %b %Y %H:%M:%S"), e.get_event_display(),
            Paragraph(esc(e.actor_name or (str(e.actor) if e.actor else "System")), small),
            e.ip or "—", Paragraph(esc(e.note or "—"), small),
        ])
    at = Table(rows, colWidths=[30 * mm, 26 * mm, 32 * mm, 20 * mm, 70 * mm], repeatRows=1)
    at.setStyle(grid)
    story.append(at)

    def footer(c, _doc):
        c.saveState()
        c.setFont("Helvetica", 6.5)
        c.setFillColor(colors.HexColor("#9CA3AF"))
        c.drawString(16 * mm, 10 * mm, f"{esign_brand()} workflow {run.reference}")
        c.drawRightString(A4[0] - 16 * mm, 10 * mm, f"Record page {c.getPageNumber()}")
        c.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
