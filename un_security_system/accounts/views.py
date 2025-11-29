# accounts/views.py
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash, get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.paginator import Paginator
from django.db.models import Q, Count
from django.http import JsonResponse, HttpResponseForbidden, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import ListView, CreateView, UpdateView, DetailView, TemplateView
from django.contrib.auth.views import PasswordChangeView
from .models import TrustedDevice, OneTimeCode
from .utils import create_otp_for_user, send_otp_email, remember_device
from datetime import timedelta
from django.views.decorators.http import require_http_methods


from .forms import (
    LoginForm,
    UserProfileForm,
    CustomUserCreationForm,
    CustomUserChangeForm,
    SecurityIncidentForm,
)
from .models import SecurityIncident

User = get_user_model()


# --------------------------- Role helpers ---------------------------

def is_lsa(user):
    return user.is_authenticated and (user.role == 'lsa' or user.is_superuser)

def is_data_entry(user):
    return user.is_authenticated and (user.role == 'data_entry' or user.is_superuser)

def is_soc(user):
    return user.is_authenticated and (user.role == 'soc' or user.is_superuser)


class LSARequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return is_lsa(self.request.user)


# --------------------------- Auth views -----------------------------
DEVICE_COOKIE_NAME = "trusted_device_id"
DEVICE_COOKIE_AGE = 30 * 24 * 60 * 60  # 30 days


def _get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def login_view(request):
    if request.user.is_authenticated:
        return redirect('accounts:profile')

    if request.method == 'POST':
        form = LoginForm(request.POST, request=request)
        if form.is_valid():
            user = form.user

            # 1) Check if this device is already trusted for this user
            device_id = request.COOKIES.get(DEVICE_COOKIE_NAME)
            trusted = None
            if device_id:
                trusted = TrustedDevice.objects.filter(
                    user=user,
                    device_id=device_id,
                    expires_at__gt=timezone.now(),
                    is_active=True,
                ).first()

            # 1a) If trusted device is valid -> normal login, no OTP
            if trusted:
                login(request, user)
                trusted.expires_at = timezone.now() + timedelta(days=30)
                trusted.save(update_fields=["expires_at"])

                messages.success(request, f'Welcome back, {user.first_name or user.username}!')
                response = redirect('accounts:profile')
                response.set_cookie(
                    DEVICE_COOKIE_NAME,
                    device_id,
                    max_age=DEVICE_COOKIE_AGE,
                    httponly=True,
                    secure=False,   # set to True in production (HTTPS)
                    samesite="Lax",
                )
                return response

            # 2) Not yet trusted: kick off OTP flow
            import uuid
            new_device_id = device_id or uuid.uuid4().hex
            ip = _get_client_ip(request)
            ua = request.META.get("HTTP_USER_AGENT", "")

            otp = create_otp_for_user(user, new_device_id, ip_address=ip, user_agent=ua)

            # DEBUG: confirm this path is hit
            # print("Sending OTP", otp.code, "to", user.email)

            send_otp_email(user, otp.code)

            # Store pending info in session
            request.session["otp_user_id"] = user.pk
            request.session["otp_device_id"] = new_device_id

            messages.info(
                request,
                "We have sent a verification code to your email. "
                "Please enter it to complete your login."
            )
            return redirect('accounts:otp_verify')
    else:
        form = LoginForm(request=request)

    return render(request, 'accounts/login.html', {'form': form})

@login_required
def logout_view(request):
    logout(request)
    messages.info(request, 'You have been logged out.')
    return redirect('accounts:login')


@login_required
def profile_view(request):
    if request.method == 'POST':
        form = UserProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated.')
            return redirect('accounts:profile')
    else:
        form = UserProfileForm(instance=request.user)

    # Template suggestion: templates/accounts/profile.html
    return render(request, 'accounts/profile.html', {'form': form})


@login_required
def change_password_view(request):
    if request.method == 'POST':
        form = PasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # keep the user logged in
            messages.success(request, 'Password changed successfully.')
            return redirect('accounts:profile')
    else:
        form = PasswordChangeForm(user=request.user)

    # Template suggestion: templates/accounts/change_password.html
    return render(request, 'accounts/change_password.html', {'form': form})


@require_http_methods(["GET", "POST"])
def otp_verify_view(request):
    user_id = request.session.get("otp_user_id")
    device_id = request.session.get("otp_device_id")

    if not user_id or not device_id:
        messages.error(request, "Your verification session has expired. Please login again.")
        return redirect("accounts:login")

    user = User.objects.filter(pk=user_id).first()
    if not user:
        messages.error(request, "Invalid user for verification. Please login again.")
        return redirect("accounts:login")

    if request.method == "POST":
        code_entered = (request.POST.get("code") or "").strip()
        if not code_entered:
            messages.error(request, "Please enter the code you received.")
        else:
            otp = OneTimeCode.objects.filter(
                user=user,
                device_id=device_id,
                code=code_entered,
                is_used=False,
                expires_at__gt=timezone.now(),
            ).order_by("-created_at").first()

            if not otp:
                messages.error(request, "Invalid or expired code. Please try again.")
            else:
                # Mark OTP as used
                otp.is_used = True
                otp.save(update_fields=["is_used"])

                # Mark this device as trusted for 30 days
                remember_device(
                    user=user,
                    device_id=device_id,
                    user_agent=request.META.get("HTTP_USER_AGENT", ""),
                    ip_address=_get_client_ip(request),
                )

                # Final login
                login(request, user)
                messages.success(request, f"Welcome, {user.first_name or user.username}! Device verified.")

                # Clean up session
                for key in ("otp_user_id", "otp_device_id"):
                    if key in request.session:
                        del request.session[key]

                # Set device cookie
                response = redirect("accounts:profile")
                response.set_cookie(
                    DEVICE_COOKIE_NAME,
                    device_id,
                    max_age=DEVICE_COOKIE_AGE,
                    httponly=True,
                    secure=True,
                    samesite="Lax",
                )
                return response

    return render(request, "accounts/otp_verify.html", {"user": user})


class PasswordChangeAndClearFlagView(PasswordChangeView):
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("password_change_done")

    def form_valid(self, form):
        resp = super().form_valid(form)
        user = self.request.user
        if getattr(user, "must_change_password", False):
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
        return resp

# ----------------------- User management (LSA) ----------------------

class UserListView(LoginRequiredMixin, LSARequiredMixin, ListView):
    model = User
    template_name = 'accounts/user_list.html'
    context_object_name = 'users'
    paginate_by = 25

    def get_queryset(self):
        qs = User.objects.all().order_by('username')
        q = self.request.GET.get('q')
        role = self.request.GET.get('role')
        if q:
            qs = qs.filter(
                Q(username__icontains=q) |
                Q(email__icontains=q) |
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(employee_id__icontains=q) |
                Q(phone__icontains=q)
            )
        if role in ('lsa', 'data_entry', 'soc'):
            qs = qs.filter(role=role)
        return qs


class UserCreateView(LoginRequiredMixin, LSARequiredMixin, CreateView):
    model = User
    form_class = CustomUserCreationForm
    template_name = 'accounts/user_form.html'
    success_url = reverse_lazy('accounts:user_list')

    def form_valid(self, form):
        resp = super().form_valid(form)
        messages.success(self.request, f'User {form.instance.username} created.')
        return resp


class UserUpdateView(LoginRequiredMixin, LSARequiredMixin, UpdateView):
    model = User
    form_class = CustomUserChangeForm
    template_name = 'accounts/user_form.html'
    success_url = reverse_lazy('accounts:user_list')

    def form_valid(self, form):
        resp = super().form_valid(form)
        messages.success(self.request, f'User {form.instance.username} updated.')
        return resp


@login_required
@user_passes_test(is_lsa)
def toggle_user_status(request, pk):
    user = get_object_or_404(User, pk=pk)
    if user == request.user:
        messages.error(request, "You can't deactivate your own account.")
        return redirect('accounts:user_list')
    user.is_active = not user.is_active
    user.save(update_fields=['is_active'])
    state = 'activated' if user.is_active else 'deactivated'
    messages.success(request, f'User {user.username} {state}.')
    return redirect('accounts:user_list')


# ------------------------ Activity log views ------------------------

@login_required
def user_activity_log(request, user_id=None):
    """
    If user_id provided (LSA route): show that user's incidents.
    Else: show current user's incidents.
    """
    if user_id:
        if not is_lsa(request.user):
            return HttpResponseForbidden('Not allowed')
        target_user = get_object_or_404(User, pk=user_id)
    else:
        target_user = request.user

    incidents = SecurityIncident.objects.filter(reported_by=target_user).order_by('-reported_at')
    paginator = Paginator(incidents, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    # Template suggestion: templates/accounts/activity_log.html
    return render(request, 'accounts/activity_log.html', {
        'target_user': target_user,
        'page_obj': page_obj,
    })


# --------------------- Security incidents views ---------------------

class SecurityIncidentListView(LoginRequiredMixin, ListView):
    model = SecurityIncident
    template_name = 'accounts/incident_list.html'
    context_object_name = 'incidents'
    paginate_by = 25

    def get_queryset(self):
        qs = SecurityIncident.objects.select_related('reported_by').order_by('-reported_at')
        # Non-LSA users only see their own incidents
        if not is_lsa(self.request.user) and not is_soc(self.request.user):
            qs = qs.filter(reported_by=self.request.user)
        sev = self.request.GET.get('severity')
        if sev in ('low', 'medium', 'high', 'critical'):
            qs = qs.filter(severity=sev)
        status = self.request.GET.get('status')
        if status == 'open':
            qs = qs.filter(resolved=False)
        elif status == 'resolved':
            qs = qs.filter(resolved=True)
        return qs


class SecurityIncidentCreateView(LoginRequiredMixin, CreateView):
    model = SecurityIncident
    form_class = SecurityIncidentForm
    template_name = 'accounts/incident_form.html'
    success_url = reverse_lazy('accounts:incident_list')

    def form_valid(self, form):
        form.instance.reported_by = self.request.user
        resp = super().form_valid(form)
        messages.success(self.request, 'Incident reported.')
        return resp


class SecurityIncidentDetailView(LoginRequiredMixin, DetailView):
    model = SecurityIncident
    template_name = 'accounts/incident_detail.html'
    context_object_name = 'incident'

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        # Restrict visibility for non-LSA/SOC to only their incidents
        if not (is_lsa(self.request.user) or is_soc(self.request.user) or obj.reported_by_id == self.request.user.id):
            raise Http404("Incident not found")
        return obj


@login_required
def resolve_incident(request, pk):
    incident = get_object_or_404(SecurityIncident, pk=pk)
    if not (is_lsa(request.user) or incident.reported_by_id == request.user.id):
        return HttpResponseForbidden('Not allowed')
    if incident.resolved:
        messages.info(request, 'Incident is already resolved.')
        return redirect('accounts:incident_detail', pk=pk)

    incident.resolved = True
    incident.resolved_by = request.user
    incident.resolved_at = timezone.now()
    incident.save(update_fields=['resolved', 'resolved_by', 'resolved_at'])
    messages.success(request, 'Incident resolved.')
    return redirect('accounts:incident_detail', pk=pk)


# ---------------------- Analytics (LSA only) ------------------------

class AccountAnalyticsView(LoginRequiredMixin, LSARequiredMixin, TemplateView):
    template_name = 'accounts/analytics.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['user_counts'] = (
            User.objects.values('role')
            .annotate(c=Count('id'))
            .order_by('-c')
        )
        ctx['incidents_today'] = SecurityIncident.objects.filter(
            reported_at__date=timezone.now().date()
        ).count()
        ctx['open_incidents'] = SecurityIncident.objects.filter(resolved=False).count()
        ctx['resolved_incidents'] = SecurityIncident.objects.filter(resolved=True).count()
        return ctx


# --------------------------- JSON APIs ------------------------------

@login_required
def user_search_api(request):
    if not is_lsa(request.user):
        return JsonResponse({'error': 'Not allowed'}, status=403)

    q = (request.GET.get('q') or '').strip()
    qs = User.objects.all()
    if q:
        qs = qs.filter(
            Q(username__icontains=q) |
            Q(email__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(employee_id__icontains=q) |
            Q(phone__icontains=q)
        )
    qs = qs.order_by('username')[:20]
    data = [{
        'id': u.id,
        'username': u.username,
        'full_name': f"{u.first_name} {u.last_name}".strip(),
        'email': u.email,
        'role': u.role,
        'is_active': u.is_active,
        'employee_id': u.employee_id,
        'phone': u.phone,
    } for u in qs]
    return JsonResponse({'results': data})


@login_required
def dashboard_stats_api(request):
    # Basic cross-app stats you can surface on dashboards
    users_total = User.objects.count()
    by_role = User.objects.values('role').annotate(c=Count('id')).order_by('-c')
    incidents_open = SecurityIncident.objects.filter(resolved=False).count()
    incidents_today = SecurityIncident.objects.filter(reported_at__date=timezone.now().date()).count()

    return JsonResponse({
        'users_total': users_total,
        'users_by_role': list(by_role),
        'incidents_open': incidents_open,
        'incidents_today': incidents_today,
    })
