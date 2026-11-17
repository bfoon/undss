# access/services.py
"""
The access engine.

    can(user, "packages.log_incoming", office=pkg_office)   → bool
    acts_as(user, "lsa")                                     → bool (legacy power)
    users_with("access.approve_requests", office=office)     → User queryset
    explain(user, "packages.see_office")                     → why they have it

Where a person's roles come from
--------------------------------
1. The base role stored on their account (user.role) — its seeded system role,
   scoped to their own office.
2. Roles assigned to them directly (optionally time-boxed or delegated).
3. Roles of every group they belong to (manual or smart membership).
4. Roles inherited by any of the above.

Results are cached per user and invalidated whenever a role, group or
assignment changes (see signals in apps.py), and every five minutes so that
start and end dates take effect.
"""

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.functional import LazyObject, empty

from . import registry

logger = logging.getLogger(__name__)

CACHE_TTL = 300
VERSION_KEY = "access:version"
MAX_DELEGATION_DAYS = 30
_SYSTEM_SYNCED = {"done": False}


# ─────────────────────────────────────────────────────────────────────────────
# Cache housekeeping
# ─────────────────────────────────────────────────────────────────────────────

def version():
    v = cache.get(VERSION_KEY)
    if v is None:
        v = 1
        cache.set(VERSION_KEY, v, None)
    return v


def bump(*_args, **_kwargs):
    try:
        cache.incr(VERSION_KEY)
    except ValueError:
        cache.set(VERSION_KEY, 2, None)


def real_user(user):
    """request.user is a SimpleLazyObject — reach the actual User instance."""
    if isinstance(user, LazyObject):
        if user._wrapped is empty:
            user._setup()
        return user._wrapped
    return user


def _raw_role(user):
    """The stored base role, bypassing the dynamic RoleValue wrapper."""
    user = real_user(user)
    raw = user.__dict__.get("role")
    return str.__str__(raw) if isinstance(raw, str) else (raw or "")


# ─────────────────────────────────────────────────────────────────────────────
# System roles (the old fixed roles, seeded as editable roles)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_system_roles(force=False):
    """
    Create a system role for every old fixed role, and offer it any new default
    capability exactly once (capabilities an admin removed stay removed).
    """
    if _SYSTEM_SYNCED["done"] and not force:
        return
    from .models import AccessRole

    User = get_user_model()
    try:
        for code, label in getattr(User, "ROLE_CHOICES", []):
            defaults = registry.legacy_defaults(code)
            role, created = AccessRole.objects.get_or_create(
                legacy_code=code,
                defaults=dict(
                    name=label, is_system=True, capabilities=defaults, seeded=defaults,
                    icon=registry.LEGACY_ICONS.get(code, "bi-person-badge"),
                    color=registry.LEGACY_COLORS.get(code, "#64748B"),
                    description=f"Seeded from the old fixed role “{label}”. Edit it like any other role.",
                ),
            )
            if not created:
                new = [c for c in defaults if c not in (role.seeded or [])]
                if new:
                    role.capabilities = sorted(set(role.capabilities or []) | set(new))
                    role.seeded = sorted(set(role.seeded or []) | set(new))
                    role.save(update_fields=["capabilities", "seeded", "slug", "updated_at"])
        _SYSTEM_SYNCED["done"] = True
    except Exception:  # noqa: BLE001 - e.g. tables not migrated yet
        logger.debug("access: system roles not ready", exc_info=True)


def system_role_for(code):
    from .models import AccessRole

    if not code:
        return None
    return AccessRole.objects.filter(legacy_code=code, is_active=True).first()


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot: everything one person holds
# ─────────────────────────────────────────────────────────────────────────────

def _home_scope(user):
    if getattr(user, "country_office_id", None):
        return ["office", user.country_office_id]
    if getattr(user, "agency_id", None):
        return ["agency", user.agency_id]
    return ["global", None]


def _scope_of_group(group):
    s, i = group.scope
    return [s, i]


def groups_of(user):
    from .models import AccessGroup

    user = real_user(user)
    candidates = (AccessGroup.objects.filter(is_active=True)
                  .filter(Q(members=user) | ~Q(rule={})).distinct())
    return [g for g in candidates if g.has_member(user)]


def _compute(user):
    from .models import AccessRole, RoleAssignment

    ensure_system_roles()
    held = {}  # role_id -> list of [scope, id, via]

    def add(role_id, scope, via):
        held.setdefault(role_id, [])
        entry = [scope[0], scope[1], via]
        if entry not in held[role_id]:
            held[role_id].append(entry)

    base = system_role_for(_raw_role(user))
    if base:
        add(base.pk, _home_scope(user), "Base role on the account")

    for a in RoleAssignment.objects.active().filter(user=user).select_related("role", "delegated_by", "granted_by"):
        if not a.role.is_active:
            continue
        if a.delegated_by_id:
            via = f"Covering for {a.delegated_by.get_full_name() or a.delegated_by.username}"
        else:
            via = "Assigned directly"
        if a.ends_at:
            via += f" until {timezone.localtime(a.ends_at):%d %b %Y}"
        add(a.role_id, list(a.scope_key), via)

    groups = groups_of(user)
    if groups:
        for g in groups:
            for role in g.roles.filter(is_active=True):
                add(role.pk, _scope_of_group(g), f"Member of {g.name}")
        for a in RoleAssignment.objects.active().filter(group__in=groups).select_related("role", "group"):
            if a.role.is_active:
                add(a.role_id, list(a.scope_key), f"Member of {a.group.name}")

    # Inheritance, breadth-first, same scope as the parent.
    roles = {r.pk: r for r in AccessRole.objects.filter(pk__in=list(held)).prefetch_related("inherits")}
    queue = list(held)
    seen = set()
    while queue:
        rid = queue.pop(0)
        if rid in seen:
            continue
        seen.add(rid)
        parent = roles.get(rid) or AccessRole.objects.filter(pk=rid).prefetch_related("inherits").first()
        if parent is None:
            continue
        roles[rid] = parent
        for child in parent.inherits.filter(is_active=True):
            for scope, sid, _via in list(held.get(rid, [])):
                add(child.pk, [scope, sid], f"Included in {parent.name}")
            roles.setdefault(child.pk, child)
            queue.append(child.pk)

    caps = {}
    for rid, scopes in held.items():
        role = roles.get(rid)
        if role is None:
            continue
        for code in set(role.capabilities or []) | role.locked_capabilities:
            bucket = caps.setdefault(code, [])
            for scope, sid, _via in scopes:
                if [scope, sid] not in bucket:
                    bucket.append([scope, sid])

    legacy = {code[7:] for code in caps if code.startswith("legacy.")}
    raw = _raw_role(user)
    if raw:
        legacy.add(raw)
    if len(legacy) > 1:
        legacy.discard("requester")
    if not legacy:
        legacy.add("requester")

    return {"roles": held, "caps": caps, "legacy": sorted(legacy),
            "names": {rid: roles[rid].name for rid in held if rid in roles}}


_EMPTY = {"roles": {}, "caps": {}, "legacy": ["requester"], "names": {}}


def snapshot(user):
    user = real_user(user)
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "pk", None):
        return _EMPTY
    memo = user.__dict__.get("_access_snapshot")
    key = (f"access:snap:{version()}:{user.pk}:{_raw_role(user)}:"
           f"{getattr(user, 'agency_id', '')}:{getattr(user, 'country_office_id', '')}:{getattr(user, 'unit_id', '')}")
    if memo and memo[0] == key:
        return memo[1]
    data = cache.get(key)
    if data is None:
        try:
            data = _compute(user)
        except Exception:  # noqa: BLE001 - access must never 500 a page
            logger.exception("access: could not compute access for user %s", user.pk)
            data = {"roles": {}, "caps": {}, "legacy": [_raw_role(user) or "requester"], "names": {}}
        else:
            # JSON-safe: role ids become strings in some cache backends
            data["roles"] = {int(k): v for k, v in data["roles"].items()}
            cache.set(key, data, CACHE_TTL)
    user.__dict__["_access_snapshot"] = (key, data)
    return data


def legacy_codes(user):
    return set(snapshot(user)["legacy"])


def acts_as(user, *codes):
    if getattr(user, "is_superuser", False):
        return True
    have = legacy_codes(user)
    return any(c in have for c in codes)


# ─────────────────────────────────────────────────────────────────────────────
# can()
# ─────────────────────────────────────────────────────────────────────────────

def _is_office_admin(user):
    try:
        from tenancy.services import is_office_admin

        return is_office_admin(user)
    except Exception:  # noqa: BLE001
        return False


def _target_ids(office=None, agency=None):
    office_id = getattr(office, "pk", office) if office is not None else None
    agency_id = getattr(agency, "pk", agency) if agency is not None else None
    if office is not None and agency_id is None:
        agency_id = getattr(office, "agency_id", None)
    return office_id, agency_id


def _covers(scopes, office_id, agency_id):
    if office_id is None and agency_id is None:
        return bool(scopes)
    for scope, sid in scopes:
        if scope == "global":
            return True
        if scope == "agency" and agency_id is not None and sid == agency_id:
            return True
        if scope == "office" and office_id is not None and sid == office_id:
            return True
    return False


def can(user, code, office=None, agency=None):
    """Does `user` hold capability `code` (optionally for one office / agency)?"""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if code.startswith("access.") and _is_office_admin(user):
        return True
    scopes = snapshot(user)["caps"].get(code)
    if not scopes:
        return False
    office_id, agency_id = _target_ids(office, agency)
    return _covers(scopes, office_id, agency_id)


def caps_of(user):
    if getattr(user, "is_superuser", False):
        return set(registry.all_capabilities())
    return set(snapshot(user)["caps"])


# ─────────────────────────────────────────────────────────────────────────────
# Who holds …?
# ─────────────────────────────────────────────────────────────────────────────

def _roles_with_caps(codes):
    """Role ids holding any of `codes`, plus every role that inherits them."""
    from .models import AccessRole

    codes = set(codes)
    direct = set()
    for role in AccessRole.objects.filter(is_active=True).only("pk", "capabilities", "is_system", "legacy_code"):
        if codes & (set(role.capabilities or []) | role.locked_capabilities):
            direct.add(role.pk)
    result, frontier = set(direct), set(direct)
    while frontier:
        parents = set(AccessRole.objects.filter(is_active=True, inherits__in=frontier)
                      .values_list("pk", flat=True)) - result
        result |= parents
        frontier = parents
    return result


def _holder_ids_for_roles(role_ids):
    from .models import AccessGroup, AccessRole, RoleAssignment

    User = get_user_model()
    ids = set()
    if not role_ids:
        return ids
    legacy = list(AccessRole.objects.filter(pk__in=role_ids, is_system=True)
                  .exclude(legacy_code="").values_list("legacy_code", flat=True))
    if legacy:
        ids |= set(User._base_manager.filter(role__in=legacy, is_active=True).values_list("pk", flat=True))
    ids |= set(RoleAssignment.objects.active().filter(role_id__in=role_ids, user__isnull=False)
               .values_list("user_id", flat=True))
    groups = set(AccessGroup.objects.filter(is_active=True, roles__in=role_ids).values_list("pk", flat=True))
    groups |= set(RoleAssignment.objects.active().filter(role_id__in=role_ids, group__isnull=False)
                  .values_list("group_id", flat=True))
    for g in AccessGroup.objects.filter(pk__in=groups, is_active=True):
        ids |= g.member_ids()
    return ids


def legacy_holder_ids(codes):
    """Users who act as any of the old roles `codes` through dynamic roles."""
    codes = sorted({str.__str__(c) if isinstance(c, str) else str(c) for c in codes if c})
    if not codes:
        return set()
    key = f"access:legacy:{version()}:{','.join(codes)}"
    ids = cache.get(key)
    if ids is None:
        try:
            ensure_system_roles()
            ids = _holder_ids_for_roles(_roles_with_caps({f"legacy.{c}" for c in codes}))
        except Exception:  # noqa: BLE001 - queries must keep working before migrate
            logger.debug("access: legacy holders unavailable", exc_info=True)
            ids = set()
        cache.set(key, ids, 60)
    return ids


def users_with(code, office=None, agency=None, active_only=True):
    """Everyone holding `code` (for that office / agency, if given)."""
    User = get_user_model()
    candidates = _holder_ids_for_roles(_roles_with_caps({code}))
    qs = User._base_manager.filter(pk__in=candidates)
    if active_only:
        qs = qs.filter(is_active=True)
    if office is None and agency is None:
        return User.objects.filter(pk__in=list(qs.values_list("pk", flat=True)))
    keep = [u.pk for u in qs if can(u, code, office=office, agency=agency)]
    return User.objects.filter(pk__in=keep)


def users_with_role(role, office=None):
    """Everyone holding one role (directly, by group, base role or inheritance)."""
    User = get_user_model()
    from .models import AccessRole

    ids = set()
    result, frontier = {role.pk}, {role.pk}
    while frontier:
        parents = set(AccessRole.objects.filter(is_active=True, inherits__in=frontier)
                      .values_list("pk", flat=True)) - result
        result |= parents
        frontier = parents
    ids = _holder_ids_for_roles(result)
    qs = User._base_manager.filter(pk__in=ids, is_active=True)
    if office is not None:
        oid = getattr(office, "pk", office)
        qs = [u for u in qs if any(
            _covers([[s, i]], oid, getattr(office, "agency_id", None))
            for s, i, _v in snapshot(u)["roles"].get(role.pk, []))]
        return User.objects.filter(pk__in=[u.pk for u in qs])
    return User.objects.filter(pk__in=list(qs.values_list("pk", flat=True)))


def explain(user, code):
    """[{"role", "via", "scope"}] — every path that gives `user` the capability."""
    from .models import AccessRole

    if getattr(user, "is_superuser", False):
        return [{"role": None, "via": "Superuser", "scope": "Everywhere"}]
    out = []
    if code.startswith("access.") and _is_office_admin(user):
        out.append({"role": None, "via": "Office admin (platform console)", "scope": "Their office"})
    snap = snapshot(user)
    roles = {r.pk: r for r in AccessRole.objects.filter(pk__in=list(snap["roles"]))}
    for rid, scopes in snap["roles"].items():
        role = roles.get(rid)
        if role and code in (set(role.capabilities or []) | role.locked_capabilities):
            for scope, sid, via in scopes:
                out.append({"role": role, "via": via, "scope": scope_label(scope, sid)})
    return out


def scope_label(scope, sid):
    if scope == "global":
        return "Everywhere"
    if scope == "agency":
        from accounts.models import Agency

        return str(Agency.objects.filter(pk=sid).first() or "Agency")
    from tenancy.models import CountryOffice

    return str(CountryOffice.objects.filter(pk=sid).first() or "Office")


# ─────────────────────────────────────────────────────────────────────────────
# Changing access safely
# ─────────────────────────────────────────────────────────────────────────────

def all_caps_of_role(role):
    caps, seen, stack = set(), set(), [role]
    while stack:
        r = stack.pop()
        if r.pk in seen:
            continue
        seen.add(r.pk)
        caps |= set(r.capabilities or []) | r.locked_capabilities
        stack.extend(r.inherits.filter(is_active=True))
    return caps


def _is_main_admin(user):
    try:
        from tenancy.services import is_main_admin

        return is_main_admin(user)
    except Exception:  # noqa: BLE001
        return False


def grant_check(actor, role, permission="access.assign"):
    """
    -> (ok, missing_caps). No privilege escalation: besides `permission`, the
    actor must hold every capability the role (with what it inherits) carries.
    """
    if getattr(actor, "is_superuser", False) or _is_main_admin(actor):
        return True, []
    if not can(actor, permission):
        return False, [permission]
    missing = sorted(all_caps_of_role(role) - caps_of(actor))
    return not missing, missing


def audit(actor, action, summary, detail=None, agency=None, office=None):
    from .models import AccessAudit

    AccessAudit.objects.create(
        actor=actor if getattr(actor, "pk", None) else None, action=action[:40], summary=summary[:300],
        detail=detail or {},
        agency=agency or getattr(actor, "agency", None),
        country_office=office or getattr(actor, "country_office", None),
    )


@transaction.atomic
def assign(actor, role, *, user=None, group=None, scope="office", scope_agency=None, scope_office=None,
           ends_at=None, reason="", delegated_by=None, skip_check=False):
    from .models import RoleAssignment

    if not skip_check:
        ok, missing = grant_check(actor, role)
        if not ok:
            labels = [(registry.get(c).label if registry.get(c) else c) for c in missing[:4]]
            raise PermissionError("You can't hand out powers you don't hold yourself: " + ", ".join(labels))
    now = timezone.now()
    if role.max_days:
        limit = now + timedelta(days=role.max_days)
        if ends_at is None or ends_at > limit:
            ends_at = limit
    a = RoleAssignment(role=role, user=user, group=group, scope=scope, scope_agency=scope_agency,
                       scope_office=scope_office, ends_at=ends_at, reason=reason[:255],
                       granted_by=actor, delegated_by=delegated_by, starts_at=now)
    a.full_clean()
    a.save()
    who = user.get_full_name() or user.username if user else f"group {group.name}"
    audit(actor, "delegate" if delegated_by else "assign",
          f"{role.name} → {who} ({a.scope_label}{', until ' + f'{ends_at:%d %b %Y}' if ends_at else ''})",
          {"role": role.pk, "user": getattr(user, "pk", None), "group": getattr(group, "pk", None),
           "assignment": a.pk, "reason": reason})
    return a


def revoke(actor, assignment):
    if assignment.revoked_at:
        return
    assignment.revoked_at = timezone.now()
    assignment.revoked_by = actor
    assignment.save(update_fields=["revoked_at", "revoked_by"])
    audit(actor, "revoke", f"Removed {assignment}", {"assignment": assignment.pk})


def delegable_roles(user):
    """Roles the user holds personally (base role or direct assignment) and may hand over."""
    from .models import AccessRole

    out = []
    for rid, scopes in snapshot(user)["roles"].items():
        if any(v.startswith("Base role") or v.startswith("Assigned directly") for *_x, v in scopes):
            role = AccessRole.objects.filter(pk=rid, is_active=True).first()
            if role and role.legacy_code != "requester":
                out.append((role, scopes[0][:2]))
    return out


def delegate(actor, to_user, roles_with_scope, ends_at, reason=""):
    if not can(actor, "access.delegate"):
        raise PermissionError("Your roles don't include handing over your access.")
    now = timezone.now()
    if ends_at is None or ends_at <= now:
        raise ValueError("Choose when the cover ends.")
    if ends_at > now + timedelta(days=MAX_DELEGATION_DAYS):
        raise ValueError(f"Cover can last at most {MAX_DELEGATION_DAYS} days.")
    if to_user.pk == actor.pk:
        raise ValueError("Choose someone else.")
    made = []
    for role, (scope, sid) in roles_with_scope:
        kwargs = {"scope": scope}
        if scope == "office":
            kwargs["scope_office_id"] = sid
        elif scope == "agency":
            kwargs["scope_agency_id"] = sid
        from tenancy.models import CountryOffice
        from accounts.models import Agency

        made.append(assign(
            actor, role, user=to_user, scope=scope,
            scope_office=CountryOffice.objects.filter(pk=kwargs.get("scope_office_id")).first(),
            scope_agency=Agency.objects.filter(pk=kwargs.get("scope_agency_id")).first(),
            ends_at=ends_at, reason=reason or "Covering while away", delegated_by=actor, skip_check=True,
        ))
    return made
