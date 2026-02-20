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

User = get_user_model()


# you already use this check in the file
def is_ict_focal_point(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, "role", "") in ("ict_focal", "lsa", "soc"))


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
    """Mixin to ensure ICT focal can only access users in their agency."""

    def get_object(self, queryset=None):
        """Get user object and verify it's in the ICT focal's agency."""
        obj = super().get_object(queryset)
        user = self.request.user

        # Check if the user belongs to the ICT focal's agency
        if not user.agency_id:
            raise Http404("You are not assigned to an agency.")

        if obj.agency_id != user.agency_id:
            raise Http404("User not found in your agency.")

        return obj


class ICTUserListView(ICTUserGuardMixin, ListView):
    """List users within the ICT focal point's agency."""

    template_name = "accounts/ict/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        """Return users in the ICT focal's agency with optional search."""
        user = self.request.user

        # Base queryset with related data
        qs = User.objects.select_related('agency').order_by(
            'last_name', 'first_name', 'username'
        )

        # Filter by agency - ICT focal can only see their agency's users
        if user.agency_id:
            qs = qs.filter(agency_id=user.agency_id)
        else:
            # If ICT focal has no agency, show empty queryset
            return User.objects.none()

        # Apply search filter if provided
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
        context['q'] = self.request.GET.get('q', '').strip()

        # Add user counts for better UX
        if user.agency_id:
            context['total_agency_users'] = User.objects.filter(
                agency_id=user.agency_id
            ).count()
        else:
            context['total_agency_users'] = 0

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

    def form_valid(self, form):
        """
        Handle successful form submission:
        - Create user
        - Send password setup / reset link to the new user's email (if present)
        """
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

    def form_valid(self, form):
        """Handle successful form submission."""
        response = super().form_valid(form)

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

    if not request.user.agency_id:
        messages.error(request, 'You are not assigned to an agency.')
        return redirect('accounts:ict_user_list')

    if target_user.agency_id != request.user.agency_id:
        messages.error(request, 'User not found in your agency.')
        return redirect('accounts:ict_user_list')

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

    if not request.user.agency_id:
        messages.error(request, 'You are not assigned to an agency.')
        return redirect('accounts:ict_user_list')

    if target_user.agency_id != request.user.agency_id:
        messages.error(request, 'User not found in your agency.')
        return redirect('accounts:ict_user_list')

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

    if not request.user.agency_id:
        messages.error(request, 'You are not assigned to an agency.')
        return redirect('accounts:ict_user_list')

    if target_user.agency_id != request.user.agency_id:
        messages.error(request, 'User not found in your agency.')
        return redirect('accounts:ict_user_list')

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

            # Same agency as the ICT focal point who created the invite
            if hasattr(user, "agency") and hasattr(invite.created_by, "agency"):
                user.agency = invite.created_by.agency

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
