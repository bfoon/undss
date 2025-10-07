from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, DetailView
from django.utils import timezone


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
    """LSA/SOC triage list of all incidents"""
    model = IncidentReport
    template_name = "incidents/incident_triage_list.html"
    context_object_name = "incidents"
    paginate_by = 20

    def test_func(self):
        return is_lsa_or_soc(self.request.user)

    def get_queryset(self):
        qs = IncidentReport.objects.all().order_by("-created_at")
        status = self.request.GET.get("status", "")
        if status:
            qs = qs.filter(status=status)
        return qs

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
