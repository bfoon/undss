from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import models
from django.http import HttpResponseForbidden, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy, reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.generic import ListView, CreateView, UpdateView, DetailView

from .forms import ICTUserCreateForm, ICTUserUpdateForm
from .permissions import is_ict_focal

User = get_user_model()


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

    # Prevent ICT focal from deactivating themselves
    if target_user.id == request.user.id:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect('accounts:ict_user_detail', pk=pk)

    # Toggle status
    target_user.is_active = not target_user.is_active
    target_user.save(update_fields=['is_active'])

    status = 'activated' if target_user.is_active else 'deactivated'
    messages.success(request, f'User "{target_user.username}" has been {status}.')

    return redirect('accounts:ict_user_detail', pk=pk)
