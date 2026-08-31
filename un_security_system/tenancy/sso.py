"""
tenancy/sso.py
==============

Microsoft Entra ID (Azure AD) sign-in — the room, not the furniture.

What exists now
---------------
* SSOConfiguration rows, per agency or per country office.
* A superuser page to fill them in (tenancy:sso_settings).
* The three URLs an OIDC flow needs, reserved and named, so the redirect URI
  you register in Entra today stays valid when the flow goes live.
* A metadata endpoint that shows an administrator exactly what to paste into
  the Entra app registration.

What is deliberately not built
------------------------------
The token exchange. Turning these stubs into a working login is roughly:

1.  pip install msal
2.  In sso_start: build the auth-code URL with msal.ConfidentialClientApplication,
    store the state and PKCE verifier in request.session, redirect.
3.  In sso_callback: validate state, exchange the code for tokens, read the
    id_token claims (oid, preferred_username, email, groups).
4.  Match the claim to a User by email. If none and auto_provision_users is on,
    create one in cfg's office with cfg.default_role, applying group_role_map.
5.  django.contrib.auth.login(request, user) then redirect to the dashboard.
6.  Add the Microsoft button to the login template, shown when a config with
    is_enabled and a matching email domain exists.
7.  If enforce_sso is set, hide the password form for that scope (keeping the
    superuser escape hatch when bypass_for_superusers is on).

Everything above reads configuration that already exists; no schema change is
needed to complete it.
"""

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from .services import sso_config_for, sso_config_for_email


def _resolve_config(request):
    """Find the config to use from ?scope=, ?email= or the signed-in user."""
    email = request.GET.get("email", "").strip()
    if email:
        cfg = sso_config_for_email(email)
        if cfg:
            return cfg

    scope = request.GET.get("scope", "").strip()
    if scope:
        from .services import parse_scope_key
        office, agency = parse_scope_key(scope)
        return sso_config_for(
            agency_id=agency.pk if agency else (office.agency_id if office else None),
            office_id=office.pk if office else None,
        )

    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        from .services import scope_of
        agency_id, office_id = scope_of(user)
        return sso_config_for(agency_id, office_id)
    return None


def sso_start(request):
    """
    Entry point for 'Sign in with Microsoft'.

    Reserved and named now so the Entra redirect URI never has to change. Until
    the token exchange is implemented this sends the person back to the normal
    login page with a clear message rather than failing silently.
    """
    cfg = _resolve_config(request)

    if cfg is None or not cfg.is_ready:
        messages.info(
            request,
            "Microsoft sign-in is not configured for this office yet. "
            "Use your username and password.",
        )
        return redirect("accounts:login")

    messages.info(
        request,
        f"Microsoft sign-in for {cfg.scope_label} is configured but not yet "
        "activated on this deployment. Use your username and password for now.",
    )
    return redirect("accounts:login")


def sso_callback(request):
    """Redirect URI registered with Entra. Same holding behaviour as sso_start."""
    messages.info(request, "Microsoft sign-in is not active on this deployment yet.")
    return redirect("accounts:login")


def sso_metadata(request):
    """
    What an administrator needs to paste into the Entra app registration.
    Never returns the client secret.
    """
    cfg = _resolve_config(request)
    if cfg is None:
        return JsonResponse({"configured": False}, status=404)

    return JsonResponse({
        "configured": True,
        "scope": cfg.scope_label,
        "provider": cfg.get_provider_display(),
        "enabled": cfg.is_enabled,
        "ready": cfg.is_ready,
        "tenant_id": cfg.tenant_id,
        "client_id": cfg.client_id,
        "authority": cfg.resolved_authority,
        "redirect_uri": cfg.redirect_uri or request.build_absolute_uri(
            reverse("tenancy:sso_callback")
        ),
        "scopes": cfg.scopes.split(),
        "allowed_email_domains": cfg.domain_list(),
        "auto_provision_users": cfg.auto_provision_users,
        "enforce_sso": cfg.enforce_sso,
    })
