from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import ListView, CreateView, UpdateView, DetailView
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.utils import timezone
from django.http import JsonResponse, HttpResponse
from django.db.models import Q, Count
from django.core.paginator import Paginator
from django.utils.decorators import method_decorator
from datetime import timedelta
import csv

from .models import Visitor, VisitorLog
from .forms import VisitorForm, VisitorApprovalForm, QuickVisitorCheckForm


# Helper functions
def is_lsa(user):
    return user.is_authenticated and user.role == 'lsa'


def is_lsa_or_soc(user):
    return user.is_authenticated and user.role in ['lsa', 'soc']


class VisitorListView(LoginRequiredMixin, ListView):
    model = Visitor
    template_name = 'visitors/visitor_list.html'
    context_object_name = 'visitors'
    paginate_by = 20

    def get_queryset(self):
        queryset = Visitor.objects.all()

        # Handle filter_status from URL kwargs (for filtered views)
        filter_status = self.kwargs.get('filter_status')
        if filter_status:
            queryset = queryset.filter(status=filter_status)

        # Handle search and filters from GET parameters
        status = self.request.GET.get('status')
        search = self.request.GET.get('search')

        if status and not filter_status:  # Don't override URL-based filter
            queryset = queryset.filter(status=status)

        if search:
            queryset = queryset.filter(
                Q(full_name__icontains=search) |
                Q(organization__icontains=search) |
                Q(id_number__icontains=search) |
                Q(phone__icontains=search)
            )

        return queryset.order_by('-registered_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'status_filter': self.request.GET.get('status', ''),
            'search_query': self.request.GET.get('search', ''),
            'filter_status': self.kwargs.get('filter_status', ''),
            'status_choices': Visitor.APPROVAL_STATUS,
        })
        return context


class VisitorDetailView(LoginRequiredMixin, DetailView):
    model = Visitor
    template_name = 'visitors/visitor_detail.html'
    context_object_name = 'visitor'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['logs'] = VisitorLog.objects.filter(
            visitor=self.object
        ).select_related('performed_by').order_by('-timestamp')
        return context


class VisitorCreateView(LoginRequiredMixin, CreateView):
    model = Visitor
    form_class = VisitorForm
    template_name = 'visitors/visitor_form.html'
    success_url = reverse_lazy('visitors:visitor_list')

    def form_valid(self, form):
        form.instance.registered_by = self.request.user
        response = super().form_valid(form)

        # Auto-approve if user is LSA
        if self.request.user.role == 'lsa':
            visitor = form.instance
            visitor.status = 'approved'
            visitor.approved_by = self.request.user
            visitor.approval_date = timezone.now()
            visitor.save()

            VisitorLog.objects.create(
                visitor=visitor,
                action='approval',
                performed_by=self.request.user,
                notes='Auto-approved by LSA'
            )
            messages.success(self.request, 'Visitor registered and approved successfully.')
        else:
            messages.success(self.request, 'Visitor registered successfully. Awaiting LSA approval.')

        return response


class VisitorUpdateView(LoginRequiredMixin, UpdateView):
    model = Visitor
    form_class = VisitorForm
    template_name = 'visitors/visitor_form.html'

    def get_success_url(self):
        return reverse('visitors:visitor_detail', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f'Visitor {form.instance.full_name} updated successfully.')
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context


@login_required
@user_passes_test(is_lsa)
def approve_visitor(request, visitor_id):
    visitor = get_object_or_404(Visitor, id=visitor_id)

    if request.method == 'POST':
        form = VisitorApprovalForm(request.POST)
        if form.is_valid():
            action = form.cleaned_data['action']
            notes = form.cleaned_data['notes']

            if action == 'approve':
                visitor.status = 'approved'
                visitor.approved_by = request.user
                visitor.approval_date = timezone.now()
                visitor.save()

                VisitorLog.objects.create(
                    visitor=visitor,
                    action='approval',
                    performed_by=request.user,
                    notes=notes
                )
                messages.success(request, f'Visitor {visitor.full_name} approved.')

            elif action == 'reject':
                visitor.status = 'rejected'
                visitor.rejection_reason = form.cleaned_data['rejection_reason']
                visitor.save()

                VisitorLog.objects.create(
                    visitor=visitor,
                    action='rejection',
                    performed_by=request.user,
                    notes=visitor.rejection_reason
                )
                messages.success(request, f'Visitor {visitor.full_name} rejected.')

            return redirect('visitors:visitor_list')
    else:
        form = VisitorApprovalForm()

    return render(request, 'visitors/approve_visitor.html', {
        'visitor': visitor,
        'form': form
    })


@login_required
def check_in_visitor(request, visitor_id):
    visitor = get_object_or_404(Visitor, id=visitor_id)

    if visitor.status != 'approved':
        return JsonResponse({'error': 'Visitor not approved'}, status=400)

    if visitor.checked_in:
        return JsonResponse({'error': 'Visitor already checked in'}, status=400)

    visitor.checked_in = True
    visitor.check_in_time = timezone.now()
    visitor.save()

    VisitorLog.objects.create(
        visitor=visitor,
        action='check_in',
        performed_by=request.user,
        gate=request.POST.get('gate', 'front'),
        notes=f'Checked in at {visitor.check_in_time.strftime("%H:%M")}'
    )

    return JsonResponse({
        'success': True,
        'message': f'Visitor {visitor.full_name} checked in successfully',
        'check_in_time': visitor.check_in_time.isoformat()
    })


@login_required
def check_out_visitor(request, visitor_id):
    visitor = get_object_or_404(Visitor, id=visitor_id)

    if not visitor.checked_in:
        return JsonResponse({'error': 'Visitor not checked in'}, status=400)

    if visitor.checked_out:
        return JsonResponse({'error': 'Visitor already checked out'}, status=400)

    visitor.checked_out = True
    visitor.check_out_time = timezone.now()
    visitor.save()

    # Calculate visit duration
    duration = visitor.check_out_time - visitor.check_in_time
    duration_str = str(duration).split('.')[0]  # Remove microseconds

    VisitorLog.objects.create(
        visitor=visitor,
        action='check_out',
        performed_by=request.user,
        gate=request.POST.get('gate', 'front'),
        notes=f'Checked out at {visitor.check_out_time.strftime("%H:%M")} (Duration: {duration_str})'
    )

    return JsonResponse({
        'success': True,
        'message': f'Visitor {visitor.full_name} checked out successfully',
        'check_out_time': visitor.check_out_time.isoformat(),
        'duration': duration_str
    })


@login_required
def quick_check_page(request):
    form = QuickVisitorCheckForm()
    return render(request, 'visitors/quick_check.html', {'form': form})


@login_required
def active_visitors_view(request):
    active_visitors = Visitor.objects.filter(
        checked_in=True,
        checked_out=False
    ).select_related('registered_by')

    return render(request, 'visitors/active_visitors.html', {
        'visitors': active_visitors,
        'total_active': active_visitors.count()
    })


class VisitorLogListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    model = VisitorLog
    template_name = 'visitors/visitor_logs.html'
    context_object_name = 'logs'
    paginate_by = 50

    def test_func(self):
        return self.request.user.role in ['lsa', 'soc']

    def get_queryset(self):
        return VisitorLog.objects.select_related(
            'visitor', 'performed_by'
        ).order_by('-timestamp')


@login_required
def visitor_logs_detail(request, visitor_id):
    visitor = get_object_or_404(Visitor, id=visitor_id)
    logs = VisitorLog.objects.filter(
        visitor=visitor
    ).select_related('performed_by').order_by('-timestamp')

    return render(request, 'visitors/visitor_logs_detail.html', {
        'visitor': visitor,
        'logs': logs
    })


# API Views
@login_required
def quick_visitor_check(request):
    """API endpoint for quick visitor check-in/out"""
    if request.method == 'POST':
        visitor_id = request.POST.get('visitor_id')
        action = request.POST.get('action')
        gate = request.POST.get('gate', 'front')

        try:
            # Try to find visitor by ID number, name, or database ID
            visitor = None
            if visitor_id.isdigit():
                # Try database ID first
                try:
                    visitor = Visitor.objects.get(id=visitor_id)
                except Visitor.DoesNotExist:
                    pass

            if not visitor:
                # Search by ID number or name
                visitors = Visitor.objects.filter(
                    Q(id_number=visitor_id) |
                    Q(full_name__icontains=visitor_id)
                )
                if visitors.count() == 1:
                    visitor = visitors.first()
                elif visitors.count() > 1:
                    return JsonResponse({
                        'error': 'Multiple visitors found. Please be more specific.',
                        'suggestions': [{
                            'id': v.id,
                            'name': v.full_name,
                            'org': v.organization,
                            'id_number': v.id_number
                        } for v in visitors[:5]]
                    })
                else:
                    return JsonResponse({'error': 'Visitor not found'})

            if action == 'check_in':
                return check_in_visitor(request, visitor.id)
            elif action == 'check_out':
                return check_out_visitor(request, visitor.id)
            else:
                return JsonResponse({'error': 'Invalid action'})

        except Exception as e:
            return JsonResponse({'error': str(e)})

    return JsonResponse({'error': 'Invalid request method'})


@login_required
def visitor_search_api(request):
    query = request.GET.get('q', '').strip()

    if len(query) < 2:
        return JsonResponse({'visitors': []})

    visitors = Visitor.objects.filter(
        Q(full_name__icontains=query) |
        Q(id_number__icontains=query) |
        Q(organization__icontains=query) |
        Q(phone__icontains=query)
    )[:10]

    return JsonResponse({
        'visitors': [{
            'id': visitor.id,
            'full_name': visitor.full_name,
            'organization': visitor.organization,
            'id_number': visitor.id_number,
            'status': visitor.get_status_display(),
            'checked_in': visitor.checked_in,
            'checked_out': visitor.checked_out
        } for visitor in visitors]
    })


@login_required
def visitor_stats_api(request):
    today = timezone.now().date()

    stats = {
        'total_today': Visitor.objects.filter(registered_at__date=today).count(),
        'pending': Visitor.objects.filter(status='pending').count(),
        'approved': Visitor.objects.filter(status='approved').count(),
        'rejected': Visitor.objects.filter(status='rejected').count(),
        'active': Visitor.objects.filter(checked_in=True, checked_out=False).count(),
        'completed_today': Visitor.objects.filter(
            check_out_time__date=today
        ).count(),
        'by_type': {
            vtype[0]: Visitor.objects.filter(visitor_type=vtype[0]).count()
            for vtype in Visitor.VISITOR_TYPES
        }
    }

    return JsonResponse(stats)


@login_required
def visitor_status_api(request, visitor_id):
    visitor = get_object_or_404(Visitor, id=visitor_id)
    return JsonResponse({
        'id': visitor.id,
        'full_name': visitor.full_name,
        'status': visitor.status,
        'checked_in': visitor.checked_in,
        'checked_out': visitor.checked_out,
        'check_in_time': visitor.check_in_time.isoformat() if visitor.check_in_time else None,
        'check_out_time': visitor.check_out_time.isoformat() if visitor.check_out_time else None,
        'approved_by': visitor.approved_by.username if visitor.approved_by else None
    })


# Bulk Operations
@login_required
@user_passes_test(is_lsa)
def bulk_approve_visitors(request):
    if request.method == 'POST':
        visitor_ids = request.POST.getlist('visitor_ids')
        visitors = Visitor.objects.filter(id__in=visitor_ids, status='pending')

        count = 0
        for visitor in visitors:
            visitor.status = 'approved'
            visitor.approved_by = request.user
            visitor.approval_date = timezone.now()
            visitor.save()

            VisitorLog.objects.create(
                visitor=visitor,
                action='approval',
                performed_by=request.user,
                notes='Bulk approval'
            )
            count += 1

        messages.success(request, f'{count} visitors approved successfully.')

    return redirect('visitors:pending_approvals')


@login_required
def export_visitors(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="visitors_export.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'Name', 'ID Number', 'Organization', 'Phone', 'Email', 'Status',
        'Visitor Type', 'Purpose', 'Person to Visit', 'Department',
        'Expected Date', 'Expected Time', 'Has Vehicle', 'Vehicle Plate',
        'Registered Date', 'Registered By', 'Approved By', 'Approval Date',
        'Checked In', 'Check In Time', 'Checked Out', 'Check Out Time'
    ])

    # Filter by query parameters
    queryset = Visitor.objects.all().select_related('registered_by', 'approved_by')

    # Apply filters
    status = request.GET.get('status')
    if status:
        queryset = queryset.filter(status=status)

    date_from = request.GET.get('date_from')
    if date_from:
        queryset = queryset.filter(registered_at__date__gte=date_from)

    date_to = request.GET.get('date_to')
    if date_to:
        queryset = queryset.filter(registered_at__date__lte=date_to)

    for visitor in queryset.order_by('-registered_at'):
        writer.writerow([
            visitor.full_name,
            visitor.id_number,
            visitor.organization,
            visitor.phone,
            visitor.email,
            visitor.get_status_display(),
            visitor.get_visitor_type_display(),
            visitor.purpose_of_visit,
            visitor.person_to_visit,
            visitor.department_to_visit,
            visitor.expected_date,
            visitor.expected_time,
            'Yes' if visitor.has_vehicle else 'No',
            visitor.vehicle_plate or '',
            visitor.registered_at.strftime('%Y-%m-%d %H:%M'),
            visitor.registered_by.username,
            visitor.approved_by.username if visitor.approved_by else '',
            visitor.approval_date.strftime('%Y-%m-%d %H:%M') if visitor.approval_date else '',
            'Yes' if visitor.checked_in else 'No',
            visitor.check_in_time.strftime('%Y-%m-%d %H:%M') if visitor.check_in_time else '',
            'Yes' if visitor.checked_out else 'No',
            visitor.check_out_time.strftime('%Y-%m-%d %H:%M') if visitor.check_out_time else ''
        ])

    return response