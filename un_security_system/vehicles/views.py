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
from io import BytesIO
import random, string
import threading
import logging

from .models import (
    Vehicle, VehicleMovement,
    ParkingCard, AssetExit,
    AgencyApprover, ParkingCardRequest, Key, KeyLog,
    Package, PackageEvent
)
from .forms import (
    VehicleForm, ParkingCardForm,
    VehicleMovementForm, QuickVehicleCheckForm,
    AssetExitForm, AssetExitItemFormSet,
    ParkingCardRequestForm, KeyForm, KeyIssueForm, KeyReturnForm,
    PackageLogForm, PackageReceptionForm, PackageAgencyReceiveForm, PackageDeliverForm
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

@login_required
@user_passes_test(_is_guard)
def package_log_new(request):
    if request.method == "POST":
        form = PackageLogForm(request.POST)
        if form.is_valid():
            pkg = form.save(commit=False)
            pkg.tracking_code = _new_tracking_code()
            pkg.logged_by = request.user
            pkg.status = "to_reception"  # guard logged and forwards to reception
            pkg.save()
            PackageEvent.objects.create(package=pkg, status="logged", who=request.user, note="Logged at gate")
            PackageEvent.objects.create(package=pkg, status="to_reception", who=request.user, note="Forwarded to reception")
            messages.success(request, f"Package logged. Tracking: {pkg.tracking_code}")

            # Notify reception & LSA/SOC
            rec_emails = _emails_for_roles("reception", "lsa", "soc", include_superusers=True)
            if rec_emails:
                detail_url = request.build_absolute_uri(reverse("vehicles:package_detail", args=[pkg.pk]))
                subject = f"[Packages] New package logged at gate ({pkg.tracking_code})"
                msg = (
                    f"Dear colleagues,\n\n"
                    f"A new package has been logged at the gate and forwarded to Reception.\n\n"
                    f"Tracking: {pkg.tracking_code}\n"
                    f"Sender: {pkg.sender_name or pkg.sender_org or 'N/A'}\n"
                    f"Destination agency: {pkg.destination_agency or 'N/A'}\n"
                    f"For: {pkg.for_recipient or 'N/A'}\n\n"
                    f"Details: {detail_url}\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, rec_emails)

            return redirect("vehicles:package_detail", pk=pkg.pk)
    else:
        form = PackageLogForm()
    return render(request, "vehicles/packages/package_form.html", {"form": form, "is_guard": True})



@login_required
def package_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    user = request.user
    role = getattr(user, "role", "") or ""

    # Base queryset
    qs = Package.objects.all()

    # --- Visibility rules ---
    if can_view_all_packages(user):
        # Your previous inbox-style scoping for special roles
        if role == "reception":
            qs = qs.filter(Q(status__in=["to_reception", "at_reception", "to_agency"]))
        elif role in ("registry", "agency_fp"):
            qs = qs.filter(Q(status__in=["to_agency", "with_agency", "delivered"]))
        # LSA/SOC/superuser: see all, no extra filter
    else:
        # Normal users: only packages "they own"
        # Here we assume `for_recipient` holds the staff name or similar.
        full_name = (user.get_full_name() or "").strip()
        username = user.username

        own_filter = Q(for_recipient__icontains=username)
        if full_name:
            own_filter = own_filter | Q(for_recipient__icontains=full_name)

        qs = qs.filter(own_filter)

    # --- Search filter ---
    if q:
        qs = qs.filter(
            Q(tracking_code__icontains=q) |
            Q(sender_name__icontains=q) |
            Q(sender_org__icontains=q) |
            Q(destination_agency__icontains=q) |
            Q(for_recipient__icontains=q)
        )

    # --- Status filter ---
    if status:
        qs = qs.filter(status=status)

    qs = qs.select_related(
        "logged_by",
        "reception_received_by",
        "agency_received_by",
        "delivered_by",
    )

    return render(
        request,
        "vehicles/packages/package_list.html",
        {
            "packages": qs[:200],
            "q": q,
            "status_filter": status,
            "can_view_all": can_view_all_packages(user),
        },
    )

@login_required
def package_detail(request, pk):
    pkg = get_object_or_404(Package, pk=pk)
    return render(request, "vehicles/packages/package_detail.html", {"package": pkg})


@login_required
@user_passes_test(_is_reception)
def package_mark_reception_received(request, pk):
    pkg = get_object_or_404(Package, pk=pk)
    if request.method == "POST":
        form = PackageReceptionForm(request.POST)
        if form.is_valid():
            pkg.status = "at_reception"
            pkg.reception_received_at = timezone.now()
            pkg.reception_received_by = request.user
            pkg.save()
            PackageEvent.objects.create(package=pkg, status="at_reception", who=request.user, note=form.cleaned_data.get("note",""))
            messages.success(request, "Package marked received at Reception.")

            # Notify agency focal / registry
            agency_emails = _agency_focal_emails(pkg.destination_agency)
            registry_emails = _emails_for_roles("registry", "agency_fp")
            recipients = list(set(agency_emails + registry_emails))
            if recipients:
                detail_url = request.build_absolute_uri(reverse("vehicles:package_detail", args=[pkg.pk]))
                subject = f"[Packages] Package arrived at Reception ({pkg.tracking_code})"
                msg = (
                    f"Dear colleagues,\n\n"
                    f"A package for your agency has arrived at Reception.\n\n"
                    f"Tracking: {pkg.tracking_code}\n"
                    f"Destination agency: {pkg.destination_agency or 'N/A'}\n"
                    f"For: {pkg.for_recipient or 'N/A'}\n\n"
                    f"Details: {detail_url}\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, recipients)

            # Optionally notify package owner if we can detect an email
            owner_emails = _package_owner_emails(pkg)
            if owner_emails:
                subject = "[Packages] Your package has reached Reception"
                msg = (
                    f"Hello,\n\n"
                    f"A package addressed to you has arrived at UN House Reception.\n\n"
                    f"Tracking: {pkg.tracking_code}\n"
                    f"Agency: {pkg.destination_agency or 'N/A'}\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, owner_emails)

            return redirect("vehicles:package_detail", pk=pkg.pk)
    else:
        form = PackageReceptionForm()
    return render(request, "vehicles/packages/package_action_form.html", {
        "package": pkg, "form": form, "title": "Receive at Reception", "action": "Receive"
    })


@login_required
@user_passes_test(_is_reception)
def package_send_to_agency(request, pk):
    pkg = get_object_or_404(Package, pk=pk)
    # immediate transition (simple POST button)
    pkg.status = "to_agency"
    pkg.save()
    PackageEvent.objects.create(package=pkg, status="to_agency", who=request.user, note="Sent to Agency/Registry")
    messages.info(request, "Package sent to Agency/Registry.")

    # notify agency focal / registry & owner
    agency_emails = _agency_focal_emails(pkg.destination_agency)
    registry_emails = _emails_for_roles("registry", "agency_fp")
    recipients = list(set(agency_emails + registry_emails))
    if recipients:
        detail_url = request.build_absolute_uri(reverse("vehicles:package_detail", args=[pkg.pk]))
        subject = f"[Packages] Package sent to your agency ({pkg.tracking_code})"
        msg = (
            f"Dear colleagues,\n\n"
            f"A package has been sent from Reception to your agency/registry.\n\n"
            f"Tracking: {pkg.tracking_code}\n"
            f"For: {pkg.for_recipient or 'N/A'}\n\n"
            f"Details: {detail_url}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, recipients)

    owner_emails = _package_owner_emails(pkg)
    if owner_emails:
        subject = "[Packages] Your package is on its way to your agency"
        msg = (
            f"Hello,\n\n"
            f"Your package is on its way from Reception to your agency/registry.\n\n"
            f"Tracking: {pkg.tracking_code}\n"
            f"Agency: {pkg.destination_agency or 'N/A'}\n\n"
            f"Best regards,\nUN Security / Common Services System"
        )
        _send_notification(subject, msg, owner_emails)

    return redirect("vehicles:package_detail", pk=pkg.pk)


@login_required
@user_passes_test(_is_agency_or_registry)
def package_mark_agency_received(request, pk):
    pkg = get_object_or_404(Package, pk=pk)
    if request.method == "POST":
        form = PackageAgencyReceiveForm(request.POST)
        if form.is_valid():
            pkg.status = "with_agency"
            pkg.agency_received_at = timezone.now()
            pkg.agency_received_by = request.user
            pkg.save()
            PackageEvent.objects.create(package=pkg, status="with_agency", who=request.user, note=form.cleaned_data.get("note",""))
            messages.success(request, "Package marked received by Agency/Registry.")

            # Notify package owner
            owner_emails = _package_owner_emails(pkg)
            if owner_emails:
                subject = "[Packages] Your package has reached your agency"
                msg = (
                    f"Hello,\n\n"
                    f"Your package has been received by your agency/registry.\n\n"
                    f"Tracking: {pkg.tracking_code}\n"
                    f"Agency: {pkg.destination_agency or 'N/A'}\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, owner_emails)

            return redirect("vehicles:package_detail", pk=pkg.pk)
    else:
        form = PackageAgencyReceiveForm()
    return render(request, "vehicles/packages/package_action_form.html", {
        "package": pkg, "form": form, "title": "Receive by Agency/Registry", "action": "Receive"
    })


@login_required
@user_passes_test(_is_agency_or_registry)
def package_mark_delivered(request, pk):
    pkg = get_object_or_404(Package, pk=pk)
    if request.method == "POST":
        form = PackageDeliverForm(request.POST)
        if form.is_valid():
            pkg.status = "delivered"
            pkg.delivered_at = timezone.now()
            pkg.delivered_to = form.cleaned_data["delivered_to"]
            pkg.delivered_by = request.user
            pkg.save()
            PackageEvent.objects.create(package=pkg, status="delivered", who=request.user, note=form.cleaned_data.get("note",""))
            messages.success(request, "Package marked delivered.")

            # Notify package owner
            owner_emails = _package_owner_emails(pkg)
            if owner_emails:
                subject = "[Packages] Your package has been delivered"
                msg = (
                    f"Hello,\n\n"
                    f"Your package has been delivered.\n\n"
                    f"Tracking: {pkg.tracking_code}\n"
                    f"Delivered to: {pkg.delivered_to}\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, owner_emails)

            # Notify reception that package is fully delivered (closing loop)
            rec_emails = _emails_for_roles("reception", "lsa", "soc", include_superusers=True)
            if rec_emails:
                subject = f"[Packages] Package delivered ({pkg.tracking_code})"
                msg = (
                    f"Dear colleagues,\n\n"
                    f"The package with tracking {pkg.tracking_code} has been delivered to the final recipient.\n\n"
                    f"Best regards,\nUN Security / Common Services System"
                )
                _send_notification(subject, msg, rec_emails)

            return redirect("vehicles:package_detail", pk=pkg.pk)
    else:
        form = PackageDeliverForm()
    return render(request, "vehicles/packages/package_action_form.html", {
        "package": pkg, "form": form, "title": "Deliver Package", "action": "Deliver"
    })


# Public/simple tracking (optional)
@login_required
def package_track_api(request):
    code = (request.GET.get("code") or "").strip()
    pkg = Package.objects.filter(tracking_code__iexact=code).first()
    if not pkg:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    return JsonResponse({
        "ok": True,
        "tracking_code": pkg.tracking_code,
        "status": pkg.status,
        "last_update": pkg.last_update.isoformat(),
        "destination_agency": pkg.destination_agency,
        "events": [
            {"at": e.at.isoformat(), "status": e.status, "note": e.note or "", "who": (e.who.username if e.who else None)}
            for e in pkg.events.all().order_by("-at")[:20]
        ],
    })


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
