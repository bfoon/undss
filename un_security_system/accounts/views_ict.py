import base64
import io

import qrcode
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
import threading
from django.db import models
from django.http import HttpResponseForbidden, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy, reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.generic import ListView, CreateView, UpdateView, DetailView
from .models import RegistrationInvite, RegistrationInviteUsage
from .utils import is_ict_focal_point
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login
from django.utils import timezone
from django.db.models import Sum, F

from .forms import ICTUserCreateForm, ICTUserUpdateForm, RegistrationInviteForm
from .forms import CustomUserRegistrationForm as UserCreationForm

from .permissions import is_ict_focal

# ── Country-office scoping ───────────────────────────────────────────────────
# Everything below used to scope by agency alone. With several country offices
# per agency, "same agency" is far too wide: a UNDP Gambia focal point could
# administer UNDP Senegal's accounts.
#
# tenancy.services owns the rules — superuser sees everything, a main admin
# sees their own office including its sub admins, a sub admin sees ordinary
# users in their office only. Importing them keeps one definition rather than
# re-deriving the same logic in five places.
#
# The fallback keeps this module importable if the tenancy app is not installed
# yet, degrading to the previous agency-only behaviour rather than breaking.
try:
    from tenancy.services import (
        can_manage_user as _can_manage_user,
        manageable_users as _manageable_users,
    )
    from tenancy.models import CountryOffice

    TENANCY_AVAILABLE = True
except ImportError:  # pragma: no cover - only on a deployment without tenancy
    CountryOffice = None
    TENANCY_AVAILABLE = False

    def _manageable_users(actor, base_queryset=None):
        qs = base_queryset if base_queryset is not None else get_user_model().objects.all()
        if getattr(actor, "is_superuser", False):
            return qs
        if not getattr(actor, "agency_id", None):
            return qs.none()
        return qs.filter(agency_id=actor.agency_id)

    def _can_manage_user(actor, target):
        if getattr(actor, "is_superuser", False):
            return True
        return bool(
            getattr(actor, "agency_id", None)
            and getattr(target, "agency_id", None) == actor.agency_id
        )


User = get_user_model()


# you already use this check in the file
def is_ict_focal_point(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, "role", "") in ("ict_focal", "lsa", "soc"))


def assignable_offices(actor):
    """
    Country offices this person may put a user into.

    Superuser: every office. Everyone else: their own only — moving somebody
    into another office is a tenancy decision, not an ICT one.
    """
    if not TENANCY_AVAILABLE or CountryOffice is None:
        return []
    if getattr(actor, "is_superuser", False):
        return list(
            CountryOffice.objects.filter(is_active=True)
            .select_related("agency")
            .order_by("agency__code", "name")
        )
    office = getattr(actor, "country_office", None)
    return [office] if office else []


def default_office_for(actor):
    """The office a newly created user should land in."""
    offices = assignable_offices(actor)
    if len(offices) == 1:
        return offices[0]
    return getattr(actor, "country_office", None)


def _require_manageable(request, pk, redirect_to="accounts:ict_user_list"):
    """
    Fetch a user the caller is allowed to administer, or bounce them.

    Returns (user, None) on success and (None, response) on refusal, so the
    calling view stays a straight line. Replaces the agency check that was
    copy-pasted into four separate views.
    """
    target = get_object_or_404(User, pk=pk)

    if not _can_manage_user(request.user, target):
        if TENANCY_AVAILABLE and not getattr(request.user, "country_office_id", None):
            messages.error(
                request,
                "Your account is not assigned to a country office yet, so you "
                "cannot administer users. Ask the platform superuser to assign you.",
            )
        else:
            messages.error(request, "That user is not in your country office.")
        return None, redirect(redirect_to)

    return target, None


def _make_qr_png_bytes(text: str) -> bytes:
    """
    Generate QR code PNG bytes for a given text (URL).
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def send_account_activation_email_async(user):
    """Send an account activated email in the background."""

    def _send():
        if not user.email:
            return

        subject = "Your UN Security Management System account has been activated"
        message = (
            f"Dear {user.get_full_name() or user.username},\n\n"
            "We are pleased to inform you that your account on the UN Security Management System "
            "has now been activated. You may now log in using your username and password via the portal.\n\n"
            "If you experience any issues, please contact the ICT department of your agency.\n\n"
            "Best regards,\n"
            "UN Security Management System ICT Team"
        )

        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)
        if from_email:
            try:
                send_mail(subject, message, from_email, [user.email], fail_silently=True)
            except Exception:
                pass

    threading.Thread(target=_send, daemon=True).start()

def send_registration_email_async(user, first_name):
    """
    Send the 'account created, pending activation' email in a background thread.
    """
    def _send():
        # Build email content
        subject = "Your UN Security Management System account request"
        display_name = first_name or user.username or "User"
        message = (
            f"Dear {display_name},\n\n"
            "Your account has been created in the UN Security Management System, "
            "but it is not yet active.\n\n"
            "Your profile is now pending activation by the ICT focal point / ICT department "
            "of your agency. You will be able to sign in once your account is approved.\n\n"
            "If you need urgent access, please contact the ICT department of your agency.\n\n"
            "Best regards,\n"
            "UN Security Management System ICT Team"
        )

        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)
        if not from_email or not user.email:
            return  # nothing to send

        try:
            send_mail(
                subject,
                message,
                from_email,
                [user.email],
                fail_silently=True,
            )
        except Exception:
            # Don't crash the thread if email fails
            pass

    # Start background thread (daemon=True so it won't block shutdown)
    threading.Thread(target=_send, daemon=True).start()


def _attach_office_field(form, actor, current=None):
    """
    Put a `country_office` choice on a user form, limited to what the actor may
    assign. A single-office admin gets it preselected and locked, so the field
    is visible — people should see which office they are creating into — but
    not editable by someone who has no authority to change it.
    """
    if not TENANCY_AVAILABLE or CountryOffice is None:
        return form

    from django import forms as dj_forms

    offices = assignable_offices(actor)
    existing = getattr(current, 'country_office', None) if current else None

    # Never silently drop a user's current office just because the actor cannot
    # assign it — show it, disabled.
    if existing and existing not in offices:
        offices = [existing] + offices

    if not offices:
        return form

    queryset = CountryOffice.objects.filter(pk__in=[o.pk for o in offices]).select_related('agency')
    initial = existing or default_office_for(actor)

    field = dj_forms.ModelChoiceField(
        queryset=queryset,
        required=True,
        initial=initial.pk if initial else None,
        label='Country office',
        empty_label=None if len(offices) == 1 else '— choose an office —',
        help_text='Which office this user belongs to. Decides what they can see.',
    )

    locked = len(offices) == 1 and not getattr(actor, 'is_superuser', False)
    css = 'form-select'
    if locked:
        # disabled inputs are not submitted, so _apply_office puts the value
        # back on the instance rather than trusting the POST.
        field.widget.attrs['disabled'] = 'disabled'
        field.required = False
    field.widget.attrs['class'] = css

    form.fields['country_office'] = field
    return form


def _apply_office(form, actor):
    """
    Copy the chosen office onto the instance, refusing anything the actor is
    not allowed to assign. A hand-crafted POST cannot move a user into another
    office by putting a different pk in the form.
    """
    if not TENANCY_AVAILABLE or CountryOffice is None:
        return

    allowed = {o.pk for o in assignable_offices(actor) if o}
    chosen = form.cleaned_data.get('country_office') if hasattr(form, 'cleaned_data') else None

    if chosen is not None and chosen.pk in allowed:
        form.instance.country_office = chosen
        return

    # Nothing valid submitted — either the field was disabled, or someone tried
    # an office they may not use. Fall back to the actor's own office, and keep
    # whatever the user already had if there is no default.
    fallback = default_office_for(actor)
    if fallback is not None:
        form.instance.country_office = fallback


class ICTUserGuardMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Mixin to restrict access to ICT focal point users only."""

    def test_func(self):
        return is_ict_focal(self.request.user)

    def handle_no_permission(self):
        """Provide helpful feedback when access is denied."""
        messages.error(
            self.request,
            'You must be an ICT Focal Point to access this page.'
        )
        return super().handle_no_permission()


class ICTUserAccessMixin(ICTUserGuardMixin):
    """Restrict detail and edit views to users the caller may administer."""

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)

        if not _can_manage_user(self.request.user, obj):
            # 404 rather than 403 on purpose: confirming that a user exists in
            # another office is itself a small disclosure.
            raise Http404("User not found in your country office.")

        return obj


class ICTUserListView(ICTUserGuardMixin, ListView):
    """List users within the ICT focal point's agency."""

    template_name = "accounts/ict/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        """Users the caller may administer, with optional search and filters."""
        base = User.objects.select_related('agency')
        if TENANCY_AVAILABLE:
            base = base.select_related('country_office', 'country_office__agency')

        qs = _manageable_users(self.request.user, base).order_by(
            'last_name', 'first_name', 'username'
        )

        # Office filter — only meaningful for a superuser, who can see several.
        office_id = self.request.GET.get('office', '').strip()
        if office_id and TENANCY_AVAILABLE:
            if office_id == 'none':
                qs = qs.filter(country_office__isnull=True)
            elif office_id.isdigit():
                qs = qs.filter(country_office_id=int(office_id))

        role = self.request.GET.get('role', '').strip()
        if role:
            qs = qs.filter(role=role)

        status = self.request.GET.get('status', '').strip()
        if status == 'active':
            qs = qs.filter(is_active=True)
        elif status == 'inactive':
            qs = qs.filter(is_active=False)

        search_query = self.request.GET.get('q', '').strip()
        if search_query:
            qs = qs.filter(
                models.Q(username__icontains=search_query) |
                models.Q(first_name__icontains=search_query) |
                models.Q(last_name__icontains=search_query) |
                models.Q(email__icontains=search_query) |
                models.Q(employee_id__icontains=search_query)
            )

        return qs

    def get_context_data(self, **kwargs):
        """Add additional context for the template."""
        context = super().get_context_data(**kwargs)
        user = self.request.user

        context['my_agency'] = user.agency
        context['my_office'] = getattr(user, 'country_office', None)
        context['q'] = self.request.GET.get('q', '').strip()
        context['selected_office'] = self.request.GET.get('office', '').strip()
        context['selected_role'] = self.request.GET.get('role', '').strip()
        context['selected_status'] = self.request.GET.get('status', '').strip()
        context['role_choices'] = getattr(User, 'ROLE_CHOICES', [])

        offices = assignable_offices(user)
        context['offices'] = offices
        # Only worth showing the office column and filter when more than one
        # office is in play.
        context['show_office_column'] = len(offices) > 1

        # Counts come from the scoped queryset, so they always agree with the
        # list below them. Counting the whole agency, as this used to, showed a
        # total the person could not actually see.
        scoped = _manageable_users(user, User.objects.all())
        context['total_scoped_users'] = scoped.count()
        context['total_active_users'] = scoped.filter(is_active=True).count()
        # Kept under the old name so an unmodified user_list.html still renders.
        context['total_agency_users'] = context['total_scoped_users']

        if TENANCY_AVAILABLE:
            context['unassigned_count'] = scoped.filter(country_office__isnull=True).count()
        else:
            context['unassigned_count'] = 0

        return context


class ICTUserDetailView(ICTUserAccessMixin, DetailView):
    """View detailed information about a user in the agency."""

    model = User
    template_name = "accounts/ict/user_detail.html"
    context_object_name = "target_user"

    def get_context_data(self, **kwargs):
        """Add additional context."""
        context = super().get_context_data(**kwargs)
        target_user = self.object

        # Check if this is the ICT focal's own account
        context['is_own_account'] = (target_user.id == self.request.user.id)

        return context


class ICTUserCreateView(ICTUserGuardMixin, CreateView):
    """Create a new user within the ICT focal point's agency."""

    template_name = "accounts/ict/user_form.html"
    form_class = ICTUserCreateForm
    success_url = reverse_lazy("accounts:ict_user_list")

    def get_form_kwargs(self):
        """Pass the requesting user to the form for validation."""
        kwargs = super().get_form_kwargs()
        kwargs['request_user'] = self.request.user
        return kwargs

    def get_form(self, form_class=None):
        """
        Add the country office selector.

        Injected here rather than declared in ICTUserCreateForm so that forms.py
        does not need to know about the tenancy app, and so this file degrades
        cleanly on a deployment that has not installed it.
        """
        form = super().get_form(form_class)
        _attach_office_field(form, self.request.user)
        return form

    def form_valid(self, form):
        """
        Handle successful form submission:
        - Assign the country office
        - Create user
        - Send password setup / reset link to the new user's email (if present)
        """
        _apply_office(form, self.request.user)
        response = super().form_valid(form)

        new_user = form.instance

        # Build password reset / setup link for the new user
        if new_user.email:
            from_email = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER
            recipient = (new_user.email or "").strip()

            if not from_email:
                messages.warning(
                    self.request,
                    'User created, but email sending is not configured (no from address).'
                )
                return response

            if not recipient:
                messages.warning(
                    self.request,
                    f'User "{new_user.username}" created, but email address is invalid.'
                )
                return response

            try:
                uid = urlsafe_base64_encode(force_bytes(new_user.pk))
                token = default_token_generator.make_token(new_user)

                # Try built-in password_reset_confirm URL
                try:
                    reset_url = self.request.build_absolute_uri(
                        reverse('password_reset_confirm', kwargs={'uidb64': uid, 'token': token})
                    )
                except Exception:
                    # Fallback to your own namespaced URL if any
                    reset_url = self.request.build_absolute_uri(
                        reverse('accounts:password_reset_confirm', kwargs={'uidb64': uid, 'token': token})
                    )

                send_mail(
                    subject='Your UN Security System account has been created',
                    message=(
                        f'Hello {new_user.get_full_name() or new_user.username},\n\n'
                        f'An account has been created for you on the UN Security / Common Services platform.\n\n'
                        f'Please click the link below to set your password and access the system:\n'
                        f'{reset_url}\n\n'
                        f'If you were not expecting this account, please contact ICT Support.\n\n'
                        f'Best regards,\nICT Support Team'
                    ),
                    from_email=from_email,
                    recipient_list=[recipient],
                    fail_silently=False,
                )

                messages.success(
                    self.request,
                    f'User "{new_user.username}" has been created and a password setup link '
                    f'has been emailed to {recipient}.'
                )
            except Exception as e:
                messages.warning(
                    self.request,
                    f'User created, but failed to send email: {e}'
                )
        else:
            messages.success(
                self.request,
                f'User "{new_user.username}" has been created successfully, '
                f'but no email was sent because the user has no email address.'
            )

        return response

    def form_invalid(self, form):
        """Handle form validation errors."""
        messages.error(
            self.request,
            'Please correct the errors below to create the user.'
        )
        return super().form_invalid(form)


class ICTUserUpdateView(ICTUserAccessMixin, UpdateView):
    """Update user information within the ICT focal point's agency."""

    model = User
    form_class = ICTUserUpdateForm
    template_name = "accounts/ict/user_form.html"

    def get_success_url(self):
        """Redirect to user detail page after update."""
        return reverse('accounts:ict_user_detail', kwargs={'pk': self.object.pk})

    def get_form_kwargs(self):
        """Pass the requesting user to the form."""
        kwargs = super().get_form_kwargs()
        kwargs['request_user'] = self.request.user
        return kwargs

    def get_form(self, form_class=None):
        """Add the country office selector, preselected to the user's own."""
        form = super().get_form(form_class)
        _attach_office_field(form, self.request.user, current=self.object)
        return form

    def form_valid(self, form):
        """Handle successful form submission."""
        moved_from = getattr(self.object, 'country_office', None)
        _apply_office(form, self.request.user)
        response = super().form_valid(form)

        moved_to = getattr(form.instance, 'country_office', None)
        if moved_from != moved_to and moved_to is not None:
            messages.success(
                self.request,
                f'User "{form.instance.username}" has been updated and moved to '
                f'{moved_to}.'
            )
        else:
            messages.success(
                self.request,
                f'User "{form.instance.username}" has been updated successfully.'
            )

        return response

    def form_invalid(self, form):
        """Handle form validation errors."""
        messages.error(
            self.request,
            'Please correct the errors below to update the user.'
        )
        return super().form_invalid(form)


@login_required
def ict_user_set_password(request, pk):
    """Allow ICT focal to set a new password for a user in their agency."""

    # Check ICT focal permission
    if not is_ict_focal(request.user):
        messages.error(request, 'You must be an ICT Focal Point to access this page.')
        return HttpResponseForbidden('Access denied')

    # Get user and verify agency
    target_user = get_object_or_404(User, pk=pk)

    scoped, refusal = _require_manageable(request, target_user.pk)
    if refusal:
        return refusal
    target_user = scoped

    # Prevent ICT focal from changing their own password this way
    if target_user.id == request.user.id:
        messages.warning(request, 'Please use the profile page to change your own password.')
        return redirect('accounts:profile')

    if request.method == 'POST':
        form = SetPasswordForm(user=target_user, data=request.POST)
        if form.is_valid():
            form.save()

            # Clear the must_change_password flag if it exists
            if hasattr(target_user, 'must_change_password'):
                target_user.must_change_password = False
                target_user.save(update_fields=['must_change_password'])

            # Send notification email
            from_email = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER
            recipient = (target_user.email or "").strip()

            if recipient and from_email:
                try:
                    send_mail(
                        subject='Your password has been changed',
                        message=(
                            f'Hello {target_user.get_full_name() or target_user.username},\n\n'
                            f'The password for your UN Security / Common Services account has just been set or '
                            f'changed by your ICT Focal Point.\n\n'
                            f'If you did not expect this change, please contact ICT Support immediately.\n\n'
                            f'Best regards,\nICT Support Team'
                        ),
                        from_email=from_email,
                        recipient_list=[recipient],
                        fail_silently=False,
                    )
                except Exception as e:
                    messages.warning(
                        request,
                        f'Password updated, but failed to send notification email: {e}'
                    )

            messages.success(
                request,
                f'Password for "{target_user.username}" has been set successfully.'
            )
            return redirect('accounts:ict_user_detail', pk=pk)
    else:
        form = SetPasswordForm(user=target_user)

    return render(request, 'accounts/ict/user_set_password.html', {
        'form': form,
        'target_user': target_user,
    })


@login_required
def ict_user_send_reset_link(request, pk):
    """Send password reset link to a user in the agency."""

    # Check ICT focal permission
    if not is_ict_focal(request.user):
        messages.error(request, 'You must be an ICT Focal Point to access this page.')
        return HttpResponseForbidden('Access denied')

    # Get user and verify agency
    target_user = get_object_or_404(User, pk=pk)

    scoped, refusal = _require_manageable(request, target_user.pk)
    if refusal:
        return refusal
    target_user = scoped

    if not target_user.email:
        messages.error(request, f'User "{target_user.username}" has no email address set.')
        return redirect('accounts:ict_user_detail', pk=pk)

    # Generate password reset token
    token = default_token_generator.make_token(target_user)
    uid = urlsafe_base64_encode(force_bytes(target_user.pk))

    # Build reset URL - try to use Django's built-in or custom reset view
    try:
        # Try to use Django's built-in password reset confirm URL
        reset_url = request.build_absolute_uri(
            reverse('password_reset_confirm', kwargs={'uidb64': uid, 'token': token})
        )
    except Exception:
        # Fallback: use custom reset URL or admin
        try:
            reset_url = request.build_absolute_uri(
                reverse('accounts:password_reset_confirm', kwargs={'uidb64': uid, 'token': token})
            )
        except Exception:
            # Last resort: direct them to contact admin
            messages.warning(
                request,
                'Password reset URL is not configured. Please set a password directly '
                'or contact the system administrator to configure password reset emails.'
            )
            return redirect('accounts:ict_user_detail', pk=pk)

    # Prepare email addresses safely
    from_email = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER
    recipient = (target_user.email or "").strip()

    if not from_email:
        messages.error(
            request,
            'Email sending is not configured properly: from address is empty.'
        )
        return redirect('accounts:ict_user_detail', pk=pk)

    if not recipient:
        messages.error(
            request,
            f'User "{target_user.username}" has no valid email address.'
        )
        return redirect('accounts:ict_user_detail', pk=pk)

    # Send email
    try:
        send_mail(
            subject='Password Reset Request',
            message=(
                f'Hello {target_user.get_full_name() or target_user.username},\n\n'
                f'A password reset has been requested for your account.\n\n'
                f'Please click the following link to set your new password:\n'
                f'{reset_url}\n\n'
                f'If you did not request this, please ignore this email.\n\n'
                f'Best regards,\nICT Support Team'
            ),
            from_email=from_email,
            recipient_list=[recipient],
            fail_silently=False,
        )

        messages.success(
            request,
            f'Password reset link has been sent to {recipient}.'
        )
    except Exception as e:
        messages.error(
            request,
            f'Failed to send email: {e}'
        )

    return redirect('accounts:ict_user_detail', pk=pk)

@login_required
def ict_user_toggle_status(request, pk):
    """Toggle user active/inactive status."""

    # Check ICT focal permission
    if not is_ict_focal(request.user):
        messages.error(request, 'You must be an ICT Focal Point to access this page.')
        return HttpResponseForbidden('Access denied')

    # Get user and verify agency
    target_user = get_object_or_404(User, pk=pk)

    scoped, refusal = _require_manageable(request, target_user.pk)
    if refusal:
        return refusal
    target_user = scoped

    # Prevent ICT focal from deactivating themselves 😂
    if target_user.id == request.user.id:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect('accounts:ict_user_detail', pk=pk)

    # Determine current action
    activating = not target_user.is_active

    # Apply the change
    target_user.is_active = activating
    target_user.save(update_fields=['is_active'])

    status = 'activated' if activating else 'deactivated'
    messages.success(request, f'User "{target_user.username}" has been {status}.')

    # 🚀 If activated, send async notification email
    if activating:
        send_account_activation_email_async(target_user)

    return redirect('accounts:ict_user_detail', pk=pk)



@login_required
@user_passes_test(is_ict_focal_point)
def create_registration_link(request):
    """
    ICT focal point generates a new registration link + QR code.
    - Shows QR code on the success page
    - Provides a downloadable QR PNG endpoint
    """
    if request.method == "POST":
        form = RegistrationInviteForm(request.POST)
        if form.is_valid():
            invite = form.save(commit=False)
            invite.created_by = request.user
            invite.save()

            # Build full URL for registration
            invite_url = request.build_absolute_uri(
                reverse("accounts:register_with_invite", args=[invite.code])
            )

            # Build QR
            qr_png = _make_qr_png_bytes(invite_url)
            qr_data_uri = "data:image/png;base64," + base64.b64encode(qr_png).decode("utf-8")

            # Download URL for QR PNG
            qr_download_url = reverse("accounts:invite_qr_download", args=[invite.code])

            return render(
                request,
                "accounts/invite_created.html",
                {
                    "invite": invite,
                    "invite_url": invite_url,
                    "qr_data_uri": qr_data_uri,
                    "qr_download_url": qr_download_url,
                },
            )
    else:
        form = RegistrationInviteForm()

    return render(request, "accounts/create_invite.html", {"form": form})


@login_required
@user_passes_test(is_ict_focal_point)
def invite_qr_download(request, code):
    """
    Download QR as PNG for an invite code.
    """
    invite = get_object_or_404(RegistrationInvite, code=code)

    invite_url = request.build_absolute_uri(
        reverse("accounts:register_with_invite", args=[invite.code])
    )

    png_bytes = _make_qr_png_bytes(invite_url)

    response = HttpResponse(png_bytes, content_type="image/png")
    response["Content-Disposition"] = f'attachment; filename="registration_invite_qr_{invite.code}.png"'
    return response


def register_with_invite(request, code):
    invite = get_object_or_404(RegistrationInvite, code=code)

    # If link is expired / full / manually deactivated
    if not invite.can_be_used:
        if not invite.is_active:
            error_msg = "This registration link has been deactivated by ICT and is no longer usable."
        elif invite.is_expired:
            error_msg = "This registration link has expired and is no longer valid."
        else:
            error_msg = "This registration link has reached its maximum number of allowed registrations."

        messages.error(request, error_msg)
        return render(request, "accounts/invite_invalid.html", {"invite": invite})

    errors = {}
    form_data = {}

    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        email = (request.POST.get("email") or "").strip()
        first_name = (request.POST.get("first_name") or "").strip()
        last_name = (request.POST.get("last_name") or "").strip()
        password1 = request.POST.get("password1") or ""
        password2 = request.POST.get("password2") or ""

        form_data = {
            "username": username,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
        }

        # --- Basic validation ---
        if not username:
            errors["username"] = "Username is required."
        if not email:
            errors["email"] = "Email is required."
        if not first_name:
            errors["first_name"] = "First name is required."
        if not last_name:
            errors["last_name"] = "Last name is required."
        if not password1 or not password2:
            errors["password"] = "Both password fields are required."
        elif password1 != password2:
            errors["password"] = "Passwords do not match."

        if password1 and len(password1) < 8:
            errors["password"] = "Password must be at least 8 characters long."

        if username and User.objects.filter(username=username).exists():
            errors["username"] = "This username is already taken."

        if email and User.objects.filter(email=email).exists():
            errors["email"] = "An account with this email already exists."

        if not errors:
            # Create user as INACTIVE (pending activation)
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password1,
                first_name=first_name,
                last_name=last_name,
            )
            user.is_active = False

            # Same agency AND the same country office as whoever issued the
            # invite. Without the office the new account lands nowhere: the
            # feature switches resolve to the agency defaults and the user
            # shows up in no office's list.
            if hasattr(user, "agency") and hasattr(invite.created_by, "agency"):
                user.agency = invite.created_by.agency
            if hasattr(user, "country_office_id") and getattr(invite.created_by, "country_office_id", None):
                user.country_office_id = invite.created_by.country_office_id

            user.save()

            # Mark invite as used
            invite.mark_used()

            # ✅ RECORD USAGE HERE
            RegistrationInviteUsage.objects.create(
                invite=invite,
                user=user,
            )

            # Send pending-activation email (async, if you added that helper)
            # send_registration_email_async(user, first_name)

            messages.success(
                request,
                "Your account has been created and is pending activation by your ICT department. "
                "You will receive an email or can contact your ICT focal point for follow-up."
            )
            return redirect("accounts:login")

    # GET or invalid POST
    return render(
        request,
        "accounts/register_with_invite.html",
        {
            "invite": invite,
            "errors": errors,
            "form_data": form_data,
        },
    )


@login_required
@user_passes_test(is_ict_focal_point)
def registration_links_list(request):
    """
    Show all registration links created by the logged-in ICT focal point.
    """
    invites_qs = (
        RegistrationInvite.objects
        .filter(created_by=request.user)
        .order_by('-created_at')
    )

    # Limit table rows to 5 (but use full data for summaries)
    invites_display = invites_qs[:5]

    # Totals for the footer (use full queryset)
    total_links = invites_qs.count()

    active_links = invites_qs.filter(
        expires_at__gt=timezone.now(),
        max_uses__gt=F("used_count"),
    ).count()

    total_registrations = invites_qs.aggregate(
        total=Sum("used_count")
    )["total"] or 0

    return render(
        request,
        "accounts/registration_links_list.html",
        {
            "invites": invites_display,  # limited query for display
            "total_links": total_links,  # full queryset numbers
            "active_links": active_links,
            "total_registrations": total_registrations,
        },
    )


@login_required
@user_passes_test(is_ict_focal_point)
def registration_link_detail(request, pk):
    """
    Show detailed information about a specific registration link.
    """
    invite = get_object_or_404(RegistrationInvite, pk=pk, created_by=request.user)
    registrations = invite.registrations.select_related("user")
    return render(
        request,
        "accounts/registration_link_detail.html",
        {"invite": invite, "registrations": registrations},
    )

@login_required
@user_passes_test(is_ict_focal_point)
def registration_link_toggle_active(request, pk):
    invite = get_object_or_404(RegistrationInvite, pk=pk, created_by=request.user)

    invite.is_active = not invite.is_active
    invite.save(update_fields=["is_active"])

    status = "activated" if invite.is_active else "deactivated"
    messages.success(request, f'Registration link "{invite.code}" has been {status}.')

    return redirect("accounts:registration_link_detail", pk=pk)
