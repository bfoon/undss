from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from django.contrib import messages
from django.utils import timezone
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.db.models import Q, Count
import csv
from .models import Vehicle, VehicleMovement, ParkingCard, AssetExit, AgencyApprover
from .forms import VehicleForm, ParkingCardForm, VehicleMovementForm, QuickVehicleCheckForm, AssetExitForm, AssetExitItemFormSet

def is_lsa(u): return u.is_authenticated and (getattr(u, 'role', '') == 'lsa' or u.is_superuser)
def is_data_entry(u): return u.is_authenticated and (getattr(u, 'role', '') == 'data_entry' or u.is_superuser)


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


# ---- Create / list / detail ----

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
            messages.success(request, 'Asset exit request submitted (awaiting LSA).')
            return redirect('vehicles:asset_exit_detail', pk=obj.pk)
    else:
        form = AssetExitForm()
        formset = AssetExitItemFormSet()
    return render(request, 'vehicles/asset_exit_form.html', {'form': form, 'formset': formset})

@login_required
def my_asset_exits(request):
    items = AssetExit.objects.filter(requester=request.user).order_by('-created_at')
    return render(request, 'vehicles/asset_exit_list.html', {'items': items})

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
    return redirect('vehicles:asset_exit_detail', pk=pk)

@login_required
@user_passes_test(is_data_entry)
def asset_exit_mark_signed_in(request, pk):
    obj = get_object_or_404(AssetExit, pk=pk)
    obj.mark_signed_in(request.user)
    messages.success(request, 'Assets marked as signed in.')
    return redirect('vehicles:asset_exit_detail', pk=pk)

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