from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from django.contrib import messages
from django.utils import timezone
from django.http import JsonResponse, HttpResponse
from django.db.models import Q, Count
import csv

from .models import Vehicle, VehicleMovement, ParkingCard
from .forms import VehicleForm, ParkingCardForm, VehicleMovementForm, QuickVehicleCheckForm


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
