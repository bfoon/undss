# dashboard/views.py
from datetime import timedelta
import csv

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import models
from django.db.models import Count, Max, Q
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.shortcuts import render
from django.utils import timezone
from django.views.generic import TemplateView

from visitors.models import Visitor, VisitorLog
from vehicles.models import VehicleMovement, Vehicle, PackageEvent, AssetExit
from accounts.models import SecurityIncident
from incidents.models import IncidentReport


# ----------------------------- role helpers -----------------------------

def is_lsa(user):
    return user.is_authenticated and (user.role == 'lsa' or user.is_superuser)

def is_soc(user):
    return user.is_authenticated and (user.role == 'soc' or user.is_superuser)

def is_data_entry(user):
    return user.is_authenticated and (user.role == 'data_entry' or user.is_superuser)

def _is_lsa_or_soc(user):
    return user.is_authenticated and getattr(user, "role", "") in ("lsa", "soc")

class LSARequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return is_lsa(self.request.user)


class SOCRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return is_soc(self.request.user)


class DataEntryRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return is_data_entry(self.request.user)


# ----------------------------- shared helpers ---------------------------

def vehicles_in_compound_estimate():
    """
    Heuristic: a vehicle is 'inside' if its last movement is 'entry'.
    """
    # last movement per vehicle
    last_moves = (VehicleMovement.objects
                  .values('vehicle_id')
                  .annotate(last_ts=Max('timestamp'))
                  )
    inside_count = 0
    if last_moves:
        # build a map: vehicle_id -> last_ts
        last_map = {row['vehicle_id']: row['last_ts'] for row in last_moves}
        # fetch those movements and check type
        for vid, ts in last_map.items():
            mv = (VehicleMovement.objects
                  .filter(vehicle_id=vid, timestamp=ts)
                  .only('movement_type')
                  .first())
            if mv and mv.movement_type == 'entry':
                inside_count += 1
    return inside_count


def base_dashboard_context():
    today = timezone.now().date()
    ctx = {
        'today': today,
        'total_visitors_today': Visitor.objects.filter(registered_at__date=today).count(),
        'pending_approvals': Visitor.objects.filter(status='pending').count(),
        'active_visitors': Visitor.objects.filter(checked_in=True, checked_out=False).count(),
        'vehicles_in_compound': vehicles_in_compound_estimate(),
        'movements_today': VehicleMovement.objects.filter(timestamp__date=today).count(),
    }
    return ctx


# ------------------------------ main dashboard ------------------------------

class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'dashboard/dashboard.html'  # create this

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        today = timezone.now().date()

        ctx.update(base_dashboard_context())

        # Recent items for all roles
        ctx['recent_movements'] = VehicleMovement.objects.select_related('vehicle').order_by('-timestamp')[:10]
        ctx['recent_visitors'] = Visitor.objects.order_by('-registered_at')[:10]

        # Role-specific extras
        if is_lsa(user) or is_soc(user):
            ctx['recent_incidents'] = SecurityIncident.objects.filter(reported_at__date=today).order_by('-reported_at')[:5]

        return ctx


# ------------------------- role-specific dashboards -------------------------

class DataEntryDashboardView(LoginRequiredMixin, DataEntryRequiredMixin, TemplateView):
    template_name = 'dashboard/data_entry_dashboard.html'  # create this

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(base_dashboard_context())
        # Gate-focused quick stats
        ctx['recent_movements'] = VehicleMovement.objects.select_related('vehicle').order_by('-timestamp')[:15]
        ctx['recent_visitor_logs'] = VisitorLog.objects.order_by('-timestamp')[:15]
        return ctx


class LSADashboardView(LoginRequiredMixin, LSARequiredMixin, TemplateView):
    template_name = 'dashboard/lsa_dashboard.html'  # create this

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(base_dashboard_context())
        today = timezone.now().date()
        ctx['recent_incidents'] = SecurityIncident.objects.order_by('-reported_at')[:10]
        ctx['open_incidents'] = SecurityIncident.objects.filter(resolved=False).count()
        ctx['incidents_today'] = SecurityIncident.objects.filter(reported_at__date=today).count()
        return ctx


class SOCDashboardView(LoginRequiredMixin, SOCRequiredMixin, TemplateView):
    template_name = 'dashboard/soc_dashboard.html'  # create this

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(base_dashboard_context())
        # Live feed sections populated by APIs / websockets on the page
        return ctx


# ----------------------------- analytics & reports -----------------------------

class AnalyticsDashboardView(LoginRequiredMixin, LSARequiredMixin, TemplateView):
    template_name = 'dashboard/analytics.html'  # create this

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.now().date()

        ctx['visitors_by_status'] = (Visitor.objects
                                     .values('status')
                                     .annotate(c=Count('id'))
                                     .order_by('-c'))
        ctx['movements_by_type_today'] = (VehicleMovement.objects
                                          .filter(timestamp__date=today)
                                          .values('movement_type')
                                          .annotate(c=Count('id'))
                                          .order_by('-c'))
        ctx['incidents_by_severity'] = (SecurityIncident.objects
                                        .values('severity')
                                        .annotate(c=Count('id'))
                                        .order_by('-c'))
        return ctx


class ReportsView(LoginRequiredMixin, LSARequiredMixin, TemplateView):
    template_name = 'dashboard/reports.html'  # create this


@login_required
def daily_report_view(request):
    date_str = request.GET.get('date')
    date = timezone.now().date()
    if date_str:
        try:
            date = timezone.datetime.fromisoformat(date_str).date()
        except Exception:
            pass

    context = {
        'date': date,
        'visitors': Visitor.objects.filter(registered_at__date=date),
        'movements': VehicleMovement.objects.filter(timestamp__date=date).select_related('vehicle'),
        'incidents': SecurityIncident.objects.filter(reported_at__date=date),
    }
    return render(request, 'dashboard/reports_daily.html', context)  # create this


@login_required
def weekly_report_view(request):
    today = timezone.now().date()
    start = today - timedelta(days=7)
    context = {
        'start': start, 'end': today,
        'visitors': Visitor.objects.filter(registered_at__date__gte=start, registered_at__date__lte=today),
        'movements': VehicleMovement.objects.filter(timestamp__date__gte=start, timestamp__date__lte=today).select_related('vehicle'),
        'incidents': SecurityIncident.objects.filter(reported_at__date__gte=start, reported_at__date__lte=today),
    }
    return render(request, 'dashboard/reports_weekly.html', context)  # create this


@login_required
def monthly_report_view(request):
    today = timezone.now().date()
    start = today.replace(day=1)
    context = {
        'start': start, 'end': today,
        'visitors': Visitor.objects.filter(registered_at__date__gte=start, registered_at__date__lte=today),
        'movements': VehicleMovement.objects.filter(timestamp__date__gte=start, timestamp__date__lte=today).select_related('vehicle'),
        'incidents': SecurityIncident.objects.filter(reported_at__date__gte=start, reported_at__date__lte=today),
    }
    return render(request, 'dashboard/reports_monthly.html', context)  # create this


# ----------------------------- real-time API endpoints -----------------------------

@login_required
def dashboard_api(request):
    today = timezone.now().date()
    data = {
        'active_visitors': Visitor.objects.filter(checked_in=True, checked_out=False).count(),
        'pending_approvals': Visitor.objects.filter(status='pending').count(),
        'vehicles_in_compound': vehicles_in_compound_estimate(),
        'movements_today': VehicleMovement.objects.filter(timestamp__date=today).count(),
    }
    return JsonResponse(data)


@login_required
def dashboard_stats_api(request):
    today = timezone.now().date()
    stats = {
        'visitors_today': Visitor.objects.filter(registered_at__date=today).count(),
        'incidents_today': SecurityIncident.objects.filter(reported_at__date=today).count(),
        'open_incidents': SecurityIncident.objects.filter(resolved=False).count(),
        'movements_today': VehicleMovement.objects.filter(timestamp__date=today).count(),
        'inside_estimate': vehicles_in_compound_estimate(),
        'vehicles_total': Vehicle.objects.count(),
    }
    return JsonResponse(stats)


@login_required
def recent_activities_api(request):
    today = timezone.now().date()
    # Visitor logs
    vlogs = VisitorLog.objects.filter(timestamp__date=today).select_related('visitor', 'performed_by').order_by('-timestamp')[:10]
    activities = [{
        'type': 'visitor',
        'who': log.visitor.full_name,
        'action': log.get_action_display() if hasattr(log, 'get_action_display') else log.action,
        'by': getattr(log.performed_by, 'username', ''),
        'timestamp': log.timestamp.isoformat(),
    } for log in vlogs]

    # Movements
    moves = VehicleMovement.objects.select_related('vehicle').filter(timestamp__date=today).order_by('-timestamp')[:10]
    activities += [{
        'type': 'vehicle',
        'plate': mv.vehicle.plate_number if mv.vehicle else '',
        'movement': mv.movement_type,
        'gate': mv.gate,
        'timestamp': mv.timestamp.isoformat(),
    } for mv in moves]

    return JsonResponse({'results': activities})


@login_required
def security_alerts_api(request):
    # Simple rule: high/critical unresolved incidents are "alerts"
    alerts = SecurityIncident.objects.filter(resolved=False, severity__in=['high', 'critical']).order_by('-reported_at')[:10]
    data = [{
        'id': inc.id,
        'title': inc.title,
        'severity': inc.severity,
        'location': inc.location,
        'reported_at': inc.reported_at.isoformat(),
    } for inc in alerts]
    return JsonResponse({'alerts': data})


@login_required
def live_feed_api(request):
    """
    Lightweight polling endpoint for SOC live feed.
    Combine the latest movements + incidents + visitor logs.
    """
    latest_movements = VehicleMovement.objects.select_related('vehicle').order_by('-timestamp')[:5]
    latest_incidents = SecurityIncident.objects.order_by('-reported_at')[:5]
    latest_vlogs = VisitorLog.objects.order_by('-timestamp')[:5]

    data = {
        'movements': [{
            'plate': m.vehicle.plate_number if m.vehicle else '',
            'movement': m.movement_type,
            'gate': m.gate,
            'timestamp': m.timestamp.isoformat(),
        } for m in latest_movements],
        'incidents': [{
            'id': i.id,
            'title': i.title,
            'severity': i.severity,
            'reported_at': i.reported_at.isoformat(),
        } for i in latest_incidents],
        'visitor_logs': [{
            'visitor': v.visitor.full_name if v.visitor_id else '',
            'action': v.get_action_display() if hasattr(v, 'get_action_display') else v.action,
            'timestamp': v.timestamp.isoformat(),
        } for v in latest_vlogs],
    }
    return JsonResponse(data)


# ----------------------------- quick actions / search -----------------------------

@login_required
def quick_actions_page(request):
    """
    Provide links/buttons for fastest gate operations.
    """
    ctx = base_dashboard_context()
    return render(request, 'dashboard/quick_actions.html', ctx)  # create this


@login_required
def global_search_view(request):
    q = (request.GET.get('q') or '').strip()
    results = {
        'visitors': [],
        'vehicles': [],
        'incidents': [],
    }
    if q:
        results['visitors'] = list(Visitor.objects.filter(
            Q(full_name__icontains=q) | Q(status__icontains=q)
        ).values('id', 'full_name', 'status')[:20])

        results['vehicles'] = list(Vehicle.objects.filter(
            Q(plate_number__icontains=q) | Q(make__icontains=q) | Q(model__icontains=q)
        ).values('id', 'plate_number', 'make', 'model')[:20])

        results['incidents'] = list(SecurityIncident.objects.filter(
            Q(title__icontains=q) | Q(description__icontains=q) | Q(location__icontains=q)
        ).values('id', 'title', 'severity', 'location')[:20])

    return render(request, 'dashboard/search.html', {'q': q, 'results': results})  # create this


# ----------------------------- settings / help -----------------------------

@login_required
def settings_view(request):
    # Simple placeholder; expand with actual system settings UI
    return render(request, 'dashboard/settings.html', {})  # create this


@login_required
def help_view(request):
    return render(request, 'dashboard/help.html', {})  # create this


# ----------------------------- exports (CSV) -----------------------------

@login_required
def export_daily_summary(request):
    """
    Export a CSV daily summary with totals and top lines.
    """
    date_str = request.GET.get('date')
    day = timezone.now().date()
    if date_str:
        try:
            day = timezone.datetime.fromisoformat(date_str).date()
        except Exception:
            pass

    visitors = Visitor.objects.filter(registered_at__date=day).count()
    movements = VehicleMovement.objects.filter(timestamp__date=day).count()
    incidents = SecurityIncident.objects.filter(reported_at__date=day).count()

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="daily_summary_{day.isoformat()}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Metric', 'Count'])
    writer.writerow(['Visitors registered', visitors])
    writer.writerow(['Vehicle movements', movements])
    writer.writerow(['Incidents reported', incidents])
    return response


@login_required
def export_security_report(request):
    """
    Export unresolved incidents (high/critical) as CSV.
    """
    qs = SecurityIncident.objects.filter(resolved=False, severity__in=['high', 'critical']).order_by('-reported_at')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="security_alerts.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Title', 'Severity', 'Location', 'Reported At', 'Reported By'])
    for i in qs:
        writer.writerow([i.id, i.title, i.severity, i.location, i.reported_at.isoformat(), getattr(i.reported_by, 'username', '')])
    return response

class LsaSocDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "dashboard/lsa_soc_dashboard.html"

    def test_func(self):
        return _is_lsa_or_soc(self.request.user)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        since = now - timedelta(days=1)

        # Recent, NOT resolved incidents (limit 5)
        try:
            ctx["recent_incidents"] = (
                IncidentReport.objects
                .filter(created_at__gte=since)
                .exclude(status__in=["resolved", "closed", "dismissed"])
                .order_by("-created_at")[:5]
            )
        except Exception:
            ctx["recent_incidents"] = []

        # ---- build recent_activities (unchanged) ----
        activities = []
        for v in VisitorLog.objects.select_related("visitor", "performed_by").order_by("-timestamp")[:10]:
            activities.append({
                "when": v.timestamp,
                "title": v.get_action_display() if hasattr(v, "get_action_display") else (v.action or "Visitor activity"),
                "detail": getattr(v, "notes", None) or getattr(v, "description", None) or getattr(v.visitor, "full_name", ""),
                "icon": "bi-person-badge",
            })

        for e in PackageEvent.objects.select_related("package","who").order_by("-at")[:10]:
            activities.append({
                "when": e.at,
                "title": f"Package {e.package.tracking_code} · {e.get_status_display()}",
                "detail": e.note or e.package.destination_agency,
                "icon": "bi-box-seam",
            })

        try:
            for m in VehicleMovement.objects.select_related("vehicle","recorded_by").order_by("-timestamp")[:10]:
                plate = getattr(m.vehicle, "plate_number", "") or getattr(m, "plate_number", "")
                gate = getattr(m, "gate", "Gate")
                direction = getattr(m, "direction", "").capitalize()
                activities.append({
                    "when": m.timestamp,
                    "title": "Vehicle movement recorded",
                    "detail": f"{plate} {direction} at {gate}",
                    "icon": "bi-car-front",
                })
        except Exception:
            pass

        try:
            for a in AssetExit.objects.order_by("-updated_at")[:10]:
                status = getattr(a, "status", "Updated")
                activities.append({
                    "when": getattr(a, "updated_at", getattr(a, "created_at", now)),
                    "title": "Asset exit " + status.replace("_"," ").title(),
                    "detail": getattr(a, "reference_no", "") or getattr(a, "title", "Asset"),
                    "icon": "bi-box-arrow-right",
                })
        except Exception:
            pass

        activities.sort(key=lambda x: x["when"], reverse=True)
        ctx["recent_activities"] = activities[:5]
        return ctx


# -------- Live partials (HTMX) --------

def recent_activities_partial(request):
    if not _is_lsa_or_soc(request.user):
        return HttpResponseForbidden()
    now = timezone.now()

    rows = []
    for v in VisitorLog.objects.select_related("visitor").order_by("-timestamp")[:8]:
        rows.append({
            "when": v.timestamp,
            "title": v.get_action_display() if hasattr(v, "get_action_display") else (v.action or "Visitor activity"),
            "detail": getattr(v, "notes", None) or getattr(v, "description", None) or getattr(v.visitor, "full_name", ""),
            "icon": "bi-person-badge",
        })

    for e in PackageEvent.objects.select_related("package").order_by("-at")[:8]:
        rows.append({
            "when": e.at,
            "title": f"Package {e.package.tracking_code} · {e.get_status_display()}",
            "detail": e.note or e.package.destination_agency,
            "icon": "bi-box-seam",
        })

    try:
        for m in VehicleMovement.objects.select_related("vehicle").order_by("-timestamp")[:8]:
            plate = getattr(m.vehicle, "plate_number", "") or getattr(m, "plate_number", "")
            gate = getattr(m, "gate", "Gate")
            direction = getattr(m, "direction", "").capitalize()
            rows.append({
                "when": m.timestamp,
                "title": "Vehicle movement recorded",
                "detail": f"{plate} {direction} at {gate}",
                "icon": "bi-car-front",
            })
    except Exception:
        pass

    rows.sort(key=lambda x: x["when"], reverse=True)
    rows = rows[:5]
    return render(request, "dashboard/_recent_activities.html", {"rows": rows, "now": now})


def recent_incidents_partial(request):
    if not _is_lsa_or_soc(request.user):
        return HttpResponseForbidden()
    try:
        incidents = (
            IncidentReport.objects
            .exclude(status__in=["resolved", "closed", "dismissed"])
            .order_by("-created_at")[:5]
        )
    except Exception:
        incidents = []
    return render(request, "dashboard/_recent_incidents.html", {"recent_incidents": incidents})