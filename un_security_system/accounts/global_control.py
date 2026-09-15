"""
tenancy/global_control.py
=========================

UNPASS platform-wide master switches.

Precedence
----------
GLOBAL OFF -> superuser bypass -> country-office grant -> agency grant
-> catalogue default.

Persistence
-----------
The existing FeatureGrant table is reused for the global master state:

    agency = NULL
    country_office = NULL
    feature_code = <module>
    enabled = True/False

Normal agency/office resolvers never select these rows, because they query a
specific agency_id or office_id.  No schema migration is required.

Every master-switch change is also appended to FeatureAuditLog with
scope_key="global" for history and accountability.
"""

from typing import Dict, FrozenSet

from .catalog import FEATURE_CODES, FEATURES, FEATURES_BY_CODE


GLOBAL_SCOPE_KEY = "global"
GLOBAL_SCOPE_LABEL = "UNPASS platform"


def _global_grants() -> Dict[str, object]:
    from .models import FeatureGrant

    rows = (
        FeatureGrant.objects
        .filter(
            agency__isnull=True,
            country_office__isnull=True,
            feature_code__in=FEATURE_CODES,
        )
        .select_related("updated_by")
        .order_by("feature_code", "-updated_at", "-pk")
    )

    # update_or_create() keeps one logical row per code, but use a dict so this
    # is robust if historic/manual duplicate NULL-scope rows ever exist.
    out: Dict[str, object] = {}
    for row in rows:
        if row.feature_code not in out:
            out[row.feature_code] = row
    return out


def global_switch_map() -> Dict[str, bool]:
    """
    Raw master-switch state.

    Absence of a global row means ON, preserving existing UNPASS behaviour after
    these files are first deployed.
    """
    states = {code: True for code in FEATURE_CODES}
    for code, grant in _global_grants().items():
        states[code] = bool(grant.enabled)
    return states


def global_disabled_codes() -> FrozenSet[str]:
    states = global_switch_map()
    return frozenset(code for code, enabled in states.items() if not enabled)


def global_enabled_codes() -> FrozenSet[str]:
    """
    Effective globally allowed codes after feature dependencies are applied.
    """
    codes = {code for code, enabled in global_switch_map().items() if enabled}

    changed = True
    while changed:
        changed = False
        for feat in FEATURES:
            if feat.code in codes and any(
                parent not in codes for parent in feat.requires
            ):
                codes.discard(feat.code)
                changed = True

    return frozenset(codes)


def is_globally_enabled(code: str) -> bool:
    return code in global_enabled_codes()


def global_switch_details() -> Dict[str, Dict[str, object]]:
    """
    Bulk detail map for administration screens.
    """
    grants = _global_grants()
    states = {code: True for code in FEATURE_CODES}
    for code, grant in grants.items():
        states[code] = bool(grant.enabled)

    effective = {code for code, enabled in states.items() if enabled}
    changed = True
    while changed:
        changed = False
        for feat in FEATURES:
            if feat.code in effective and any(
                parent not in effective for parent in feat.requires
            ):
                effective.discard(feat.code)
                changed = True

    details: Dict[str, Dict[str, object]] = {}
    for code in FEATURE_CODES:
        feat = FEATURES_BY_CODE.get(code)
        grant = grants.get(code)
        blocked_by = []
        if feat:
            blocked_by = [
                parent for parent in feat.requires
                if parent not in effective
            ]

        details[code] = {
            "code": code,
            "name": feat.name if feat else code,
            "enabled": states[code],
            "effective": code in effective,
            "blocked_by": blocked_by,
            "grant": grant,
            "reason": (getattr(grant, "notes", "") or "") if grant else "",
            "updated_at": getattr(grant, "updated_at", None),
            "updated_by": getattr(grant, "updated_by", None),
        }

    return details


def global_switch_detail(code: str) -> Dict[str, object]:
    return global_switch_details().get(
        code,
        {
            "code": code,
            "name": code,
            "enabled": False,
            "effective": False,
            "blocked_by": [],
            "grant": None,
            "reason": "",
            "updated_at": None,
            "updated_by": None,
        },
    )


def set_global_feature(
    *,
    code: str,
    enabled: bool,
    actor=None,
    reason: str = "",
):
    """
    Create/update the NULL-scope FeatureGrant used as the platform master state.

    Existing agency and country-office FeatureGrant rows are never altered.
    """
    if code not in FEATURES_BY_CODE:
        raise ValueError(f"Unknown feature code: {code}")

    from .models import FeatureAuditLog, FeatureGrant

    qs = (
        FeatureGrant.objects
        .filter(
            agency__isnull=True,
            country_office__isnull=True,
            feature_code=code,
        )
        .order_by("-updated_at", "-pk")
    )
    grant = qs.first()

    if grant is None:
        grant = FeatureGrant.objects.create(
            agency=None,
            country_office=None,
            feature_code=code,
            enabled=enabled,
            notes=(reason or "").strip(),
            updated_by=actor,
            is_paid=False,
            cost_amount=None,
            cost_currency="USD",
            valid_until=None,
        )
    else:
        grant.enabled = enabled
        grant.notes = (reason or "").strip()
        grant.updated_by = actor
        grant.is_paid = False
        grant.cost_amount = None
        grant.cost_currency = "USD"
        grant.valid_until = None
        grant.save(update_fields=[
            "enabled",
            "notes",
            "updated_by",
            "is_paid",
            "cost_amount",
            "cost_currency",
            "valid_until",
            "updated_at",
        ])

        # PostgreSQL UNIQUE constraints allow multiple NULL values.  If a
        # historic/manual duplicate global row exists, keep the newest one only.
        qs.exclude(pk=grant.pk).delete()

    FeatureAuditLog.objects.create(
        actor=actor,
        action="feature_on" if enabled else "feature_off",
        scope_key=GLOBAL_SCOPE_KEY,
        scope_label=GLOBAL_SCOPE_LABEL,
        feature_code=code,
        detail=(reason or "").strip(),
    )

    # FeatureGrant's post_save signal also invalidates the cache. Keep this
    # explicit call so a test harness that has not loaded signals is still safe.
    from .services import bump_cache
    bump_cache()

    return grant
