from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, DetailView
from django.utils import timezone
from django.db.models import Q


from .models import IncidentReport
from .forms import IncidentReportForm, IncidentUpdateForm

def is_lsa_or_soc(user):
    return user.is_authenticated and (getattr(user, "role", None) in ("lsa", "soc") or user.is_superuser)

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
            qs = qs.filter(title__icontains=q) | qs.filter(description__icontains=q)
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
        # Tweak select_related/prefetch as your model allows
        return (IncidentReport.objects
                .select_related("reported_by")
                .order_by("-created_at"))

    def _apply_common_filters(self, qs):
        """
        Filters that affect both the table and the stat cards:
        - search (q)
        - severity
        - date range (created_at)
        We intentionally do NOT apply 'status' here so cards can show their own counts.
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

    # ---- queryset for the table --------------------------------------------
    def get_queryset(self):
        qs = self._apply_common_filters(self._base_queryset())

        # Status handling:
        # - If user explicitly passes ?status=..., filter by it.
        # - Otherwise, hide resolved by default.
        status = (self.request.GET.get("status") or "").strip()
        if status:
            qs = qs.filter(status=status)
        else:
            qs = qs.exclude(status="resolved")

        return qs

    # ---- context (stats for the cards) --------------------------------------
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Build stats from the *same search/severity/date* filters,
        # but WITHOUT the status filter (so cards are informative).
        base = self._apply_common_filters(self._base_queryset())

        ctx["stats"] = {
            "total": base.count(),
            "new": base.filter(status="new").count(),
            "in_review": base.filter(status="in_review").count(),
            "critical": base.filter(severity="critical").count(),
        }

        # Echo filters (useful for template inputs’ values)
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
        messages.success(self.request, "Incident submitted successfully. Security will review it.")
        return super().form_valid(form)

class IncidentDetailView(LoginRequiredMixin, DetailView):
    model = IncidentReport
    template_name = "incidents/incident_detail.html"
    context_object_name = "incident"

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()
        # Requester can view only their own; LSA/SOC can view all
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
        else:
            messages.error(request, "Please fix the errors in the update form.")
    return redirect("incidents:incident_detail", pk=incident.pk)

@login_required
@user_passes_test(is_lsa_or_soc)
def change_status(request, pk):
    incident = get_object_or_404(IncidentReport, pk=pk)
    new_status = request.POST.get("status")
    if new_status in dict(IncidentReport.Status.choices):
        incident.status = new_status
        if new_status == IncidentReport.Status.IN_REVIEW and not incident.assigned_to:
            incident.assigned_to = request.user
        incident.updated_at = timezone.now()
        incident.save()
        messages.success(request, f"Incident status changed to {incident.get_status_display()}.")
    else:
        messages.error(request, "Invalid status.")
    return redirect("incidents:incident_detail", pk=pk)
