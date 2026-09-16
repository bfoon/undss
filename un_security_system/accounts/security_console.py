"""
UNPASS ICT Security Console
===========================

Security telemetry + inherited Country Office password policy.

Privacy rules:
- never store passwords
- never store OTP codes
- never store raw unknown login identifiers
- Country Office is snapshotted on every event so historic analytics do not
  move when a user changes office.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import (
    CommonPasswordValidator,
    NumericPasswordValidator,
    UserAttributeSimilarityValidator,
)
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, models
from django.utils import timezone

from tenancy.models import CountryOffice, OfficeAdmin


User = get_user_model()

SCOPE_GLOBAL = "global"
SCOPE_OFFICE = "office"

DEFAULT_GLOBAL_POLICY = {
    "min_length": 8,
    "require_uppercase": False,
    "require_lowercase": False,
    "require_number": False,
    "require_symbol": False,
    "block_common_passwords": True,
    "block_personal_info": True,
    "block_numeric_only": True,
    "prevent_current_reuse": False,
}

EVENT_LOGIN_SUCCESS = "login_success"
EVENT_LOGIN_FAILURE = "login_failure"
EVENT_PASSWORD_ACCEPTED = "password_accepted"
EVENT_OTP_CHALLENGE = "otp_challenge"
EVENT_OTP_SUCCESS = "otp_success"
EVENT_OTP_FAILURE = "otp_failure"
EVENT_PASSWORD_CHANGED = "password_changed"

EVENT_LABELS = {
    EVENT_LOGIN_SUCCESS: "Login success",
    EVENT_LOGIN_FAILURE: "Login failure",
    EVENT_PASSWORD_ACCEPTED: "Password accepted",
    EVENT_OTP_CHALLENGE: "OTP challenge issued",
    EVENT_OTP_SUCCESS: "OTP success",
    EVENT_OTP_FAILURE: "OTP failure",
    EVENT_PASSWORD_CHANGED: "Password changed",
}


class SecurityAuthEvent(models.Model):
    """
    Authentication event with immutable Agency/CO snapshots.

    The user FK is nullable because unknown usernames still matter to the
    platform security view. Unknown identifiers are hashed + masked; the raw
    value is never stored.
    """

    at = models.DateTimeField(default=timezone.now, db_index=True)
    event_type = models.CharField(max_length=32, db_index=True)
    result = models.CharField(max_length=16, blank=True, default="")
    reason = models.CharField(max_length=64, blank=True, default="")
    auth_method = models.CharField(max_length=32, blank=True, default="")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    agency_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    country_office_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True
    )

    identifier_hash = models.CharField(max_length=64, blank=True, default="")
    identifier_hint = models.CharField(max_length=100, blank=True, default="")

    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    user_agent = models.CharField(max_length=400, blank=True, default="")
    device_hash = models.CharField(max_length=64, blank=True, default="")
    request_path = models.CharField(max_length=180, blank=True, default="")

    class Meta:
        app_label = "accounts"
        managed = False
        db_table = "accounts_security_auth_event"
        ordering = ["-at", "-id"]
        indexes = [
            models.Index(fields=["country_office_id", "at"]),
            models.Index(fields=["event_type", "at"]),
            models.Index(fields=["ip_address", "at"]),
            models.Index(fields=["user", "at"]),
        ]

    @property
    def event_label(self):
        return EVENT_LABELS.get(self.event_type, self.event_type.replace("_", " ").title())

    @property
    def browser_family(self):
        return browser_family(self.user_agent)

    @property
    def device_family(self):
        return device_family(self.user_agent)


class SecurityPasswordPolicy(models.Model):
    """
    One global policy plus optional Country Office overrides.

    No CO row means inheritance. An office row with inherit_global=True also
    resolves to the global policy.
    """

    scope_kind = models.CharField(max_length=12)
    scope_id = models.PositiveIntegerField(default=0)
    inherit_global = models.BooleanField(default=True)

    min_length = models.PositiveSmallIntegerField(default=8)
    require_uppercase = models.BooleanField(default=False)
    require_lowercase = models.BooleanField(default=False)
    require_number = models.BooleanField(default=False)
    require_symbol = models.BooleanField(default=False)

    block_common_passwords = models.BooleanField(default=True)
    block_personal_info = models.BooleanField(default=True)
    block_numeric_only = models.BooleanField(default=True)
    prevent_current_reuse = models.BooleanField(default=False)

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "accounts"
        managed = False
        db_table = "accounts_security_password_policy"
        unique_together = (("scope_kind", "scope_id"),)

    def as_dict(self):
        return {
            "min_length": self.min_length,
            "require_uppercase": self.require_uppercase,
            "require_lowercase": self.require_lowercase,
            "require_number": self.require_number,
            "require_symbol": self.require_symbol,
            "block_common_passwords": self.block_common_passwords,
            "block_personal_info": self.block_personal_info,
            "block_numeric_only": self.block_numeric_only,
            "prevent_current_reuse": self.prevent_current_reuse,
        }


@dataclass(frozen=True)
class SecurityScope:
    kind: str
    scope_id: int
    label: str
    can_manage_policy: bool = False

    @property
    def key(self):
        return f"{self.kind}:{self.scope_id}"


def event_table_exists():
    try:
        return SecurityAuthEvent._meta.db_table in connection.introspection.table_names()
    except Exception:
        return False


def policy_table_exists():
    try:
        return (
            SecurityPasswordPolicy._meta.db_table
            in connection.introspection.table_names()
        )
    except Exception:
        return False


def _policy_office_admin(user, office_id):
    """
    Any active CO admin who has password-administration authority may set the
    CO password policy. This reuses the existing OfficeAdmin permission rather
    than inventing a second admin hierarchy.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return OfficeAdmin.objects.filter(
        user=user,
        country_office_id=office_id,
        is_active=True,
        can_reset_passwords=True,
    ).exists()


def _any_office_admin(user, office_id):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return OfficeAdmin.objects.filter(
        user=user,
        country_office_id=office_id,
        is_active=True,
    ).exists()


def available_security_scopes(user):
    if not user or not getattr(user, "is_authenticated", False):
        return []

    if user.is_superuser:
        scopes = [
            SecurityScope(
                kind=SCOPE_GLOBAL,
                scope_id=0,
                label="All Country Offices / Platform",
                can_manage_policy=True,
            )
        ]
        for office in (
            CountryOffice.objects.filter(is_active=True)
            .select_related("agency")
            .order_by("agency__code", "name")
        ):
            scopes.append(
                SecurityScope(
                    kind=SCOPE_OFFICE,
                    scope_id=office.pk,
                    label=str(office),
                    can_manage_policy=True,
                )
            )
        return scopes

    office_id = getattr(user, "country_office_id", None)
    if not office_id:
        return []

    is_admin = _any_office_admin(user, office_id)
    is_ict = getattr(user, "role", "") == "ict_focal"

    if not (is_admin or is_ict):
        return []

    office = (
        CountryOffice.objects.filter(pk=office_id, is_active=True)
        .select_related("agency")
        .first()
    )
    if not office:
        return []

    return [
        SecurityScope(
            kind=SCOPE_OFFICE,
            scope_id=office.pk,
            label=str(office),
            # CO admins with the existing password-admin permission can
            # change policy. A bare ICT focal can view analytics but cannot
            # change a CO-wide password rule.
            can_manage_policy=_policy_office_admin(user, office.pk),
        )
    ]


def resolve_security_scope(user, requested_key=""):
    scopes = available_security_scopes(user)
    if not scopes:
        return None

    requested_key = (requested_key or "").strip().lower()
    if requested_key:
        for scope in scopes:
            if scope.key.lower() == requested_key:
                return scope

    return scopes[0]


def security_access_summary(user):
    scopes = available_security_scopes(user)
    if not scopes:
        return {
            "can_view": False,
            "can_manage_policy": False,
            "preferred_scope": "",
        }

    return {
        "can_view": True,
        "can_manage_policy": any(s.can_manage_policy for s in scopes),
        "preferred_scope": scopes[0].key,
    }


def _hash(value):
    value = (value or "").strip().lower()
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def mask_identifier(value):
    value = (value or "").strip()
    if not value:
        return ""

    if "@" in value:
        local, _, domain = value.partition("@")
        prefix = local[:2] if len(local) > 1 else local[:1]
        return f"{prefix}***@{domain}"

    prefix = value[:2] if len(value) > 1 else value[:1]
    return f"{prefix}***"


def browser_family(user_agent):
    ua = (user_agent or "").lower()
    if "edg/" in ua:
        return "Microsoft Edge"
    if "firefox/" in ua:
        return "Firefox"
    if "chrome/" in ua and "chromium" not in ua:
        return "Chrome"
    if "safari/" in ua and "chrome/" not in ua:
        return "Safari"
    if "curl/" in ua:
        return "CLI / curl"
    if "python" in ua:
        return "Python client"
    return "Other"


def device_family(user_agent):
    ua = (user_agent or "").lower()
    if any(x in ua for x in ("android", "iphone", "ipad", "mobile")):
        return "Mobile / Tablet"
    if any(x in ua for x in ("windows", "macintosh", "linux", "x11")):
        return "Desktop / Laptop"
    return "Other"


def client_ip(request):
    if request is None:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def lookup_user(identifier):
    value = (identifier or "").strip()
    if not value:
        return None

    qs = User.objects.all()
    if "@" in value:
        return qs.filter(email__iexact=value).first()
    return qs.filter(username__iexact=value).first()


def record_auth_event(
    *,
    request=None,
    event_type,
    result="",
    reason="",
    auth_method="",
    user=None,
    identifier="",
    device_id="",
):
    """
    Best-effort telemetry. Authentication must never fail because logging failed.
    """
    try:
        if not event_table_exists():
            return

        if user is None and identifier:
            user = lookup_user(identifier)

        agency_id = getattr(user, "agency_id", None) if user else None
        office_id = getattr(user, "country_office_id", None) if user else None

        ua = ""
        path = ""
        if request is not None:
            ua = (request.META.get("HTTP_USER_AGENT") or "")[:400]
            path = (getattr(request, "path", "") or "")[:180]

        SecurityAuthEvent.objects.create(
            event_type=event_type,
            result=(result or "")[:16],
            reason=(reason or "")[:64],
            auth_method=(auth_method or "")[:32],
            user=user,
            agency_id=agency_id,
            country_office_id=office_id,
            identifier_hash=_hash(identifier),
            identifier_hint=mask_identifier(identifier)[:100],
            ip_address=client_ip(request),
            user_agent=ua,
            device_hash=_hash(device_id),
            request_path=path,
        )
    except Exception:
        # Security telemetry must be fail-open for availability.
        return


def _global_policy_row():
    if not policy_table_exists():
        return None
    try:
        return SecurityPasswordPolicy.objects.filter(
            scope_kind=SCOPE_GLOBAL,
            scope_id=0,
        ).first()
    except DatabaseError:
        return None


def effective_password_policy(office_id=None):
    source = "built-in fallback"
    values = dict(DEFAULT_GLOBAL_POLICY)

    global_row = _global_policy_row()
    if global_row:
        values.update(global_row.as_dict())
        source = "Global policy"

    if office_id and policy_table_exists():
        try:
            office_row = SecurityPasswordPolicy.objects.filter(
                scope_kind=SCOPE_OFFICE,
                scope_id=office_id,
            ).first()
        except DatabaseError:
            office_row = None

        if office_row and not office_row.inherit_global:
            values.update(office_row.as_dict())
            source = "Country Office override"

    values["source"] = source
    return values


def password_policy_state(office_id=None):
    effective = effective_password_policy(office_id)
    override = None

    if office_id and policy_table_exists():
        try:
            override = SecurityPasswordPolicy.objects.filter(
                scope_kind=SCOPE_OFFICE,
                scope_id=office_id,
            ).first()
        except DatabaseError:
            override = None

    return {
        "effective": effective,
        "override": override,
        "inherits_global": (
            office_id is not None
            and (override is None or override.inherit_global)
        ),
    }


def validate_password_for_policy(password, *, user=None, office_id=None):
    if user is not None and office_id is None:
        office_id = getattr(user, "country_office_id", None)

    policy = effective_password_policy(office_id)
    errors = []

    if len(password or "") < int(policy["min_length"]):
        errors.append(
            f"Password must contain at least {policy['min_length']} characters."
        )

    if policy["require_uppercase"] and not re.search(r"[A-Z]", password or ""):
        errors.append("Password must contain at least one uppercase letter.")

    if policy["require_lowercase"] and not re.search(r"[a-z]", password or ""):
        errors.append("Password must contain at least one lowercase letter.")

    if policy["require_number"] and not re.search(r"\d", password or ""):
        errors.append("Password must contain at least one number.")

    if policy["require_symbol"] and not re.search(
        r"[^A-Za-z0-9\s]", password or ""
    ):
        errors.append("Password must contain at least one special character.")

    if policy["block_common_passwords"]:
        try:
            CommonPasswordValidator().validate(password, user)
        except ValidationError as exc:
            errors.extend(exc.messages)

    if policy["block_personal_info"] and user is not None:
        try:
            UserAttributeSimilarityValidator().validate(password, user)
        except ValidationError as exc:
            errors.extend(exc.messages)

    if policy["block_numeric_only"]:
        try:
            NumericPasswordValidator().validate(password, user)
        except ValidationError as exc:
            errors.extend(exc.messages)

    if (
        policy["prevent_current_reuse"]
        and user is not None
        and getattr(user, "pk", None)
        and user.has_usable_password()
        and user.check_password(password)
    ):
        errors.append("Your new password cannot be the same as your current password.")

    if errors:
        raise ValidationError(errors)


def validate_password_for_office(password, *, user=None, office_id=None):
    validate_password_for_policy(
        password,
        user=user,
        office_id=office_id,
    )


class SecurityPasswordPolicyValidator:
    """
    Django password validator that resolves the Country Office policy at runtime.
    """

    def validate(self, password, user=None):
        validate_password_for_policy(password, user=user)

    def get_help_text(self):
        return (
            "Your password must meet the effective UNPASS password policy "
            "for your Country Office."
        )


def _url_name(request):
    match = getattr(request, "resolver_match", None)
    return getattr(match, "url_name", "") if match else ""


def _looks_like_login(request):
    name = _url_name(request)
    path = (getattr(request, "path", "") or "").lower()
    return name == "login" or path.endswith("/login/")


def _looks_like_otp(request):
    name = _url_name(request)
    path = (getattr(request, "path", "") or "").lower()
    return name == "otp_verify" or "otp" in path and "verify" in path


def _looks_like_password_change(request):
    name = _url_name(request)
    return name in {
        "password_change",
        "change_password",
        "password_reset_confirm",
    }


class SecurityTelemetryMiddleware:
    """
    Observe the existing authentication flow without changing its behavior.

    It classifies POST outcomes after the normal login / OTP view has run.
    Raw passwords and OTP codes are never inspected or stored.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        method = getattr(request, "method", "GET")
        pending_before = None
        try:
            pending_before = request.session.get("otp_user_id")
        except Exception:
            pass

        user_before = getattr(request, "user", None)
        response = self.get_response(request)

        if method != "POST":
            return response

        try:
            if _looks_like_login(request):
                identifier = (request.POST.get("login") or "").strip()
                authenticated = (
                    getattr(request, "user", None)
                    and request.user.is_authenticated
                )
                pending_after = request.session.get("otp_user_id")

                if authenticated:
                    # Trusted-device flow reached a final authenticated session.
                    device_id = request.COOKIES.get("trusted_device_id", "")
                    record_auth_event(
                        request=request,
                        event_type=EVENT_PASSWORD_ACCEPTED,
                        result="success",
                        auth_method="trusted_device",
                        user=request.user,
                        identifier=identifier,
                        device_id=device_id,
                    )
                    record_auth_event(
                        request=request,
                        event_type=EVENT_LOGIN_SUCCESS,
                        result="success",
                        auth_method="trusted_device",
                        user=request.user,
                        identifier=identifier,
                        device_id=device_id,
                    )
                elif pending_after:
                    user = User.objects.filter(pk=pending_after).first()
                    device_id = request.session.get("otp_device_id", "")
                    record_auth_event(
                        request=request,
                        event_type=EVENT_PASSWORD_ACCEPTED,
                        result="success",
                        auth_method="password_otp",
                        user=user,
                        identifier=identifier,
                        device_id=device_id,
                    )
                    record_auth_event(
                        request=request,
                        event_type=EVENT_OTP_CHALLENGE,
                        result="issued",
                        auth_method="email_otp",
                        user=user,
                        identifier=identifier,
                        device_id=device_id,
                    )
                else:
                    record_auth_event(
                        request=request,
                        event_type=EVENT_LOGIN_FAILURE,
                        result="failure",
                        reason="credentials_rejected",
                        auth_method="password",
                        identifier=identifier,
                    )

            elif _looks_like_otp(request):
                user = (
                    User.objects.filter(pk=pending_before).first()
                    if pending_before
                    else None
                )
                authenticated = (
                    getattr(request, "user", None)
                    and request.user.is_authenticated
                )

                if authenticated:
                    record_auth_event(
                        request=request,
                        event_type=EVENT_OTP_SUCCESS,
                        result="success",
                        auth_method="email_otp",
                        user=user or request.user,
                    )
                    record_auth_event(
                        request=request,
                        event_type=EVENT_LOGIN_SUCCESS,
                        result="success",
                        auth_method="email_otp",
                        user=user or request.user,
                    )
                elif pending_before:
                    record_auth_event(
                        request=request,
                        event_type=EVENT_OTP_FAILURE,
                        result="failure",
                        reason="invalid_or_expired",
                        auth_method="email_otp",
                        user=user,
                    )

            elif _looks_like_password_change(request):
                if (
                    getattr(request, "user", None)
                    and request.user.is_authenticated
                    and getattr(response, "status_code", 200) in (301, 302, 303)
                ):
                    record_auth_event(
                        request=request,
                        event_type=EVENT_PASSWORD_CHANGED,
                        result="success",
                        auth_method="password",
                        user=request.user,
                    )
        except Exception:
            # Never make login unavailable because telemetry failed.
            pass

        return response
