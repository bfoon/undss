import csv
from io import BytesIO
from typing import Optional
from django.db.models.functions import Coalesce

from django.conf import settings
from django.core.mail import send_mail
from django.contrib.auth import get_user_model

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q, Exists, OuterRef, QuerySet, Value, CharField
from django.http import HttpResponse, HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import ListView, CreateView, DetailView, FormView, UpdateView
import threading
import logging


from un_security_system.roles import is_lsa_or_soc, is_not_guard
from .forms import CommunicationDeviceForm, RadioCheckSessionForm
from .models import CommunicationDevice, RadioCheckSession, RadioCheckEntry

logger = logging.getLogger(__name__)

User = get_user_model()

# ============================================================================
# PERMISSION HELPERS
# ============================================================================

def _is_lsa_or_soc(user) -> bool:
    """Check if user has LSA or SOC role, or is superuser."""
    return getattr(user, "role", None) in ("lsa", "soc") or getattr(user, "is_superuser", False)


class OnlyTeamMixin(UserPassesTestMixin):
    """Mixin to restrict access to LSA/SOC users only."""

    def test_func(self) -> bool:
        return is_lsa_or_soc(self.request.user)


# ============================================================================
# EMAIL / NOTIFICATION HELPERS
# ============================================================================

def _send_notification(subject: str, message: str, recipients):
    """
    Central helper to send email notifications in the background using a thread.
    Uses DEFAULT_FROM_EMAIL or EMAIL_HOST_USER.
    Silently ignores if no sender or recipients.
    """
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "EMAIL_HOST_USER", None)
    if not from_email:
        return

    if isinstance(recipients, str):
        recipients = [recipients]

    emails = [e.strip() for e in recipients if e and str(e).strip()]
    if not emails:
        return

    def _worker():
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=emails,
                fail_silently=False,
            )
        except Exception as exc:
            # Optional: log for debugging
            logger.exception("Background email send failed: %s", exc)

    # Fire-and-forget thread
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def _lsa_soc_users_qs():
    """All active LSA/SOC + superusers."""
    return User.objects.filter(
        is_active=True
    ).filter(
        Q(role__in=["lsa", "soc"]) | Q(is_superuser=True)
    )


def _notify_device_status_change(device: CommunicationDevice, old_status: str, old_assigned_to):
    """
    Notify users when device assignment / status changes in ways that matter.
    - If device is newly assigned to a user: notify them.
    - If device is taken away/unassigned: notify previous user.
    - If device is marked damaged/repair: notify last holder.
    """
    # Newly assigned
    if device.assigned_to and (not old_assigned_to or device.assigned_to_id != old_assigned_to.id):
        user = device.assigned_to
        if user.email:
            subject = f"[Comms] Device assigned to you: {device.call_sign or device.serial_number or device.imei}"
            message = (
                f"Hello {user.get_full_name() or user.username},\n\n"
                f"A communication device has been assigned to you.\n\n"
                f"Type: {device.get_device_type_display()}\n"
                f"Call sign/ID: {device.call_sign or device.serial_number or device.imei or 'N/A'}\n"
                f"Status: {device.get_status_display()}\n\n"
                f"Please ensure it is kept charged and available for radio checks.\n\n"
                f"Best regards,\nUN Security / Common Services System"
            )
            _send_notification(subject, message, user.email)

    # Unassigned now, had old_assigned_to before
    if old_assigned_to and not device.assigned_to:
        if old_assigned_to.email:
            subject = f"[Comms] Device removed from your profile"
            message = (
                f"Hello {old_assigned_to.get_full_name() or old_assigned_to.username},\n\n"
                f"The communication device previously associated with you "
                f"({device.call_sign or device.serial_number or device.imei or 'N/A'}) "
                f"is no longer logged as 'with you' in the system.\n\n"
                f"If this is unexpected, please contact Security or ICT.\n\n"
                f"Best regards,\nUN Security / Common Services System"
            )
            _send_notification(subject, message, old_assigned_to.email)

    # Marked damaged or repair – notify whoever last held it (old_assigned_to or current)
    if device.status in ["damaged", "repair"]:
        holder = device.assigned_to or old_assigned_to
        if holder and holder.email:
            subject = f"[Comms] Device marked as {device.get_status_display()}"
            message = (
                f"Hello {holder.get_full_name() or holder.username},\n\n"
                f"The communication device ({device.call_sign or device.serial_number or device.imei or 'N/A'}) "
                f"linked to you has been marked as '{device.get_status_display()}'.\n\n"
                f"Best regards,\nUN Security / Common Services System"
            )
            _send_notification(subject, message, holder.email)


def _notify_radio_check_started(session: RadioCheckSession, request: HttpRequest):
    """
    When a radio check starts:
    - Notify all LSA/SOC
    - Notify all users who currently have HF/VHF radios 'with_user'
    """
    # LSA/SOC & superusers
    team_recipients = list(_lsa_soc_users_qs().values_list("email", flat=True))

    # Users with radios
    radios_qs = CommunicationDevice.objects.filter(
        device_type__in=["hf", "vhf"],
        status="with_user",
        assigned_to__isnull=False,
        assigned_to__is_active=True,
    ).select_related("assigned_to")

    radio_users = set()
    for r in radios_qs:
        if r.assigned_to and r.assigned_to.email:
            radio_users.add(r.assigned_to.email)

    all_recipients = list(set(team_recipients) | radio_users)
    if not all_recipients:
        return

    try:
        session_url = request.build_absolute_uri(
            reverse("comms:check_run", kwargs={"pk": session.pk})
        )
    except Exception:
        session_url = ""

    subject = f"[Radio Check] Session started: {session.name}"
    message = (
        f"Dear colleagues,\n\n"
        f"A radio check session has started.\n\n"
        f"Name: {session.name}\n"
        f"Started at: {session.started_at.strftime('%Y-%m-%d %H:%M') if hasattr(session, 'started_at') and session.started_at else 'N/A'}\n\n"
        f"All radio holders should ensure their sets are ON and respond when called.\n"
    )
    if session_url:
        message += f"\nSession details (for LSA/SOC):\n{session_url}\n"

    message += "\nBest regards,\nUN Security / Common Services System"

    _send_notification(subject, message, all_recipients)


def _notify_user_missed_radio_check(user, device: CommunicationDevice, session: RadioCheckSession):
    """
    Notify an individual user that their radio did not respond in the check.
    """
    if not user or not user.email:
        return

    subject = "[Radio Check] No response from your radio"
    message = (
        f"Hello {user.get_full_name() or user.username},\n\n"
        f"During the radio check session '{session.name}', your assigned radio did not respond.\n\n"
        f"Device: {device.get_device_type_display()} - {device.call_sign or device.serial_number or device.imei or 'N/A'}\n"
        f"Please ensure your radio is charged, powered on, and in working condition.\n"
        f"If you believe this is an error, contact Security/ICT.\n\n"
        f"Best regards,\nUN Security / Common Services System"
    )
    _send_notification(subject, message, user.email)


def _notify_radio_check_report(session: RadioCheckSession):
    """
    Automatically generate and send a summary report of the radio check
    to LSA/SOC and the session creator when the session ends.
    """
    entries = session.entries.select_related("device", "device__assigned_to").all()

    total = entries.count()
    responded = entries.filter(responded=True).count()
    missed = entries.filter(responded=False).count()
    pending = entries.filter(responded__isnull=True).count()

    missed_lines = []
    for e in entries.filter(responded=False):
        dev = e.device
        user = dev.assigned_to if dev else None
        holder = (user.get_full_name() or user.username) if user else "Unassigned"
        dev_label = dev.call_sign or dev.serial_number or dev.imei or "N/A"
        missed_lines.append(f"- {dev_label} (Holder: {holder})")

    try:
        report_url = reverse("comms:export_check_xlsx", kwargs={"pk": session.pk})
        # we’ll resolve absolute only if request is available; might not have here, so keep it relative
    except Exception:
        report_url = ""

    subject = f"[Radio Check] Session report: {session.name}"
    message = (
        f"Dear Security team,\n\n"
        f"The radio check session '{session.name}' has been completed.\n\n"
        f"Started at: {session.started_at.strftime('%Y-%m-%d %H:%M') if hasattr(session, 'started_at') and session.started_at else 'N/A'}\n"
        f"Ended at: {session.ended_at.strftime('%Y-%m-%d %H:%M') if hasattr(session, 'ended_at') and session.ended_at else 'N/A'}\n\n"
        f"Summary:\n"
        f"- Total radios: {total}\n"
        f"- Responded: {responded}\n"
        f"- Missed: {missed}\n"
        f"- Pending/Not checked: {pending}\n\n"
    )

    if missed_lines:
        message += "Radios that did NOT respond:\n" + "\n".join(missed_lines) + "\n\n"

    if report_url:
        message += f"Detailed export (XLSX): {report_url}\n\n"

    message += "Best regards,\nUN Security / Common Services System"

    recipients = list(_lsa_soc_users_qs().values_list("email", flat=True))
    if session.created_by and session.created_by.email:
        recipients.append(session.created_by.email)

    _send_notification(subject, message, recipients)


# ============================================================================
# STAFF VIEWS (Non-Guards)
# ============================================================================

class MyDevicesView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """Display devices assigned to the current user."""
    model = CommunicationDevice
    template_name = "comms/my_devices.html"
    context_object_name = "devices"

    def test_func(self) -> bool:
        return is_not_guard(self.request.user)

    def get_queryset(self) -> QuerySet:
        return CommunicationDevice.objects.filter(
            assigned_to=self.request.user
        ).order_by("device_type", "call_sign", "imei")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_add"] = True
        return ctx


class DeviceCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    """Allow staff to register devices assigned to themselves."""
    form_class = CommunicationDeviceForm
    template_name = "comms/device_form.html"
    success_url = reverse_lazy("comms:my_devices")

    def test_func(self) -> bool:
        return is_not_guard(self.request.user)

    def form_valid(self, form):
        # User can only create devices assigned to themselves
        obj = form.save(commit=False)
        obj.assigned_to = self.request.user
        obj.status = "with_user"
        obj.save()
        messages.success(self.request, "Device recorded successfully.")

        # Notify user (confirmation)
        _notify_device_status_change(obj, old_status="available", old_assigned_to=None)

        return super().form_valid(form)


# ============================================================================
# LSA/SOC VIEWS - RADIO MANAGEMENT
# ============================================================================

class RadioListView(LoginRequiredMixin, OnlyTeamMixin, ListView):
    """Display all HF/VHF radios with search and filtering."""
    model = CommunicationDevice
    template_name = "comms/radios_list.html"
    context_object_name = "radios"
    paginate_by = 50

    def get_queryset(self) -> QuerySet:
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "").strip()

        qs = (
            CommunicationDevice.objects
            .filter(device_type__in=["hf", "vhf"])
            .select_related("assigned_to")
            .order_by("call_sign")
        )

        if q:
            qs = qs.filter(
                Q(call_sign__icontains=q) |
                Q(assigned_to__username__icontains=q) |
                Q(assigned_to__first_name__icontains=q) |
                Q(assigned_to__last_name__icontains=q) |
                Q(serial_number__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        base = CommunicationDevice.objects.filter(device_type__in=["hf", "vhf"])
        ctx["stats"] = {
            "total": base.count(),
            "available": base.filter(status="available").count(),
            "with_user": base.filter(status="with_user").count(),
            "damaged": base.filter(status="damaged").count(),
            "repair": base.filter(status="repair").count(),
        }
        # Preserve search parameters for pagination
        ctx["search_query"] = self.request.GET.get("q", "")
        ctx["status_filter"] = self.request.GET.get("status", "")
        return ctx


class SatPhoneListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    template_name = "comms/satphones_list.html"
    context_object_name = "satphones"
    paginate_by = 50

    def test_func(self):
        return is_lsa_or_soc(self.request.user)

    def get_queryset(self):
        q = (self.request.GET.get("q") or "").strip()
        qs = (CommunicationDevice.objects
              .filter(device_type="satphone")
              .select_related("assigned_to")
              .annotate(
                  display_id=Coalesce(
                      "imei",
                      "serial_number",
                      "call_sign",
                      output_field=CharField()
                  )
              )
        )
        if q:
            qs = qs.filter(
                Q(phone_number__icontains=q) |
                Q(imei__icontains=q) |
                Q(serial_number__icontains=q) |
                Q(call_sign__icontains=q) |
                Q(assigned_to__username__icontains=q) |
                Q(assigned_to__first_name__icontains=q) |
                Q(assigned_to__last_name__icontains=q)
            )
        return qs.order_by("assigned_to__last_name", "assigned_to__first_name", "id")


class UsersWithoutRadiosView(LoginRequiredMixin, OnlyTeamMixin, ListView):
    """Display active staff users who do NOT have any HF/VHF radio assigned."""
    template_name = "comms/users_without_radios.html"
    context_object_name = "users"

    def get_queryset(self) -> QuerySet:
        radio_subq = CommunicationDevice.objects.filter(
            device_type__in=["hf", "vhf"],
            assigned_to=OuterRef("pk")
        )
        return (
            User.objects.filter(is_active=True)
            .exclude(role__in=["guard", "data_entry"])
            .annotate(has_radio=Exists(radio_subq))
            .filter(has_radio=False)
            .order_by("username")
        )


class CommunicationDeviceDetailView(LoginRequiredMixin, OnlyTeamMixin, DetailView):
    """Display detailed information about a communication device."""
    model = CommunicationDevice
    template_name = "comms/device_detail.html"
    context_object_name = "device"


class CommunicationDeviceUpdateView(LoginRequiredMixin, OnlyTeamMixin, UpdateView):
    """Allow LSA/SOC to update device information."""
    model = CommunicationDevice
    fields = [
        "device_type", "call_sign", "imei", "serial_number",
        "status", "assigned_to", "notes"
    ]
    template_name = "comms/device_form.html"

    def form_valid(self, form):
        """Validate device-specific requirements before saving."""
        device = self.get_object()
        old_status = device.status
        old_assigned_to = device.assigned_to

        response = super().form_valid(form)
        messages.success(self.request, "Device updated successfully.")

        # After save, notify about important changes
        self.object.refresh_from_db()
        _notify_device_status_change(self.object, old_status, old_assigned_to)

        return response

    def get_success_url(self):
        return reverse_lazy("comms:device_detail", args=[self.object.pk])


# ============================================================================
# DEVICE STATUS UPDATES
# ============================================================================

@require_POST
@login_required
@user_passes_test(is_lsa_or_soc)
def radio_update_status(request: HttpRequest, pk: int):
    """Update radio status (LSA/SOC only)."""
    radio = get_object_or_404(
        CommunicationDevice,
        pk=pk,
        device_type__in=["hf", "vhf"]
    )

    old_status = radio.status
    old_assigned_to = radio.assigned_to

    new_status = request.POST.get("status")
    valid_statuses = dict(CommunicationDevice.STATUS).keys()

    if new_status not in valid_statuses:
        messages.error(request, "Invalid status.")
    else:
        radio.status = new_status
        # Auto-unassign if not 'with_user'
        if new_status != "with_user":
            radio.assigned_to = None
        radio.save(update_fields=["status", "assigned_to"])
        messages.success(
            request,
            f"Status updated to {radio.get_status_display()}."
        )

        _notify_device_status_change(radio, old_status, old_assigned_to)

    next_url = request.POST.get("next")
    return redirect(next_url or "comms:radios")


@require_POST
@login_required
@user_passes_test(is_lsa_or_soc)
def device_mark_status(request: HttpRequest, pk: int):
    """Update any device status (LSA/SOC only)."""
    device = get_object_or_404(CommunicationDevice, pk=pk)

    old_status = device.status
    old_assigned_to = device.assigned_to

    new_status = request.POST.get("status", "").strip()
    valid_statuses = dict(CommunicationDevice.STATUS).keys()

    if new_status not in valid_statuses:
        messages.error(request, "Invalid status.")
    else:
        device.status = new_status
        # Auto-unassign if not 'with_user'
        if new_status != "with_user":
            device.assigned_to = None
        device.save(update_fields=["status", "assigned_to"])
        messages.success(
            request,
            f"Status updated to {device.get_status_display()}."
        )

        _notify_device_status_change(device, old_status, old_assigned_to)

    next_url = request.POST.get("next")
    return redirect(next_url or "comms:device_detail", pk=device.pk)


# ============================================================================
# RADIO CHECK SESSIONS
# ============================================================================

class RadioCheckStartView(LoginRequiredMixin, OnlyTeamMixin, FormView):
    """Start a new radio check session."""
    template_name = "comms/check_start.html"
    form_class = RadioCheckSessionForm

    def form_valid(self, form):
        session = form.save(commit=False)
        session.created_by = self.request.user
        # if your model has started_at, set it
        if hasattr(session, "started_at") and not session.started_at:
            session.started_at = timezone.now()
        session.save()

        # Pre-populate entries for all radios
        radios = CommunicationDevice.objects.filter(
            device_type__in=["hf", "vhf"]
        ).order_by("call_sign")

        bulk_entries = [
            RadioCheckEntry(
                session=session,
                device=radio,
                call_sign=radio.call_sign or "",
                responded=None,
                checked_by=self.request.user
            )
            for radio in radios
        ]
        RadioCheckEntry.objects.bulk_create(bulk_entries)

        messages.success(self.request, "Radio check started.")

        # Notify all relevant users about start
        _notify_radio_check_started(session, self.request)

        return redirect("comms:check_run", pk=session.pk)


class RadioCheckRunView(LoginRequiredMixin, OnlyTeamMixin, DetailView):
    """Run a radio check session - mark radios as responding or not."""
    model = RadioCheckSession
    template_name = "comms/check_run.html"
    context_object_name = "session"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        session = self.object
        entries = session.entries.all()

        # Calculate stats
        responded_count = entries.filter(responded=True).count()
        missed_count = entries.filter(responded=False).count()
        pending_count = entries.filter(responded=None).count()

        ctx['stats'] = {
            'total': entries.count(),
            'responded': responded_count,
            'missed': missed_count,
            'pending': pending_count,
        }
        return ctx

    def post(self, request, *args, **kwargs):
        session = self.get_object()
        entries_to_update = []

        for entry in session.entries.select_related("device").all():
            val = request.POST.get(f"responded_{entry.pk}")
            issue = request.POST.get(f"issue_{entry.pk}", "").strip()

            if val in ("yes", "no"):
                entry.responded = (val == "yes")
                entry.noted_issue = issue
                entry.checked_by = request.user
                entry.checked_at = timezone.now()
                entries_to_update.append(entry)

        # Bulk update for better performance
        if entries_to_update:
            RadioCheckEntry.objects.bulk_update(
                entries_to_update,
                ["responded", "noted_issue", "checked_by", "checked_at"]
            )

        # If the form includes a "finish session" button named 'finish_session',
        # treat this as the end of the radio check and trigger report + notifications.
        if "finish_session" in request.POST:
            # Mark session as ended if your model supports it
            update_fields = []
            if hasattr(session, "ended_at"):
                session.ended_at = timezone.now()
                update_fields.append("ended_at")
            if hasattr(session, "status") and getattr(session, "status", None) != "completed":
                session.status = "completed"
                update_fields.append("status")
            if update_fields:
                session.save(update_fields=update_fields)

            # Notify users whose radios did not respond
            missed_entries = session.entries.select_related("device", "device__assigned_to").filter(responded=False)
            for entry in missed_entries:
                dev = entry.device
                if dev and dev.assigned_to:
                    _notify_user_missed_radio_check(dev.assigned_to, dev, session)

            # Send summary report to LSA/SOC + creator
            _notify_radio_check_report(session)

            messages.success(request, "Radio check completed and report generated.")
            return redirect("comms:check_run", pk=session.pk)

        messages.success(request, "Radio check updated.")
        return redirect("comms:check_run", pk=session.pk)


# ============================================================================
# EXPORT FUNCTIONALITY
# ============================================================================

def _create_xlsx_response(
        filename: str,
        headers: list,
        rows: list
) -> Optional[HttpResponse]:
    """Helper to create XLSX response using openpyxl."""
    try:
        from openpyxl import Workbook
    except ImportError:
        return None

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    resp = HttpResponse(
        bio.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
@user_passes_test(is_lsa_or_soc)
def export_radios_csv(request: HttpRequest) -> HttpResponse:
    """Export radios to CSV."""
    qs = (
        CommunicationDevice.objects
        .filter(device_type__in=["hf", "vhf"])
        .select_related("assigned_to")
        .order_by("call_sign")
    )

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="radios.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "Call Sign", "Type", "Status",
        "Assigned To", "Serial", "Notes"
    ])

    for radio in qs:
        writer.writerow([
            radio.call_sign,
            radio.get_device_type_display(),
            radio.get_status_display(),
            radio.assigned_to.username if radio.assigned_to else "",
            radio.serial_number or "",
            radio.notes or ""
        ])

    return response


@login_required
@user_passes_test(is_lsa_or_soc)
def export_radios_xlsx(request: HttpRequest) -> HttpResponse:
    """Export radios to XLSX (falls back to CSV if openpyxl unavailable)."""
    qs = (
        CommunicationDevice.objects
        .filter(device_type__in=["hf", "vhf"])
        .select_related("assigned_to")
        .order_by("call_sign")
    )

    rows = [
        [
            r.call_sign,
            r.get_device_type_display(),
            r.get_status_display(),
            r.assigned_to.username if r.assigned_to else "",
            r.serial_number or "",
            r.notes or ""
        ]
        for r in qs
    ]

    headers = ["Call Sign", "Type", "Status", "Assigned To", "Serial", "Notes"]
    resp = _create_xlsx_response("radios.xlsx", headers, rows)
    return resp or export_radios_csv(request)


@login_required
@user_passes_test(is_lsa_or_soc)
def export_satphones_csv(request: HttpRequest) -> HttpResponse:
    """Export satellite phones to CSV."""
    qs = (
        CommunicationDevice.objects
        .filter(device_type="satphone")
        .select_related("assigned_to")
        .order_by("imei")
    )

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="satphones.csv"'

    writer = csv.writer(response)
    writer.writerow(["IMEI", "Status", "Assigned To", "Serial", "Notes"])

    for phone in qs:
        writer.writerow([
            phone.imei or "",
            phone.get_status_display(),
            phone.assigned_to.username if phone.assigned_to else "",
            phone.serial_number or "",
            phone.notes or ""
        ])

    return response


@login_required
@user_passes_test(is_lsa_or_soc)
def export_satphones_xlsx(request: HttpRequest) -> HttpResponse:
    """Export satellite phones to XLSX (falls back to CSV)."""
    qs = (
        CommunicationDevice.objects
        .filter(device_type="satphone")
        .select_related("assigned_to")
        .order_by("imei")
    )

    rows = [
        [
            p.imei or "",
            p.get_status_display(),
            p.assigned_to.username if p.assigned_to else "",
            p.serial_number or "",
            p.notes or ""
        ]
        for p in qs
    ]

    headers = ["IMEI", "Status", "Assigned To", "Serial", "Notes"]
    resp = _create_xlsx_response("satphones.xlsx", headers, rows)
    return resp or export_satphones_csv(request)


@login_required
@user_passes_test(is_lsa_or_soc)
def export_check_csv(request: HttpRequest, pk: int) -> HttpResponse:
    """Export radio check session to CSV."""
    session = get_object_or_404(RadioCheckSession, pk=pk)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="radio_check_{session.pk}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "Session", "Started", "Call Sign", "Responded",
        "Issue", "Checked By", "Checked At"
    ])

    for entry in session.entries.select_related("checked_by").order_by("call_sign"):
        responded_display = {True: "YES", False: "NO", None: "—"}[entry.responded]
        writer.writerow([
            session.name,
            session.started_at,
            entry.call_sign,
            responded_display,
            entry.noted_issue or "",
            entry.checked_by.username if entry.checked_by else "",
            entry.checked_at or ""
        ])

    return response


@login_required
@user_passes_test(is_lsa_or_soc)
def export_check_xlsx(request: HttpRequest, pk: int) -> HttpResponse:
    """Export radio check session to XLSX (falls back to CSV)."""
    session = get_object_or_404(RadioCheckSession, pk=pk)

    rows = []
    for entry in session.entries.select_related("checked_by").order_by("call_sign"):
        responded_display = {True: "YES", False: "NO", None: "—"}[entry.responded]
        rows.append([
            session.name,
            session.started_at,
            entry.call_sign,
            responded_display,
            entry.noted_issue or "",
            entry.checked_by.username if entry.checked_by else "",
            entry.checked_at or ""
        ])

    headers = [
        "Session", "Started", "Call Sign", "Responded",
        "Issue", "Checked By", "Checked At"
    ]
    resp = _create_xlsx_response(f"radio_check_{session.pk}.xlsx", headers, rows)
    return resp or export_check_csv(request, pk)


@login_required
@user_passes_test(is_lsa_or_soc)
def export_users_without_radios_csv(request: HttpRequest) -> HttpResponse:
    """Export users without radios to CSV."""
    radio_subq = CommunicationDevice.objects.filter(
        device_type__in=["hf", "vhf"],
        assigned_to=OuterRef("pk")
    )
    users = (
        User.objects.filter(is_active=True)
        .exclude(role__in=["guard", "data_entry"])
        .annotate(has_radio=Exists(radio_subq))
        .filter(has_radio=False)
        .order_by("username")
    )

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="users_without_radios.csv"'

    writer = csv.writer(response)
    writer.writerow(["Username", "First Name", "Last Name", "Full Name", "Email", "Role"])

    for user in users:
        writer.writerow([
            user.username,
            user.first_name or "",
            user.last_name or "",
            user.get_full_name() or "",
            user.email or "",
            user.get_role_display() if hasattr(user, 'get_role_display') else (
                user.role if hasattr(user, 'role') else "")
        ])

    return response


@login_required
@user_passes_test(is_lsa_or_soc)
def export_users_without_radios_xlsx(request: HttpRequest) -> HttpResponse:
    """Export users without radios to XLSX (falls back to CSV)."""
    radio_subq = CommunicationDevice.objects.filter(
        device_type__in=["hf", "vhf"],
        assigned_to=OuterRef("pk")
    )
    users = (
        User.objects.filter(is_active=True)
        .exclude(role__in=["guard", "data_entry"])
        .annotate(has_radio=Exists(radio_subq))
        .filter(has_radio=False)
        .order_by("username")
    )

    rows = [
        [
            user.username,
            user.first_name or "",
            user.last_name or "",
            user.get_full_name() or "",
            user.email or "",
            user.get_role_display() if hasattr(user, 'get_role_display') else (
                user.role if hasattr(user, 'role') else "")
        ]
        for user in users
    ]

    headers = ["Username", "First Name", "Last Name", "Full Name", "Email", "Role"]
    resp = _create_xlsx_response("users_without_radios.xlsx", headers, rows)
    return resp or export_users_without_radios_csv(request)
