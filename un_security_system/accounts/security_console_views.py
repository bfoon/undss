import csv
from collections import Counter, defaultdict
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.functions import TruncDay, TruncHour
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from tenancy.models import CountryOffice

from .security_console import (
    DEFAULT_GLOBAL_POLICY,
    EVENT_LABELS,
    EVENT_LOGIN_FAILURE,
    EVENT_LOGIN_SUCCESS,
    EVENT_OTP_CHALLENGE,
    EVENT_OTP_FAILURE,
    EVENT_OTP_SUCCESS,
    EVENT_PASSWORD_ACCEPTED,
    SCOPE_GLOBAL,
    SCOPE_OFFICE,
    SecurityAuthEvent,
    SecurityPasswordPolicy,
    available_security_scopes,
    browser_family,
    device_family,
    effective_password_policy,
    event_table_exists,
    password_policy_state,
    policy_table_exists,
    resolve_security_scope,
)


PERIOD_CHOICES = (
    ("1", "Last 24 hours"),
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("90", "Last 90 days"),
)


class PasswordPolicyForm(forms.Form):
    use_global = forms.BooleanField(
        required=False,
        label="Use global password policy",
    )
    min_length = forms.IntegerField(
        min_value=8,
        max_value=128,
        initial=8,
        label="Minimum password length",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    require_uppercase = forms.BooleanField(required=False, label="Require uppercase letter")
    require_lowercase = forms.BooleanField(required=False, label="Require lowercase letter")
    require_number = forms.BooleanField(required=False, label="Require number")
    require_symbol = forms.BooleanField(required=False, label="Require special character")
    block_common_passwords = forms.BooleanField(
        required=False,
        label="Block common passwords",
    )
    block_personal_info = forms.BooleanField(
        required=False,
        label="Block passwords similar to username/name/email",
    )
    block_numeric_only = forms.BooleanField(
        required=False,
        label="Block numeric-only passwords",
    )
    prevent_current_reuse = forms.BooleanField(
        required=False,
        label="Prevent reuse of current password",
    )


def _period(request):
    value = (request.GET.get("period") or "7").strip()
    allowed = {v for v, _ in PERIOD_CHOICES}
    if value not in allowed:
        value = "7"
    days = int(value)
    return value, timezone.now() - timedelta(days=days), dict(PERIOD_CHOICES)[value]


def _require_scope(request):
    scope = resolve_security_scope(
        request.user,
        request.GET.get("scope") or request.POST.get("scope") or "",
    )
    if scope is None:
        raise PermissionDenied("You do not have access to ICT Security Analytics.")
    return scope


def _events_for_scope(scope):
    qs = SecurityAuthEvent.objects.all()
    if scope.kind == SCOPE_OFFICE:
        qs = qs.filter(country_office_id=scope.scope_id)
    return qs


def _display_user(event):
    if event.user_id and event.user:
        return event.user.get_full_name() or event.user.username
    return event.identifier_hint or "Unknown account"


def _event_counts(qs):
    return {
        row["event_type"]: row["total"]
        for row in qs.values("event_type")
        .annotate(total=Count("id"))
        .order_by()
    }


def _percentage(numerator, denominator):
    return round(numerator * 100.0 / denominator, 1) if denominator else 0.0


def _trend_data(qs, period_value):
    use_hour = period_value == "1"
    trunc = TruncHour("at") if use_hour else TruncDay("at")

    rows = (
        qs.filter(
            event_type__in=[
                EVENT_LOGIN_SUCCESS,
                EVENT_LOGIN_FAILURE,
                EVENT_OTP_SUCCESS,
                EVENT_OTP_FAILURE,
            ]
        )
        .annotate(bucket=trunc)
        .values("bucket", "event_type")
        .annotate(total=Count("id"))
        .order_by("bucket")
    )

    buckets = {}
    for row in rows:
        bucket = row["bucket"]
        if not bucket:
            continue
        label = (
            timezone.localtime(bucket).strftime("%d %b %H:00")
            if use_hour
            else timezone.localtime(bucket).strftime("%d %b")
        )
        buckets.setdefault(label, {})
        buckets[label][row["event_type"]] = row["total"]

    labels = list(buckets.keys())
    return {
        "labels": labels,
        "login_success": [
            buckets[label].get(EVENT_LOGIN_SUCCESS, 0) for label in labels
        ],
        "login_failure": [
            buckets[label].get(EVENT_LOGIN_FAILURE, 0) for label in labels
        ],
        "otp_success": [
            buckets[label].get(EVENT_OTP_SUCCESS, 0) for label in labels
        ],
        "otp_failure": [
            buckets[label].get(EVENT_OTP_FAILURE, 0) for label in labels
        ],
    }


def _hourly_activity(qs):
    values = [0] * 24
    for at in qs.values_list("at", flat=True):
        if at:
            values[timezone.localtime(at).hour] += 1
    return {
        "labels": [f"{hour:02d}:00" for hour in range(24)],
        "values": values,
    }


def _browser_data(qs):
    browser_counts = Counter()
    device_counts = Counter()

    for ua in qs.exclude(user_agent="").values_list("user_agent", flat=True):
        browser_counts[browser_family(ua)] += 1
        device_counts[device_family(ua)] += 1

    return (
        {
            "labels": list(browser_counts.keys()) or ["No data"],
            "values": list(browser_counts.values()) or [1],
        },
        {
            "labels": list(device_counts.keys()) or ["No data"],
            "values": list(device_counts.values()) or [1],
        },
    )


def _risk_signals(qs):
    failures = qs.filter(
        event_type__in=[EVENT_LOGIN_FAILURE, EVENT_OTP_FAILURE]
    )

    signals = []

    repeated_ips = list(
        failures.exclude(ip_address__isnull=True)
        .values("ip_address")
        .annotate(total=Count("id"), users=Count("user_id", distinct=True))
        .filter(total__gte=5)
        .order_by("-total")[:8]
    )
    if repeated_ips:
        signals.append(
            {
                "severity": "high",
                "title": "Repeated failures from source IPs",
                "detail": (
                    f"{len(repeated_ips)} IP address(es) reached the "
                    "5-failure review threshold."
                ),
            }
        )

    repeated_users = list(
        failures.exclude(user_id__isnull=True)
        .values("user_id")
        .annotate(total=Count("id"))
        .filter(total__gte=3)
        .order_by("-total")[:8]
    )
    if repeated_users:
        signals.append(
            {
                "severity": "warning",
                "title": "Repeated failures against user accounts",
                "detail": (
                    f"{len(repeated_users)} account(s) recorded at least "
                    "3 failed authentication events."
                ),
            }
        )

    unknown = failures.filter(user_id__isnull=True).count()
    if unknown:
        signals.append(
            {
                "severity": "info",
                "title": "Unknown-account login attempts",
                "detail": f"{unknown} failed attempt(s) did not map to a known user.",
            }
        )

    otp_success = qs.filter(event_type=EVENT_OTP_SUCCESS).count()
    otp_failure = qs.filter(event_type=EVENT_OTP_FAILURE).count()
    otp_total = otp_success + otp_failure
    if otp_total >= 5 and _percentage(otp_failure, otp_total) >= 30:
        signals.append(
            {
                "severity": "warning",
                "title": "High OTP failure ratio",
                "detail": (
                    f"{_percentage(otp_failure, otp_total)}% of OTP "
                    "verification attempts failed."
                ),
            }
        )

    if not signals:
        signals.append(
            {
                "severity": "success",
                "title": "No threshold alerts",
                "detail": "No configured review threshold was crossed in this period.",
            }
        )

    return signals, repeated_ips, repeated_users


def _build_dashboard_context(request, scope):
    period_value, start, period_label = _period(request)
    qs = _events_for_scope(scope).filter(at__gte=start).select_related("user")

    counts = _event_counts(qs)

    password_ok = counts.get(EVENT_PASSWORD_ACCEPTED, 0)
    login_failures = counts.get(EVENT_LOGIN_FAILURE, 0)
    credential_attempts = password_ok + login_failures

    login_success = counts.get(EVENT_LOGIN_SUCCESS, 0)
    otp_challenges = counts.get(EVENT_OTP_CHALLENGE, 0)
    otp_success = counts.get(EVENT_OTP_SUCCESS, 0)
    otp_failure = counts.get(EVENT_OTP_FAILURE, 0)
    otp_attempts = otp_success + otp_failure

    unique_users = (
        qs.exclude(user_id__isnull=True)
        .values("user_id")
        .distinct()
        .count()
    )
    unique_ips = (
        qs.exclude(ip_address__isnull=True)
        .values("ip_address")
        .distinct()
        .count()
    )

    trusted_logins = qs.filter(
        event_type=EVENT_LOGIN_SUCCESS,
        auth_method="trusted_device",
    ).count()
    otp_logins = qs.filter(
        event_type=EVENT_LOGIN_SUCCESS,
        auth_method="email_otp",
    ).count()

    browser_data, device_data = _browser_data(qs)

    failure_reasons_rows = list(
        qs.filter(event_type__in=[EVENT_LOGIN_FAILURE, EVENT_OTP_FAILURE])
        .values("reason")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    failure_reasons = {
        "labels": [
            (row["reason"] or "unspecified").replace("_", " ").title()
            for row in failure_reasons_rows
        ] or ["No failures"],
        "values": [row["total"] for row in failure_reasons_rows] or [0],
    }

    login_paths = {
        "labels": ["Trusted device", "Email OTP"],
        "values": [trusted_logins, otp_logins],
    }

    risk_signals, repeated_ips, repeated_users = _risk_signals(qs)

    # Resolve repeated-user labels without exposing users outside scope.
    user_ids = [row["user_id"] for row in repeated_users]
    user_map = {
        u.pk: (u.get_full_name() or u.username)
        for u in User.objects.filter(pk__in=user_ids)
    }
    for row in repeated_users:
        row["label"] = user_map.get(row["user_id"], f"User #{row['user_id']}")

    recent_events = list(qs.order_by("-at")[:30])

    effective_policy = effective_password_policy(
        scope.scope_id if scope.kind == SCOPE_OFFICE else None
    )

    return {
        "scope": scope,
        "available_scopes": available_security_scopes(request.user),
        "period_choices": PERIOD_CHOICES,
        "period_value": period_value,
        "period_label": period_label,
        "generated_at": timezone.now(),
        "table_ready": event_table_exists(),
        "metrics": {
            "credential_attempts": credential_attempts,
            "password_accepted": password_ok,
            "login_failures": login_failures,
            "credential_success_rate": _percentage(password_ok, credential_attempts),
            "completed_logins": login_success,
            "otp_challenges": otp_challenges,
            "otp_attempts": otp_attempts,
            "otp_success": otp_success,
            "otp_failure": otp_failure,
            "otp_success_rate": _percentage(otp_success, otp_attempts),
            "unique_users": unique_users,
            "unique_ips": unique_ips,
            "trusted_logins": trusted_logins,
            "otp_logins": otp_logins,
        },
        "trend": _trend_data(qs, period_value),
        "hourly": _hourly_activity(qs),
        "browser_data": browser_data,
        "device_data": device_data,
        "failure_reasons": failure_reasons,
        "login_paths": login_paths,
        "risk_signals": risk_signals,
        "repeated_ips": repeated_ips,
        "repeated_users": repeated_users,
        "recent_events": recent_events,
        "effective_policy": effective_policy,
    }


@login_required
def security_dashboard(request):
    scope = _require_scope(request)

    if not event_table_exists():
        messages.warning(
            request,
            "Security telemetry table is not installed yet. "
            "Run: python manage.py ensure_security_console",
        )

    return render(
        request,
        "accounts/ict/security/dashboard.html",
        _build_dashboard_context(request, scope),
    )


@login_required
def security_events(request):
    scope = _require_scope(request)
    if not event_table_exists():
        raise Http404("Security event table is not installed.")

    period_value, start, period_label = _period(request)
    qs = _events_for_scope(scope).filter(at__gte=start).select_related("user")

    event_type = (request.GET.get("event") or "").strip()
    result = (request.GET.get("result") or "").strip()
    q = (request.GET.get("q") or "").strip()

    if event_type:
        qs = qs.filter(event_type=event_type)
    if result:
        qs = qs.filter(result=result)
    if q:
        qs = qs.filter(
            Q(user__username__icontains=q)
            | Q(user__first_name__icontains=q)
            | Q(user__last_name__icontains=q)
            | Q(identifier_hint__icontains=q)
            | Q(ip_address__icontains=q)
        )

    paginator = Paginator(qs.order_by("-at"), 50)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "accounts/ict/security/events.html",
        {
            "scope": scope,
            "available_scopes": available_security_scopes(request.user),
            "period_choices": PERIOD_CHOICES,
            "period_value": period_value,
            "period_label": period_label,
            "event_choices": sorted(EVENT_LABELS.items()),
            "selected_event": event_type,
            "selected_result": result,
            "q": q,
            "page_obj": page,
        },
    )


@login_required
def security_export_csv(request):
    scope = _require_scope(request)
    if not event_table_exists():
        raise Http404("Security event table is not installed.")

    period_value, start, period_label = _period(request)
    qs = (
        _events_for_scope(scope)
        .filter(at__gte=start)
        .select_related("user")
        .order_by("-at")
    )

    response = HttpResponse(content_type="text/csv")
    filename = (
        f"unpass-security-{slugify(scope.label) or 'platform'}-"
        f"{period_value}d.csv"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(
        [
            "Time",
            "Event",
            "Result",
            "User",
            "Identifier hint",
            "IP address",
            "Authentication method",
            "Reason",
            "Browser",
            "Device",
        ]
    )

    for event in qs:
        writer.writerow(
            [
                timezone.localtime(event.at).strftime("%Y-%m-%d %H:%M:%S"),
                event.event_label,
                event.result,
                _display_user(event),
                event.identifier_hint,
                event.ip_address or "",
                event.auth_method,
                event.reason,
                event.browser_family,
                event.device_family,
            ]
        )

    return response


@login_required
def security_download_report(request):
    scope = _require_scope(request)
    context = _build_dashboard_context(request, scope)
    html = render_to_string(
        "accounts/ict/security/report.html",
        context=context,
        request=request,
    )

    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    filename = (
        f"unpass-security-report-{slugify(scope.label) or 'platform'}-"
        f"{context['period_value']}d.html"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _initial_policy(scope):
    if scope.kind == SCOPE_GLOBAL:
        row = None
        if policy_table_exists():
            row = SecurityPasswordPolicy.objects.filter(
                scope_kind=SCOPE_GLOBAL,
                scope_id=0,
            ).first()
        values = row.as_dict() if row else dict(DEFAULT_GLOBAL_POLICY)
        values["use_global"] = False
        return values

    state = password_policy_state(scope.scope_id)
    values = dict(state["effective"])
    values.pop("source", None)
    values["use_global"] = state["inherits_global"]
    return values


@login_required
def password_policy(request):
    scope = _require_scope(request)

    if not policy_table_exists():
        messages.error(
            request,
            "Password-policy table is not installed yet. "
            "Run: python manage.py ensure_security_console",
        )
        return redirect(
            f"/ict/security/?scope={scope.key}"
        )

    # Global policy is superuser-only.
    if scope.kind == SCOPE_GLOBAL and not request.user.is_superuser:
        raise PermissionDenied

    can_edit = (
        request.user.is_superuser
        or (
            scope.kind == SCOPE_OFFICE
            and scope.can_manage_policy
        )
    )

    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied("You cannot change this password policy.")

        form = PasswordPolicyForm(request.POST)

        if form.is_valid():
            cleaned = form.cleaned_data

            if scope.kind == SCOPE_OFFICE and cleaned.get("use_global"):
                SecurityPasswordPolicy.objects.filter(
                    scope_kind=SCOPE_OFFICE,
                    scope_id=scope.scope_id,
                ).delete()
                messages.success(
                    request,
                    "Country Office password policy now inherits the global policy.",
                )
            else:
                defaults = {
                    "inherit_global": False,
                    "min_length": cleaned["min_length"],
                    "require_uppercase": cleaned["require_uppercase"],
                    "require_lowercase": cleaned["require_lowercase"],
                    "require_number": cleaned["require_number"],
                    "require_symbol": cleaned["require_symbol"],
                    "block_common_passwords": cleaned["block_common_passwords"],
                    "block_personal_info": cleaned["block_personal_info"],
                    "block_numeric_only": cleaned["block_numeric_only"],
                    "prevent_current_reuse": cleaned["prevent_current_reuse"],
                    "updated_by": request.user,
                }
                SecurityPasswordPolicy.objects.update_or_create(
                    scope_kind=scope.kind,
                    scope_id=scope.scope_id,
                    defaults=defaults,
                )
                messages.success(
                    request,
                    (
                        "Global password policy updated."
                        if scope.kind == SCOPE_GLOBAL
                        else "Country Office password-policy override saved."
                    ),
                )

            return redirect(
                f"/ict/security/password-policy/?scope={scope.key}"
            )
    else:
        form = PasswordPolicyForm(initial=_initial_policy(scope))

    state = (
        password_policy_state(scope.scope_id)
        if scope.kind == SCOPE_OFFICE
        else {
            "effective": effective_password_policy(None),
            "inherits_global": False,
            "override": None,
        }
    )

    return render(
        request,
        "accounts/ict/security/password_policy.html",
        {
            "scope": scope,
            "available_scopes": available_security_scopes(request.user),
            "form": form,
            "state": state,
            "can_edit": can_edit,
            "is_global": scope.kind == SCOPE_GLOBAL,
        },
    )
