from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy, reverse
from django.views.generic import CreateView, ListView, DetailView
from django.utils import timezone
from django.db.models import Q

from django.conf import settings
from django.core.mail import send_mail
from django.contrib.auth import get_user_model

from .models import IncidentReport
from .forms import IncidentReportForm, IncidentUpdateForm

User = get_user_model()


def is_lsa_or_soc(user):
    return user.is_authenticated and (getattr(user, "role", None) in ("lsa", "soc") or user.is_superuser)


# -------------------------------------------------------------------
# Email / notification helpers
# -------------------------------------------------------------------

def _send_notification(subject: str, message: str, recipients):
    """
    Central helper to send email notifications.
    Uses DEFAULT_FROM_EMAIL or EMAIL_HOST_USER.
    Silently skips if email is not configured.
    """
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "EMAIL_HOST_USER", None)
    if not from_email:
        # Email not configured; don't break app
        return

    if isinstance(recipients, str):
        recipients = [recipients]

    emails = [e.strip() for e in recipients if e and e.strip()]
    if not emails:
        return

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=emails,
            fail_silently=False,
        )
    except Exception:
        # You can log this if you like
        pass


def _notify_lsa_soc_new_incident(incident, request):
    """
    Notify all active LSA/SOC (optionally same-agency) when a new incident is reported.
    """
    qs = User.objects.filter(is_active=True, role__in=["lsa", "soc"])
    if incident.reported_by and getattr(incident.reported_by, "agency_id", None):
        qs = qs.filter(agency_id=incident.reported_by.agency_id)

    recipients = list(qs.values_list("email", flat=True))
    if not recipients:
        return

    try:
        detail_url = request.build_absolute_uri(
            reverse("incidents:incident_detail", kwargs={"pk": incident.pk})
        )
    except Exception:
        detail_url = ""

    subject = f"[Security Incident] New incident reported: {incident.title}"
    message = (
        f"Dear Security team,\n\n"
        f"A new incident has been reported.\n\n"
        f"Title: {incident.title}\n"
        f"Severity: {incident.get_severity_display() if hasattr(incident, 'get_severity_display') else incident.severity}\n"
        f"Status: {incident.get_status_display() if hasattr(incident, 'get_status_display') else incident.status}\n"
        f"Reported by: {incident.reported_by.get_full_name() or incident.reported_by.username}\n"
        f"Reported at: {incident.created_at.strftime('%Y-%m-%d %H:%M') if incident.created_at else 'N/A'}\n\n"
        f"Description:\n{incident.description}\n\n"
        f"You can review this incident here:\n{detail_url}\n\n"
        f"Best regards,\nUN Security / Common Services System"
    )

    _send_notification(subject, message, recipients)


def _notify_reporter_incident_created(incident):
    """
    Confirmation to the person who reported the incident.
    """
    reporter = getattr(incident, "reported_by", None)
    if not reporter or not reporter.email:
        return

    subject = f"[Security Incident] Your incident has been submitted: {incident.title}"
    message = (
        f"Hello {reporter.get_full_name() or reporter.username},\n\n"
        f"Thank you for reporting the following security incident:\n\n"
        f"Title: {incident.title}\n"
        f"Severity: {incident.get_severity_display() if hasattr(incident, 'get_severity_display') else incident.severity}\n"
        f"Status: {incident.get_status_display() if hasattr(incident, 'get_status_display') else incident.status}\n"
        f"Reported at: {incident.created_at.strftime('%Y-%m-%d %H:%M') if incident.created_at else 'N/A'}\n\n"
        f"Our Security team (LSA/SOC) will review it and may reach out for more details.\n\n"
        f"Best regards,\nUN Security / Common Services System"
    )
    _send_notification(subject, message, reporter.email)


def _notify_reporter_status_change(incident, old_status=None):
    """
    Notify reporter when status changes (new -> in_review -> resolved, etc.).
    """
    reporter = getattr(incident, "reported_by", None)
    if not reporter or not reporter.email:
        return

    subject = f"[Security Incident] Status updated: {incident.title}"
    message = (
        f"Hello {reporter.get_full_name() or reporter.username},\n\n"
        f"The status of your reported incident has been updated.\n\n"
        f"Title: {incident.title}\n"
        f"Previous status: {incident.get_status_display_from_value(old_status) if hasattr(incident, 'get_status_display_from_value') and old_status else old_status or 'N/A'}\n"
        f"New status: {incident.get_status_display() if hasattr(incident, 'get_status_display') else incident.status}\n"
        f"Last updated: {incident.updated_at.strftime('%Y-%m-%d %H:%M') if incident.updated_at else 'N/A'}\n\n"
        f"Best regards,\nUN Security / Common Services System"
    )
    _send_notification(subject, message, reporter.email)


def _notify_assigned_incident(incident, is_new_assignment=False):
    """
    Notify assigned_to person that they are responsible or status changed.
    """
    assignee = getattr(incident, "assigned_to", None)
    if not assignee or not assignee.email:
        return

    subject = f"[Security Incident] Incident assigned: {incident.title}" if is_new_assignment else \
              f"[Security Incident] Update on assigned incident: {incident.title}"

    message = (
        f"Hello {assignee.get_full_name() or assignee.username},\n\n"
        f"You are {'now assigned to' if is_new_assignment else 'responsible for'} the following incident:\n\n"
        f"Title: {incident.title}\n"
        f"Severity: {incident.get_severity_display() if hasattr(incident, 'get_severity_display') else incident.severity}\n"
        f"Status: {incident.get_status_display() if hasattr(incident, 'get_status_display') else incident.status}\n"
        f"Reported by: {incident.reported_by.get_full_name() or incident.reported_by.username}\n"
        f"Reported at: {incident.created_at.strftime('%Y-%m-%d %H:%M') if incident.created_at else 'N/A'}\n\n"
        f"Best regards,\nUN Security / Common Services System"
    )
    _send_notification(subject, message, assignee.email)


def _notify_incident_new_update(incident, update_obj):
    """
    Notify reporter and assignee when a new update/comment is added.
    Do not send to the author themselves.
    """
    author = getattr(update_obj, "author", None)
    reporter = getattr(incident, "reported_by", None)
    assignee = getattr(incident, "assigned_to", None)

    # Notify reporter if different from author
    if reporter and reporter.email and (not author or reporter.id != author.id):
        subject = f"[Security Incident] New update on your incident: {incident.title}"
        message = (
            f"Hello {reporter.get_full_name() or reporter.username},\n\n"
            f"A new update has been added to the incident you reported.\n\n"
            f"Title: {incident.title}\n"
            f"Status: {incident.get_status_display() if hasattr(incident, 'get_status_display') else incident.status}\n\n"
            f"Update by: {author.get_full_name() or author.username if author else 'System'}\n"
            f"Update:\n{update_obj.text}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, message, reporter.email)

    # Notify assignee if different from author and not the same as reporter (to avoid duplicates)
    if assignee and assignee.email and (not author or assignee.id != author.id):
        subject = f"[Security Incident] New update on assigned incident: {incident.title}"
        message = (
            f"Hello {assignee.get_full_name() or assignee.username},\n\n"
            f"A new update has been added to an incident assigned to you.\n\n"
            f"Title: {incident.title}\n"
            f"Status: {incident.get_status_display() if hasattr(incident, 'get_status_display') else incident.status}\n\n"
            f"Update by: {author.get_full_name() or author.username if author else 'System'}\n"
            f"Update:\n{update_obj.text}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, message, assignee.email)


# -------------------------------------------------------------------
# Views
# -------------------------------------------------------------------

class MyIncidentListView(LoginRequiredMixin, ListView):
    model = IncidentReport
    template_name = "incidents/incident_list.html"
    context_object_name = "incidents"
    paginate_by = 20

    def get_queryset(self):
        qs = IncidentReport.objects.filter(reported_by=self.request.user).order_by("-created_at")
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
        if status:
            qs = qs.filter(status=status)
        return qs


class TeamIncidentListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """
    LSA/SOC triage list of incidents with filtering + dashboard stats.
    """
    model = IncidentReport
    template_name = "incidents/incident_triage_list.html"
    context_object_name = "incidents"
    paginate_by = 20

    def test_func(self):
        return is_lsa_or_soc(self.request.user)

    # ---- helpers ------------------------------------------------------------
    def _base_queryset(self):
        return (IncidentReport.objects
                .select_related("reported_by", "assigned_to")
                .order_by("-created_at"))

    def _apply_common_filters(self, qs):
        """
        Filters that affect both the table and the stat cards:
        - search (q)
        - severity
        - date range (created_at)
        """
        request = self.request
        q = (request.GET.get("q") or "").strip()
        severity = (request.GET.get("severity") or "").strip()
        date_from = (request.GET.get("date_from") or "").strip()
        date_to = (request.GET.get("date_to") or "").strip()

        if q:
            qs = qs.filter(
                Q(title__icontains=q) |
                Q(id__icontains=q) |
                Q(reported_by__username__icontains=q)
            )

        if severity:
            qs = qs.filter(severity=severity)

        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        return qs

    def get_queryset(self):
        qs = self._apply_common_filters(self._base_queryset())

        status = (self.request.GET.get("status") or "").strip()
        if status:
            qs = qs.filter(status=status)
        else:
            qs = qs.exclude(status="resolved")

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        base = self._apply_common_filters(self._base_queryset())

        ctx["stats"] = {
            "total": base.count(),
            "new": base.filter(status="new").count(),
            "in_review": base.filter(status="in_review").count(),
            "critical": base.filter(severity="critical").count(),
        }

        ctx["filters"] = {
            "q": (self.request.GET.get("q") or "").strip(),
            "severity": (self.request.GET.get("severity") or "").strip(),
            "status": (self.request.GET.get("status") or "").strip(),
            "date_from": (self.request.GET.get("date_from") or "").strip(),
            "date_to": (self.request.GET.get("date_to") or "").strip(),
        }

        return ctx


class IncidentCreateView(LoginRequiredMixin, CreateView):
    model = IncidentReport
    form_class = IncidentReportForm
    template_name = "incidents/incident_form.html"
    success_url = reverse_lazy("incidents:my_incidents")

    def form_valid(self, form):
        form.instance.reported_by = self.request.user
        response = super().form_valid(form)

        incident = form.instance
        messages.success(self.request, "Incident submitted successfully. Security will review it.")

        # Notify reporter (confirmation)
        _notify_reporter_incident_created(incident)

        # Notify LSA/SOC team
        _notify_lsa_soc_new_incident(incident, self.request)

        return response


class IncidentDetailView(LoginRequiredMixin, DetailView):
    model = IncidentReport
    template_name = "incidents/incident_detail.html"
    context_object_name = "incident"

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.reported_by_id != request.user.id and not is_lsa_or_soc(request.user):
            messages.error(request, "You don't have permission to view that incident.")
            return redirect("incidents:my_incidents")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["update_form"] = IncidentUpdateForm()
        ctx["status_choices"] = self.model._meta.get_field("status").choices
        ctx["can_triage"] = is_lsa_or_soc(self.request.user)
        return ctx


@login_required
def add_update(request, pk):
    incident = get_object_or_404(IncidentReport, pk=pk)
    if incident.reported_by_id != request.user.id and not is_lsa_or_soc(request.user):
        messages.error(request, "You don't have permission to update this incident.")
        return redirect("incidents:my_incidents")

    if request.method == "POST":
        form = IncidentUpdateForm(request.POST)
        if form.is_valid():
            upd = form.save(commit=False)
            upd.incident = incident
            upd.author = request.user
            upd.save()
            messages.success(request, "Update added.")

            # Notify reporter + assignee about the new update
            _notify_incident_new_update(incident, upd)
        else:
            messages.error(request, "Please fix the errors in the update form.")
    return redirect("incidents:incident_detail", pk=incident.pk)


@login_required
@user_passes_test(is_lsa_or_soc)
def change_status(request, pk):
    incident = get_object_or_404(IncidentReport, pk=pk)
    new_status = request.POST.get("status")
    valid_statuses = dict(IncidentReport.Status.choices)

    if new_status in valid_statuses:
        old_status = incident.status
        old_assigned_to_id = incident.assigned_to_id if hasattr(incident, "assigned_to_id") else None

        incident.status = new_status
        # auto-assign if moving to in_review and nobody assigned yet
        if new_status == IncidentReport.Status.IN_REVIEW and not incident.assigned_to:
            incident.assigned_to = request.user
        incident.updated_at = timezone.now()
        incident.save()

        messages.success(request, f"Incident status changed to {incident.get_status_display()}.")

        # Notify reporter about status change
        _notify_reporter_status_change(incident, old_status=old_status)

        # Notify assignee if new assignment happened
        if incident.assigned_to_id and incident.assigned_to_id != old_assigned_to_id:
            _notify_assigned_incident(incident, is_new_assignment=True)
        elif incident.assigned_to_id:
            # Assigned person already, just let them know of status change
            _notify_assigned_incident(incident, is_new_assignment=False)
    else:
        messages.error(request, "Invalid status.")
    return redirect("incidents:incident_detail", pk=pk)
