import csv
from io import BytesIO
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q, Count, Exists, OuterRef
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import ListView, CreateView, DetailView, FormView

from un_security_system.roles import is_lsa_or_soc, is_guard, is_not_guard
from .forms import CommunicationDeviceForm, AdminDeviceForm, RadioCheckSessionForm, RadioCheckEntryForm
from .models import CommunicationDevice, RadioCheckSession, RadioCheckEntry

# -------- Staff (not guards) --------

class MyDevicesView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    model = CommunicationDevice
    template_name = "comms/my_devices.html"
    context_object_name = "devices"

    def test_func(self): return is_not_guard(self.request.user)

    def get_queryset(self):
        return CommunicationDevice.objects.filter(assigned_to=self.request.user).order_by("device_type","call_sign","imei")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_add"] = True
        return ctx


class DeviceCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    form_class = CommunicationDeviceForm
    template_name = "comms/device_form.html"
    success_url = reverse_lazy("comms:my_devices")

    def test_func(self): return is_not_guard(self.request.user)

    def form_valid(self, form):
        # user can only create devices assigned to themselves
        obj = form.save(commit=False)
        obj.assigned_to = self.request.user
        obj.status = "with_user"
        obj.save()
        messages.success(self.request, "Device recorded successfully.")
        return super().form_valid(form)

# -------- LSA / SOC views --------

class OnlyTeamMixin(UserPassesTestMixin):
    def test_func(self): return is_lsa_or_soc(self.request.user)

class RadioListView(LoginRequiredMixin, OnlyTeamMixin, ListView):
    model = CommunicationDevice
    template_name = "comms/radios_list.html"
    context_object_name = "radios"
    paginate_by = 50

    def get_queryset(self):
        q = (self.request.GET.get("q") or "").strip()
        status = (self.request.GET.get("status") or "").strip()
        qs = CommunicationDevice.objects.filter(device_type__in=["hf","vhf"]).select_related("assigned_to").order_by("call_sign")
        if q:
            qs = qs.filter(Q(call_sign__icontains=q) | Q(assigned_to__username__icontains=q) | Q(serial_number__icontains=q))
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        base = CommunicationDevice.objects.filter(device_type__in=["hf","vhf"])
        ctx["stats"] = {
            "total": base.count(),
            "available": base.filter(status="available").count(),
            "with_user": base.filter(status="with_user").count(),
            "damaged": base.filter(status="damaged").count(),
            "repair": base.filter(status="repair").count(),
        }
        return ctx


class SatPhoneListView(LoginRequiredMixin, OnlyTeamMixin, ListView):
    model = CommunicationDevice
    template_name = "comms/satphones_list.html"
    context_object_name = "phones"
    paginate_by = 50

    def get_queryset(self):
        q = (self.request.GET.get("q") or "").strip()
        qs = CommunicationDevice.objects.filter(device_type="satphone").select_related("assigned_to").order_by("imei")
        if q:
            qs = qs.filter(Q(imei__icontains=q) | Q(assigned_to__username__icontains=q) | Q(serial_number__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        base = CommunicationDevice.objects.filter(device_type="satphone")
        ctx["stats"] = {
            "total": base.count(),
            "issued": base.filter(status="with_user").count(),
            "available": base.filter(status="available").count(),
            "damaged": base.filter(status="damaged").count(),
        }
        return ctx


class UsersWithoutRadiosView(LoginRequiredMixin, OnlyTeamMixin, ListView):
    """
    Active users (excl. guards) who do NOT have any HF/VHF assigned.
    """
    template_name = "comms/users_without_radios.html"
    context_object_name = "users"

    def get_queryset(self):
        User = type(self.request.user)
        radio_subq = CommunicationDevice.objects.filter(
            device_type__in=["hf","vhf"], assigned_to=OuterRef("pk")
        )
        qs = (User.objects.filter(is_active=True)
              .exclude(role__in=["guard","data_entry"])
              .annotate(has_radio=Exists(radio_subq))
              .filter(has_radio=False)
              .order_by("username"))
        return qs

# --- mutate radio status (SOC/LSA) ---

@login_required
@user_passes_test(is_lsa_or_soc)
def radio_update_status(request, pk):
    radio = get_object_or_404(CommunicationDevice, pk=pk, device_type__in=["hf","vhf"])
    if request.method == "POST":
        status = request.POST.get("status")
        if status not in dict(CommunicationDevice.STATUS):
            messages.error(request, "Invalid status.")
        else:
            radio.status = status
            # auto-unassign if not 'with_user'
            if status != "with_user":
                radio.assigned_to = radio.assigned_to if status in ["with_user"] else None
            radio.save(update_fields=["status","assigned_to","updated_at"])
            messages.success(request, f"Status updated to {radio.get_status_display()}.")
    return redirect("comms:radios")

# --- radio checks ---

class RadioCheckStartView(LoginRequiredMixin, OnlyTeamMixin, FormView):
    template_name = "comms/check_start.html"
    form_class = RadioCheckSessionForm

    def form_valid(self, form):
        session = form.save(commit=False)
        session.created_by = self.request.user
        session.save()
        # Pre-populate entries for all radios that should respond
        radios = CommunicationDevice.objects.filter(device_type__in=["hf","vhf"]).order_by("call_sign")
        bulk = [
            RadioCheckEntry(
                session=session,
                device=r,
                call_sign=r.call_sign or "",
                responded=None,
                checked_by=self.request.user
            ) for r in radios
        ]
        RadioCheckEntry.objects.bulk_create(bulk)
        messages.success(self.request, "Radio check started.")
        return redirect("comms:check_run", pk=session.pk)


class RadioCheckRunView(LoginRequiredMixin, OnlyTeamMixin, DetailView):
    model = RadioCheckSession
    template_name = "comms/check_run.html"
    context_object_name = "session"

    def post(self, request, *args, **kwargs):
        session = self.get_object()
        for entry in session.entries.select_related("device").all():
            val = request.POST.get(f"responded_{entry.pk}")
            issue = (request.POST.get(f"issue_{entry.pk}") or "").strip()
            if val in ("yes","no"):
                entry.responded = (val == "yes")
                entry.noted_issue = issue
                entry.checked_by = request.user
                entry.checked_at = timezone.now()
                entry.save(update_fields=["responded","noted_issue","checked_by","checked_at"])
        messages.success(request, "Radio check updated.")
        return redirect("comms:check_run", pk=session.pk)

# --- Exports (CSV/XLSX) ---

@login_required
@user_passes_test(is_lsa_or_soc)
def export_radios_csv(request):
    qs = CommunicationDevice.objects.filter(device_type__in=["hf","vhf"]).select_related("assigned_to").order_by("call_sign")
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="radios.csv"'
    w = csv.writer(resp)
    w.writerow(["Call Sign","Type","Status","Assigned To","Serial","Notes"])
    for r in qs:
        w.writerow([r.call_sign, r.get_device_type_display(), r.get_status_display(),
                    getattr(r.assigned_to, "username", ""), r.serial_number, r.notes])
    return resp

@login_required
@user_passes_test(is_lsa_or_soc)
def export_satphones_csv(request):
    qs = CommunicationDevice.objects.filter(device_type="satphone").select_related("assigned_to").order_by("imei")
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="satphones.csv"'
    w = csv.writer(resp)
    w.writerow(["IMEI","Status","Assigned To","Serial","Notes"])
    for p in qs:
        w.writerow([p.imei, p.get_status_display(), getattr(p.assigned_to, "username", ""), p.serial_number, p.notes])
    return resp

@login_required
@user_passes_test(is_lsa_or_soc)
def export_check_csv(request, pk):
    s = get_object_or_404(RadioCheckSession, pk=pk)
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="radio_check_{s.pk}.csv"'
    w = csv.writer(resp)
    w.writerow(["Session","Started","Call Sign","Responded","Issue","Checked By","Checked At"])
    for e in s.entries.select_related("checked_by").order_by("call_sign"):
        w.writerow([s.name, s.started_at, e.call_sign, {True:"YES", False:"NO", None:"—"}[e.responded],
                    e.noted_issue, getattr(e.checked_by, "username", ""), e.checked_at])
    return resp

# Optional XLSX (requires openpyxl in your env)
def _xlsx_response(filename, headers, rows):
    try:
        from openpyxl import Workbook
    except Exception:
        return None
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows: ws.append(row)
    bio = BytesIO()
    wb.save(bio); bio.seek(0)
    resp = HttpResponse(bio.read(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp

@login_required
@user_passes_test(is_lsa_or_soc)
def export_radios_xlsx(request):
    qs = CommunicationDevice.objects.filter(device_type__in=["hf","vhf"]).select_related("assigned_to").order_by("call_sign")
    rows = [[r.call_sign, r.get_device_type_display(), r.get_status_display(),
             getattr(r.assigned_to, "username", ""), r.serial_number, r.notes] for r in qs]
    resp = _xlsx_response("radios.xlsx", ["Call Sign","Type","Status","Assigned To","Serial","Notes"], rows)
    return resp or export_radios_csv(request)

@login_required
@user_passes_test(is_lsa_or_soc)
def export_satphones_xlsx(request):
    qs = CommunicationDevice.objects.filter(device_type="satphone").select_related("assigned_to").order_by("imei")
    rows = [[p.imei, p.get_status_display(), getattr(p.assigned_to, "username",""), p.serial_number, p.notes] for p in qs]
    resp = _xlsx_response("satphones.xlsx", ["IMEI","Status","Assigned To","Serial","Notes"], rows)
    return resp or export_satphones_csv(request)

@login_required
@user_passes_test(is_lsa_or_soc)
def export_check_xlsx(request, pk):
    s = get_object_or_404(RadioCheckSession, pk=pk)
    rows = []
    for e in s.entries.select_related("checked_by").order_by("call_sign"):
        rows.append([s.name, s.started_at, e.call_sign, {True:"YES", False:"NO", None:"—"}[e.responded],
                     e.noted_issue, getattr(e.checked_by, "username",""), e.checked_at])
    resp = _xlsx_response(f"radio_check_{s.pk}.xlsx",
                          ["Session","Started","Call Sign","Responded","Issue","Checked By","Checked At"], rows)
    return resp or export_check_csv(request, pk)
