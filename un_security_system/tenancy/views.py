"""
tenancy/views.py
================

The superuser console, plus the office-admin pages.

Manual POST handling throughout — no Django forms — to match the rest of the
codebase.

Pages
-----
    /platform/                       overview: every office and what it has on
    /platform/features/<scope_key>/  the switch matrix for one agency or office
    /platform/offices/               create and edit country offices
    /platform/admins/<office_pk>/    appoint main and sub admins
    /platform/sharing/               link offices/agencies inside a feature
    /platform/sso/                   Microsoft SSO configuration
    /platform/audit/                 who changed what
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .catalog import (
    CATEGORY_LABELS,
    DELEGABLE_CODES,
    FEATURES,
    FEATURES_BY_CODE,
    SHAREABLE_CHOICES,
    features_by_category,
)
from .decorators import main_admin_required, superuser_required
from .models import (
    CountryOffice,
    DirectoryShare,
    FeatureAuditLog,
    FeatureGrant,
    OfficeAdmin,
    SSOConfiguration,
)
from .services import (
    admin_role,
    bump_cache,
    can_toggle,
    copy_features,
    explain,
    grant_admin,
    manageable_users,
    parse_scope_key,
    revoke_admin,
    scope_feature_map,
)

User = get_user_model()


def _agency_model():
    from django.apps import apps
    return apps.get_model("accounts", "Agency")


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@login_required
@superuser_required
def platform_overview(request):
    """Every agency and office, with a count of what each has switched on."""
    Agency = _agency_model()
    agencies = Agency.objects.prefetch_related("country_offices").order_by("code")

    rows = []
    for agency in agencies:
        agency_flags = scope_feature_map(agency.pk, None)
        offices = []
        for office in agency.country_offices.all().order_by("name"):
            flags = scope_feature_map(agency.pk, office.pk)
            offices.append({
                "office": office,
                "on_count": sum(1 for v in flags.values() if v),
                "on_codes": [c for c, v in flags.items() if v],
                "user_count": office.users.count() if hasattr(office, "users") else 0,
                "admin_count": office.admins.filter(is_active=True).count(),
                "scope_key": office.scope_key,
            })
        rows.append({
            "agency": agency,
            "scope_key": f"agency:{agency.pk}",
            "on_count": sum(1 for v in agency_flags.values() if v),
            "offices": offices,
            "office_count": len(offices),
        })

    return render(request, "tenancy/overview.html", {
        "rows": rows,
        "total_features": len(FEATURES),
        "recent_changes": FeatureAuditLog.objects.all()[:12],
    })


# ---------------------------------------------------------------------------
# Feature matrix for one scope
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["GET", "POST"])
def feature_console(request, scope_key):
    """
    The switch panel. Superusers see and may change everything. A main admin
    reaching their own office sees the full picture but may only flip the
    features the catalogue marks delegable.
    """
    office, agency = parse_scope_key(scope_key)
    if not office and not agency:
        raise Http404("Unknown scope.")

    is_super = request.user.is_superuser
    if not is_super:
        role = admin_role(request.user)
        if not role or not office or role.country_office_id != office.pk or role.level != "main":
            messages.error(request, "You can only manage your own office.")
            return redirect("dashboard:dashboard")

    agency_id = office.agency_id if office else agency.pk
    office_id = office.pk if office else None

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "copy" and is_super:
            source = request.POST.get("source_scope", "")
            n = copy_features(source_key=source, target_key=scope_key, actor=request.user)
            messages.success(request, f"Copied {n} feature settings across.")
            return redirect("tenancy:feature_console", scope_key=scope_key)

        submitted = set(request.POST.getlist("features"))
        changed = 0
        for feat in FEATURES:
            if not (is_super or (feat.code in DELEGABLE_CODES and
                                 can_toggle(request.user, feat.code, office))):
                continue

            wanted = feat.code in submitted
            current = FeatureGrant.objects.filter(
                agency=None if office else agency,
                country_office=office,
                feature_code=feat.code,
            ).first()
            if current and current.enabled == wanted:
                continue

            from .services import set_feature
            set_feature(
                code=feat.code, enabled=wanted, actor=request.user,
                agency=None if office else agency, country_office=office,
            )
            changed += 1

        if changed:
            messages.success(request, f"Saved. {changed} module(s) changed.")
        else:
            messages.info(request, "No changes to save.")
        return redirect("tenancy:feature_console", scope_key=scope_key)

    # ---- GET -------------------------------------------------------------
    resolved = scope_feature_map(agency_id, office_id)

    explicit = {}
    grants = (FeatureGrant.objects.for_office(office_id) if office_id
              else FeatureGrant.objects.for_agency(agency_id))
    for g in grants:
        explicit[g.feature_code] = g

    inherited = scope_feature_map(agency_id, None) if office_id else {}

    groups = []
    for cat_code, cat_label, feats in features_by_category():
        items = []
        for feat in feats:
            grant = explicit.get(feat.code)
            items.append({
                "feature": feat,
                "enabled": resolved.get(feat.code, False),
                "grant": grant,
                "is_explicit": grant is not None,
                "inherited_value": inherited.get(feat.code) if office_id else None,
                "editable": is_super or can_toggle(request.user, feat.code, office),
                "parent_names": [
                    FEATURES_BY_CODE[p].name for p in feat.requires
                    if p in FEATURES_BY_CODE
                ],
                "parents_ok": all(resolved.get(p) for p in feat.requires),
            })
        groups.append({"code": cat_code, "label": cat_label, "items": items})

    other_scopes = []
    if is_super:
        Agency = _agency_model()
        for a in Agency.objects.order_by("code"):
            if f"agency:{a.pk}" != scope_key:
                other_scopes.append((f"agency:{a.pk}", f"{a.code} (agency-wide)"))
        for o in CountryOffice.objects.select_related("agency").order_by("agency__code", "name"):
            if o.scope_key != scope_key:
                other_scopes.append((o.scope_key, str(o)))

    return render(request, "tenancy/feature_console.html", {
        "scope_key": scope_key,
        "office": office,
        "agency": agency or (office.agency if office else None),
        "groups": groups,
        "enabled_count": sum(1 for v in resolved.values() if v),
        "total_count": len(FEATURES),
        "other_scopes": other_scopes,
        "is_super": is_super,
        "delegable_codes": sorted(DELEGABLE_CODES),
    })


@login_required
@superuser_required
def feature_explain(request, scope_key, code):
    """Small JSON endpoint behind the 'why?' link on each switch."""
    office, agency = parse_scope_key(scope_key)
    agency_id = office.agency_id if office else (agency.pk if agency else None)
    office_id = office.pk if office else None
    return JsonResponse(explain(agency_id, office_id, code))


# ---------------------------------------------------------------------------
# Country offices
# ---------------------------------------------------------------------------

@login_required
@superuser_required
@require_http_methods(["GET", "POST"])
def office_list(request):
    Agency = _agency_model()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create":
            agency_id = request.POST.get("agency")
            name = (request.POST.get("name") or "").strip()
            code = (request.POST.get("code") or "").strip().upper()
            if not (agency_id and name and code):
                messages.error(request, "Agency, name and code are all required.")
                return redirect("tenancy:office_list")
            if CountryOffice.objects.filter(agency_id=agency_id, code=code).exists():
                messages.error(request, f"That agency already has an office coded {code}.")
                return redirect("tenancy:office_list")

            office = CountryOffice.objects.create(
                agency_id=agency_id,
                name=name,
                code=code,
                country=(request.POST.get("country") or "").strip(),
                city=(request.POST.get("city") or "").strip(),
                timezone_name=(request.POST.get("timezone_name") or "UTC").strip(),
                contact_email=(request.POST.get("contact_email") or "").strip(),
                contact_phone=(request.POST.get("contact_phone") or "").strip(),
            )
            FeatureAuditLog.objects.create(
                actor=request.user, action="office_created",
                scope_key=office.scope_key, scope_label=str(office),
            )
            messages.success(request, f"Created {office}. Switch its modules on next.")
            return redirect("tenancy:feature_console", scope_key=office.scope_key)

        if action == "update":
            office = get_object_or_404(CountryOffice, pk=request.POST.get("office_id"))
            office.name = (request.POST.get("name") or office.name).strip()
            office.country = (request.POST.get("country") or "").strip()
            office.city = (request.POST.get("city") or "").strip()
            office.timezone_name = (request.POST.get("timezone_name") or "UTC").strip()
            office.contact_email = (request.POST.get("contact_email") or "").strip()
            office.contact_phone = (request.POST.get("contact_phone") or "").strip()
            office.is_active = request.POST.get("is_active") == "on"
            office.save()
            FeatureAuditLog.objects.create(
                actor=request.user, action="office_updated",
                scope_key=office.scope_key, scope_label=str(office),
            )
            messages.success(request, f"Updated {office}.")
            return redirect("tenancy:office_list")

        if action == "set_default":
            office = get_object_or_404(CountryOffice, pk=request.POST.get("office_id"))
            CountryOffice.objects.filter(agency=office.agency).update(is_default=False)
            office.is_default = True
            office.save(update_fields=["is_default"])
            messages.success(request, f"{office} is now the default office for {office.agency}.")
            return redirect("tenancy:office_list")

    offices = (CountryOffice.objects
               .select_related("agency")
               .annotate(
                   n_users=Count("users", distinct=True),
                   n_admins=Count("admins", filter=Q(admins__is_active=True), distinct=True),
               )
               .order_by("agency__code", "name"))

    return render(request, "tenancy/offices.html", {
        "offices": offices,
        "agencies": Agency.objects.order_by("code"),
    })


# ---------------------------------------------------------------------------
# Office admins
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["GET", "POST"])
def office_admins(request, pk):
    """
    Superuser: appoints main admins (and sub admins if they want to).
    Main admin: appoints and revokes sub admins in their own office only.
    """
    office = get_object_or_404(CountryOffice.objects.select_related("agency"), pk=pk)

    is_super = request.user.is_superuser
    role = admin_role(request.user, office)
    if not is_super and not (role and role.level == "main"):
        messages.error(request, "Only a main admin of this office can manage its admins.")
        return redirect("dashboard:dashboard")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "grant":
            user_id = request.POST.get("user")
            level = request.POST.get("level", OfficeAdmin.LEVEL_SUB)

            if level == OfficeAdmin.LEVEL_MAIN and not is_super:
                messages.error(request, "Only the superuser can appoint a main admin.")
                return redirect("tenancy:office_admins", pk=office.pk)

            target = get_object_or_404(User, pk=user_id)
            if getattr(target, "country_office_id", None) != office.pk:
                messages.error(
                    request,
                    f"{target} is not assigned to {office}. Move them to this office first.",
                )
                return redirect("tenancy:office_admins", pk=office.pk)

            grant_admin(
                user=target, country_office=office, level=level, actor=request.user,
                can_manage_users=request.POST.get("can_manage_users") == "on",
                can_reset_passwords=request.POST.get("can_reset_passwords") == "on",
                can_invite=request.POST.get("can_invite") == "on",
                can_toggle_delegable_features=(
                    level == OfficeAdmin.LEVEL_MAIN
                    and request.POST.get("can_toggle_delegable_features") == "on"
                ),
            )
            messages.success(request, f"{target} is now a {dict(OfficeAdmin.LEVEL_CHOICES)[level].lower()}.")
            return redirect("tenancy:office_admins", pk=office.pk)

        if action == "revoke":
            target_role = get_object_or_404(
                OfficeAdmin, pk=request.POST.get("role_id"), country_office=office
            )
            if target_role.level == OfficeAdmin.LEVEL_MAIN and not is_super:
                messages.error(request, "Only the superuser can revoke a main admin.")
                return redirect("tenancy:office_admins", pk=office.pk)
            revoke_admin(role=target_role, actor=request.user)
            messages.success(request, f"Revoked admin rights for {target_role.user}.")
            return redirect("tenancy:office_admins", pk=office.pk)

    admins = (OfficeAdmin.objects
              .filter(country_office=office)
              .select_related("user", "granted_by")
              .order_by("-is_active", "level", "user__username"))

    existing_ids = admins.filter(is_active=True).values_list("user_id", flat=True)
    candidates = (User.objects
                  .filter(country_office=office, is_active=True)
                  .exclude(pk__in=list(existing_ids))
                  .order_by("last_name", "first_name", "username"))

    return render(request, "tenancy/office_admins.html", {
        "office": office,
        "admins": admins,
        "candidates": candidates,
        "is_super": is_super,
        "levels": OfficeAdmin.LEVEL_CHOICES,
        "user_count": User.objects.filter(country_office=office).count(),
    })


@login_required
@main_admin_required
def office_users(request, pk):
    """Read-only roster for a main or sub admin: who they may administer."""
    office = get_object_or_404(CountryOffice.objects.select_related("agency"), pk=pk)
    users = manageable_users(request.user).filter(country_office=office).order_by(
        "last_name", "first_name", "username"
    )
    return render(request, "tenancy/office_users.html", {
        "office": office,
        "users": users,
    })


# ---------------------------------------------------------------------------
# Directory sharing
# ---------------------------------------------------------------------------

@login_required
@superuser_required
@require_http_methods(["GET", "POST"])
def sharing_links(request):
    Agency = _agency_model()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create":
            code = request.POST.get("feature_code")
            source = request.POST.get("source_scope", "")
            target = request.POST.get("target_scope", "")
            if source == target:
                messages.error(request, "Pick two different scopes.")
                return redirect("tenancy:sharing_links")

            s_office, s_agency = parse_scope_key(source)
            t_office, t_agency = parse_scope_key(target)
            if not (s_office or s_agency) or not (t_office or t_agency):
                messages.error(request, "Both a source and a target are required.")
                return redirect("tenancy:sharing_links")

            share = DirectoryShare(
                feature_code=code,
                source_office=s_office, source_agency=s_agency,
                target_office=t_office, target_agency=t_agency,
                is_mutual=request.POST.get("is_mutual") == "on",
                note=(request.POST.get("note") or "").strip(),
                created_by=request.user,
            )
            try:
                share.full_clean()
            except Exception as exc:
                messages.error(request, str(exc))
                return redirect("tenancy:sharing_links")
            share.save()

            FeatureAuditLog.objects.create(
                actor=request.user, action="share_created",
                feature_code=code, scope_key=share.source_key,
                scope_label=f"{share.source_label} → {share.target_label}",
            )
            bump_cache()
            messages.success(request, f"Linked {share.source_label} and {share.target_label}.")
            return redirect("tenancy:sharing_links")

        if action == "toggle":
            share = get_object_or_404(DirectoryShare, pk=request.POST.get("share_id"))
            share.is_active = not share.is_active
            share.save(update_fields=["is_active"])
            bump_cache()
            messages.success(
                request,
                f"Link {'reactivated' if share.is_active else 'paused'}.",
            )
            return redirect("tenancy:sharing_links")

        if action == "delete":
            share = get_object_or_404(DirectoryShare, pk=request.POST.get("share_id"))
            label = str(share)
            FeatureAuditLog.objects.create(
                actor=request.user, action="share_removed",
                feature_code=share.feature_code,
                scope_key=share.source_key, scope_label=label,
            )
            share.delete()
            bump_cache()
            messages.success(request, "Link removed.")
            return redirect("tenancy:sharing_links")

    scopes = []
    for a in Agency.objects.order_by("code"):
        scopes.append((f"agency:{a.pk}", f"{a.code} — whole agency"))
    for o in CountryOffice.objects.select_related("agency").order_by("agency__code", "name"):
        scopes.append((o.scope_key, str(o)))

    return render(request, "tenancy/sharing.html", {
        "shares": DirectoryShare.objects.select_related(
            "source_agency", "source_office", "target_agency", "target_office"
        ).order_by("feature_code", "-created_at"),
        "scopes": scopes,
        "shareable_features": SHAREABLE_CHOICES,
    })


# ---------------------------------------------------------------------------
# Microsoft SSO configuration
# ---------------------------------------------------------------------------

@login_required
@superuser_required
@require_http_methods(["GET", "POST"])
def sso_settings(request):
    """
    Stores Entra ID settings so an office can be switched to SSO later. The
    sign-in flow itself is not wired yet — see tenancy/sso.py.
    """
    Agency = _agency_model()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "save":
            scope_key = request.POST.get("scope", "")
            office, agency = parse_scope_key(scope_key)
            if not office and not agency:
                messages.error(request, "Choose an agency or an office first.")
                return redirect("tenancy:sso_settings")

            cfg, _ = SSOConfiguration.objects.get_or_create(
                agency=agency, country_office=office
            )
            cfg.display_name = (request.POST.get("display_name") or "Sign in with Microsoft").strip()
            cfg.tenant_id = (request.POST.get("tenant_id") or "").strip()
            cfg.client_id = (request.POST.get("client_id") or "").strip()
            secret = (request.POST.get("client_secret") or "").strip()
            if secret:
                cfg.client_secret = secret
            cfg.authority = (request.POST.get("authority") or "").strip()
            cfg.redirect_uri = (request.POST.get("redirect_uri") or "").strip()
            cfg.scopes = (request.POST.get("scopes") or "openid profile email User.Read").strip()
            cfg.allowed_email_domains = (request.POST.get("allowed_email_domains") or "").strip()
            cfg.auto_provision_users = request.POST.get("auto_provision_users") == "on"
            cfg.default_role = (request.POST.get("default_role") or "requester").strip()
            cfg.enforce_sso = request.POST.get("enforce_sso") == "on"
            cfg.bypass_for_superusers = request.POST.get("bypass_for_superusers") == "on"
            cfg.is_enabled = request.POST.get("is_enabled") == "on"
            cfg.save()

            FeatureAuditLog.objects.create(
                actor=request.user, action="sso_updated",
                scope_key=cfg.scope_key, scope_label=cfg.scope_label,
            )
            bump_cache()
            messages.success(request, f"Saved SSO settings for {cfg.scope_label}.")
            return redirect("tenancy:sso_settings")

        if action == "delete":
            cfg = get_object_or_404(SSOConfiguration, pk=request.POST.get("config_id"))
            label = cfg.scope_label
            cfg.delete()
            bump_cache()
            messages.success(request, f"Removed SSO settings for {label}.")
            return redirect("tenancy:sso_settings")

    scopes = []
    for a in Agency.objects.order_by("code"):
        scopes.append((f"agency:{a.pk}", f"{a.code} — whole agency"))
    for o in CountryOffice.objects.select_related("agency").order_by("agency__code", "name"):
        scopes.append((o.scope_key, str(o)))

    return render(request, "tenancy/sso.html", {
        "configs": SSOConfiguration.objects.select_related("agency", "country_office"),
        "scopes": scopes,
        "callback_hint": request.build_absolute_uri(reverse("tenancy:sso_callback")),
        "role_choices": getattr(User, "ROLE_CHOICES", []),
    })


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@login_required
@superuser_required
def audit_log(request):
    entries = FeatureAuditLog.objects.select_related("actor")

    scope = request.GET.get("scope", "").strip()
    if scope:
        entries = entries.filter(scope_key=scope)
    action = request.GET.get("action", "").strip()
    if action:
        entries = entries.filter(action=action)

    return render(request, "tenancy/audit.html", {
        "entries": entries[:300],
        "actions": FeatureAuditLog.ACTIONS,
        "selected_action": action,
        "selected_scope": scope,
    })
