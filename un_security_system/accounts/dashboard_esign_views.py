from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.db.models.functions import TruncDay, TruncMonth
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from tenancy.models import OfficeAdmin

from .dashboard_esign_access import (
    DashboardAccessGrant,
    LEVEL_ANALYTICS,
    LEVEL_CHOICES,
    LEVEL_DRILLDOWN,
    LEVEL_LABELS,
    LEVEL_ACCESS_MANAGER,
    SCOPE_AGENCY,
    SCOPE_OFFICE,
    SCOPE_PLATFORM,
    available_scopes,
    grant_table_exists,
    resolve_scope_access,
)
from .models import (
    DocumentWorkflow,
    Envelope,
    EnvelopeRecipient,
    FormSubmission,
    FormTemplate,
    User,
    WorkflowRun,
    WorkflowTask,
)


PERIOD_CHOICES = (
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("90", "Last 90 days"),
    ("365", "Last 12 months"),
    ("all", "All time"),
)


def _period(request):
    value = (request.GET.get("period") or "30").lower()
    allowed = {v for v, _ in PERIOD_CHOICES}
    if value not in allowed:
        value = "30"

    if value == "all":
        return value, None, "All time"

    days = int(value)
    return value, timezone.now() - timedelta(days=days), dict(PERIOD_CHOICES)[value]


def _require_access(request, minimum=1):
    requested = request.GET.get("scope") or request.POST.get("scope") or ""
    access = resolve_scope_access(request.user, requested)
    if access is None or access.level < minimum:
        raise PermissionDenied("You do not have access to this dashboard level.")
    return access


def _scoped(qs, dataset, access):
    if access.kind == SCOPE_PLATFORM:
        return qs

    if access.kind == SCOPE_AGENCY:
        filters = {
            "envelopes": Q(agency_id=access.scope_id),
            "recipients": Q(envelope__agency_id=access.scope_id),
            "forms": Q(agency_id=access.scope_id),
            "submissions": Q(agency_id=access.scope_id),
            "workflows": Q(agency_id=access.scope_id),
            "runs": Q(agency_id=access.scope_id),
            "tasks": Q(run__agency_id=access.scope_id),
        }
        return qs.filter(filters[dataset])

    if access.kind == SCOPE_OFFICE:
        filters = {
            # Envelope currently has Agency + creator, but no immutable office
            # snapshot. CO eSign analytics therefore use the creator's current CO.
            "envelopes": Q(created_by__country_office_id=access.scope_id),
            "recipients": Q(
                envelope__created_by__country_office_id=access.scope_id
            ),
            "forms": Q(office_id=access.scope_id),
            "submissions": (
                Q(form__office_id=access.scope_id)
                | Q(
                    form__isnull=True,
                    submitted_by__country_office_id=access.scope_id,
                )
            ),
            "workflows": Q(office_id=access.scope_id),
            "runs": (
                Q(workflow__office_id=access.scope_id)
                | Q(
                    workflow__isnull=True,
                    initiator__country_office_id=access.scope_id,
                )
            ),
            "tasks": (
                Q(run__workflow__office_id=access.scope_id)
                | Q(
                    run__workflow__isnull=True,
                    run__initiator__country_office_id=access.scope_id,
                )
            ),
        }
        return qs.filter(filters[dataset])

    return qs.none()


def _after(qs, field, start):
    if start is None:
        return qs
    return qs.filter(**{f"{field}__gte": start})


def _status_counts(qs):
    return {
        row["status"]: row["total"]
        for row in qs.values("status")
        .annotate(total=Count("id"))
        .order_by()
    }


def _rate(done, total):
    return round(done * 100.0 / total, 1) if total else 0.0


def _duration_text(value):
    if not value:
        return "—"

    seconds = int(value.total_seconds())
    if seconds < 60:
        return "< 1 min"

    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _trend(qs, field, monthly=False):
    trunc = TruncMonth(field) if monthly else TruncDay(field)
    rows = (
        qs.annotate(bucket=trunc)
        .values("bucket")
        .annotate(total=Count("id"))
        .order_by("bucket")
    )

    output = {}
    for row in rows:
        bucket = row["bucket"]
        if not bucket:
            continue
        key = bucket.strftime("%Y-%m" if monthly else "%Y-%m-%d")
        output[key] = row["total"]

    return output


def _querysets(access, start):
    envelopes_all = _scoped(Envelope.objects.all(), "envelopes", access)
    recipients_all = _scoped(
        EnvelopeRecipient.objects.all(), "recipients", access
    )
    forms_all = _scoped(FormTemplate.objects.all(), "forms", access)
    submissions_all = _scoped(
        FormSubmission.objects.all(), "submissions", access
    )
    workflows_all = _scoped(
        DocumentWorkflow.objects.all(), "workflows", access
    )
    runs_all = _scoped(WorkflowRun.objects.all(), "runs", access)
    tasks_all = _scoped(WorkflowTask.objects.all(), "tasks", access)

    return {
        "envelopes_all": envelopes_all,
        "envelopes": _after(envelopes_all, "created_at", start),
        "recipients": _after(
            recipients_all, "envelope__created_at", start
        ),
        "forms_all": forms_all,
        "forms": _after(forms_all, "created_at", start),
        "submissions": _after(submissions_all, "created_at", start),
        "workflows_all": workflows_all,
        "workflows": _after(workflows_all, "created_at", start),
        "runs": _after(runs_all, "started_at", start),
        "tasks": _after(tasks_all, "created_at", start),
    }


@login_required
def dashboard(request):
    access = _require_access(request, 1)
    period_value, start, period_label = _period(request)
    qs = _querysets(access, start)

    envelopes = qs["envelopes"]
    recipients = qs["recipients"]
    submissions = qs["submissions"]
    runs = qs["runs"]
    tasks = qs["tasks"]

    envelope_status = _status_counts(envelopes)
    submission_status = _status_counts(submissions)
    run_status = _status_counts(runs)

    envelope_total = envelopes.count()
    envelope_completed = envelope_status.get(
        Envelope.STATUS_COMPLETED, 0
    )
    sent_population = envelopes.exclude(
        status=Envelope.STATUS_DRAFT
    ).count()

    signing_recipients = recipients.filter(
        role__in=[
            EnvelopeRecipient.ROLE_SIGNER,
            EnvelopeRecipient.ROLE_APPROVER,
        ]
    )
    signature_total = signing_recipients.count()
    signature_signed = signing_recipients.filter(
        status=EnvelopeRecipient.STATUS_SIGNED
    ).count()

    avg_envelope = (
        envelopes.filter(
            sent_at__isnull=False,
            completed_at__isnull=False,
        )
        .aggregate(
            avg=Avg(
                ExpressionWrapper(
                    F("completed_at") - F("sent_at"),
                    output_field=DurationField(),
                )
            )
        )
        .get("avg")
    )

    submission_total = submissions.count()
    submission_completed = submission_status.get(
        FormSubmission.STATUS_COMPLETED, 0
    )

    run_total = runs.count()
    run_completed = run_status.get(
        WorkflowRun.STATUS_COMPLETED, 0
    )

    avg_run = (
        runs.filter(completed_at__isnull=False)
        .aggregate(
            avg=Avg(
                ExpressionWrapper(
                    F("completed_at") - F("started_at"),
                    output_field=DurationField(),
                )
            )
        )
        .get("avg")
    )

    open_tasks = tasks.filter(status__in=WorkflowTask.OPEN)
    overdue_tasks = open_tasks.filter(
        due_at__isnull=False,
        due_at__lt=timezone.now(),
    ).count()

    active_user_ids = set(
        envelopes.exclude(created_by_id__isnull=True)
        .values_list("created_by_id", flat=True)
        .distinct()
    )
    active_user_ids.update(
        submissions.exclude(submitted_by_id__isnull=True)
        .values_list("submitted_by_id", flat=True)
        .distinct()
    )
    active_user_ids.update(
        runs.exclude(initiator_id__isnull=True)
        .values_list("initiator_id", flat=True)
        .distinct()
    )

    metrics = {
        "envelope_total": envelope_total,
        "envelope_completed": envelope_completed,
        "envelope_completion_rate": _rate(
            envelope_completed, sent_population
        ),
        "signature_total": signature_total,
        "signature_signed": signature_signed,
        "signature_completion_rate": _rate(
            signature_signed, signature_total
        ),
        "avg_envelope_completion": _duration_text(avg_envelope),
        "forms_total": qs["forms_all"].count(),
        "forms_published": qs["forms_all"].filter(
            is_published=True
        ).count(),
        "submissions_total": submission_total,
        "submissions_completed": submission_completed,
        "form_completion_rate": _rate(
            submission_completed, submission_total
        ),
        "workflows_total": qs["workflows_all"].count(),
        "workflows_active": qs["workflows_all"].filter(
            is_active=True
        ).count(),
        "runs_total": run_total,
        "runs_completed": run_completed,
        "run_completion_rate": _rate(run_completed, run_total),
        "avg_run_completion": _duration_text(avg_run),
        "open_tasks": open_tasks.count(),
        "overdue_tasks": overdue_tasks,
        "active_users": len(active_user_ids),
    }

    level2 = {}
    if access.level >= LEVEL_ANALYTICS:
        monthly = period_value in {"365", "all"}
        env_trend = _trend(envelopes, "created_at", monthly)
        form_trend = _trend(submissions, "created_at", monthly)
        run_trend = _trend(runs, "started_at", monthly)

        labels = sorted(
            set(env_trend) | set(form_trend) | set(run_trend)
        )

        level2 = {
            "trend": {
                "labels": labels,
                "envelopes": [
                    env_trend.get(label, 0) for label in labels
                ],
                "submissions": [
                    form_trend.get(label, 0) for label in labels
                ],
                "runs": [
                    run_trend.get(label, 0) for label in labels
                ],
            },
            "envelope_status": envelope_status,
            "submission_status": submission_status,
            "run_status": run_status,
            "top_forms": list(
                submissions.values("form_name")
                .annotate(total=Count("id"))
                .order_by("-total", "form_name")[:10]
            ),
            "top_flows": list(
                runs.values("workflow_name")
                .annotate(total=Count("id"))
                .order_by("-total", "workflow_name")[:10]
            ),
        }

    level3 = {}
    if access.level >= LEVEL_DRILLDOWN:
        level3 = {
            "recent_envelopes": (
                envelopes.select_related("created_by")
                .order_by("-created_at")[:8]
            ),
            "recent_submissions": (
                submissions.select_related(
                    "form", "submitted_by"
                ).order_by("-created_at")[:8]
            ),
            "recent_runs": (
                runs.select_related(
                    "workflow", "initiator"
                ).order_by("-started_at")[:8]
            ),
        }

    return render(
        request,
        "esign_analytics/dashboard.html",
        {
            "access": access,
            "available_scopes": available_scopes(request.user),
            "period_choices": PERIOD_CHOICES,
            "period_value": period_value,
            "period_label": period_label,
            "metrics": metrics,
            "level2": level2,
            "level3": level3,
            "office_attribution_note": (
                access.kind == SCOPE_OFFICE
            ),
        },
    )


DRILL_DATASETS = {
    "envelopes": "eSign Envelopes",
    "forms": "Form Templates",
    "submissions": "Form Submissions",
    "workflows": "Flows",
    "runs": "Flow Runs",
    "tasks": "Flow Tasks",
}


@login_required
def drilldown(request, dataset):
    if dataset not in DRILL_DATASETS:
        raise Http404

    access = _require_access(request, LEVEL_DRILLDOWN)
    period_value, start, period_label = _period(request)
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    columns = []
    rows = []
    status_choices = []

    if dataset == "envelopes":
        qs = _after(
            _scoped(
                Envelope.objects.all(),
                "envelopes",
                access,
            ),
            "created_at",
            start,
        ).select_related("created_by")

        if q:
            qs = qs.filter(
                Q(subject__icontains=q)
                | Q(envelope_id__icontains=q)
                | Q(created_by__username__icontains=q)
                | Q(created_by__first_name__icontains=q)
                | Q(created_by__last_name__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)

        status_choices = Envelope.STATUS_CHOICES
        qs = qs.order_by("-created_at")
        columns = [
            "Envelope",
            "Subject",
            "Sender",
            "Status",
            "Created",
            "Completed",
        ]

        paginator = Paginator(qs, 30)
        page = paginator.get_page(request.GET.get("page"))

        for obj in page.object_list:
            sender = "—"
            if obj.created_by:
                sender = (
                    obj.created_by.get_full_name()
                    or obj.created_by.username
                )
            rows.append(
                [
                    obj.short_id,
                    obj.subject,
                    sender,
                    obj.get_status_display(),
                    obj.created_at,
                    obj.completed_at or "—",
                ]
            )

    elif dataset == "forms":
        qs = _after(
            _scoped(
                FormTemplate.objects.all(),
                "forms",
                access,
            ),
            "created_at",
            start,
        ).select_related("created_by")

        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(description__icontains=q)
                | Q(created_by__username__icontains=q)
            )

        qs = qs.order_by("-updated_at")
        columns = [
            "Form",
            "Owner",
            "Published",
            "Fields",
            "Created",
            "Updated",
        ]

        paginator = Paginator(qs, 30)
        page = paginator.get_page(request.GET.get("page"))

        for obj in page.object_list:
            owner = "—"
            if obj.created_by:
                owner = (
                    obj.created_by.get_full_name()
                    or obj.created_by.username
                )
            rows.append(
                [
                    obj.name,
                    owner,
                    "Yes" if obj.is_published else "No",
                    obj.field_count,
                    obj.created_at,
                    obj.updated_at,
                ]
            )

    elif dataset == "submissions":
        qs = _after(
            _scoped(
                FormSubmission.objects.all(),
                "submissions",
                access,
            ),
            "created_at",
            start,
        ).select_related("form", "submitted_by")

        if q:
            qs = qs.filter(
                Q(reference__icontains=q)
                | Q(form_name__icontains=q)
                | Q(submitter_name__icontains=q)
                | Q(submitter_email__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)

        status_choices = FormSubmission.STATUS_CHOICES
        qs = qs.order_by("-created_at")
        columns = [
            "Reference",
            "Form",
            "Submitter",
            "Status",
            "Created",
            "Updated",
        ]

        paginator = Paginator(qs, 30)
        page = paginator.get_page(request.GET.get("page"))

        for obj in page.object_list:
            submitter = (
                obj.submitter_name
                or obj.submitter_email
                or "—"
            )
            if obj.submitted_by:
                submitter = (
                    obj.submitted_by.get_full_name()
                    or obj.submitted_by.username
                )

            rows.append(
                [
                    obj.reference,
                    obj.form_name
                    or (obj.form.name if obj.form else "—"),
                    submitter,
                    obj.get_status_display(),
                    obj.created_at,
                    obj.updated_at,
                ]
            )

    elif dataset == "workflows":
        qs = _after(
            _scoped(
                DocumentWorkflow.objects.all(),
                "workflows",
                access,
            ),
            "created_at",
            start,
        ).select_related("created_by")

        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(description__icontains=q)
                | Q(created_by__username__icontains=q)
            )

        qs = qs.order_by("-updated_at")
        columns = [
            "Flow",
            "Owner",
            "Active",
            "Steps",
            "Version",
            "Updated",
        ]

        paginator = Paginator(qs, 30)
        page = paginator.get_page(request.GET.get("page"))

        for obj in page.object_list:
            owner = "—"
            if obj.created_by:
                owner = (
                    obj.created_by.get_full_name()
                    or obj.created_by.username
                )

            rows.append(
                [
                    obj.name,
                    owner,
                    "Yes" if obj.is_active else "No",
                    obj.step_count,
                    obj.version,
                    obj.updated_at,
                ]
            )

    elif dataset == "runs":
        qs = _after(
            _scoped(
                WorkflowRun.objects.all(),
                "runs",
                access,
            ),
            "started_at",
            start,
        ).select_related("workflow", "initiator")

        if q:
            qs = qs.filter(
                Q(reference__icontains=q)
                | Q(subject__icontains=q)
                | Q(workflow_name__icontains=q)
                | Q(initiator__username__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)

        status_choices = WorkflowRun.STATUS_CHOICES
        qs = qs.order_by("-started_at")
        columns = [
            "Reference",
            "Subject",
            "Flow",
            "Initiator",
            "Status",
            "Started",
            "Completed",
        ]

        paginator = Paginator(qs, 30)
        page = paginator.get_page(request.GET.get("page"))

        for obj in page.object_list:
            initiator = "—"
            if obj.initiator:
                initiator = (
                    obj.initiator.get_full_name()
                    or obj.initiator.username
                )

            rows.append(
                [
                    obj.reference,
                    obj.subject,
                    obj.workflow_name
                    or (
                        obj.workflow.name
                        if obj.workflow
                        else "—"
                    ),
                    initiator,
                    obj.get_status_display(),
                    obj.started_at,
                    obj.completed_at or "—",
                ]
            )

    else:
        qs = _after(
            _scoped(
                WorkflowTask.objects.all(),
                "tasks",
                access,
            ),
            "created_at",
            start,
        ).select_related("run", "user")

        if q:
            qs = qs.filter(
                Q(run__reference__icontains=q)
                | Q(node_label__icontains=q)
                | Q(name__icontains=q)
                | Q(email__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)

        status_choices = WorkflowTask.STATUS_CHOICES
        qs = qs.order_by("-created_at")
        columns = [
            "Run",
            "Step",
            "Type",
            "Person",
            "Status",
            "Due",
            "Created",
        ]

        paginator = Paginator(qs, 30)
        page = paginator.get_page(request.GET.get("page"))

        for obj in page.object_list:
            person = obj.name or obj.email or "—"
            if obj.user:
                person = (
                    obj.user.get_full_name()
                    or obj.user.username
                )

            rows.append(
                [
                    obj.run.reference,
                    obj.node_label or obj.node_id,
                    obj.get_kind_display(),
                    person,
                    obj.get_status_display(),
                    obj.due_at or "—",
                    obj.created_at,
                ]
            )

    return render(
        request,
        "esign_analytics/drilldown.html",
        {
            "access": access,
            "available_scopes": available_scopes(request.user),
            "dataset": dataset,
            "dataset_label": DRILL_DATASETS[dataset],
            "datasets": DRILL_DATASETS,
            "columns": columns,
            "rows": rows,
            "page_obj": page,
            "period_choices": PERIOD_CHOICES,
            "period_value": period_value,
            "period_label": period_label,
            "q": q,
            "status": status,
            "status_choices": status_choices,
        },
    )


def _users_for_scope(access):
    qs = (
        User.objects
        .filter(is_active=True)
        .select_related("agency", "country_office")
    )

    if access.kind == SCOPE_OFFICE:
        return qs.filter(
            country_office_id=access.scope_id
        ).order_by(
            "first_name",
            "last_name",
            "username",
        )

    if access.kind == SCOPE_AGENCY:
        return qs.filter(
            agency_id=access.scope_id
        ).order_by(
            "country_office__name",
            "first_name",
            "last_name",
            "username",
        )

    return qs.order_by(
        "agency__code",
        "country_office__name",
        "username",
    )


@login_required
def manage_access(request):
    access = _require_access(
        request, LEVEL_ACCESS_MANAGER
    )

    table_ready = grant_table_exists()

    if request.method == "POST":
        if not table_ready:
            messages.error(
                request,
                "Dashboard access table is not installed yet. "
                "Run: python manage.py ensure_esign_dashboard",
            )
            return redirect(
                f"/insights/esign/access/?scope={access.key}"
            )

        try:
            level = int(request.POST.get("level") or 0)
        except (TypeError, ValueError):
            level = 0

        allowed_levels = {value for value, _ in LEVEL_CHOICES}
        max_grant_level = (
            LEVEL_ACCESS_MANAGER
            if request.user.is_superuser
            else LEVEL_DRILLDOWN
        )

        if (
            level not in allowed_levels
            or level > max_grant_level
        ):
            messages.error(
                request,
                "You cannot grant that dashboard level.",
            )
            return redirect(
                f"/insights/esign/access/?scope={access.key}"
            )

        target = get_object_or_404(
            _users_for_scope(access),
            pk=request.POST.get("user_id"),
        )

        DashboardAccessGrant.objects.update_or_create(
            user=target,
            scope_kind=access.kind,
            scope_id=access.scope_id,
            defaults={
                "level": level,
                "is_active": True,
                "granted_by": request.user,
            },
        )

        messages.success(
            request,
            f"{target.get_full_name() or target.username} now has "
            f"{LEVEL_LABELS[level]} for {access.label}.",
        )
        return redirect(
            f"/insights/esign/access/?scope={access.key}"
        )

    max_grant_level = (
        LEVEL_ACCESS_MANAGER
        if request.user.is_superuser
        else LEVEL_DRILLDOWN
    )

    grants = []
    if table_ready:
        try:
            grants = list(
                DashboardAccessGrant.objects
                .filter(
                    scope_kind=access.kind,
                    scope_id=access.scope_id,
                    is_active=True,
                )
                .select_related("user", "granted_by")
                .order_by("-level", "user__username")
            )
        except DatabaseError:
            grants = []

    automatic_admins = []
    if access.kind == SCOPE_OFFICE:
        automatic_admins = list(
            OfficeAdmin.objects
            .filter(
                country_office_id=access.scope_id,
                is_active=True,
            )
            .select_related("user")
            .order_by("level", "user__username")
        )

    return render(
        request,
        "esign_analytics/access_manage.html",
        {
            "access": access,
            "available_scopes": [
                scope
                for scope in available_scopes(request.user)
                if scope.can_manage_access
            ],
            "users": _users_for_scope(access),
            "grants": grants,
            "automatic_admins": automatic_admins,
            "level_choices": [
                choice
                for choice in LEVEL_CHOICES
                if choice[0] <= max_grant_level
            ],
            "max_grant_level": max_grant_level,
            "grant_table_ready": table_ready,
        },
    )


@login_required
@require_POST
def revoke_access(request, pk):
    if not grant_table_exists():
        raise Http404

    grant = get_object_or_404(
        DashboardAccessGrant, pk=pk
    )
    scope_key = f"{grant.scope_kind}:{grant.scope_id}"
    access = resolve_scope_access(
        request.user, scope_key
    )

    if access is None or not access.can_manage_access:
        raise PermissionDenied

    if (
        not request.user.is_superuser
        and grant.level >= LEVEL_ACCESS_MANAGER
    ):
        raise PermissionDenied(
            "Only the superuser can revoke Level 4 access."
        )

    grant.is_active = False
    grant.granted_by = request.user
    grant.save(
        update_fields=[
            "is_active",
            "granted_by",
            "updated_at",
        ]
    )

    messages.success(
        request,
        "Dashboard access revoked for "
        f"{grant.user.get_full_name() or grant.user.username}.",
    )
    return redirect(
        f"/insights/esign/access/?scope={scope_key}"
    )
