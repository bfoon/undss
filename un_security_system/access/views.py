# access/views.py
"""Access Studio — design roles, build teams, hand out access, see why."""

from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import registry
from . import services as S
from .forms import (COLORS, ICONS, AssignForm, DelegateForm, GroupForm, RequestForm, RoleForm,
                    people_for, visible_groups, visible_roles)
from .models import AccessAudit, AccessGroup, AccessRequest, AccessRole, RoleAssignment


def capability_required(code):
    """View guard: the user must hold capability `code`."""
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            if not S.can(request.user, code):
                cap = registry.get(code)
                messages.error(request, f"You need the “{cap.label if cap else code}” power for that page.")
                raise PermissionDenied
            return view(request, *args, **kwargs)
        wrapped.required_capability = code
        return wrapped
    return deco


def _feature_map(request):
    return getattr(request, "tenancy_features", None)


def can_edit_role(user, role):
    if user.is_superuser:
        return True
    if role.agency_id is None:          # shared and system roles affect every agency
        return False
    if not S.can(user, "access.manage_roles"):
        return False
    if role.agency_id != getattr(user, "agency_id", None):
        return False
    return not role.country_office_id or role.country_office_id == getattr(user, "country_office_id", None)


def can_edit_group(user, group):
    if user.is_superuser:
        return True
    return (S.can(user, "access.manage_roles") and group.agency_id
            and group.agency_id == getattr(user, "agency_id", None))


def _visible_assignments(user):
    qs = RoleAssignment.objects.active().select_related("role", "user", "group", "granted_by", "delegated_by")
    if user.is_superuser:
        return qs
    aid = getattr(user, "agency_id", None)
    return qs.filter(Q(user__agency_id=aid) | Q(group__agency_id=aid))


# ─────────────────────────────────────────────────────────────────────────────
# Hub
# ─────────────────────────────────────────────────────────────────────────────

@capability_required("access.view")
def hub(request):
    user = request.user
    roles = (visible_roles(user).annotate(n_assign=Count("assignments", filter=Q(assignments__revoked_at__isnull=True)))
             .prefetch_related("inherits"))
    User = get_user_model()
    base_counts = dict(User._base_manager.filter(is_active=True).values_list("role").annotate(n=Count("pk")))
    if not user.is_superuser and user.agency_id:
        base_counts = dict(User._base_manager.filter(is_active=True, agency_id=user.agency_id)
                           .values_list("role").annotate(n=Count("pk")))
    cards = []
    for r in roles:
        caps = set(r.capabilities or []) | r.locked_capabilities
        cards.append({
            "role": r, "caps": len(caps),
            "legacy": [registry.get(c).label for c in sorted(caps) if c.startswith("legacy.") and registry.get(c)],
            "holders": r.n_assign + (base_counts.get(r.legacy_code, 0) if r.is_system else 0),
            "editable": can_edit_role(user, r),
            "conflicts": registry.conflicts_in(caps),
        })

    now = timezone.now()
    assignments = _visible_assignments(user)
    pending = AccessRequest.objects.filter(status="pending").select_related("user", "role")
    if not user.is_superuser:
        pending = pending.filter(user__agency_id=user.agency_id)

    return render(request, "access/hub.html", {
        "cards": cards,
        "groups": visible_groups(user).annotate(n_members=Count("members", distinct=True)).prefetch_related("roles"),
        "pending": pending[:20] if S.can(user, "access.approve_requests") else [],
        "expiring": assignments.filter(ends_at__isnull=False, ends_at__lte=now + timedelta(days=7)).order_by("ends_at")[:10],
        "delegations": assignments.filter(delegated_by__isnull=False)[:10],
        "audit": AccessAudit.objects.filter(
            Q(agency_id=user.agency_id) if not user.is_superuser else Q())[:8] if S.can(user, "access.audit") else [],
        "stats": {
            "roles": len(cards), "custom": sum(1 for c in cards if not c["role"].is_system),
            "groups": visible_groups(user).count(), "assignments": assignments.count(),
            "pending": pending.count(),
        },
        "can_manage": S.can(user, "access.manage_roles"),
        "can_assign": S.can(user, "access.assign"),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Roles
# ─────────────────────────────────────────────────────────────────────────────

def _role_page(request, form, role=None, selected=None):
    selected = set(selected if selected is not None else ((role.capabilities or []) if role else []))
    locked = role.locked_capabilities if role else set()
    mine = S.caps_of(request.user)
    return render(request, "access/role_form.html", {
        "form": form, "role": role, "icons": ICONS, "colors": COLORS,
        "groups": [
            (module, [{"cap": c, "on": c.code in selected or c.code in locked, "locked": c.code in locked,
                       "mine": c.code in mine or request.user.is_superuser} for c in caps])
            for module, caps in registry.grouped(_feature_map(request))
        ],
        "conflicts": registry.CONFLICTS,
    })


def _save_role(request, form, role_is_new):
    user = request.user
    caps = [c for c in request.POST.getlist("cap") if registry.get(c)]
    role = form.save(commit=False)
    if role_is_new:
        role.created_by = user
    if not role.is_system:
        shared = form.cleaned_data.get("shared") if "shared" in form.fields else False
        role.agency = None if (shared and user.is_superuser) else getattr(user, "agency", None)
        role.country_office = (getattr(user, "country_office", None)
                               if form.cleaned_data.get("only_my_office") and not role.agency is None else None)
    # No privilege escalation: you can't put powers into a role that you don't hold.
    if not user.is_superuser:
        mine = S.caps_of(user)
        before = set(role.capabilities or []) if not role_is_new else set()
        sneaked = [c for c in caps if c not in mine and c not in before]
        if sneaked:
            labels = ", ".join(registry.get(c).label for c in sneaked[:3])
            messages.error(request, f"You can only add powers you hold yourself: {labels}.")
            return None
    role.capabilities = sorted(set(caps) | role.locked_capabilities)
    role.save()
    form.save_m2m()
    # A role must not include itself through inheritance.
    if role.pk in {r.pk for r in role.inherits.all()}:
        role.inherits.remove(role)
    S.audit(user, "role_create" if role_is_new else "role_edit",
            f"{'Created' if role_is_new else 'Updated'} role {role.name} ({len(role.capabilities)} powers)",
            {"role": role.pk, "capabilities": role.capabilities})
    for _a, _b, why in registry.conflicts_in(S.all_caps_of_role(role)):
        messages.warning(request, f"Separation of duties: {why}")
    return role


@capability_required("access.manage_roles")
def role_create(request):
    clone = None
    if request.GET.get("from"):
        clone = get_object_or_404(visible_roles(request.user), pk=request.GET["from"])
    form = RoleForm(request.POST or None, user=request.user,
                    initial={"name": f"{clone.name} (copy)", "description": clone.description, "icon": clone.icon,
                             "color": clone.color} if clone else {"icon": "bi-person-badge", "color": "#0F766E",
                                                                    "is_active": True})
    if request.method == "POST" and form.is_valid():
        role = _save_role(request, form, True)
        if role:
            messages.success(request, f"Role “{role.name}” created. Now give it to people.")
            return redirect("access:role_detail", pk=role.pk)
    selected = request.POST.getlist("cap") if request.method == "POST" else (
        sorted((set(clone.capabilities or []) | clone.locked_capabilities)) if clone else [])
    return _role_page(request, form, selected=selected)


@capability_required("access.manage_roles")
def role_edit(request, pk):
    role = get_object_or_404(AccessRole, pk=pk)
    if not can_edit_role(request.user, role):
        messages.error(request, "Shared and system roles affect every agency — only a superuser can change them. "
                                "Clone it to make your own version.")
        return redirect("access:role_detail", pk=pk)
    form = RoleForm(request.POST or None, instance=role, user=request.user)
    if request.method == "POST" and form.is_valid():
        if _save_role(request, form, False):
            messages.success(request, f"Role “{role.name}” saved. It takes effect immediately.")
            return redirect("access:role_detail", pk=pk)
    return _role_page(request, form, role, selected=request.POST.getlist("cap") if request.method == "POST" else None)


@capability_required("access.manage_roles")
@require_POST
def role_archive(request, pk):
    role = get_object_or_404(AccessRole, pk=pk)
    if role.is_system or not can_edit_role(request.user, role):
        raise PermissionDenied
    role.is_active = False
    role.save(update_fields=["is_active", "updated_at"])
    S.audit(request.user, "role_archive", f"Archived role {role.name}", {"role": role.pk})
    messages.success(request, f"“{role.name}” archived — nobody holds its powers any more.")
    return redirect("access:hub")


@capability_required("access.view")
def role_detail(request, pk):
    user = request.user
    role = get_object_or_404(visible_roles(user) | AccessRole.objects.filter(pk=pk, is_active=False,
                                                                              agency_id=user.agency_id), pk=pk)
    form = AssignForm(actor=user, role=role)
    holders = []
    for person in S.users_with_role(role).order_by("first_name", "last_name")[:300]:
        if not user.is_superuser and person.agency_id != user.agency_id:
            continue
        paths = S.snapshot(person)["roles"].get(role.pk, [])
        holders.append({"user": person, "paths": [(v, S.scope_label(s, i)) for s, i, v in paths]})
    caps = S.all_caps_of_role(role)
    ok, missing = S.grant_check(user, role)
    return render(request, "access/role_detail.html", {
        "role": role, "form": form, "holders": holders,
        "assignments": _visible_assignments(user).filter(role=role),
        "caps": [registry.get(c) for c in sorted(caps) if registry.get(c)],
        "conflicts": registry.conflicts_in(caps),
        "editable": can_edit_role(user, role),
        "can_assign": S.can(user, "access.assign"),
        "grant_ok": ok, "grant_missing": [registry.get(c).label for c in missing if registry.get(c)],
    })


@capability_required("access.assign")
@require_POST
def assign(request, pk):
    role = get_object_or_404(visible_roles(request.user), pk=pk)
    form = AssignForm(request.POST, actor=request.user, role=role)
    if not form.is_valid():
        for errs in form.errors.values():
            messages.error(request, " ".join(errs))
        return redirect("access:role_detail", pk=pk)
    cd = form.cleaned_data
    scope = cd["scope"]
    try:
        a = S.assign(
            request.user, role,
            user=cd["user"] if cd["target"] == "user" else None,
            group=cd["group"] if cd["target"] == "group" else None,
            scope=scope,
            scope_office=request.user.country_office if scope == "office" else None,
            scope_agency=request.user.agency if scope == "agency" else None,
            ends_at=cd["ends_at"], reason=cd["reason"],
        )
    except PermissionError as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001 - validation from full_clean
        messages.error(request, f"Couldn't assign: {exc}")
    else:
        who = a.user.get_full_name() if a.user else a.group.name
        end = f" until {timezone.localtime(a.ends_at):%d %b %Y}" if a.ends_at else ""
        messages.success(request, f"{who} now holds “{role.name}” ({a.scope_label}){end}.")
    return redirect("access:role_detail", pk=pk)


@login_required
@require_POST
def revoke(request, pk):
    a = get_object_or_404(RoleAssignment, pk=pk)
    mine = a.delegated_by_id == request.user.pk or a.user_id == request.user.pk
    if not (mine or S.can(request.user, "access.assign")):
        raise PermissionDenied
    S.revoke(request.user, a)
    messages.success(request, f"Removed: {a.role.name} for {a.user or a.group}.")
    return redirect(request.POST.get("next") or "access:hub")


# ─────────────────────────────────────────────────────────────────────────────
# Matrix
# ─────────────────────────────────────────────────────────────────────────────

@capability_required("access.view")
def matrix(request):
    user = request.user
    roles = list(visible_roles(user))
    rows = []
    for module, caps in registry.grouped(_feature_map(request)):
        rows.append({"module": module, "caps": [{
            "cap": c,
            "cells": [{"role": r, "on": c.code in (set(r.capabilities or []) | r.locked_capabilities),
                       "locked": c.code in r.locked_capabilities} for r in roles],
        } for c in caps]})
    return render(request, "access/matrix.html", {
        "roles": roles, "rows": rows,
        "editable": {r.pk for r in roles if can_edit_role(user, r)},
        "mine": S.caps_of(user),
    })


@capability_required("access.manage_roles")
@require_POST
def matrix_toggle(request):
    role = get_object_or_404(AccessRole, pk=request.POST.get("role"))
    code = request.POST.get("cap", "")
    if not registry.get(code) or not can_edit_role(request.user, role):
        return JsonResponse({"ok": False, "error": "You can't change this role."}, status=403)
    if code in role.locked_capabilities:
        return JsonResponse({"ok": False, "error": "A system role always keeps its own legacy power."}, status=400)
    caps = set(role.capabilities or [])
    turn_on = code not in caps
    if turn_on and not request.user.is_superuser and code not in S.caps_of(request.user):
        return JsonResponse({"ok": False, "error": "You can only add powers you hold yourself."}, status=403)
    (caps.add if turn_on else caps.discard)(code)
    role.capabilities = sorted(caps)
    role.save(update_fields=["capabilities", "slug", "updated_at"])
    S.audit(request.user, "role_edit", f"{'Gave' if turn_on else 'Took'} “{registry.get(code).label}” "
                                       f"{'to' if turn_on else 'from'} {role.name}", {"role": role.pk, "cap": code})
    warn = [why for _a, _b, why in registry.conflicts_in(S.all_caps_of_role(role))]
    return JsonResponse({"ok": True, "on": turn_on, "warnings": warn})


# ─────────────────────────────────────────────────────────────────────────────
# Groups
# ─────────────────────────────────────────────────────────────────────────────

def _group_page(request, form, group=None):
    preview = []
    if group:
        User = get_user_model()
        preview = User._base_manager.filter(pk__in=group.member_ids()).order_by("first_name")[:200]
    return render(request, "access/group_form.html", {
        "form": form, "group": group, "icons": ICONS, "colors": COLORS, "members_preview": preview,
    })


def _save_group(request, form, is_new):
    user = request.user
    g = form.save(commit=False)
    if is_new:
        g.created_by = user
    g.agency = getattr(user, "agency", None) if not user.is_superuser or user.agency_id else None
    g.country_office = user.country_office if form.cleaned_data.get("office_only") and user.country_office_id else None
    g.rule = form.build_rule()
    if not user.is_superuser:
        mine = S.caps_of(user)
        too_much = [r.name for r in form.cleaned_data.get("roles") or []
                    if not S.all_caps_of_role(r) <= mine]
        if too_much:
            messages.error(request, f"You can only attach roles whose powers you hold: {', '.join(too_much)}.")
            return None
    g.save()
    form.save_m2m()
    S.audit(user, "group_create" if is_new else "group_edit",
            f"{'Created' if is_new else 'Updated'} group {g.name}",
            {"group": g.pk, "roles": [r.pk for r in g.roles.all()], "rule": g.rule})
    return g


@capability_required("access.manage_roles")
def group_create(request):
    form = GroupForm(request.POST or None, user=request.user, initial={"icon": "bi-people", "color": "#1D4ED8"})
    if request.method == "POST" and form.is_valid():
        g = _save_group(request, form, True)
        if g:
            messages.success(request, f"Group “{g.name}” created with {len(g.member_ids())} member(s).")
            return redirect("access:group_edit", pk=g.pk)
    return _group_page(request, form)


@capability_required("access.manage_roles")
def group_edit(request, pk):
    g = get_object_or_404(visible_groups(request.user), pk=pk)
    if not can_edit_group(request.user, g):
        raise PermissionDenied
    form = GroupForm(request.POST or None, instance=g, user=request.user)
    if request.method == "POST":
        if request.POST.get("archive") == "1":
            g.is_active = False
            g.save(update_fields=["is_active", "updated_at"])
            S.audit(request.user, "group_archive", f"Archived group {g.name}", {"group": g.pk})
            messages.success(request, f"Group “{g.name}” archived.")
            return redirect("access:hub")
        if form.is_valid() and _save_group(request, form, False):
            messages.success(request, f"Group “{g.name}” saved.")
            return redirect("access:group_edit", pk=pk)
    return _group_page(request, form, g)


# ─────────────────────────────────────────────────────────────────────────────
# My access: what I hold, cover while away, ask for more
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def me(request):
    from .templatetags.access_tags import access_roles

    user = request.user
    delegable = S.delegable_roles(user)
    dform = DelegateForm(actor=user, delegable=delegable)
    rform = RequestForm(user=user)

    if request.method == "POST" and request.POST.get("action") == "delegate":
        dform = DelegateForm(request.POST, actor=user, delegable=delegable)
        if dform.is_valid():
            chosen = {int(x) for x in dform.cleaned_data["roles"]}
            picks = [(r, s) for r, s in delegable if r.pk in chosen]
            from .forms import _end_of
            try:
                S.delegate(user, dform.cleaned_data["to_user"], picks, _end_of(dform.cleaned_data["until"]),
                           dform.cleaned_data["reason"])
            except (PermissionError, ValueError) as exc:
                messages.error(request, str(exc))
            else:
                who = dform.cleaned_data["to_user"].get_full_name()
                messages.success(request, f"{who} covers {len(picks)} role(s) for you until "
                                          f"{dform.cleaned_data['until']:%d %b %Y}. It ends on its own.")
                return redirect("access:me")

    if request.method == "POST" and request.POST.get("action") == "request":
        rform = RequestForm(request.POST, user=user)
        if rform.is_valid():
            req = rform.save(commit=False)
            req.user = user
            if AccessRequest.objects.filter(user=user, role=req.role, status="pending").exists():
                messages.info(request, "You already asked for that role — it's waiting for a decision.")
            else:
                req.save()
                S.audit(user, "request", f"{user.get_full_name() or user.username} asked for {req.role.name}",
                        {"request": req.pk})
                messages.success(request, f"Request for “{req.role.name}” sent.")
            return redirect("access:me")

    caps = [registry.get(c) for c in sorted(S.caps_of(user)) if registry.get(c)]
    return render(request, "access/me.html", {
        "held": access_roles(user),
        "caps": caps,
        "legacy": S.legacy_codes(user),
        "dform": dform, "rform": rform, "can_delegate": S.can(user, "access.delegate") and delegable,
        "given": RoleAssignment.objects.active().filter(delegated_by=user).select_related("role", "user"),
        "received": RoleAssignment.objects.active().filter(user=user, delegated_by__isnull=False).select_related("role", "delegated_by"),
        "requests": AccessRequest.objects.filter(user=user).select_related("role")[:10],
    })


@capability_required("access.approve_requests")
@require_POST
def request_decide(request, pk):
    req = get_object_or_404(AccessRequest, pk=pk, status="pending")
    user = request.user
    if not user.is_superuser and req.user.agency_id != user.agency_id:
        raise PermissionDenied
    if req.user_id == user.pk:
        messages.error(request, "You can't approve your own request.")
        return redirect("access:hub")
    decision = request.POST.get("decision")
    req.decided_by, req.decided_at = user, timezone.now()
    req.decision_note = (request.POST.get("note") or "")[:255]
    if decision == "approve":
        ok, missing = S.grant_check(user, req.role, permission="access.approve_requests")
        if not ok:
            messages.error(request, "You can't approve a role with powers you don't hold yourself.")
            return redirect("access:hub")
        with transaction.atomic():
            office = req.user.country_office
            a = S.assign(user, req.role, user=req.user,
                         scope="office" if office else "agency",
                         scope_office=office, scope_agency=None if office else req.user.agency,
                         ends_at=timezone.now() + timedelta(days=req.days) if req.days else None,
                         reason=f"Request: {req.reason[:200]}", skip_check=True)
            req.status, req.assignment = "approved", a
            req.save()
        messages.success(request, f"Approved — {req.user.get_full_name()} holds “{req.role.name}”.")
    else:
        req.status = "rejected"
        req.save()
        S.audit(user, "request_reject", f"Declined {req.user} → {req.role.name}", {"request": req.pk})
        messages.info(request, "Request declined.")
    return redirect("access:hub")


# ─────────────────────────────────────────────────────────────────────────────
# Who can …? / Why can …?
# ─────────────────────────────────────────────────────────────────────────────

@capability_required("access.view")
def who_can(request):
    user = request.user
    code = request.GET.get("cap", "")
    person_id = request.GET.get("person")
    cap = registry.get(code)
    holders, person, person_caps = [], None, []
    if cap:
        office = user.country_office if not user.is_superuser else None
        for u in S.users_with(code, office=office).order_by("first_name", "last_name")[:300]:
            holders.append({"user": u, "paths": S.explain(u, code)})
    if person_id:
        person = get_object_or_404(people_for(user), pk=person_id)
        for c in sorted(S.caps_of(person)):
            if registry.get(c):
                person_caps.append({"cap": registry.get(c), "paths": S.explain(person, c)})
    return render(request, "access/who_can.html", {
        "groups": registry.grouped(_feature_map(request)), "cap": cap, "code": code, "holders": holders,
        "people": people_for(user)[:1000], "person": person, "person_caps": person_caps,
        "person_legacy": S.legacy_codes(person) if person else [],
    })


@capability_required("access.audit")
def audit_log(request):
    qs = AccessAudit.objects.select_related("actor")
    if not request.user.is_superuser:
        qs = qs.filter(agency_id=request.user.agency_id)
    return render(request, "access/audit.html", {"entries": qs[:300]})
