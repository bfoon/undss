from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy, reverse
from django.utils.decorators import method_decorator
from django.contrib import messages
from django.utils import timezone
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden, HttpResponseNotAllowed
from django.db.models import Q, Count, Exists, OuterRef
from django.conf import settings
from django.core.mail import send_mail
from django.contrib.auth import get_user_model
import csv
import base64
import hashlib, json
from io import BytesIO
import random, string
import secrets
import threading
import logging
from django.db import models as db_models
from django.views.decorators.http import require_POST

from .models import (
    Vehicle, VehicleMovement,
    ParkingCard, AssetExit,
    AgencyApprover, ParkingCardRequest, Key, KeyLog,
    Package, PackageEvent,
    PackageFlowTemplate, PackageFlowStep,
    PackageStepLog, PackageNotification, PackageEvent,
    UserSignature, PackageDocument, PackageStepLog,
    SignatureField, SignatureRecord, PackageNotification,
)
from .forms import (
    VehicleForm, ParkingCardForm,
    VehicleMovementForm, QuickVehicleCheckForm,
    AssetExitForm, AssetExitItemFormSet,
    ParkingCardRequestForm, KeyForm, KeyIssueForm, KeyReturnForm,
    PackageLogForm,
    PackageFlowTemplateForm, PackageFlowStepForm,
    PackageStepActionForm, PackageOutgoingLogForm,
    UserSignatureForm, PackageDocumentUploadForm, SignatureFieldForm,
)

User = get_user_model()

logger = logging.getLogger(__name__)

def is_lsa(u): return u.is_authenticated and (getattr(u, 'role', '') == 'lsa' or u.is_superuser)
def is_lsa_or_soc(user):
    return user.is_authenticated and (getattr(user, "role", "") in ("lsa", "soc") or user.is_superuser)
def is_data_entry(u): return u.is_authenticated and (getattr(u, 'role', '') == 'data_entry' or u.is_superuser)
def _is_guard(u): return u.is_authenticated and getattr(u, "role", "") in ("data_entry", "soc", "lsa")
def _is_reception(u): return u.is_authenticated and getattr(u, "role", "") in ("reception", "lsa", "soc")
def _is_agency_or_registry(u): return u.is_authenticated and getattr(u, "role", "") in ("registry", "agency_fp", "lsa", "soc")

# ------------ Role helpers (works with either user.role or Django Groups) ------------
def user_has_role(user, *roles):
    if not user or not user.is_authenticated:
        return False
    if hasattr(user, "role") and isinstance(user.role, str):
        return user.role.lower() in [r.lower() for r in roles]
    wanted = [r.upper() for r in roles]
    return user.groups.filter(name__in=wanted).exists() or user.is_superuser


class RoleRequiredMixin(UserPassesTestMixin):
    required_roles = ()
    def test_func(self):
        return user_has_role(self.request.user, *self.required_roles)


def is_agency_approver_for(user, agency_name: str) -> bool:
    if not user.is_authenticated:
        return False
    # superusers always allowed
    if getattr(user, 'is_superuser', False):
        return True
    # Designated approver record must exist for the given agency
    return AgencyApprover.objects.filter(user=user, agency_name=agency_name).exists()


def can_view_all_packages(user):
    """
    Reception, Registry, Agency FP, LSA, SOC, Superuser can see all packages.
    Everyone else is restricted to their own packages.
    """
    if not user.is_authenticated:
        return False
    role = getattr(user, "role", "") or ""
    return user.is_superuser or role in ("reception", "registry", "agency_fp", "lsa", "soc")

def _is_lsa(user):
    return user.is_authenticated and (getattr(user, "role", "") == "lsa" or user.is_superuser)


@login_required
def key_toggle_active(request, pk):
    """
    Activate or deactivate a key (LSA / superuser only).
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    if not _is_lsa(request.user):
        return HttpResponseForbidden("You are not allowed to change key status.")

    key = get_object_or_404(Key, pk=pk)
    key.is_active = not key.is_active
    key.save(update_fields=["is_active"])

    if key.is_active:
        messages.success(request, f"Key {key.code} has been activated and can now be issued.")
    else:
        messages.warning(request, f"Key {key.code} has been deactivated and can no longer be issued.")

    return redirect("vehicles:key_detail", pk=key.pk)


# =============================================================================
# EMAIL / NOTIFICATION HELPERS
# =============================================================================

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

def _emails_for_roles(*roles, include_superusers: bool = False):
    """
    Return list of emails for active users having any of the given roles.
    """
    qs = User.objects.filter(is_active=True)
    if roles:
        qs = qs.filter(role__in=roles)
    if include_superusers:
        qs = qs | User.objects.filter(is_active=True, is_superuser=True)
    return [e for e in qs.values_list("email", flat=True) if e]


def _agency_focal_emails(agency_name: str):
    """
    Emails of agency focal points / approvers for the given agency name.
    """
    if not agency_name:
        return []
    qs = AgencyApprover.objects.filter(agency_name=agency_name).select_related("user")
    return [a.user.email for a in qs if a.user and a.user.email]


def _package_owner_emails(pkg: Package):
    """
    Try to infer the package 'owner' / intended recipient emails from common fields.
    This is defensive and will just return [] if nothing matches.
    """
    emails = set()

    # Try direct email fields on package
    for attr in ["recipient_email", "for_recipient_email", "owner_email", "email"]:
        val = getattr(pkg, attr, None)
        if val:
            emails.add(val)

    # Try foreign key relations that might be user-like
    for u_attr in ["for_recipient_user", "owner_user", "requested_by", "recipient_user"]:
        u = getattr(pkg, u_attr, None)
        if u and getattr(u, "email", None):
            emails.add(u.email)

    return list(emails)


def _guard_team_emails():
    """
    Guards / control room team (data_entry + SOC + LSA).
    """
    return _emails_for_roles("data_entry", "soc", "lsa", include_superusers=True)


# ─── Helpers For Packages ──────────────────────────────────────────────────────────────────
# ── Permission helpers ─────────────────────────────────────────────────────────

def _is_ict_focal(user) -> bool:
    """Only ICT focal points (or superusers) may configure flows."""
    return user.is_superuser or getattr(user, 'role', None) == 'ict_focal'


def _user_agency(user):
    """Return the Agency the user belongs to, or None."""
    return getattr(user, 'agency', None)


def _user_can_perform_step(user, step: PackageFlowStep) -> bool:
    return step.user_can_act(user)  # delegates to the model method


# ── Tracking code ─────────────────────────────────────────────────────────────

def _generate_tracking_code():
    year = timezone.now().year
    while True:
        code = f"PKG-{year}-{secrets.token_hex(3).upper()}"
        if not Package.objects.filter(tracking_code=code).exists():
            return code


# ── Notifications ─────────────────────────────────────────────────────────────

def _send_notifications(package, completed_step, log, actor):
    actor_name = actor.get_full_name() or actor.username
    next_step = completed_step.next_step()
    is_outgoing = package.direction == 'outgoing'

    # 1. In-app: notify original logger
    if completed_step.notify_requester and package.logged_by_id:
        PackageNotification.objects.create(
            package=package,
            recipient=package.logged_by,
            message=f"{'Outgoing' if is_outgoing else 'Package'} {package.tracking_code}: "
                    f"'{completed_step.name}' completed by {actor_name}.",
        )

    # 2. Email agency focal / internal originator
    notify_email = package.dest_focal_email if not is_outgoing else package.sender_email
    if completed_step.notify_focal_email and notify_email:
        lines = [
            f"{'Outgoing item' if is_outgoing else 'Package'} {package.tracking_code} "
            f"({package.item_type}) has completed the '{completed_step.name}' stage.",
            "",
            f"{'To' if is_outgoing else 'Destination'} : {package.destination_agency}",
            f"For          : {package.for_recipient or '—'}",
            f"Performed by : {actor_name}",
            f"Time         : {log.performed_at:%Y-%m-%d %H:%M}",
        ]
        if log.note:      lines += ["", f"Note: {log.note}"]
        if log.routed_to: lines += [f"Dispatched via: {log.routed_to}"]
        lines += ["", f"Next step: {next_step.name}"] if next_step else ["", "Workflow complete."]
        send_mail(
            subject=f"[UN PASS Mailroom] {package.tracking_code} — {completed_step.name}",
            message="\n".join(lines),
            from_email=None,
            recipient_list=[notify_email],
            fail_silently=True,
        )

    # 3. In-app: next-step role + named-user notifications (agency-scoped)
    if next_step:
        notified_pks = set()
        for role in next_step.notify_next_roles_list:
            for u in User.objects.filter(
                    agency=completed_step.template.agency, role=role, is_active=True
            ):
                if u.pk not in notified_pks:
                    PackageNotification.objects.create(
                        package=package, recipient=u,
                        message=f"{'Outgoing' if is_outgoing else 'Package'} "
                                f"{package.tracking_code} is ready for: '{next_step.name}'.",
                    )
                    notified_pks.add(u.pk)
        for u in next_step.notify_next_users.filter(is_active=True):
            if u.pk not in notified_pks:
                PackageNotification.objects.create(
                    package=package, recipient=u,
                    message=f"{'Outgoing' if is_outgoing else 'Package'} "
                            f"{package.tracking_code} is ready for: '{next_step.name}'.",
                )
                notified_pks.add(u.pk)

    # 4. Email internal recipient (incoming) or external recipient (outgoing) on terminal
    if completed_step.is_terminal and completed_step.notify_recipient:
        if is_outgoing:
            # Email the external recipient that their item is on its way / delivered
            ext_email = getattr(package, 'recipient_email', '') or package.dest_focal_email
            if ext_email:
                send_mail(
                    subject=f"[UN PASS] Item {package.tracking_code} dispatched to you",
                    message="\n".join([
                        f"Dear {package.for_recipient or 'Recipient'},",
                        "",
                        f"An item ({package.item_type}) from {package.sender_name} "
                        f"({package.sender_org or package.destination_agency}) "
                        f"has been dispatched and is on its way to you.",
                        "",
                        f"Reference : {package.tracking_code}",
                        f"Dispatched: {log.performed_at:%Y-%m-%d %H:%M}",
                        f"By        : {actor_name}",
                    ]),
                    from_email=None,
                    recipient_list=[ext_email],
                    fail_silently=True,
                )
        else:
            # Existing incoming: email agency focal
            if package.dest_focal_email:
                recipient_display = log.recipient_name or package.for_recipient or "the designated recipient"
                send_mail(
                    subject=f"[UN PASS] Package {package.tracking_code} Delivered",
                    message=(
                        f"Package {package.tracking_code} has been delivered "
                        f"to {recipient_display}.\n"
                        f"Delivered by: {actor_name}\n"
                        f"Time: {log.performed_at:%Y-%m-%d %H:%M}"
                    ),
                    from_email=None,
                    recipient_list=[package.dest_focal_email],
                    fail_silently=True,
                )

    # 5. Email original sender (incoming) / internal originator (outgoing) on delivery
    if completed_step.notify_sender:
        if is_outgoing:
            # Notify internal originator that their outgoing item was dispatched
            if package.sender_email:
                send_mail(
                    subject=f"[UN PASS] Your outgoing item {package.tracking_code} has been dispatched",
                    message="\n".join([
                        f"Dear {package.sender_name},",
                        "",
                        f"Your outgoing item (ref: {package.tracking_code}) addressed to "
                        f"{package.for_recipient or package.destination_agency} "
                        f"has been successfully dispatched.",
                        "",
                        f"Item type   : {package.item_type}",
                        f"Dispatched by: {actor_name}",
                        f"Date & time  : {log.performed_at:%Y-%m-%d %H:%M}",
                    ]),
                    from_email=None,
                    recipient_list=[package.sender_email],
                    fail_silently=True,
                )
        else:
            # Existing incoming: notify external sender that their package arrived
            if package.sender_email:
                recipient_display = log.recipient_name or package.for_recipient or "the intended recipient"
                send_mail(
                    subject=f"[UN PASS] Your package {package.tracking_code} has been delivered",
                    message="\n".join([
                        f"Dear {package.sender_name},",
                        "",
                        f"Your package (ref: {package.tracking_code}) addressed to "
                        f"{package.destination_agency} has been successfully delivered "
                        f"to {recipient_display}.",
                        "",
                        f"Item type    : {package.item_type}",
                        f"Delivered by : {actor_name}",
                        f"Date & time  : {log.performed_at:%Y-%m-%d %H:%M}",
                    ]),
                    from_email=None,
                    recipient_list=[package.sender_email],
                    fail_silently=True,
                )


# --------------------------------- Vehicle CRUD -------------------------------------

class VehicleListView(LoginRequiredMixin, ListView):
    model = Vehicle
    template_name = 'vehicles/vehicle_list.html'
    context_object_name = 'vehicles'
    paginate_by = 20

    def get_queryset(self):
        qs = Vehicle.objects.all().order_by('plate_number')
        search = self.request.GET.get('search')
        if search:
            qs = qs.filter(
                Q(plate_number__icontains=search) |
                Q(make__icontains=search) |
                Q(model__icontains=search) |
                Q(color__icontains=search) |
                Q(un_agency__icontains=search)
            )
        vt = self.request.GET.get('type')
        if vt:
            qs = qs.filter(vehicle_type=vt)
        return qs


class VehicleDetailView(LoginRequiredMixin, DetailView):
    model = Vehicle
    template_name = 'vehicles/vehicle_detail.html'
    context_object_name = 'vehicle'


class VehicleCreateView(LoginRequiredMixin, RoleRequiredMixin, CreateView):
    model = Vehicle
    form_class = VehicleForm
    template_name = 'vehicles/vehicle_form.html'
    success_url = reverse_lazy('vehicles:vehicle_list')
    required_roles = ('lsa', 'data_entry')

    def form_valid(self, form):
        resp = super().form_valid(form)
        messages.success(self.request, f'Vehicle {form.instance.plate_number} registered successfully.')
        return resp


class VehicleUpdateView(LoginRequiredMixin, RoleRequiredMixin, UpdateView):
    model = Vehicle
    form_class = VehicleForm
    template_name = 'vehicles/vehicle_form.html'
    success_url = reverse_lazy('vehicles:vehicle_list')
    required_roles = ('lsa', 'data_entry')

    def form_valid(self, form):
        resp = super().form_valid(form)
        messages.success(self.request, f'Vehicle {form.instance.plate_number} updated successfully.')
        return resp


class VehicleDeleteView(LoginRequiredMixin, RoleRequiredMixin, DeleteView):
    model = Vehicle
    template_name = 'vehicles/vehicle_confirm_delete.html'
    success_url = reverse_lazy('vehicles:vehicle_list')
    required_roles = ('lsa',)

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        plate = obj.plate_number
        resp = super().delete(request, *args, **kwargs)
        messages.success(request, f'Vehicle {plate} deleted.')
        return resp


# ------------------------------- Vehicle Movements -----------------------------------

class VehicleMovementListView(LoginRequiredMixin, ListView):
    model = VehicleMovement
    template_name = 'vehicles/movement_list.html'
    context_object_name = 'movements'
    paginate_by = 30

    def get_queryset(self):
        qs = VehicleMovement.objects.select_related('vehicle').order_by('-timestamp')
        plate = self.request.GET.get('plate')
        mtype = self.request.GET.get('type')
        if plate:
            qs = qs.filter(vehicle__plate_number__icontains=plate)
        if mtype in ('entry', 'exit'):
            qs = qs.filter(movement_type=mtype)
        return qs


class VehicleMovementDetailView(LoginRequiredMixin, DetailView):
    model = VehicleMovement
    template_name = 'vehicles/movement_detail.html'
    context_object_name = 'movement'


@login_required
def record_vehicle_movement(request):
    """
    Uses your VehicleMovementForm which:
    - accepts plate_number
    - creates/gets Vehicle (visitor default) in form.save()
    - saves movement fields (movement_type, gate, driver_name, purpose, notes)
    """
    if request.method == 'POST':
        form = VehicleMovementForm(request.POST)
        if form.is_valid():
            movement = form.save(commit=False)
            movement.recorded_by = request.user
            if not getattr(movement, 'timestamp', None):
                movement.timestamp = timezone.now()
            movement.save()

            word = "entered" if movement.movement_type == 'entry' else 'exited'
            messages.success(request, f"Vehicle {movement.vehicle.plate_number} {word} recorded successfully.")
            return redirect('vehicles:record_movement')
    else:
        form = VehicleMovementForm()

    return render(request, 'vehicles/record_movement.html', {'form': form})


@login_required
def quick_movement_page(request):
    """
    Simple page combining:
    - QuickVehicleCheckForm (validate card via AJAX)
    - VehicleMovementForm (fast entry/exit)
    """
    movement_form = VehicleMovementForm()
    check_form = QuickVehicleCheckForm()
    return render(request, 'vehicles/quick_movement.html', {
        'movement_form': movement_form,
        'check_form': check_form
    })


# -------------------------------- Parking Cards -------------------------------------

class ParkingCardListView(LoginRequiredMixin, ListView):
    model = ParkingCard
    template_name = 'vehicles/parking_card_list.html'
    context_object_name = 'cards'
    paginate_by = 20

    def get_queryset(self):
        qs = ParkingCard.objects.all().order_by('-expiry_date')
        search = self.request.GET.get('search')
        if search:
            qs = qs.filter(
                Q(card_number__icontains=search) |
                Q(owner_name__icontains=search) |
                Q(vehicle_plate__icontains=search) |
                Q(vehicle_make__icontains=search) |
                Q(vehicle_model__icontains=search)
            )
        return qs


class ParkingCardDetailView(LoginRequiredMixin, DetailView):
    model = ParkingCard
    template_name = 'vehicles/parking_card_detail.html'
    context_object_name = 'card'


class ParkingCardCreateView(LoginRequiredMixin, RoleRequiredMixin, CreateView):
    model = ParkingCard
    form_class = ParkingCardForm
    template_name = 'vehicles/parking_card_form.html'
    success_url = reverse_lazy('vehicles:parking_card_list')
    required_roles = ('lsa',)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # If your model has is_active
        if hasattr(ParkingCard, 'is_active'):
            ctx['active_cards_count'] = ParkingCard.objects.filter(is_active=True).count()
        else:
            ctx['active_cards_count'] = ParkingCard.objects.count()
        return ctx

    def form_valid(self, form):
        if hasattr(self.request.user, 'id') and hasattr(ParkingCard, 'created_by'):
            form.instance.created_by = self.request.user
        resp = super().form_valid(form)
        messages.success(self.request, f'Parking card {form.instance.card_number} created successfully.')
        return resp


class ParkingCardUpdateView(LoginRequiredMixin, RoleRequiredMixin, UpdateView):
    model = ParkingCard
    form_class = ParkingCardForm
    template_name = 'vehicles/parking_card_form.html'
    success_url = reverse_lazy('vehicles:parking_card_list')
    required_roles = ('lsa',)

    def form_valid(self, form):
        resp = super().form_valid(form)
        messages.success(self.request, f'Parking card {form.instance.card_number} updated.')
        return resp


@login_required
def deactivate_parking_card(request, pk):
    card = get_object_or_404(ParkingCard, pk=pk)
    if not user_has_role(request.user, 'lsa'):
        messages.error(request, "You don't have permission to deactivate cards.")
        return redirect('vehicles:parking_card_detail', pk=pk)

    if hasattr(card, 'is_active'):
        card.is_active = False
        card.save(update_fields=['is_active'])
        messages.success(request, f'Parking card {card.card_number} deactivated.')
    else:
        messages.error(request, "This ParkingCard model has no 'is_active' field.")
    return redirect('vehicles:parking_card_detail', pk=pk)


@login_required
def reactivate_parking_card(request, pk):
    card = get_object_or_404(ParkingCard, pk=pk)
    if not user_has_role(request.user, 'lsa'):
        messages.error(request, "You don't have permission to reactivate cards.")
        return redirect('vehicles:parking_card_detail', pk=pk)

    if hasattr(card, 'is_active'):
        card.is_active = True
        card.save(update_fields=['is_active'])
        messages.success(request, f'Parking card {card.card_number} reactivated.')
    else:
        messages.error(request, "This ParkingCard model has no 'is_active' field.")
    return redirect('vehicles:parking_card_detail', pk=pk)


# ----------------------------- Reports & Analytics (HTML) ----------------------------

@login_required
def vehicle_reports_view(request):
    total = Vehicle.objects.count()
    by_type = Vehicle.objects.values('vehicle_type').annotate(c=Count('id')).order_by('-c')
    return render(request, 'vehicles/reports/vehicles.html', {
        'total': total,
        'by_type': by_type,
    })


@login_required
def movement_reports_view(request):
    start = request.GET.get('start')
    end = request.GET.get('end')
    qs = VehicleMovement.objects.select_related('vehicle').order_by('-timestamp')
    if start:
        qs = qs.filter(timestamp__date__gte=start)
    if end:
        qs = qs.filter(timestamp__date__lte=end)
    return render(request, 'vehicles/reports/movements.html', {'movements': qs})


@login_required
def parking_card_reports_view(request):
    # If model has is_active:
    if hasattr(ParkingCard, 'is_active'):
        active = ParkingCard.objects.filter(is_active=True).count()
        inactive = ParkingCard.objects.filter(is_active=False).count()
    else:
        active = ParkingCard.objects.count()
        inactive = 0

    expiring_soon = ParkingCard.objects.filter(
        expiry_date__lte=timezone.now().date() + timezone.timedelta(days=30)
    ).count()

    return render(request, 'vehicles/reports/parking_cards.html', {
        'active': active,
        'inactive': inactive,
        'expiring_soon': expiring_soon
    })


# ------------------------------------- APIs (JSON) -----------------------------------

@login_required
def validate_parking_card(request):
    """
    GET ?card_number=PC-001
    Matches your ParkingCardForm fields and your VehicleForm filtering active cards.
    """
    card_number = (request.GET.get('card_number') or '').strip()
    if not card_number:
        return JsonResponse({'valid': False, 'error': 'Card number is required'})

    try:
        qs = ParkingCard.objects
        if hasattr(ParkingCard, 'is_active'):
            qs = qs.filter(is_active=True)
        card = qs.get(card_number=card_number)

        # Expiry check if provided
        if card.expiry_date and card.expiry_date <= timezone.now().date():
            return JsonResponse({
                'valid': False,
                'error': 'Parking card expired',
                'expiry_date': card.expiry_date.isoformat()
            })

        return JsonResponse({
            'valid': True,
            'owner_name': getattr(card, 'owner_name', ''),
            'vehicle_plate': getattr(card, 'vehicle_plate', ''),
            'department': getattr(card, 'department', ''),
            'expiry_date': card.expiry_date.isoformat() if card.expiry_date else None,
            'owner_id': getattr(card, 'owner_id', ''),
            'phone': getattr(card, 'phone', ''),
            'vehicle_make': getattr(card, 'vehicle_make', ''),
            'vehicle_model': getattr(card, 'vehicle_model', ''),
            'vehicle_color': getattr(card, 'vehicle_color', ''),
        })
    except ParkingCard.DoesNotExist:
        return JsonResponse({'valid': False, 'error': 'Invalid parking card number'})


@login_required
def vehicle_lookup(request):
    """
    GET ?plate_number=ABC-123
    Matches your VehicleForm fields and adds latest movement info.
    """
    plate_number = (request.GET.get('plate_number') or '').upper().strip()
    if not plate_number:
        return JsonResponse({'found': False, 'error': 'Plate number is required'})

    try:
        vehicle = Vehicle.objects.get(plate_number=plate_number)

        latest = VehicleMovement.objects.filter(vehicle=vehicle).order_by('-timestamp').first()
        data = {
            'found': True,
            'plate_number': vehicle.plate_number,
            'vehicle_type': getattr(vehicle, 'vehicle_type', ''),
            'make': getattr(vehicle, 'make', ''),
            'model': getattr(vehicle, 'model', ''),
            'color': getattr(vehicle, 'color', ''),
            'un_agency': getattr(vehicle, 'un_agency', ''),  # str or FK name, adjust if FK
        }

        if hasattr(vehicle, 'parking_card') and vehicle.parking_card:
            pc = vehicle.parking_card
            valid = True
            if hasattr(pc, 'is_active'):
                valid = pc.is_active
            if pc.expiry_date:
                valid = valid and pc.expiry_date >= timezone.now().date()
            data['parking_card'] = {
                'number': pc.card_number,
                'owner': getattr(pc, 'owner_name', ''),
                'valid': valid
            }

        if latest:
            data['last_movement'] = {
                'type': latest.movement_type,
                'timestamp': latest.timestamp.isoformat(),
                'gate': latest.gate,
                'driver_name': getattr(latest, 'driver_name', ''),
                'purpose': getattr(latest, 'purpose', ''),
            }

        return JsonResponse(data)

    except Vehicle.DoesNotExist:
        return JsonResponse({'found': False, 'message': 'Vehicle not found in system'})


@login_required
def recent_movements_api(request):
    count = int(request.GET.get('count', 10))
    qs = VehicleMovement.objects.select_related('vehicle').order_by('-timestamp')[:count]
    data = [{
        'id': m.id,
        'vehicle': m.vehicle.plate_number if m.vehicle else None,
        'type': m.movement_type,
        'gate': m.gate,
        'driver_name': getattr(m, 'driver_name', ''),
        'timestamp': m.timestamp.isoformat(),
    } for m in qs]
    return JsonResponse({'results': data})


@login_required
def vehicle_stats_api(request):
    total = Vehicle.objects.count()
    by_type = Vehicle.objects.values('vehicle_type').annotate(c=Count('id')).order_by('-c')

    # Estimate "inside" by last movement per vehicle = entry w/o a later exit
    # (simple heuristic; adapt if you store explicit "inside" flags)
    entries = VehicleMovement.objects.filter(movement_type='entry').values_list('vehicle_id', flat=True)
    exits = set(VehicleMovement.objects.filter(movement_type='exit').values_list('vehicle_id', flat=True))
    inside_estimate = len([vid for vid in set(entries) if vid not in exits])

    return JsonResponse({
        'total': total,
        'by_type': list(by_type),
        'estimated_inside': inside_estimate,
    })


@login_required
def compound_status_api(request):
    """
    Count vehicles inside by type using a simple heuristic:
    vehicles with more entries than exits are likely inside.
    """
    inside_by_type = {}
    vehicles = Vehicle.objects.values('id', 'vehicle_type', 'plate_number')
    for v in vehicles:
        vid = v['id']
        entries = VehicleMovement.objects.filter(vehicle_id=vid, movement_type='entry').count()
        exits = VehicleMovement.objects.filter(vehicle_id=vid, movement_type='exit').count()
        if entries > exits:
            inside_by_type[v['vehicle_type']] = inside_by_type.get(v['vehicle_type'], 0) + 1
    return JsonResponse({'inside_by_type': inside_by_type})


# ------------------------------------- CSV exports -----------------------------------

@login_required
def export_movements(request):
    start = request.GET.get('start')
    end = request.GET.get('end')
    qs = VehicleMovement.objects.select_related('vehicle').order_by('-timestamp')
    if start:
        qs = qs.filter(timestamp__date__gte=start)
    if end:
        qs = qs.filter(timestamp__date__lte=end)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="movements.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Plate', 'Type', 'Gate', 'Driver', 'Purpose', 'Timestamp', 'Recorded By'])
    for m in qs:
        writer.writerow([
            m.id,
            m.vehicle.plate_number if m.vehicle else '',
            m.movement_type,
            m.gate,
            getattr(m, 'driver_name', ''),
            getattr(m, 'purpose', ''),
            timezone.localtime(m.timestamp).isoformat(),
            getattr(m.recorded_by, 'username', ''),
        ])
    return response


@login_required
def export_parking_cards(request):
    qs = ParkingCard.objects.all().order_by('card_number')
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="parking_cards.csv"'
    writer = csv.writer(response)
    writer.writerow([
        'Card Number', 'Owner Name', 'Owner ID', 'Phone', 'Department',
        'Vehicle Make', 'Vehicle Model', 'Vehicle Plate', 'Vehicle Color',
        'Expiry Date', 'Active'
    ])
    for c in qs:
        writer.writerow([
            c.card_number,
            getattr(c, 'owner_name', ''),
            getattr(c, 'owner_id', ''),
            getattr(c, 'phone', ''),
            getattr(c, 'department', ''),
            getattr(c, 'vehicle_make', ''),
            getattr(c, 'vehicle_model', ''),
            getattr(c, 'vehicle_plate', ''),
            getattr(c, 'vehicle_color', ''),
            c.expiry_date.isoformat() if getattr(c, 'expiry_date', None) else '',
            ('Yes' if getattr(c, 'is_active', True) else 'No')
        ])
    return response


# ---- Asset Exit: Create / list / detail ----

@login_required
def asset_exit_new(request):
    if request.method == 'POST':
        form = AssetExitForm(request.POST)
        formset = AssetExitItemFormSet(request.POST)
        if form.is_valid() and formset.is_valid():
            obj = form.save(commit=False)
            obj.requester = request.user
            obj.status = 'pending'
            obj.save()
            formset.instance = obj
            formset.save()
            messages.success(request, 'Asset exit request submitted (awaiting agency approval).')

            # --- Notifications ---
            # Notify agency approver(s)
            agency_emails = _agency_focal_emails(obj.agency_name)
            if agency_emails:
                subject = f"[Assets] New asset exit request for {obj.agency_name}"
                url = request.build_absolute_uri(reverse('vehicles:asset_exit_detail', args=[obj.pk]))
                msg = (
                    f"Dear colleague,\n\n"
                    f"A new asset exit request has been submitted and is pending your approval.\n\n"
                    f"Code: {obj.code}\n"
                    f"Agency: {obj.agency_name}\n"
                    f"Destination: {obj.destination}\n"
                    f"Requested by: {request.user.get_full_name() or request.user.username}\n\n"
                    f"Details: {url}\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, agency_emails)

            # Notify requester (confirmation)
            if request.user.email:
                subject = "[Assets] Asset exit request submitted"
                url = request.build_absolute_uri(reverse('vehicles:asset_exit_detail', args=[obj.pk]))
                msg = (
                    f"Hello {request.user.get_full_name() or request.user.username},\n\n"
                    f"Your asset exit request has been submitted and is pending agency approval.\n\n"
                    f"Code: {obj.code}\n"
                    f"Agency: {obj.agency_name}\n"
                    f"Destination: {obj.destination}\n\n"
                    f"Details: {url}\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, request.user.email)

            return redirect('vehicles:asset_exit_detail', pk=obj.pk)
    else:
        form = AssetExitForm()
        formset = AssetExitItemFormSet()
    return render(request, 'vehicles/asset_exit_form.html', {'form': form, 'formset': formset})


@login_required
def my_asset_exits(request):
    """
    Requester’s own Asset Exit list + summary cards.
    Filters: ?q=…&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD[&status=…]
    """
    # ----- Base queryset (scoped to this user) -----
    base_qs = (
        AssetExit.objects
        .select_related("requester", "agency_approver", "lsa_user", "signed_out_by", "signed_in_by")
        .filter(requester=request.user)
        .order_by("-created_at")
    )

    # ----- Common filters (affect list + summary cards) -----
    q = (request.GET.get("q") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to   = (request.GET.get("date_to") or "").strip()

    if q:
        base_qs = base_qs.filter(
            Q(code__icontains=q) |
            Q(agency_name__icontains=q) |
            Q(reason__icontains=q) |
            Q(destination__icontains=q)
        )

    if date_from:
        base_qs = base_qs.filter(created_at__date__gte=date_from)
    if date_to:
        base_qs = base_qs.filter(created_at__date__lte=date_to)

    # ----- Table queryset (optional status filter) -----
    status = (request.GET.get("status") or "").strip()
    if status:
        items = base_qs.filter(status=status)
    else:
        items = base_qs

    # ----- Summary cards -----
    total_requests  = base_qs.count()
    pending_count   = base_qs.filter(status="pending").count()
    approved_count  = base_qs.filter(status="lsa_cleared").count()          # “LSA Cleared”
    signed_out_count = base_qs.filter(signed_out_at__isnull=False).count()  # derived
    completed_count  = base_qs.filter(signed_in_at__isnull=False).count()   # derived (returned)
    rejected_count   = base_qs.filter(status__in=["rejected", "cancelled"]).count()

    context = {
        "items": items,
        "total_requests": total_requests,
        "pending_count": pending_count,
        "approved_count": approved_count,
        "signed_out_count": signed_out_count,
        "completed_count": completed_count,
        "rejected_count": rejected_count,
        "filters": {
            "q": q,
            "date_from": date_from,
            "date_to": date_to,
            "status": status,
        },
    }
    return render(request, "vehicles/asset_exit_list.html", context)


@login_required
def asset_exit_detail(request, pk):
    obj = get_object_or_404(AssetExit, pk=pk)
    return render(request, 'vehicles/asset_exit_detail.html', {'obj': obj})


@login_required
def asset_exit_agency_approve(request, pk):
    obj = get_object_or_404(AssetExit, pk=pk)
    if obj.status != 'pending':
        messages.info(request, 'This request is not pending agency approval.')
        return redirect('vehicles:asset_exit_detail', pk=pk)

    if not is_agency_approver_for(request.user, obj.agency_name):
        messages.error(request, "You're not a designated approver for this agency.")
        return redirect('vehicles:asset_exit_detail', pk=pk)

    obj.approve_by_agency(request.user)
    messages.success(request, 'Agency approved. Awaiting LSA clearance.')

    # --- Notifications ---
    detail_url = request.build_absolute_uri(reverse('vehicles:asset_exit_detail', args=[obj.pk]))

    # Notify LSA / SOC
    lsa_emails = _emails_for_roles("lsa", "soc", include_superusers=True)
    if lsa_emails:
        subject = f"[Assets] Asset exit pending LSA clearance ({obj.code})"
        msg = (
            f"Dear Security team,\n\n"
            f"The following asset exit request has been approved by the agency and is pending LSA clearance:\n\n"
            f"Code: {obj.code}\n"
            f"Agency: {obj.agency_name}\n"
            f"Destination: {obj.destination}\n"
            f"Requester: {obj.requester.get_full_name() or obj.requester.username}\n\n"
            f"Details: {detail_url}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, lsa_emails)

    # Notify requester
    if obj.requester and obj.requester.email:
        subject = "[Assets] Agency approved your asset exit request"
        msg = (
            f"Hello {obj.requester.get_full_name() or obj.requester.username},\n\n"
            f"Your asset exit request ({obj.code}) has been approved by your agency.\n"
            f"It is now pending LSA clearance.\n\n"
            f"Details: {detail_url}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, obj.requester.email)

    return redirect('vehicles:asset_exit_detail', pk=pk)


@login_required
def asset_exit_edit(request, pk):
    ax = get_object_or_404(AssetExit, pk=pk)
    # optional: check ownership/permissions here
    if request.method == 'POST':
        form = AssetExitForm(request.POST, instance=ax)
        if form.is_valid():
            form.save()
            return redirect('vehicles:asset_exit_detail', pk=ax.pk)
    else:
        form = AssetExitForm(instance=ax)
    return render(request, 'vehicles/asset_exit_form.html', {'form': form, 'object': ax})


@login_required
def asset_exit_print(request, pk):
    ax = get_object_or_404(AssetExit, pk=pk)
    # Render a print-friendly template
    return render(request, 'vehicles/asset_exit_print.html', {'object': ax})


@login_required
def asset_exit_duplicate(request, pk):
    ax = get_object_or_404(AssetExit, pk=pk)
    # Create a new draft request copying fields; adjust to your model
    new_ax = AssetExit.objects.create(
        requested_by=request.user,
        agency_name=ax.agency_name,
        items_summary=ax.items_summary,
        # copy any other safe fields; do NOT copy approval/decision fields
        status='pending'
    )
    # If you have related items, copy them as well here
    return redirect('vehicles:asset_exit_detail', pk=new_ax.pk)


# ---- LSA actions ----

@login_required
@user_passes_test(is_lsa)
def asset_exit_lsa_clear(request, pk):
    obj = get_object_or_404(AssetExit, pk=pk)
    if obj.status != 'pending':
        messages.info(request, 'This request is not pending.')
        return redirect('vehicles:asset_exit_detail', pk=pk)
    obj.clear_by_lsa(request.user)
    messages.success(request, 'Asset exit cleared by LSA.')

    # --- Notifications ---
    url = request.build_absolute_uri(reverse('vehicles:asset_exit_detail', args=[obj.pk]))

    # Notify requester
    if obj.requester and obj.requester.email:
        subject = "[Assets] Your asset exit has been cleared by LSA"
        msg = (
            f"Hello {obj.requester.get_full_name() or obj.requester.username},\n\n"
            f"Your asset exit request ({obj.code}) has been cleared by LSA.\n"
            f"The assets can now be signed out at the gate.\n\n"
            f"Details: {url}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, obj.requester.email)

    # Notify guard/Control room
    guard_emails = _guard_team_emails()
    if guard_emails:
        subject = f"[Assets] Asset exit ready at gate ({obj.code})"
        msg = (
            f"Dear guards,\n\n"
            f"The following asset exit has been cleared by LSA and can be signed out at the gate:\n\n"
            f"Code: {obj.code}\n"
            f"Agency: {obj.agency_name}\n"
            f"Destination: {obj.destination}\n"
            f"Requester: {obj.requester.get_full_name() or obj.requester.username}\n\n"
            f"Details: {url}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, guard_emails)

    return redirect('vehicles:asset_exit_detail', pk=pk)


@login_required
@user_passes_test(is_lsa)
def asset_exit_lsa_reject(request, pk):
    obj = get_object_or_404(AssetExit, pk=pk)
    if obj.status != 'pending':
        messages.info(request, 'This request is not pending.')
        return redirect('vehicles:asset_exit_detail', pk=pk)
    obj.reject_by_lsa(request.user)
    messages.success(request, 'Asset exit rejected by LSA.')

    url = request.build_absolute_uri(reverse('vehicles:asset_exit_detail', args=[obj.pk]))

    # Notify requester
    if obj.requester and obj.requester.email:
        subject = "[Assets] Asset exit request rejected"
        msg = (
            f"Hello {obj.requester.get_full_name() or obj.requester.username},\n\n"
            f"Your asset exit request ({obj.code}) has been rejected by LSA.\n\n"
            f"Details: {url}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, obj.requester.email)

    # Notify agency focal(s)
    focals = _agency_focal_emails(obj.agency_name)
    if focals:
        subject = f"[Assets] Asset exit request rejected ({obj.code})"
        msg = (
            f"Dear colleague,\n\n"
            f"The asset exit request for {obj.agency_name} (code {obj.code}) has been rejected by LSA.\n\n"
            f"Details: {url}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, focals)

    return redirect('vehicles:asset_exit_detail', pk=pk)


# ---- Requester cancel ----

@login_required
def asset_exit_cancel(request, pk):
    obj = get_object_or_404(AssetExit, pk=pk)
    if obj.requester != request.user and not is_lsa(request.user):
        return HttpResponseForbidden('Not allowed')
    if obj.status in ['lsa_cleared','rejected','cancelled']:
        messages.info(request, 'This request cannot be cancelled.')
    else:
        obj.status = 'cancelled'
        obj.save(update_fields=['status'])
        messages.success(request, 'Request cancelled.')

        url = request.build_absolute_uri(reverse('vehicles:asset_exit_detail', args=[obj.pk]))

        # Notify agency focal(s)
        focals = _agency_focal_emails(obj.agency_name)
        if focals:
            subject = f"[Assets] Asset exit request cancelled ({obj.code})"
            msg = (
                f"Dear colleague,\n\n"
                f"The asset exit request for {obj.agency_name} (code {obj.code}) has been cancelled by the requester.\n\n"
                f"Details: {url}\n\n"
                f"Best regards,\nUN Security / Common Services System"
            )
            _send_notification(subject, msg, focals)

        # Notify LSA team
        lsa_emails = _emails_for_roles("lsa", "soc", include_superusers=True)
        if lsa_emails:
            subject = f"[Assets] Asset exit request cancelled ({obj.code})"
            msg = (
                f"Dear Security team,\n\n"
                f"The asset exit request {obj.code} has been cancelled.\n\n"
                f"Details: {url}\n\n"
                f"Best regards,\nUN Security / Common Services System"
            )
            _send_notification(subject, msg, lsa_emails)

    return redirect('vehicles:asset_exit_detail', pk=pk)


# ---- Guard verification (page + API) ----

@login_required
@user_passes_test(is_data_entry)
def asset_exit_verify_page(request):
    return render(request, 'vehicles/verify_asset_exit.html', {})


@login_required
def asset_exit_lookup_api(request):
    code = (request.GET.get('code') or '').strip()
    try:
        ax = AssetExit.objects.prefetch_related('items').get(code=code)
        ok = ax.status == 'lsa_cleared'
        items = [{
            'description': i.description,
            'category': i.category,
            'quantity': i.quantity,
            'serial_or_tag': i.serial_or_tag,
        } for i in ax.items.all()]
        return JsonResponse({
            'found': True,
            'ok': ok,
            'status': ax.status,
            'agency': ax.agency_name,
            'destination': ax.destination,
            'expected_date': ax.expected_date.isoformat(),
            'items': items,
            'id': ax.id,
        })
    except AssetExit.DoesNotExist:
        return JsonResponse({'found': False}, status=404)


# ---- Guard mark sign-out / sign-in (optional) ----

@login_required
@user_passes_test(is_data_entry)
def asset_exit_mark_signed_out(request, pk):
    obj = get_object_or_404(AssetExit, pk=pk)
    if obj.status != 'lsa_cleared':
        messages.warning(request, 'Cannot sign out assets that are not LSA-cleared.')
        return redirect('vehicles:asset_exit_detail', pk=pk)
    obj.mark_signed_out(request.user)
    messages.success(request, 'Assets marked as signed out.')

    # Notify requester
    if obj.requester and obj.requester.email:
        subject = "[Assets] Assets have been signed out"
        url = request.build_absolute_uri(reverse('vehicles:asset_exit_detail', args=[obj.pk]))
        msg = (
            f"Hello {obj.requester.get_full_name() or obj.requester.username},\n\n"
            f"The assets for exit request {obj.code} have been signed out at the gate.\n\n"
            f"Details: {url}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, obj.requester.email)

    return redirect('vehicles:asset_exit_detail', pk=pk)


@login_required
@user_passes_test(is_data_entry)
def asset_exit_mark_signed_in(request, pk):
    obj = get_object_or_404(AssetExit, pk=pk)
    obj.mark_signed_in(request.user)
    messages.success(request, 'Assets marked as signed in.')

    # Notify requester
    if obj.requester and obj.requester.email:
        subject = "[Assets] Assets returned to compound"
        url = request.build_absolute_uri(reverse('vehicles:asset_exit_detail', args=[obj.pk]))
        msg = (
            f"Hello {obj.requester.get_full_name() or obj.requester.username},\n\n"
            f"The assets for exit request {obj.code} have been signed back in at the gate.\n\n"
            f"Details: {url}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, obj.requester.email)

    return redirect('vehicles:asset_exit_detail', pk=pk)


def asset_exit_qr_code(request, pk):
    exit_obj = get_object_or_404(AssetExit, pk=pk)

    # Encode whatever you want the QR to represent:
    # here we embed the absolute URL to the detail page.
    target_url = request.build_absolute_uri(
        reverse('vehicles:asset_exit_detail', args=[exit_obj.pk])
    )

    try:
        import qrcode
        img = qrcode.make(target_url)
        buf = BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        ctx = {"exit": exit_obj, "qr_b64": qr_b64, "target_url": target_url}
        return render(request, "vehicles/asset_exit_qr.html", ctx)
    except Exception:
        messages.error(request, "QR code generation is not available on this server.")
        return redirect('vehicles:asset_exit_detail', pk=exit_obj.pk)


def _is_lsa(user):
    return getattr(user, "role", None) == "lsa" or user.is_superuser


@login_required
def parking_card_print(request, pk):
    card = get_object_or_404(ParkingCard, pk=pk)
    # Optional: require at least data_entry/lsa/soc
    if not (getattr(request.user, "role", None) in ("data_entry", "lsa", "soc") or request.user.is_superuser):
        messages.error(request, "You do not have permission to print parking cards.")
        return redirect("vehicles:parking_card_list")
    return render(request, "vehicles/parking_card_print.html", {"card": card})


@login_required
def parking_card_duplicate(request, pk):
    if not _is_lsa(request.user):
        messages.error(request, "Only LSA can duplicate parking cards.")
        return redirect("vehicles:parking_card_list")

    card = get_object_or_404(ParkingCard, pk=pk)

    # Generate a unique card_number suggestion based on original
    base = f"{card.card_number}-COPY" if card.card_number else "PC-COPY"
    new_number = base
    i = 2
    while ParkingCard.objects.filter(card_number=new_number).exists():
        new_number = f"{base}-{i}"
        i += 1

    dup = ParkingCard.objects.create(
        card_number=new_number,
        owner_name=card.owner_name,
        owner_id=card.owner_id,
        phone=card.phone,
        department=card.department,
        vehicle_make=card.vehicle_make,
        vehicle_model=card.vehicle_model,
        vehicle_plate=card.vehicle_plate,
        vehicle_color=card.vehicle_color,
        expiry_date=card.expiry_date,
        is_active=False,               # keep inactive until reviewed
        # created_by left null if your model has it, or set:
        # created_by=request.user,
    )
    messages.success(request, f"Duplicated card as {dup.card_number}. It is inactive until you activate it.")
    return redirect("vehicles:parking_card_detail", pk=dup.pk)


@login_required
def parking_card_delete(request, pk):
    card = get_object_or_404(ParkingCard, pk=pk)
    if not _is_lsa(request.user):
        messages.error(request, "Only LSA can delete parking cards.")
        return redirect('vehicles:parking_card_list')

    if request.method == "POST":
        number = card.card_number
        card.delete()
        messages.success(request, f"Parking card {number} deleted.")
        return redirect('vehicles:parking_card_list')

    # GET: show confirm page
    return render(request, 'vehicles/parking_card_confirm_delete.html', {'card': card})


@login_required
def pc_request_new(request):
    """Requester creates a new Parking Card request"""
    if request.method == 'POST':
        form = ParkingCardRequestForm(request.POST)
        if form.is_valid():
            req = form.save(commit=False)
            req.requested_by = request.user
            req.status = 'pending'
            req.save()
            messages.success(request, "Parking card request submitted. Awaiting LSA approval.")

            # Notify LSA
            lsa_emails = _emails_for_roles("lsa", "soc", include_superusers=True)
            if lsa_emails:
                url = request.build_absolute_uri(reverse('vehicles:pc_requests_pending'))
                subject = "[Parking] New parking card request pending"
                msg = (
                    f"Dear Security team,\n\n"
                    f"A new parking card request has been submitted.\n\n"
                    f"Requested by: {request.user.get_full_name() or request.user.username}\n"
                    f"Vehicle: {req.vehicle_plate} ({req.vehicle_make} {req.vehicle_model})\n\n"
                    f"Pending list: {url}\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, lsa_emails)

            # Notify requester
            if request.user.email:
                subject = "[Parking] Parking card request submitted"
                msg = (
                    f"Hello {request.user.get_full_name() or request.user.username},\n\n"
                    f"Your parking card request has been submitted and is pending approval.\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, request.user.email)

            return redirect('vehicles:my_pc_requests')
    else:
        # prefill with requester data
        initial = {
            'owner_name': request.user.get_full_name() or request.user.username,
            'owner_id': getattr(request.user, 'employee_id', '') or '',
            'phone': getattr(request.user, 'phone', '') or '',
            'department': '',
        }
        form = ParkingCardRequestForm(initial=initial)

    return render(request, 'vehicles/pc_request_form.html', {'form': form})


@login_required
def my_pc_requests(request):
    """Requester can see his/her own requests"""
    qs = ParkingCardRequest.objects.filter(requested_by=request.user).order_by('-requested_at')
    return render(request, 'vehicles/pc_request_list.html', {'requests': qs, 'mine': True})


@user_passes_test(is_lsa)
def pc_requests_pending(request):
    """LSA dashboard: all pending requests"""
    qs = ParkingCardRequest.objects.filter(status='pending').order_by('requested_at')
    return render(request, 'vehicles/pc_request_list.html', {'requests': qs, 'pending': True})


@user_passes_test(is_lsa)
def pc_request_approve(request, pk):
    req = get_object_or_404(ParkingCardRequest, pk=pk, status='pending')
    # Create a ParkingCard on approve
    card = ParkingCard.objects.create(
        card_number=f"PC-{req.id:05d}",
        owner_name=req.owner_name,
        owner_id=req.owner_id,
        phone=req.phone,
        department=req.department,
        vehicle_make=req.vehicle_make,
        vehicle_model=req.vehicle_model,
        vehicle_plate=req.vehicle_plate,
        vehicle_color=req.vehicle_color,
        expiry_date=req.requested_expiry,
        created_by=request.user,                         # LSA issuing the card
        issued_date=timezone.now().date(),
        is_active=True,
    )
    # mark request
    req.status = 'approved'
    req.decided_by = request.user
    req.decided_at = timezone.now()
    req.decision_notes = f"Issued card {card.card_number}"
    req.save()

    messages.success(request, f"Request approved. Card {card.card_number} issued.")

    # Notify requester
    if req.requested_by and req.requested_by.email:
        subject = "[Parking] Your parking card request has been approved"
        msg = (
            f"Hello {req.requested_by.get_full_name() or req.requested_by.username},\n\n"
            f"Your parking card request has been approved.\n\n"
            f"Card number: {card.card_number}\n"
            f"Vehicle: {card.vehicle_plate} ({card.vehicle_make} {card.vehicle_model})\n"
            f"Expiry date: {card.expiry_date}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, req.requested_by.email)

    return redirect('vehicles:pc_requests_pending')


@user_passes_test(is_lsa)
def pc_request_reject(request, pk):
    req = get_object_or_404(ParkingCardRequest, pk=pk, status='pending')
    req.status = 'rejected'
    req.decided_by = request.user
    req.decided_at = timezone.now()
    reason = request.POST.get('reason', '')[:500]
    req.decision_notes = reason
    req.save()
    messages.warning(request, "Request rejected.")

    # Notify requester
    if req.requested_by and req.requested_by.email:
        subject = "[Parking] Your parking card request was rejected"
        msg = (
            f"Hello {req.requested_by.get_full_name() or req.requested_by.username},\n\n"
            f"Your parking card request has been rejected.\n"
            f"Reason: {reason or 'Not specified'}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, req.requested_by.email)

    return redirect('vehicles:pc_requests_pending')


@login_required
def pc_request_cancel(request, pk):
    req = get_object_or_404(ParkingCardRequest, pk=pk, requested_by=request.user)
    if req.status == 'pending':
        req.status = 'cancelled'
        req.decided_by = request.user
        req.decided_at = timezone.now()
        req.decision_notes = 'Cancelled by requester'
        req.save()
        messages.info(request, "Request cancelled.")

        # Notify LSA team
        lsa_emails = _emails_for_roles("lsa", "soc", include_superusers=True)
        if lsa_emails:
            subject = "[Parking] Parking card request cancelled"
            msg = (
                f"Dear Security team,\n\n"
                f"A parking card request by {request.user.get_full_name() or request.user.username} "
                f"has been cancelled by the requester.\n\n"
                f"Best regards,\nUN Security / Common Services System"
            )
            _send_notification(subject, msg, lsa_emails)

    return redirect('vehicles:my_pc_requests')


def _gate_role(user):
    return user.is_authenticated and (getattr(user, 'role', None) in ('data_entry', 'lsa', 'soc') or user.is_superuser)


# Inventory
@method_decorator(login_required, name='dispatch')
class KeyListView(ListView):
    model = Key
    template_name = 'vehicles/keys/key_list.html'
    context_object_name = 'keys'
    paginate_by = 25

    def get_queryset(self):
        q = (self.request.GET.get('q') or '').strip()
        qs = Key.objects.all()
        if q:
            qs = qs.filter(
                Q(code__icontains=q) |
                Q(label__icontains=q) |
                Q(vehicle__plate_number__icontains=q)
            )
        key_type = self.request.GET.get('type')
        if key_type in ('office', 'vehicle'):
            qs = qs.filter(key_type=key_type)
        status = self.request.GET.get('status')
        if status == 'out':
            qs = [k for k in qs if k.is_out]
        elif status == 'in':
            qs = [k for k in qs if not k.is_out]
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Base = active keys
        base = Key.objects.filter(is_active=True)

        # Subquery: does this key have an open log?
        open_log_qs = KeyLog.objects.filter(key=OuterRef("pk"), returned_at__isnull=True)

        # Annotate once, then derive stats
        annotated = base.annotate(is_out=Exists(open_log_qs))

        total = annotated.count()
        checked_out = annotated.filter(is_out=True).count()
        available = annotated.filter(is_out=False).count()

        overdue = KeyLog.objects.filter(
            key__is_active=True,
            returned_at__isnull=True,
            due_back__lt=timezone.now(),
        ).count()

        ctx["stats"] = {
            "total": total,
            "available": available,
            "checked_out": checked_out,
            "overdue": overdue,
        }
        return ctx


@method_decorator([login_required, user_passes_test(_is_lsa)], name='dispatch')
class KeyCreateView(CreateView):
    model = Key
    form_class = KeyForm
    template_name = 'vehicles/keys/key_form.html'
    success_url = reverse_lazy('vehicles:key_list')


@method_decorator([login_required, user_passes_test(_is_lsa)], name='dispatch')
class KeyUpdateView(UpdateView):
    model = Key
    form_class = KeyForm
    template_name = 'vehicles/keys/key_form.html'
    success_url = reverse_lazy('vehicles:key_list')


@method_decorator(login_required, name='dispatch')
class KeyDetailView(DetailView):
    model = Key
    template_name = 'vehicles/keys/key_detail.html'
    context_object_name = 'key'


# Issue / Return
@login_required
@user_passes_test(_gate_role)
def key_issue(request, pk):
    key = get_object_or_404(Key, pk=pk, is_active=True)
    if key.is_out:
        messages.error(request, "This key is already issued.")
        return redirect('vehicles:key_detail', pk=key.pk)

    if request.method == 'POST':
        form = KeyIssueForm(request.POST)
        if form.is_valid():
            log = form.save(commit=False)
            log.key = key
            log.issued_by = request.user
            log.save()
            messages.success(request, f"Key {key.code} issued to {log.issued_to_name}.")
            return redirect('vehicles:key_detail', pk=key.pk)
    else:
        form = KeyIssueForm()

    return render(request, 'vehicles/keys/key_issue_form.html', {'key': key, 'form': form})


@login_required
@user_passes_test(_gate_role)
def key_return(request, pk):
    key = get_object_or_404(Key, pk=pk)
    log = key.current_log
    if not log:
        messages.error(request, "This key is not currently issued.")
        return redirect('vehicles:key_detail', pk=key.pk)

    if request.method == 'POST':
        form = KeyReturnForm(request.POST, instance=log)
        if form.is_valid():
            log = form.save(commit=False)
            log.returned_at = timezone.now()
            log.received_by = request.user
            log.save()
            messages.success(request, f"Key {key.code} returned by {log.issued_to_name}.")
            return redirect('vehicles:key_detail', pk=key.pk)
    else:
        form = KeyReturnForm()

    return render(request, 'vehicles/keys/key_return_form.html', {'key': key, 'log': log, 'form': form})


# Logs
@method_decorator(login_required, name='dispatch')
class KeyLogListView(ListView):
    model = KeyLog
    template_name = 'vehicles/keys/key_logs.html'
    context_object_name = 'logs'
    paginate_by = 50

    # --- helpers ------------------------------------------------------------
    def _apply_filters(self, qs):
        """
        Apply search + date range filters.
        Date range filters use issued_at (typical for log lists).
        """
        request = self.request
        q = (request.GET.get('q') or '').strip()
        date_from = (request.GET.get('date_from') or '').strip()
        date_to = (request.GET.get('date_to') or '').strip()

        if q:
            qs = qs.filter(
                Q(key__code__icontains=q) |
                Q(issued_to_name__icontains=q) |
                Q(issued_to_badge_id__icontains=q)
            )

        # Inclusive range by date (works with YYYY-MM-DD strings directly)
        if date_from:
            qs = qs.filter(issued_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(issued_at__date__lte=date_to)

        return qs

    # --- table queryset -----------------------------------------------------
    def get_queryset(self):
        qs = KeyLog.objects.select_related('key', 'issued_by', 'received_by').order_by('-issued_at')
        return self._apply_filters(qs)

    # --- stats + template helpers ------------------------------------------
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Base set for stats mirrors the same filters as the table
        base = self._apply_filters(KeyLog.objects.all())
        now = timezone.now()
        today = now.date()

        # Transaction stats (respect filters)
        total_logs = base.count()
        open_logs = base.filter(returned_at__isnull=True).count()
        returned_logs = base.filter(returned_at__isnull=False).count()
        overdue_logs = base.filter(returned_at__isnull=True, due_back__lt=now).count()
        issued_today = base.filter(issued_at__date=today).count()
        returned_today = base.filter(returned_at__date=today).count()

        # Distinct keys currently out within filtered set
        open_keys = base.filter(returned_at__isnull=True).values('key_id').distinct().count()

        # Global “keys out now” (not limited by filters) — optional
        open_log_qs = KeyLog.objects.filter(key=OuterRef('pk'), returned_at__isnull=True)
        keys_out_now_global = Key.objects.annotate(is_out=Exists(open_log_qs)).filter(is_out=True).count()

        ctx['stats'] = {
            'total_logs': total_logs,
            'open_logs': open_logs,
            'returned_logs': returned_logs,
            'overdue_logs': overdue_logs,
            'issued_today': issued_today,
            'returned_today': returned_today,
            'open_keys': open_keys,
            'keys_out_now_global': keys_out_now_global,
        }

        # Echo filters for your form inputs
        ctx['filters'] = {
            'q': (self.request.GET.get('q') or '').strip(),
            'date_from': (self.request.GET.get('date_from') or '').strip(),
            'date_to': (self.request.GET.get('date_to') or '').strip(),
        }

        # Preserve filters in pagination links
        get_copy = self.request.GET.copy()
        if 'page' in get_copy:
            get_copy.pop('page')
        ctx['querystring'] = f"&{get_copy.urlencode()}" if get_copy else ""

        return ctx


# Quick page (scan / type code, then issue/return)
@login_required
@user_passes_test(_gate_role)
def quick_key_page(request):
    return render(request, 'vehicles/keys/quick_key.html')


@login_required
@user_passes_test(_gate_role)
def key_lookup_api(request):
    code = (request.GET.get('code') or '').strip()
    if not code:
        return JsonResponse({'ok': False, 'error': 'missing_code'}, status=400)
    key = Key.objects.filter(code__iexact=code).first()
    if not key:
        return JsonResponse({'ok': False, 'found': False})
    current = key.current_log
    return JsonResponse({
        'ok': True,
        'found': True,
        'key': {
            'id': key.id,
            'code': key.code,
            'label': key.label,
            'key_type': key.key_type,
            'is_out': key.is_out,
        },
        'current': ({
            'issued_to_name': current.issued_to_name,
            'issued_to_badge_id': current.issued_to_badge_id,
            'issued_at': current.issued_at.isoformat(),
            'due_back': current.due_back.isoformat() if current.due_back else None,
        } if current else None)
    })


def _new_tracking_code():
    stamp = timezone.now().strftime("%Y%m%d")
    suffix = "".join(random.choices(string.digits, k=4))
    return f"PKG-{stamp}-{suffix}"


# =============================================================================
# PACKAGES – with notifications
# =============================================================================
# ── Package List ──────────────────────────────────────────────────────────────

@login_required
def package_list(request):
    qs = Package.objects.select_related(
        'flow_template', 'flow_template__agency', 'current_step', 'logged_by'
    ).order_by('-logged_at')

    q = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    template_filter = request.GET.get('template', '').strip()
    direction_filter = request.GET.get('direction', '').strip()

    if q:
        qs = qs.filter(
            db_models.Q(tracking_code__icontains=q)
            | db_models.Q(sender_name__icontains=q)
            | db_models.Q(destination_agency__icontains=q)
            | db_models.Q(for_recipient__icontains=q)
        )
    if status_filter:
        qs = qs.filter(status=status_filter)
    if template_filter:
        qs = qs.filter(flow_template_id=template_filter)
    if direction_filter:
        qs = qs.filter(direction=direction_filter)

    user_agency = _user_agency(request.user)
    all_templates = PackageFlowTemplate.objects.filter(is_active=True)
    if user_agency:
        all_templates = all_templates.filter(agency=user_agency)

    status_choices = (
        Package.objects.values_list('status', flat=True).distinct().order_by('status')
    )

    return render(request, 'vehicles/packages/package_list.html', {
        'packages': qs,
        'q': q,
        'status_filter': status_filter,
        'template_filter': template_filter,
        'direction_filter': direction_filter,
        'all_templates': all_templates,
        'status_choices': status_choices,
    })


# ── Log New Package ───────────────────────────────────────────────────────────

@login_required
def package_log_new(request):
    user_agency = _user_agency(request.user)
    flow_templates = (
        PackageFlowTemplate.objects
        .filter(is_active=True, agency=user_agency, direction='incoming')
        .prefetch_related('steps')
    ) if user_agency else PackageFlowTemplate.objects.none()

    form = PackageLogForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        package = form.save(commit=False)
        package.logged_by = request.user
        package.tracking_code = _generate_tracking_code()
        package.direction = 'incoming'

        template_id = request.POST.get('flow_template_id')
        if template_id:
            try:
                tmpl = PackageFlowTemplate.objects.get(
                    pk=template_id, is_active=True,
                    agency=user_agency, direction='incoming'
                )
                first_step = tmpl.first_step
                package.flow_template = tmpl
                if first_step:
                    package.current_step = first_step
                    package.status = first_step.status_code
            except PackageFlowTemplate.DoesNotExist:
                pass

        if not package.status:
            package.status = 'logged'

        package.save()

        if package.current_step:
            PackageStepLog.objects.create(
                package=package,
                step=package.current_step,
                step_name=package.current_step.name,
                step_order=package.current_step.order,
                performed_by=request.user,
                note=form.cleaned_data.get('notes', ''),
            )

        messages.success(request, f"Package {package.tracking_code} logged.")
        return redirect('vehicles:package_detail', pk=package.pk)

    return render(request, 'vehicles/packages/package_log_new.html', {
        'form': form,
        'flow_templates': flow_templates,
        'user_agency': user_agency,
        'direction': 'incoming',
    })


# ── Log Outgoing Package ─────────────────────────────────────────────────

@login_required
def package_log_outgoing(request):
    """Register an outgoing mail/package item."""
    user_agency = _user_agency(request.user)
    flow_templates = (
        PackageFlowTemplate.objects
        .filter(is_active=True, agency=user_agency, direction='outgoing')
        .prefetch_related('steps')
    ) if user_agency else PackageFlowTemplate.objects.none()

    # Pre-fill originator fields from the logged-in user
    initial = {
        'sender_name': request.user.get_full_name() or request.user.username,
        'sender_org': getattr(getattr(request.user, 'unit', None), 'name', ''),
        'sender_contact': getattr(request.user, 'phone', ''),
        'sender_email': request.user.email,
        'sender_type': 'individual',
    }

    form = PackageOutgoingLogForm(request.POST or None, initial=initial)

    if request.method == 'POST' and form.is_valid():
        package = form.save(commit=False)
        package.logged_by = request.user
        package.tracking_code = _generate_tracking_code()
        package.direction = 'outgoing'
        package.sender_type = 'individual'  # internal sender always individual

        # For outgoing: dest_focal_email = recipient_email (so notifications work)
        if not package.dest_focal_email:
            package.dest_focal_email = getattr(package, 'recipient_email', '')

        template_id = request.POST.get('flow_template_id')
        if template_id:
            try:
                tmpl = PackageFlowTemplate.objects.get(
                    pk=template_id, is_active=True,
                    agency=user_agency, direction='outgoing'
                )
                first_step = tmpl.first_step
                package.flow_template = tmpl
                if first_step:
                    package.current_step = first_step
                    package.status = first_step.status_code
            except PackageFlowTemplate.DoesNotExist:
                pass

        if not package.status:
            package.status = 'registered'

        package.save()

        if package.current_step:
            PackageStepLog.objects.create(
                package=package,
                step=package.current_step,
                step_name=package.current_step.name,
                step_order=package.current_step.order,
                performed_by=request.user,
                note=form.cleaned_data.get('notes', ''),
            )

        messages.success(request, f"Outgoing item {package.tracking_code} registered.")
        return redirect('vehicles:package_detail', pk=package.pk)

    return render(request, 'vehicles/packages/package_log_outgoing.html', {
        'form': form,
        'flow_templates': flow_templates,
        'user_agency': user_agency,
        'direction': 'outgoing',
    })

# ── Package Detail ────────────────────────────────────────────────────────────

@login_required
def package_detail(request, pk):
    package = get_object_or_404(
        Package.objects.select_related(
            'flow_template', 'flow_template__agency', 'current_step', 'logged_by'
        ),
        pk=pk,
    )

    step_logs = package.step_logs.select_related('step', 'performed_by').order_by('performed_at')
    all_steps = list(package.flow_template.steps_ordered) if package.flow_template else []
    completed_orders = {s.step_order for s in step_logs}

    can_advance = (
            package.current_step is not None
            and not package.is_complete
            and _user_can_perform_step(request.user, package.current_step)
    )

    # Mark in-app notifications read
    package.notifications.filter(recipient=request.user, is_read=False).update(is_read=True)

    return render(request, 'vehicles/packages/package_detail.html', {
        'package': package,
        'step_logs': step_logs,
        'all_steps': all_steps,
        'completed_orders': completed_orders,
        'can_advance': can_advance,
    })


# ── Advance Step ──────────────────────────────────────────────────────────────

@login_required
def package_advance_step(request, pk):
    package = get_object_or_404(Package, pk=pk)
    current_step = package.current_step

    if not current_step:
        messages.warning(request, "No pending step or workflow already complete.")
        return redirect('vehicles:package_detail', pk=pk)

    if not _user_can_perform_step(request.user, current_step):
        messages.error(request, f"You are not permitted to perform '{current_step.name}'.")
        return redirect('vehicles:package_detail', pk=pk)

    form = PackageStepActionForm(
        request.POST or None,
        request.FILES or None,
        step=current_step,
    )

    if request.method == 'POST' and form.is_valid():
        log = PackageStepLog.objects.create(
            package=package,
            step=current_step,
            step_name=current_step.name,
            step_order=current_step.order,
            performed_by=request.user,
            performed_at=timezone.now(),
            note=form.cleaned_data.get('note', ''),
            scan_file=form.cleaned_data.get('scan_file'),
            stamped=form.cleaned_data.get('stamped', False),
            routed_to=form.cleaned_data.get('routed_to', ''),
            recipient_name=form.cleaned_data.get('recipient_name', ''),
        )

        PackageEvent.objects.create(
            package=package,
            status=current_step.status_code[:20],
            who=request.user,
            note=(log.note or current_step.name)[:255],
        )

        next_step = current_step.next_step()
        if next_step:
            package.current_step = next_step
            package.status = next_step.status_code
        else:
            package.current_step = None
            package.is_complete = True

        package.save(update_fields=['current_step', 'status', 'is_complete', 'last_update'])
        _send_notifications(package, current_step, log, request.user)

        messages.success(request, f"✓ '{current_step.name}' completed.")
        return redirect('vehicles:package_detail', pk=pk)

    return render(request, 'vehicles/packages/package_step_action.html', {
        'package': package,
        'step': current_step,
        'form': form,
        'title': f"Perform: {current_step.name}",
        'action': current_step.name,
    })


# ── Flow Configuration ────────────────────────────────────────────────────────

@login_required
def package_flow_config(request):
    """
    ICT focal points see and manage only THEIR agency's templates.
    Superusers see all.
    """
    if not _is_ict_focal(request.user):
        messages.error(request, "Only ICT Focal Points can manage package workflows.")
        return redirect('vehicles:package_list')

    user_agency = _user_agency(request.user)
    qs = PackageFlowTemplate.objects.prefetch_related(
        'steps', 'steps__allowed_users', 'steps__notify_next_users'
    )
    if not request.user.is_superuser and user_agency:
        qs = qs.filter(agency=user_agency)

    return render(request, 'vehicles/packages/package_flow_config.html', {
        'templates': qs,
        'user_agency': user_agency,
    })


@login_required
def package_flow_template_create(request):
    if not _is_ict_focal(request.user):
        return redirect('vehicles:package_list')

    user_agency = _user_agency(request.user)
    if not user_agency and not request.user.is_superuser:
        messages.error(request, "You must belong to an agency to create a flow template.")
        return redirect('vehicles:package_flow_config')

    form = PackageFlowTemplateForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        tmpl = form.save(commit=False)
        tmpl.agency = user_agency  # always locked to requester's agency
        tmpl.created_by = request.user
        tmpl.save()
        messages.success(request, f"Flow template '{tmpl.name}' created for {tmpl.agency.code}.")
        return redirect('vehicles:package_flow_config')

    return render(request, 'vehicles/packages/package_flow_template_form.html', {
        'form': form,
        'title': 'Create Flow Template',
        'user_agency': user_agency,
    })


@login_required
def package_flow_template_edit(request, pk):
    if not _is_ict_focal(request.user):
        return redirect('vehicles:package_list')

    user_agency = _user_agency(request.user)
    qs = PackageFlowTemplate.objects.all()
    if not request.user.is_superuser:
        qs = qs.filter(agency=user_agency)  # can only edit own agency's templates
    tmpl = get_object_or_404(qs, pk=pk)

    form = PackageFlowTemplateForm(request.POST or None, instance=tmpl)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f"Template '{tmpl.name}' updated.")
        return redirect('vehicles:package_flow_config')

    return render(request, 'vehicles/packages/package_flow_template_form.html', {
        'form': form,
        'title': f"Edit Template: {tmpl.name}",
        'template': tmpl,
        'user_agency': user_agency,
    })


@login_required
def package_flow_step_create(request, template_pk):
    if not _is_ict_focal(request.user):
        return redirect('vehicles:package_list')

    user_agency = _user_agency(request.user)
    qs = PackageFlowTemplate.objects.all()
    if not request.user.is_superuser:
        qs = qs.filter(agency=user_agency)
    tmpl = get_object_or_404(qs, pk=template_pk)

    next_order = (tmpl.steps.aggregate(m=db_models.Max('order'))['m'] or 0) + 1
    form = PackageFlowStepForm(
        request.POST or None,
        initial={'order': next_order},
        agency=tmpl.agency,  # scope user pickers to this agency
    )

    if request.method == 'POST' and form.is_valid():
        step = form.save(commit=False)
        step.template = tmpl
        step.save()
        form.save_m2m()  # save M2M (allowed_users, notify_next_users)
        messages.success(request, f"Step '{step.name}' added to '{tmpl.name}'.")
        return redirect('vehicles:package_flow_config')

    return render(request, 'vehicles/packages/package_flow_step_form.html', {
        'form': form,
        'title': f"Add Step to: {tmpl.name}",
        'template': tmpl,
        'user_agency': tmpl.agency,
    })


@login_required
def package_flow_step_edit(request, pk):
    if not _is_ict_focal(request.user):
        return redirect('vehicles:package_list')

    user_agency = _user_agency(request.user)
    qs = PackageFlowStep.objects.all()
    if not request.user.is_superuser:
        qs = qs.filter(template__agency=user_agency)
    step = get_object_or_404(qs, pk=pk)

    form = PackageFlowStepForm(
        request.POST or None,
        instance=step,
        agency=step.template.agency,
    )

    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f"Step '{step.name}' updated.")
        return redirect('vehicles:package_flow_config')

    return render(request, 'vehicles/packages/package_flow_step_form.html', {
        'form': form,
        'title': f"Edit Step: {step.name}",
        'template': step.template,
        'step': step,
        'user_agency': step.template.agency,
    })


@login_required
def package_flow_step_delete(request, pk):
    if not _is_ict_focal(request.user):
        return redirect('vehicles:package_list')

    user_agency = _user_agency(request.user)
    qs = PackageFlowStep.objects.all()
    if not request.user.is_superuser:
        qs = qs.filter(template__agency=user_agency)
    step = get_object_or_404(qs, pk=pk)

    if request.method == 'POST':
        name = step.name
        step.delete()
        messages.success(request, f"Step '{name}' deleted.")
    return redirect('vehicles:package_flow_config')


# --- Asset more view options ------------------------------------

class AssetExitQueueView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    model = AssetExit
    template_name = "vehicles/asset_exit_queue.html"
    context_object_name = "exits"
    paginate_by = 25

    def test_func(self):
        return is_lsa_or_soc(self.request.user)

    def get_queryset(self):
        qs = (AssetExit.objects
              .select_related("requester")        # <-- FIXED
              .order_by("-created_at"))

        status = (self.request.GET.get("status") or "pending").strip()
        q = (self.request.GET.get("q") or "").strip()
        date_from = (self.request.GET.get("date_from") or "").strip()
        date_to = (self.request.GET.get("date_to") or "").strip()

        if status:
            qs = qs.filter(status=status)

        if q:
            qs = qs.filter(
                Q(code__icontains=q) |
                Q(agency_name__icontains=q) |
                Q(requester__username__icontains=q) |   # <-- FIXED
                Q(destination__icontains=q)
            )

        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        base = AssetExit.objects.all()
        ctx["stats"] = {
            "pending": base.filter(status="pending").count(),
            "approved": base.filter(status="approved").count(),
            "rejected": base.filter(status="rejected").count(),
            "signed_out": base.filter(status="signed_out").count(),
        }
        ctx["filters"] = {
            "status": (self.request.GET.get("status") or "pending"),
            "q": (self.request.GET.get("q") or ""),
            "date_from": (self.request.GET.get("date_from") or ""),
            "date_to": (self.request.GET.get("date_to") or ""),
        }
        return ctx


class GuardApprovedAssetExitsView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    model = AssetExit
    template_name = "vehicles/asset_exit_guard_list.html"
    context_object_name = "exits"
    paginate_by = 50

    def test_func(self):
        return _is_guard(self.request.user)

    def get_queryset(self):
        qs = (AssetExit.objects
              .filter(status="lsa_cleared")
              .select_related("requester")      # <-- FIXED
              .order_by("-created_at"))

        today = timezone.localdate()
        date_from = (self.request.GET.get("date_from") or str(today)).strip()
        date_to = (self.request.GET.get("date_to") or str(today)).strip()
        q = (self.request.GET.get("q") or "").strip()

        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        if q:
            qs = qs.filter(
                Q(code__icontains=q) |
                Q(agency_name__icontains=q) |
                Q(requester__username__icontains=q) |   # <-- FIXED
                Q(destination__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        base = AssetExit.objects.all()
        ctx["stats"] = {
            "approved_today": base.filter(status="lsa_cleared",
                                          lsa_decided_at__date=timezone.localdate()).count(),
            "pending": base.filter(status="pending").count(),
        }
        ctx["filters"] = {
            "q": (self.request.GET.get("q") or ""),
            "date_from": (self.request.GET.get("date_from") or str(timezone.localdate())),
            "date_to": (self.request.GET.get("date_to") or str(timezone.localdate())),
        }
        return ctx


# ── Signature Profile ─────────────────────────────────────────────────────────

@login_required
def signature_profile(request):
    """Let a user view, create, or update their signature profile."""
    current = UserSignature.objects.filter(user=request.user, is_active=True).first()
    all_sigs = UserSignature.objects.filter(user=request.user).order_by('-created_at')

    form = UserSignatureForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        sig = form.save(commit=False)
        sig.user = request.user
        # Auto-fill font_text from full name if blank
        if sig.sig_type == 'font' and not sig.font_text:
            sig.font_text = request.user.get_full_name() or request.user.username
        sig.save()
        messages.success(request, "Signature saved successfully.")
        return redirect('vehicles:signature_profile')

    return render(request, 'vehicles/packages/esig/signature_profile.html', {
        'form': form,
        'current': current,
        'all_sigs': all_sigs,
    })


@login_required
def signature_set_active(request, pk):
    sig = get_object_or_404(UserSignature, pk=pk, user=request.user)
    UserSignature.objects.filter(user=request.user, is_active=True).update(is_active=False)
    sig.is_active = True
    sig.save(update_fields=['is_active'])
    messages.success(request, "Signature set as active.")
    return redirect('vehicles:signature_profile')


@login_required
def signature_delete(request, pk):
    sig = get_object_or_404(UserSignature, pk=pk, user=request.user)
    if request.method == 'POST':
        sig.delete()
        messages.success(request, "Signature deleted.")
    return redirect('vehicles:signature_profile')


# ── Document Upload & Annotation ──────────────────────────────────────────────

@login_required
def document_upload(request, step_log_pk):
    """Upload a scanned document to a step log, compute its hash."""
    step_log = get_object_or_404(PackageStepLog, pk=step_log_pk)

    form = PackageDocumentUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        doc = form.save(commit=False)
        doc.step_log = step_log
        doc.uploaded_by = request.user
        doc.filename = request.FILES['file'].name
        doc.save()

        # Compute and store SHA-256
        doc.file_hash = doc.compute_hash()
        doc.save(update_fields=['file_hash'])

        messages.success(request, f"Document '{doc.filename}' uploaded.")
        return redirect('vehicles:document_annotate', pk=doc.pk)

    return render(request, 'vehicles/packages/esig/document_upload.html', {
        'form': form,
        'step_log': step_log,
        'package': step_log.package,
    })


@login_required
def document_annotate(request, pk):
    """
    Full-page annotation view.
    Renders the document (image or PDF first-page preview) with a drag-and-drop
    signature field placer. Fields are saved via AJAX.
    """
    doc = get_object_or_404(PackageDocument, pk=pk)
    package = doc.step_log.package
    agency = getattr(package.flow_template, 'agency', None) if package.flow_template else None

    existing_fields = doc.signature_fields.select_related('assigned_to', 'signature_record').all()

    form = SignatureFieldForm(agency=agency)

    if doc.status == 'uploaded':
        doc.status = 'annotation_ready'
        doc.save(update_fields=['status'])

    return render(request, 'vehicles/packages/esig/document_annotate.html', {
        'doc': doc,
        'package': package,
        'existing_fields': existing_fields,
        'form': form,
        'agency': agency,
        'is_pdf': doc.filename.lower().endswith('.pdf'),
    })


@login_required
@require_POST
def signature_field_save(request, doc_pk):
    """AJAX: save a signature field placement."""
    doc = get_object_or_404(PackageDocument, pk=doc_pk)
    agency = getattr(doc.step_log.package.flow_template, 'agency', None)
    data = json.loads(request.body)

    field = SignatureField.objects.create(
        document=doc,
        page_number=data.get('page_number', 1),
        pos_x_pct=float(data.get('pos_x_pct', 0)),
        pos_y_pct=float(data.get('pos_y_pct', 0)),
        width_pct=float(data.get('width_pct', 20)),
        height_pct=float(data.get('height_pct', 6)),
        label=data.get('label', ''),
        order=doc.signature_fields.count() + 1,
        is_required=data.get('is_required', True),
    )

    assigned_id = data.get('assigned_to')
    if assigned_id:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        try:
            field.assigned_to = User.objects.get(pk=assigned_id)
            field.save(update_fields=['assigned_to'])
        except User.DoesNotExist:
            pass

    return JsonResponse({'id': field.pk, 'order': field.order})


@login_required
@require_POST
def signature_field_delete(request, field_pk):
    """AJAX: remove a signature field."""
    field = get_object_or_404(SignatureField, pk=field_pk)
    field.delete()
    return JsonResponse({'ok': True})


@login_required
def document_send_for_signing(request, pk):
    """Mark document as sent for signing and notify assignees."""
    doc = get_object_or_404(PackageDocument, pk=pk)

    if request.method == 'POST':
        doc.status = 'pending_signature'
        doc.save(update_fields=['status'])

        # Notify each assigned user (in field order)
        notified = set()
        for field in doc.signature_fields.filter(
                assigned_to__isnull=False
        ).order_by('order'):
            u = field.assigned_to
            if u.pk not in notified:
                PackageNotification.objects.create(
                    package=doc.step_log.package,
                    recipient=u,
                    message=(
                        f"Your signature is required on document "
                        f"'{doc.filename}' for package "
                        f"{doc.step_log.package.tracking_code}."
                    ),
                )
                if u.email:
                    send_mail(
                        subject=f"[UN PASS] Signature required — {doc.step_log.package.tracking_code}",
                        message=(
                            f"Dear {u.get_full_name() or u.username},\n\n"
                            f"Your signature is required on:\n"
                            f"  Document : {doc.filename}\n"
                            f"  Package  : {doc.step_log.package.tracking_code}\n"
                            f"  Field    : {field.label or 'Signature'}\n\n"
                            f"Please log in to UN PASS to sign.\n"
                        ),
                        from_email=None,
                        recipient_list=[u.email],
                        fail_silently=True,
                    )
                notified.add(u.pk)

        messages.success(request, "Document sent for signing. Assignees have been notified.")
        return redirect('vehicles:package_detail', pk=doc.step_log.package.pk)

    return render(request, 'vehicles/packages/esig/document_send_confirm.html', {
        'doc': doc,
        'package': doc.step_log.package,
        'fields': doc.signature_fields.select_related('assigned_to').order_by('order'),
    })


# ── Signing View ──────────────────────────────────────────────────────────────

@login_required
def document_sign(request, pk):
    """
    The signing view for the assigned user.
    Shows the document with the field they need to sign highlighted,
    and lets them apply their active signature (or draw a new one inline).
    """
    doc = get_object_or_404(PackageDocument, pk=pk)

    # Find this user's pending fields
    my_fields = doc.signature_fields.filter(
        assigned_to=request.user,
    ).exclude(
        signature_record__isnull=False
    ).order_by('order')

    if not my_fields.exists():
        messages.info(request, "You have no pending signature fields on this document.")
        return redirect('vehicles:package_detail', pk=doc.step_log.package.pk)

    active_sig = UserSignature.objects.filter(user=request.user, is_active=True).first()

    if request.method == 'POST':
        field_id = request.POST.get('field_id')
        sig_data = request.POST.get('sig_data')  # base64 PNG from canvas
        field = get_object_or_404(SignatureField, pk=field_id, assigned_to=request.user)

        if not sig_data:
            messages.error(request, "No signature data received.")
            return redirect('vehicles:document_sign', pk=pk)

        now = timezone.now()
        audit_hash = SignatureRecord.compute_audit_hash(
            doc.file_hash,
            field.pk,
            request.user.pk,
            now.isoformat(),
        )

        SignatureRecord.objects.create(
            field=field,
            signed_by=request.user,
            sig_profile=active_sig,
            rendered_image=sig_data,
            signed_at=now,
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            audit_hash=audit_hash,
        )

        # Check if all required fields on this document are now signed
        unsigned_required = doc.signature_fields.filter(
            is_required=True,
        ).exclude(signature_record__isnull=False)

        if not unsigned_required.exists():
            doc.status = 'signed'
            doc.save(update_fields=['status'])
            messages.success(request, "Document fully signed.")
            # Notify the package logger
            pkg = doc.step_log.package
            if pkg.logged_by:
                PackageNotification.objects.create(
                    package=pkg,
                    recipient=pkg.logged_by,
                    message=(
                        f"Document '{doc.filename}' for package "
                        f"{pkg.tracking_code} has been fully signed."
                    ),
                )
        else:
            messages.success(request, "Your signature has been applied.")

        return redirect('vehicles:document_sign', pk=pk)

    return render(request, 'vehicles/packages/esig/document_sign.html', {
        'doc': doc,
        'my_fields': my_fields,
        'active_sig': active_sig,
        'package': doc.step_log.package,
    })


# ── Audit / Verify ────────────────────────────────────────────────────────────

@login_required
def document_audit(request, pk):
    """Show full audit trail of all signatures on a document."""
    doc = get_object_or_404(PackageDocument, pk=pk)
    records = SignatureRecord.objects.filter(
        field__document=doc
    ).select_related('field', 'signed_by', 'sig_profile').order_by('signed_at')

    verified = [(r, r.verify()) for r in records]

    return render(request, 'vehicles/packages/esig/document_audit.html', {
        'doc': doc,
        'verified': verified,
        'package': doc.step_log.package,
    })