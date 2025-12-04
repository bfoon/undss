from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import ListView, CreateView, UpdateView, DetailView
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.utils import timezone
from django.http import JsonResponse, HttpResponse
from django.db.models import Q, Count
from django.db import transaction, IntegrityError
from django.views.decorators.http import require_http_methods
from django.core.paginator import Paginator
from django.utils.decorators import method_decorator
from datetime import timedelta
import csv
import threading
import logging
from django.views.generic import TemplateView



from django.conf import settings
from django.core.mail import send_mail
from django.contrib.auth import get_user_model

from .models import Visitor, VisitorLog, VisitorCard
from .forms import VisitorForm, VisitorApprovalForm, QuickVisitorCheckForm, GateCheckForm

logger = logging.getLogger(__name__)

User = get_user_model()

# -------------------------------------------------------------------
# Helper functions
# -------------------------------------------------------------------

def is_lsa(user):
    return user.is_authenticated and user.role == 'lsa'


def is_lsa_or_soc(user):
    return user.is_authenticated and user.role in ['lsa', 'soc']


def _gate_role(user):
    # Guards (data_entry), LSA, SOC, and superusers can verify at the gate
    return user.is_authenticated and (
        getattr(user, 'role', None) in ('data_entry', 'lsa', 'soc') or user.is_superuser
    )


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

def _notify_lsa_soc_new_request(visitor, request):
    """
    Notify all LSAs and SOC in the same agency (if available) when a new request is created.
    """
    # Prefer same-agency LSA+SOC, fallback to all if no agency
    base_qs = User.objects.filter(role__in=['lsa', 'soc'], is_active=True)
    if visitor.registered_by and getattr(visitor.registered_by, "agency_id", None):
        base_qs = base_qs.filter(agency_id=visitor.registered_by.agency_id)

    recipients = list(base_qs.values_list('email', flat=True))

    if not recipients:
        return

    try:
        detail_url = request.build_absolute_uri(
            reverse('visitors:visitor_detail', kwargs={'pk': visitor.pk})
        )
    except Exception:
        detail_url = ""

    subject = f"[Visitor] New request: {visitor.full_name}"
    message = (
        f"Dear LSA/SOC,\n\n"
        f"A new visitor request has been created and requires your review.\n\n"
        f"Visitor: {visitor.full_name}\n"
        f"Organization: {visitor.organization or 'N/A'}\n"
        f"Purpose: {visitor.purpose_of_visit or 'N/A'}\n"
        f"Expected date/time: {visitor.expected_date} {visitor.expected_time}\n"
        f"Person/Dept to visit: {visitor.person_to_visit or 'N/A'}"
        f" / {visitor.department_to_visit or 'N/A'}\n"
        f"Requested by: {visitor.registered_by.get_full_name() or visitor.registered_by.username}\n\n"
        f"You can review this request here:\n{detail_url}\n\n"
        f"Best regards,\nUN Security / Common Services System"
    )

    _send_notification(subject, message, recipients)


def _notify_requester_status_change(visitor, status_label: str, extra_notes: str = ""):
    """
    Notify the requester (registered_by) when status changes: approved, rejected, cancelled.
    """
    requester = getattr(visitor, "registered_by", None)
    if not requester or not requester.email:
        return

    subject = f"[Visitor] Request {status_label}: {visitor.full_name}"
    message = (
        f"Hello {requester.get_full_name() or requester.username},\n\n"
        f"Your visitor request for {visitor.full_name} has been {status_label}.\n\n"
        f"Visitor: {visitor.full_name}\n"
        f"Organization: {visitor.organization or 'N/A'}\n"
        f"Purpose: {visitor.purpose_of_visit or 'N/A'}\n"
        f"Expected date/time: {visitor.expected_date} {visitor.expected_time}\n\n"
    )
    if extra_notes:
        message += f"Notes: {extra_notes}\n\n"

    message += "Best regards,\nUN Security / Common Services System"

    _send_notification(subject, message, requester.email)


def _notify_requester_check_in(visitor, gate=None):
    """
    Notify requester when the visitor checks in.
    """
    requester = getattr(visitor, "registered_by", None)
    if not requester or not requester.email:
        return

    subject = f"[Visitor] {visitor.full_name} has arrived"
    message = (
        f"Hello {requester.get_full_name() or requester.username},\n\n"
        f"Your visitor {visitor.full_name} has CHECKED IN at the compound.\n\n"
        f"Check-in time: {visitor.check_in_time.strftime('%Y-%m-%d %H:%M') if visitor.check_in_time else 'N/A'}\n"
    )
    if gate:
        message += f"Gate: {gate}\n"

    message += "\nBest regards,\nUN Security / Common Services System"
    _send_notification(subject, message, requester.email)


def _notify_requester_check_out(visitor, gate=None, duration_str=None):
    """
    Notify requester when the visitor checks out / leaves.
    """
    requester = getattr(visitor, "registered_by", None)
    if not requester or not requester.email:
        return

    subject = f"[Visitor] {visitor.full_name} has left the compound"
    message = (
        f"Hello {requester.get_full_name() or requester.username},\n\n"
        f"Your visitor {visitor.full_name} has CHECKED OUT from the compound.\n\n"
        f"Check-out time: {visitor.check_out_time.strftime('%Y-%m-%d %H:%M') if visitor.check_out_time else 'N/A'}\n"
    )
    if duration_str:
        message += f"Duration of visit: {duration_str}\n"
    if gate:
        message += f"Gate: {gate}\n"

    message += "\nBest regards,\nUN Security / Common Services System"
    _send_notification(subject, message, requester.email)


# -------------------------------------------------------------------
# Views
# -------------------------------------------------------------------

class VisitorListView(LoginRequiredMixin, ListView):
    model = Visitor
    template_name = 'visitors/visitor_list.html'
    context_object_name = 'visitors'
    paginate_by = 20

    def get_queryset(self):
        user = self.request.user
        qs = Visitor.objects.all()

        # Who can see everything
        privileged_roles = {'lsa', 'soc', 'data_entry'}
        is_privileged = user.is_superuser or getattr(user, 'role', None) in privileged_roles

        # Non-privileged users only see what they registered
        if not is_privileged:
            qs = qs.filter(registered_by=user)

        # --- Filters from URL kwarg (legacy) ---
        filter_status = self.kwargs.get('filter_status')

        # --- Filters from querystring ---
        # Template uses "status_filter" and "date_range"
        status_filter = self.request.GET.get('status_filter') or ''
        search = (self.request.GET.get('search') or '').strip()
        date_range = self.request.GET.get('date_range') or ''

        # Valid status values (from your model choices)
        valid_statuses = {choice[0] for choice in getattr(Visitor, 'APPROVAL_STATUS', [])}

        # 1) URL path status has highest priority if valid
        if filter_status and (not status_filter) and filter_status in valid_statuses:
            qs = qs.filter(status=filter_status)
        # 2) Otherwise use querystring status_filter
        elif status_filter and status_filter in valid_statuses:
            qs = qs.filter(status=status_filter)

        # --- Date range filter ---
        if date_range:
            today = timezone.now().date()
            if date_range == 'today':
                qs = qs.filter(registered_at__date=today)
            elif date_range == 'week':
                start = today - timezone.timedelta(days=7)
                qs = qs.filter(
                    registered_at__date__gte=start,
                    registered_at__date__lte=today,
                )
            elif date_range == 'month':
                start = today.replace(day=1)
                qs = qs.filter(
                    registered_at__date__gte=start,
                    registered_at__date__lte=today,
                )

        # --- Search filter ---
        if search:
            qs = qs.filter(
                Q(full_name__icontains=search) |
                Q(organization__icontains=search) |
                Q(id_number__icontains=search) |
                Q(phone__icontains=search)
            )

        # Order newest first
        return qs.order_by('-registered_at')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        privileged_roles = {'lsa', 'soc', 'data_entry'}
        mine_only = not (user.is_superuser or getattr(user, 'role', None) in privileged_roles)

        qs = self.object_list  # filtered queryset

        ctx.update({
            'status_filter': self.request.GET.get('status_filter', ''),
            'search_query': self.request.GET.get('search', ''),
            'filter_status': self.kwargs.get('filter_status', ''),
            'status_choices': getattr(Visitor, 'APPROVAL_STATUS', []),
            'mine_only': mine_only,

            'total_count': qs.count(),
            'pending_count': qs.filter(status='pending').count(),
            'approved_count': qs.filter(status='approved').count(),
            'checked_in_count': qs.filter(checked_in=True, checked_out=False).count(),
        })
        return ctx


class VisitorDetailView(LoginRequiredMixin, DetailView):
    model = Visitor
    template_name = 'visitors/visitor_detail.html'
    context_object_name = 'visitor'

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        privileged_roles = {'lsa', 'soc', 'data_entry'}
        if not (user.is_superuser or getattr(user, 'role', None) in privileged_roles):
            qs = qs.filter(registered_by=user)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['logs'] = VisitorLog.objects.filter(
            visitor=self.object
        ).select_related('performed_by').order_by('-timestamp')
        return ctx


class VisitorCreateView(LoginRequiredMixin, CreateView):
    model = Visitor
    form_class = VisitorForm
    template_name = 'visitors/visitor_form.html'
    success_url = reverse_lazy('visitors:visitor_list')

    def form_valid(self, form):
        form.instance.registered_by = self.request.user
        response = super().form_valid(form)
        visitor = form.instance

        # Notify LSA/SOC of new request
        _notify_lsa_soc_new_request(visitor, self.request)

        # Auto-approve if user is LSA
        if self.request.user.role == 'lsa':
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

            # Notify requester (in this case, the same LSA - but still consistent)
            _notify_requester_status_change(visitor, 'approved')
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
@user_passes_test(is_lsa_or_soc)
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

                # Notify requester that their visitor was approved
                _notify_requester_status_change(visitor, 'approved', notes)

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

                # Notify requester that request was rejected
                _notify_requester_status_change(visitor, 'rejected', visitor.rejection_reason)

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

    gate = request.POST.get('gate', 'front')

    VisitorLog.objects.create(
        visitor=visitor,
        action='check_in',
        performed_by=request.user,
        gate=gate,
        notes=f'Checked in at {visitor.check_in_time.strftime("%H:%M")}'
    )

    # Notify requester that visitor checked in
    _notify_requester_check_in(visitor, gate=gate)

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

    gate = request.POST.get('gate', 'front')

    VisitorLog.objects.create(
        visitor=visitor,
        action='check_out',
        performed_by=request.user,
        gate=gate,
        notes=f'Checked out at {visitor.check_out_time.strftime("%H:%M")} (Duration: {duration_str})'
    )

    # Notify requester that visitor left
    _notify_requester_check_out(visitor, gate=gate, duration_str=duration_str)

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
            if visitor_id and visitor_id.isdigit():
                # Try database ID first
                try:
                    visitor = Visitor.objects.get(id=visitor_id)
                except Visitor.DoesNotExist:
                    pass

            if not visitor and visitor_id:
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

            if not visitor:
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
@user_passes_test(is_lsa_or_soc)
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

            # Notify requester for each approved visitor
            _notify_requester_status_change(visitor, 'approved', 'Bulk approval')

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

    queryset = Visitor.objects.all().select_related('registered_by', 'approved_by')

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


@login_required
def visitor_verify_page(request):
    if not _gate_role(request.user):
        return render(request, "visitors/verify_clearance.html", {"forbidden": True}, status=403)

    q = (request.GET.get("q") or "").strip()
    result = None
    matches = []

    if q:
        if q.isdigit():
            result = Visitor.objects.filter(pk=int(q)).first()

        if not result:
            matches = list(
                Visitor.objects.filter(
                    Q(full_name__icontains=q) |
                    Q(vehicle_plate__icontains=q)
                ).order_by("-id")[:20]
            )
            if len(matches) == 1:
                result = matches[0]

    # Use Visitor.status directly
    is_cleared = False
    status_label = None
    if result:
        status_label = getattr(result, "status", None)
        is_cleared = (status_label == "approved")

    context = {
        "q": q,
        "result": result,
        "matches": matches,
        "is_cleared": is_cleared,
        "status_label": status_label,
        "forbidden": False,
    }
    return render(request, "visitors/verify_clearance.html", context)


@login_required
def visitor_verify_lookup_api(request):
    if not _gate_role(request.user):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"ok": False, "error": "missing_query"}, status=400)

    visitor = None
    if q.isdigit():
        visitor = Visitor.objects.filter(pk=int(q)).first()
    if not visitor:
        visitor = Visitor.objects.filter(
            Q(full_name__iexact=q) | Q(vehicle_plate__iexact=q)
        ).order_by("-id").first()

    if not visitor:
        return JsonResponse({"ok": False, "found": False})

    status = getattr(visitor, "status", None)
    is_cleared = (status == "approved")

    data = {
        "ok": True,
        "found": True,
        "visitor": {
            "id": visitor.pk,
            "full_name": getattr(visitor, "full_name", str(visitor)),
            "organization": getattr(visitor, "organization", None),
            "vehicle_plate": getattr(visitor, "vehicle_plate", None),
        },
        "approval": {
            "status": status,
            "is_cleared": is_cleared,
            "code": None,
        }
    }
    return JsonResponse(data)


# --- Lightweight approval actions to use on the Visitor detail page ---

@login_required
def visitor_request_clearance(request, pk):
    """
    Any authenticated user can (re-)request clearance by setting status to 'pending'.
    Useful if a request was cancelled/rejected and needs re-submission.
    """
    visitor = get_object_or_404(Visitor, pk=pk)
    if request.method == "POST":
        visitor.status = "pending"
        visitor.save(update_fields=["status"])
        VisitorLog.objects.create(
            visitor=visitor,
            action="request",
            performed_by=request.user,
            notes="Clearance (re)requested."
        )
        messages.success(request, "Clearance requested from LSA/SOC.")

        # Notify LSA/SOC about new (re)request
        _notify_lsa_soc_new_request(visitor, request)

    return redirect("visitors:visitor_detail", pk=pk)


@login_required
@user_passes_test(is_lsa_or_soc)
def visitor_lsa_approve(request, pk):
    """
    LSA approves the visitor. Uses your existing model fields: status, approved_by, approval_date.
    """
    visitor = get_object_or_404(Visitor, pk=pk)
    if request.method == "POST":
        visitor.status = "approved"
        visitor.approved_by = request.user
        visitor.approval_date = timezone.now()
        visitor.save(update_fields=["status", "approved_by", "approval_date"])

        VisitorLog.objects.create(
            visitor=visitor,
            action="approval",
            performed_by=request.user,
            notes="Approved on visitor detail page."
        )
        messages.success(request, f"Visitor {visitor.full_name} approved.")

        # Notify requester
        _notify_requester_status_change(visitor, 'approved')

    return redirect("visitors:visitor_detail", pk=pk)


@login_required
@user_passes_test(is_lsa_or_soc)
def visitor_lsa_reject(request, pk):
    """
    LSA rejects the visitor. If you have a rejection reason in a form, you can pass it in POST['notes'].
    """
    visitor = get_object_or_404(Visitor, pk=pk)
    if request.method == "POST":
        note = request.POST.get("notes", "").strip()
        visitor.status = "rejected"
        # If you have a field 'rejection_reason' on Visitor, set it too:
        if hasattr(visitor, "rejection_reason"):
            visitor.rejection_reason = note
            visitor.save(update_fields=["status", "rejection_reason"])
        else:
            visitor.save(update_fields=["status"])

        VisitorLog.objects.create(
            visitor=visitor,
            action="rejection",
            performed_by=request.user,
            notes=note or "Rejected on visitor detail page."
        )
        messages.warning(request, f"Visitor {visitor.full_name} rejected.")

        # Notify requester
        _notify_requester_status_change(visitor, 'rejected', note)

    return redirect("visitors:visitor_detail", pk=pk)


@login_required
def visitor_cancel_request(request, pk):
    """
    Allow the original requester, LSA, or superuser to cancel a pending request.
    """
    visitor = get_object_or_404(Visitor, pk=pk)
    can_cancel = (
        request.user.is_superuser
        or getattr(request.user, "role", None) == "lsa"
        or (visitor.registered_by_id and visitor.registered_by_id == request.user.id)
    )
    if not can_cancel:
        messages.error(request, "You cannot cancel this request.")
        return redirect("visitors:visitor_detail", pk=pk)

    if request.method == "POST":
        visitor.status = "cancelled"
        visitor.save(update_fields=["status"])
        VisitorLog.objects.create(
            visitor=visitor,
            action="cancel",
            performed_by=request.user,
            notes="Request cancelled."
        )
        messages.info(request, "Visitor request cancelled.")

        # Notify LSA/SOC that a request was cancelled (optional but useful)
        _notify_lsa_soc_new_request(visitor, request)

        # Notify requester themselves if someone else cancelled (LSA/admin)
        if request.user != visitor.registered_by:
            _notify_requester_status_change(visitor, 'cancelled')

    return redirect("visitors:visitor_detail", pk=pk)


@login_required
@require_http_methods(["GET", "POST"])
def gate_check_view(request, pk):
    """
    Gate workflow:
      - Only guards/LSA/SOC/superuser can access (_gate_role)
      - If visitor missing id_number and action=check_in => require it
      - Enforces status rules: must be 'approved' to check in
      - Sends emails on check-in / check-out
    """
    if not _gate_role(request.user):
        messages.error(request, "You don’t have permission to perform gate actions.")
        return redirect('visitors:visitor_detail', pk=pk)

    visitor = get_object_or_404(Visitor, pk=pk)
    initial_action = request.GET.get('action')  # optional shortcut from link
    form = GateCheckForm(request.POST or None, initial={'action': initial_action} if initial_action else None)

    if request.method == 'POST' and form.is_valid():
        action = form.cleaned_data['action']
        gate = form.cleaned_data['gate']
        id_number = form.cleaned_data['id_number']
        card_number = form.cleaned_data['card_number']

        # for check-in, enforce approval
        if action == 'check_in':
            if visitor.status != 'approved':
                messages.error(request, "Visitor is not approved by LSA.")
                return redirect('visitors:visitor_detail', pk=visitor.pk)

            # set id_number if missing
            if not visitor.id_number:
                visitor.id_number = id_number

            # assign/issue card
            try:
                with transaction.atomic():
                    card = VisitorCard.objects.select_for_update().get(number__iexact=card_number)
                    if not card.is_active:
                        messages.error(request, f"Card {card.number} is inactive.")
                        return redirect('visitors:visitor_detail', pk=visitor.pk)
                    if card.in_use:
                        messages.error(request, f"Card {card.number} is already in use.")
                        return redirect('visitors:visitor_detail', pk=visitor.pk)

                    # issue
                    card.in_use = True
                    card.issued_to = visitor
                    card.issued_at = timezone.now()
                    card.issued_by = request.user
                    card.returned_at = None
                    card.returned_by = None
                    card.save()

                    visitor.visitor_card = card
                    visitor.card_issued_at = card.issued_at
                    visitor.checked_in = True
                    visitor.check_in_time = timezone.now()
                    visitor.save()

                # log
                VisitorLog.objects.create(
                    visitor=visitor, action='check_in', performed_by=request.user,
                    gate=gate, notes=f"Issued card {card.number}"
                )
                messages.success(request, f"Checked in. Card {card.number} issued.")

                # Email requester
                _notify_requester_check_in(visitor, gate=gate)

                return redirect('visitors:visitor_detail', pk=visitor.pk)

            except VisitorCard.DoesNotExist:
                messages.error(request, "Card number not found.")
                return redirect('visitors:visitor_detail', pk=visitor.pk)

        elif action == 'check_out':
            # must have been checked in
            if not visitor.checked_in or visitor.checked_out:
                messages.error(request, "Visitor not currently in compound.")
                return redirect('visitors:visitor_detail', pk=visitor.pk)

            # must have a card to collect
            if not visitor.visitor_card:
                # still allow checkout, but warn
                messages.warning(request, "Visitor had no card assigned — checking out anyway.")
                visitor.checked_out = True
                visitor.check_out_time = timezone.now()
                visitor.save()
                VisitorLog.objects.create(
                    visitor=visitor, action='check_out', performed_by=request.user,
                    gate=gate, notes="Checked out (no card on file)"
                )

                # Notify requester
                duration = visitor.check_out_time - visitor.check_in_time if visitor.check_in_time else None
                duration_str = str(duration).split('.')[0] if duration else None
                _notify_requester_check_out(visitor, gate=gate, duration_str=duration_str)

                return redirect('visitors:visitor_detail', pk=visitor.pk)

            # return the card
            with transaction.atomic():
                card = VisitorCard.objects.select_for_update().get(pk=visitor.visitor_card_id)
                card.in_use = False
                card.returned_at = timezone.now()
                card.returned_by = request.user
                card.issued_to = None
                card.save()

                visitor.card_returned_at = card.returned_at
                visitor.visitor_card = None
                visitor.checked_out = True
                visitor.check_out_time = timezone.now()
                visitor.save()

            VisitorLog.objects.create(
                visitor=visitor, action='check_out', performed_by=request.user,
                gate=gate, notes=f"Collected card {card.number}"
            )
            messages.success(request, f"Checked out. Card {card.number} collected.")

            # Notify requester
            duration = visitor.check_out_time - visitor.check_in_time if visitor.check_in_time else None
            duration_str = str(duration).split('.')[0] if duration else None
            _notify_requester_check_out(visitor, gate=gate, duration_str=duration_str)

            return redirect('visitors:visitor_detail', pk=visitor.pk)

        else:
            messages.error(request, "Unknown gate action.")
            return redirect('visitors:visitor_detail', pk=visitor.pk)

    # GET or invalid POST
    return render(request, 'visitors/gate_check.html', {
        'visitor': visitor,
        'form': form,
    })


@login_required
def visitor_card_list(request):
    qs = VisitorCard.objects.all().order_by('number')

    # Search
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(number__icontains=q)
            # add more fields here if needed, e.g. holder name
        )

    # Status filter: all / available / in_use / inactive
    flt = (request.GET.get('filter') or 'all').strip()

    if flt == 'available':
        qs = qs.filter(is_active=True, in_use=False)
    elif flt == 'in_use':
        qs = qs.filter(is_active=True, in_use=True)
    elif flt == 'inactive':
        qs = qs.filter(is_active=False)
    # 'all' = no extra filter

    # Pagination
    paginator = Paginator(qs, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Stats
    total_cards = VisitorCard.objects.count()
    available_cards = VisitorCard.objects.filter(is_active=True, in_use=False).count()
    in_use_cards = VisitorCard.objects.filter(is_active=True, in_use=True).count()
    inactive_cards = VisitorCard.objects.filter(is_active=False).count()

    # Permission to manage cards (edit/history/create)
    can_manage_cards = (
        request.user.is_superuser
        or getattr(request.user, 'role', None) in {'lsa', 'soc', 'data_entry'}
    )

    context = {
        'cards': page_obj,
        'page_obj': page_obj,
        'paginator': paginator,
        'is_paginated': page_obj.has_other_pages(),
        'q': q,
        'filter': flt,
        'total_cards': total_cards,
        'available_cards': available_cards,
        'in_use_cards': in_use_cards,
        'inactive_cards': inactive_cards,
        'current_time': timezone.now(),
        'can_manage_cards': can_manage_cards,
    }
    return render(request, 'visitors/card_list.html', context)

@login_required
def visitor_card_create(request):
    """
    Create a new visitor card.

    Only LSA / SOC / data_entry / superuser can create cards.
    """
    user = request.user
    allowed_roles = {'lsa', 'soc', 'data_entry'}
    user_role = getattr(user, 'role', None)

    if not (user.is_superuser or user_role in allowed_roles):
        messages.error(request, "You do not have permission to create visitor cards.")
        return redirect('visitors:visitor_card_list')

    if request.method == "POST":
        number = (request.POST.get("number") or "").strip()
        is_active = bool(request.POST.get("is_active"))

        if not number:
            messages.error(request, "Card number is required.")
            return render(request, "visitors/card_form.html", {
                "number": number,
                "is_active": is_active,
                "page_title": "Create Visitor Card",
            })

        try:
            VisitorCard.objects.create(
                number=number,
                is_active=is_active,
                # sane defaults
                in_use=False,
            )
        except IntegrityError:
            messages.error(request, f"Card with number '{number}' already exists.")
            return render(request, "visitors/card_form.html", {
                "number": number,
                "is_active": is_active,
                "page_title": "Create Visitor Card",
            })

        messages.success(request, f"Visitor card '{number}' created successfully.")
        # redirect back to list
        return redirect("visitors:visitor_card_list")

    # GET request – show empty form
    context = {
        "page_title": "Create Visitor Card",
        "number": "",
        "is_active": True,
    }
    return render(request, "visitors/card_form.html", context)


@login_required
def visitor_card_detail(request, pk):
    card = get_object_or_404(VisitorCard, pk=pk)

    # Get all log entries related to this card
    card_history = (
        VisitorLog.objects
        .filter(card=card)
        .select_related('visitor', 'performed_by')
        .order_by('-timestamp')
    )

    can_manage = request.user.has_perm('visitors.change_visitorcard')

    if request.method == "POST" and can_manage:
        action = request.POST.get("action")

        if action == "deactivate" and card.is_active:
            card.is_active = False
            card.save()
            messages.success(request, f"Card {card.number} has been deactivated.")
        elif action == "activate" and not card.is_active:
            card.is_active = True
            card.save()
            messages.success(request, f"Card {card.number} has been activated.")
        else:
            messages.warning(request, "No changes were applied to this card.")

        return redirect("visitors:visitor_card_detail", pk=card.pk)

    context = {
        "card": card,
        "card_history": card_history,
        "can_manage": can_manage,
    }
    return render(request, "visitors/card_detail.html", context)

@login_required
def visitor_card_check_api(request):
    number = (request.GET.get('number') or '').strip()
    if not number:
        return JsonResponse({'ok': False, 'error': 'missing_number'}, status=400)
    try:
        card = VisitorCard.objects.get(number__iexact=number)
        return JsonResponse({
            'ok': True,
            'exists': True,
            'is_active': card.is_active,
            'in_use': card.in_use,
            'available': card.is_active and not card.in_use,
        })
    except VisitorCard.DoesNotExist:
        return JsonResponse({'ok': True, 'exists': False, 'available': False})

class VisitorReportView(LoginRequiredMixin, TemplateView):
    """
    Simple reports page for visitors – totals, by status, by date.
    """
    template_name = "visitors/visitor_reports.html"

    def get_queryset_base(self):
        user = self.request.user
        qs = Visitor.objects.all()

        privileged_roles = {'lsa', 'soc', 'data_entry'}
        if not (user.is_superuser or getattr(user, 'role', None) in privileged_roles):
            qs = qs.filter(registered_by=user)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = self.get_queryset_base()
        today = timezone.now().date()

        ctx["total_visitors"] = qs.count()
        ctx["pending_count"] = qs.filter(status="pending").count()
        ctx["approved_count"] = qs.filter(status="approved").count()
        ctx["rejected_count"] = qs.filter(status="rejected").count()
        ctx["cancelled_count"] = qs.filter(status="cancelled").count()

        ctx["today_visitors"] = qs.filter(
            Q(registered_at__date=today) | Q(created_at__date=today)
        ).count()

        # Simple last 7 days trend
        last_7_days = []
        for i in range(7):
            day = today - timedelta(days=i)
            c = qs.filter(
                Q(registered_at__date=day) | Q(created_at__date=day)
            ).count()
            last_7_days.append({"date": day, "count": c})
        ctx["last_7_days"] = list(reversed(last_7_days))

        return ctx
