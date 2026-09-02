"""
tenancy/services.py
===================

Everything that answers "is this feature on for this person, and who can they
see inside it?".

Import from here rather than querying FeatureGrant directly — the cache
invalidation and the dependency rules live in this module.

Quick reference
---------------
    from tenancy.services import has_feature, enabled_features, visible_users_for

    if has_feature(request.user, "esign"):
        ...

    recipients = visible_users_for(request.user, "esign")
"""

from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from .catalog import (
    DEFAULT_ENABLED_CODES,
    DELEGABLE_CODES,
    FEATURES,
    FEATURES_BY_CODE,
    FEATURE_CODES,
    SHAREABLE_CODES,
)

CACHE_PREFIX = "unpass:tenancy"
CACHE_TTL = getattr(settings, "TENANCY_CACHE_TTL", 300)
VERSION_KEY = f"{CACHE_PREFIX}:version"

#: When True a superuser sees every module regardless of grants. Set
#: TENANCY_SUPERUSER_BYPASS = False in settings to make the superuser
#: experience match what an office actually has switched on.
SUPERUSER_BYPASS = getattr(settings, "TENANCY_SUPERUSER_BYPASS", True)


# ---------------------------------------------------------------------------
# Cache versioning
# ---------------------------------------------------------------------------

def cache_version() -> int:
    v = cache.get(VERSION_KEY)
    if v is None:
        v = 1
        cache.set(VERSION_KEY, v, None)
    return v


def bump_cache() -> int:
    """Invalidate every resolved feature set. Called from signals."""
    try:
        return cache.incr(VERSION_KEY)
    except ValueError:
        cache.set(VERSION_KEY, 1, None)
        return 1


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------

def scope_of(user) -> Tuple[Optional[int], Optional[int]]:
    """Return (agency_id, country_office_id) for a user. Either may be None."""
    if not user or not getattr(user, "is_authenticated", False):
        return None, None
    office_id = getattr(user, "country_office_id", None)
    agency_id = getattr(user, "agency_id", None)
    if agency_id is None and office_id:
        office = getattr(user, "country_office", None)
        agency_id = getattr(office, "agency_id", None)
    return agency_id, office_id


def scope_key_for(agency_id, office_id) -> str:
    if office_id:
        return f"office:{office_id}"
    if agency_id:
        return f"agency:{agency_id}"
    return "global"


def parse_scope_key(key: str):
    """'office:3' -> (CountryOffice instance, None); 'agency:2' -> (None, Agency)."""
    from django.apps import apps
    if not key or ":" not in key:
        return None, None
    kind, _, raw = key.partition(":")
    try:
        pk = int(raw)
    except (TypeError, ValueError):
        return None, None
    if kind == "office":
        CountryOffice = apps.get_model("tenancy", "CountryOffice")
        return CountryOffice.objects.filter(pk=pk).select_related("agency").first(), None
    if kind == "agency":
        Agency = apps.get_model("accounts", "Agency")
        return None, Agency.objects.filter(pk=pk).first()
    return None, None


# ---------------------------------------------------------------------------
# Core resolution
# ---------------------------------------------------------------------------

def _apply_dependencies(codes: Set[str]) -> Set[str]:
    """Drop any feature whose parents are not all present. Repeats until stable."""
    changed = True
    while changed:
        changed = False
        for feat in FEATURES:
            if feat.code in codes and any(p not in codes for p in feat.requires):
                codes.discard(feat.code)
                changed = True
    return codes


def resolve_scope_features(agency_id, office_id) -> FrozenSet[str]:
    """
    Resolve the enabled feature codes for a raw (agency_id, office_id) pair.
    Cached; call bump_cache() after any grant change.
    """
    from .models import FeatureGrant

    key = f"{CACHE_PREFIX}:{cache_version()}:feat:{agency_id or 0}:{office_id or 0}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    today = timezone.localdate()
    resolved: Dict[str, bool] = {c: (c in DEFAULT_ENABLED_CODES) for c in FEATURE_CODES}

    if agency_id:
        agency_grants = FeatureGrant.objects.for_agency(agency_id).only(
            "feature_code", "enabled", "valid_until"
        )
        for g in agency_grants:
            if g.feature_code in resolved:
                expired = bool(g.valid_until and g.valid_until < today)
                resolved[g.feature_code] = g.enabled and not expired

    if office_id:
        office_grants = FeatureGrant.objects.for_office(office_id).only(
            "feature_code", "enabled", "valid_until"
        )
        for g in office_grants:
            if g.feature_code in resolved:
                expired = bool(g.valid_until and g.valid_until < today)
                resolved[g.feature_code] = g.enabled and not expired

    codes = _apply_dependencies({c for c, on in resolved.items() if on})
    result = frozenset(codes)
    cache.set(key, result, CACHE_TTL)
    return result


def enabled_features(user) -> FrozenSet[str]:
    """Enabled feature codes for a user, honouring the superuser bypass."""
    if user is not None and getattr(user, "is_superuser", False) and SUPERUSER_BYPASS:
        return frozenset(FEATURE_CODES)
    if not user or not getattr(user, "is_authenticated", False):
        return frozenset()
    agency_id, office_id = scope_of(user)
    return resolve_scope_features(agency_id, office_id)


def has_feature(user, code: str) -> bool:
    """The one call you need in a view, a template tag or a permission check."""
    return code in enabled_features(user)


def office_has_feature(office, code: str) -> bool:
    """
    Feature check for a CountryOffice rather than a user.

    Needed wherever code runs outside a request — signals, management commands,
    Celery tasks — where there is no request.user to ask. Example: the visitors
    app syncs group members from a meeting whenever a MeetingAttendee is
    accepted, and that signal must not fire for an office that has switched
    meeting-linked visitors off.
    """
    if office is None:
        return False
    office_id = getattr(office, "pk", office)
    agency_id = getattr(office, "agency_id", None)
    if agency_id is None:
        from .models import CountryOffice
        obj = CountryOffice.objects.filter(pk=office_id).only("agency_id").first()
        if obj is None:
            return False
        agency_id = obj.agency_id
    return code in resolve_scope_features(agency_id, office_id)


def has_all(user, codes: Iterable[str]) -> bool:
    active = enabled_features(user)
    return all(c in active for c in codes)


def has_any(user, codes: Iterable[str]) -> bool:
    active = enabled_features(user)
    return any(c in active for c in codes)


def feature_map(user) -> Dict[str, bool]:
    """{'esign': True, 'mailroom': False, ...} — what the context processor injects."""
    active = enabled_features(user)
    return {code: (code in active) for code in FEATURE_CODES}


def scope_feature_map(agency_id, office_id) -> Dict[str, bool]:
    """Same shape, but for an arbitrary scope. Used by the superuser console."""
    active = resolve_scope_features(agency_id, office_id)
    return {code: (code in active) for code in FEATURE_CODES}


def explain(agency_id, office_id, code: str) -> Dict[str, object]:
    """
    Where a resolved value came from. Powers the "why is this off?" tooltip in
    the console and is handy in the shell when debugging a support ticket.
    """
    from .models import FeatureGrant

    feat = FEATURES_BY_CODE.get(code)
    info = {
        "code": code,
        "name": feat.name if feat else code,
        "source": "catalogue default",
        "value": code in DEFAULT_ENABLED_CODES,
        "blocked_by": [],
    }

    if agency_id:
        g = FeatureGrant.objects.for_agency(agency_id).filter(feature_code=code).first()
        if g:
            info["source"] = f"agency grant ({g.scope_label})"
            info["value"] = g.effective
    if office_id:
        g = FeatureGrant.objects.for_office(office_id).filter(feature_code=code).first()
        if g:
            info["source"] = f"office grant ({g.scope_label})"
            info["value"] = g.effective

    if feat and feat.requires:
        active = resolve_scope_features(agency_id, office_id)
        missing = [p for p in feat.requires if p not in active]
        if missing:
            info["blocked_by"] = missing
            info["value"] = False
    return info


# ---------------------------------------------------------------------------
# Writing grants
# ---------------------------------------------------------------------------

def set_feature(*, code: str, enabled: bool, actor=None, agency=None,
                country_office=None, cascade_children: bool = True, **extra):
    """
    Create or update one grant. Pass exactly one of ``agency`` / ``country_office``.

    When a parent feature is switched off and ``cascade_children`` is True, the
    child grants are written off too, so the console reflects reality rather
    than leaving orphan switches looking on.
    """
    from .models import FeatureGrant, FeatureAuditLog
    from .catalog import children_of

    if bool(agency) == bool(country_office):
        raise ValueError("Pass exactly one of agency or country_office.")

    defaults = {"enabled": enabled, "updated_by": actor}
    defaults.update(extra)

    grant, _created = FeatureGrant.objects.update_or_create(
        agency=agency, country_office=country_office, feature_code=code,
        defaults=defaults,
    )

    if not enabled and cascade_children:
        for child in children_of(code):
            FeatureGrant.objects.update_or_create(
                agency=agency, country_office=country_office,
                feature_code=child.code,
                defaults={"enabled": False, "updated_by": actor},
            )

    FeatureAuditLog.objects.create(
        actor=actor,
        action="feature_on" if enabled else "feature_off",
        scope_key=grant.scope_key,
        scope_label=grant.scope_label,
        feature_code=code,
    )
    bump_cache()
    return grant


def copy_features(*, source_key: str, target_key: str, actor=None) -> int:
    """
    Mirror one scope onto another.

    Writes an explicit grant on the target for every feature in the catalogue,
    matching the source's *resolved* state — not just its explicit rows. That
    way the target genuinely ends up looking like the source, instead of
    silently keeping settings the source happened to inherit rather than state.
    """
    from .models import FeatureGrant

    src_office, src_agency = parse_scope_key(source_key)
    dst_office, dst_agency = parse_scope_key(target_key)
    if not (src_office or src_agency) or not (dst_office or dst_agency):
        return 0

    src_agency_id = src_office.agency_id if src_office else src_agency.pk
    src_office_id = src_office.pk if src_office else None
    source_state = scope_feature_map(src_agency_id, src_office_id)

    # Carry commercial terms across where the source stated them explicitly.
    explicit = {}
    src_grants = (FeatureGrant.objects.for_office(src_office_id) if src_office_id
                  else FeatureGrant.objects.for_agency(src_agency_id))
    for g in src_grants:
        explicit[g.feature_code] = g

    count = 0
    for code, enabled in source_state.items():
        g = explicit.get(code)
        FeatureGrant.objects.update_or_create(
            agency=dst_agency, country_office=dst_office, feature_code=code,
            defaults={
                "enabled": enabled,
                "is_paid": g.is_paid if g else False,
                "cost_amount": g.cost_amount if g else None,
                "cost_currency": g.cost_currency if g else "USD",
                "valid_until": g.valid_until if g else None,
                "updated_by": actor,
            },
        )
        count += 1
    bump_cache()
    return count


# ---------------------------------------------------------------------------
# Directory sharing
# ---------------------------------------------------------------------------

def linked_scope_keys(user, code: str) -> Set[str]:
    """
    Scope keys whose users are visible to ``user`` inside feature ``code``,
    including the user's own scope.
    """
    from .models import DirectoryShare

    agency_id, office_id = scope_of(user)

    # What the user sees with no links at all: their own office. Falling back to
    # the whole agency only when they have no office, otherwise every sibling
    # office in the agency would leak into every picker.
    if office_id:
        own_visible = {f"office:{office_id}"}
    elif agency_id:
        own_visible = {f"agency:{agency_id}"}
    else:
        return set()

    # What a share may be addressed to in order to reach this user: their
    # office and their agency, since a link can be drawn at either level.
    my_keys = set(own_visible)
    if office_id:
        my_keys.add(f"office:{office_id}")
    if agency_id:
        my_keys.add(f"agency:{agency_id}")

    if code not in SHAREABLE_CODES:
        return own_visible

    key = f"{CACHE_PREFIX}:{cache_version()}:share:{code}:{agency_id or 0}:{office_id or 0}"
    cached = cache.get(key)
    if cached is not None:
        return set(cached)

    q = Q()
    if office_id:
        q |= Q(source_office_id=office_id) | Q(target_office_id=office_id, is_mutual=True)
    if agency_id:
        q |= Q(source_agency_id=agency_id) | Q(target_agency_id=agency_id, is_mutual=True)

    reachable = set(own_visible)
    for share in DirectoryShare.objects.filter(q, feature_code=code, is_active=True):
        if share.source_key in my_keys:
            reachable.add(share.target_key)
        elif share.is_mutual and share.target_key in my_keys:
            reachable.add(share.source_key)

    cache.set(key, list(reachable), CACHE_TTL)
    return reachable


def visible_users_for(user, code: str, base_queryset=None):
    """
    Users that ``user`` may address inside feature ``code``: their own office
    plus every office or agency linked to it for that feature.

    Superusers see everyone. Feed this into recipient pickers, assignee
    dropdowns and search — it is the whole point of the linking feature.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    qs = base_queryset if base_queryset is not None else User.objects.all()
    qs = qs.filter(is_active=True)

    if getattr(user, "is_superuser", False):
        return qs

    keys = linked_scope_keys(user, code)
    if not keys:
        return qs.none()

    office_ids = [int(k.split(":")[1]) for k in keys if k.startswith("office:")]
    agency_ids = [int(k.split(":")[1]) for k in keys if k.startswith("agency:")]

    q = Q(pk__in=[])
    if office_ids:
        q |= Q(country_office_id__in=office_ids)
    if agency_ids:
        q |= Q(agency_id__in=agency_ids)
    return qs.filter(q).distinct()


# ---------------------------------------------------------------------------
# Admin delegation
# ---------------------------------------------------------------------------

def admin_role(user, office=None):
    """The user's active OfficeAdmin row, or None."""
    from .models import OfficeAdmin

    if not user or not getattr(user, "is_authenticated", False):
        return None
    qs = OfficeAdmin.objects.filter(user=user, is_active=True).select_related("country_office")
    if office is not None:
        qs = qs.filter(country_office=office)
    else:
        office_id = getattr(user, "country_office_id", None)
        if office_id:
            qs = qs.filter(country_office_id=office_id)
    return qs.first()


def is_main_admin(user, office=None) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    role = admin_role(user, office)
    return bool(role and role.level == "main")


def is_office_admin(user, office=None) -> bool:
    """Main or sub."""
    if getattr(user, "is_superuser", False):
        return True
    return admin_role(user, office) is not None


def can_manage_user(actor, target) -> bool:
    """
    Superuser: anyone.
    Main admin: anyone in their office, including sub admins — but never a
                superuser or staff account.
    Sub admin:  ordinary users in their office only — never another admin.

    The superuser carve-out matters. A superuser assigned to a country office
    would otherwise be an ordinary member of it, so the office's main admin
    could reset their password and take over the platform. Office administration
    is delegated downward; it must not reach back up.
    """
    if getattr(actor, "is_superuser", False):
        return True

    # Only a superuser may administer a superuser or a staff account.
    if getattr(target, "is_superuser", False) or getattr(target, "is_staff", False):
        return False

    role = admin_role(actor)
    if not role or not role.can_manage_users:
        return False
    if getattr(target, "country_office_id", None) != role.country_office_id:
        return False
    if role.level == "main":
        return True
    return not is_office_admin(target, role.country_office)


def manageable_users(actor, base_queryset=None):
    """Queryset of users ``actor`` may administer."""
    from django.contrib.auth import get_user_model
    from .models import OfficeAdmin

    User = get_user_model()
    qs = base_queryset if base_queryset is not None else User.objects.all()

    if getattr(actor, "is_superuser", False):
        return qs

    role = admin_role(actor)
    if not role or not role.can_manage_users:
        return qs.none()

    qs = qs.filter(country_office_id=role.country_office_id)

    # Keep this in step with can_manage_user. A list that shows people the
    # actor cannot actually act on produces confusing "not found" errors, and
    # worse, invites someone to try.
    qs = qs.exclude(is_superuser=True).exclude(is_staff=True)

    if role.level == "sub":
        admin_ids = OfficeAdmin.objects.filter(
            country_office_id=role.country_office_id, is_active=True
        ).values_list("user_id", flat=True)
        qs = qs.exclude(pk__in=list(admin_ids))
    return qs


def can_toggle(user, code: str, office=None) -> bool:
    """Who is allowed to flip a given switch."""
    if getattr(user, "is_superuser", False):
        return True
    if code not in DELEGABLE_CODES:
        return False
    role = admin_role(user, office)
    return bool(role and role.level == "main" and role.can_toggle_delegable_features)


def grant_admin(*, user, country_office, level="sub", actor=None, **flags):
    """Appoint an office admin. Main admins may only appoint sub admins."""
    from .models import OfficeAdmin, FeatureAuditLog

    defaults = {"level": level, "is_active": True, "granted_by": actor, "revoked_at": None}
    defaults.update(flags)
    if level == "main":
        defaults.setdefault("can_toggle_delegable_features", True)

    role, _ = OfficeAdmin.objects.update_or_create(
        user=user, country_office=country_office, defaults=defaults
    )
    FeatureAuditLog.objects.create(
        actor=actor, action="admin_granted",
        scope_key=country_office.scope_key, scope_label=str(country_office),
        detail=f"{user} as {role.get_level_display()}",
    )
    bump_cache()
    return role


def revoke_admin(*, role, actor=None):
    from .models import FeatureAuditLog

    role.revoke()
    FeatureAuditLog.objects.create(
        actor=actor, action="admin_revoked",
        scope_key=role.country_office.scope_key, scope_label=str(role.country_office),
        detail=f"{role.user} ({role.get_level_display()})",
    )
    bump_cache()
    return role


# ---------------------------------------------------------------------------
# SSO lookup
# ---------------------------------------------------------------------------

def sso_config_for(agency_id=None, office_id=None):
    """Office config wins over agency config. Returns None if neither exists."""
    from .models import SSOConfiguration

    if office_id:
        cfg = SSOConfiguration.objects.filter(country_office_id=office_id).first()
        if cfg:
            return cfg
    if agency_id:
        return SSOConfiguration.objects.filter(
            agency_id=agency_id, country_office__isnull=True
        ).first()
    return None


def sso_config_for_email(email: str):
    """
    Find a config by the email domain. Used on the login page to decide whether
    to show the Microsoft button before the person has authenticated.
    """
    from .models import SSOConfiguration

    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].strip().lower()
    for cfg in SSOConfiguration.objects.filter(is_enabled=True):
        if domain in cfg.domain_list():
            return cfg
    return None
