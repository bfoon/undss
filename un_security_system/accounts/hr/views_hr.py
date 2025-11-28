from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import ListView, CreateView

from .notifications import notify_users_by_role, notify_users_direct
from .forms import EmployeeIDCardRequestForm, EmployeeIDCardAdminRequestForm
from .models import EmployeeIDCardRequest

User = get_user_model()


# ---- Role helpers ---------------------------------------------------------

def is_lsa_soc_or_hr(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return getattr(user, "role", "") in ("lsa", "soc", "agency_hr")


class LsaSocHrRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return is_lsa_soc_or_hr(self.request.user)


# ---- 5.1. Employees with expiring / expired IDs ---------------------------

class ExpiringIDListView(LoginRequiredMixin, LsaSocHrRequiredMixin, ListView):
    template_name = "hr/expiring_ids.html"
    context_object_name = "users"

    def get_queryset(self):
        days = int(self.request.GET.get("days") or 30)
        today = timezone.localdate()
        warn_date = today + timedelta(days=days)

        qs = User.objects.filter(
            is_active=True,
        ).exclude(employee_id__isnull=True).exclude(employee_id__exact="")

        qs = qs.exclude(employee_id_expiry__isnull=True)
        qs = qs.filter(employee_id_expiry__lte=warn_date)

        role = getattr(self.request.user, "role", "")
        if role == "agency_hr" and getattr(self.request.user, "agency_id", None):
            # 🔐 This line restricts to their own agency only
            qs = qs.filter(agency=self.request.user.agency)

        return qs.order_by("agency__name", "employee_id_expiry", "last_name")


    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["today"] = timezone.localdate()
        ctx["days"] = int(self.request.GET.get("days") or 30)
        return ctx

@login_required
def my_idcard_request(request):
    """
    Staff request ID card for themselves.
    """
    target = request.user

    if request.method == "POST":
        form = EmployeeIDCardRequestForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.for_user = target
            obj.requested_by = request.user
            obj.save()

            # Notify LSA/SOC/Agency HR
            subject = f"ID Card Request for {target.get_full_name() or target.username}"
            msg = (
                f"A new {obj.get_request_type_display()} request was submitted.\n\n"
                f"Employee: {target.get_full_name() or target.username}\n"
                f"Employee ID: {target.employee_id or 'N/A'}\n"
                f"Agency: {getattr(target, 'un_agency', '') or 'N/A'}\n"
                f"Reason: {obj.reason or '—'}"
            )
            notify_users_by_role(["lsa", "soc", "agency_hr"], subject, msg)

            messages.success(request, "Your ID card request has been submitted.")
            return redirect("accounts:my_idcard_requests")
    else:
        form = EmployeeIDCardRequestForm()

    return render(request, "hr/my_idcard_request_form.html", {"form": form})

@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_for_user(request):
    """
    HR/LSA/SOC initiate a card request for any employee.
    - LSA / SOC / superuser: can pick any user
    - Agency HR: ONLY staff in their own agency
    Optional ?user=<id> to preselect the employee.
    """
    role = getattr(request.user, "role", "")
    is_agency_hr = (role == "agency_hr")
    has_agency = getattr(request.user, "agency_id", None) is not None

    # ---------- Build base queryset for for_user field ----------
    if is_agency_hr and has_agency:
        # Agency HR can only see their agency’s users
        base_user_qs = User.objects.filter(
            agency=request.user.agency,
            is_active=True,
        )
    else:
        # LSA / SOC / superuser: all active staff
        base_user_qs = User.objects.filter(is_active=True)

    # ---------- Optional preselect via ?user=<id> ----------
    preselect_id = request.GET.get("user")
    initial = {}
    if preselect_id:
        target = base_user_qs.filter(pk=preselect_id).first()
        if target:
            initial["for_user"] = target

    if request.method == "POST":
        form = EmployeeIDCardAdminRequestForm(request.POST)
        # Limit choices for agency HR on POST as well
        if "for_user" in form.fields:
            form.fields["for_user"].queryset = base_user_qs

        if form.is_valid():
            obj = form.save(commit=False)
            obj.requested_by = request.user

            # Extra safety check for agency HR
            if is_agency_hr and has_agency:
                if obj.for_user.agency_id != request.user.agency_id:
                    form.add_error(
                        "for_user",
                        "Agency HR can only request ID cards for staff in their own agency.",
                    )
                else:
                    obj.save()
            else:
                obj.save()

            if obj.pk:
                # notify target user + LSA/SOC/HR
                subject = f"ID Card Request Created for {obj.for_user.get_full_name() or obj.for_user.username}"
                msg = (
                    f"An ID card request was created.\n\n"
                    f"Type: {obj.get_request_type_display()}\n"
                    f"For: {obj.for_user.get_full_name() or obj.for_user.username}\n"
                    f"Employee ID: {obj.for_user.employee_id or 'N/A'}\n"
                    f"Agency: {getattr(obj.for_user.agency, 'name', 'N/A')}\n"
                    f"Requested by: {request.user.get_full_name() or request.user.username}\n"
                    f"Reason: {obj.reason or '—'}"
                )
                notify_users_direct([obj.for_user], subject, msg)
                notify_users_by_role(["lsa", "soc", "agency_hr"], subject, msg)

                messages.success(request, "ID card request created.")
                return redirect("accounts:idcard_request_list")
    else:
        form = EmployeeIDCardAdminRequestForm(initial=initial)
        if "for_user" in form.fields:
            form.fields["for_user"].queryset = base_user_qs

    return render(request, "hr/idcard_admin_request_form.html", {"form": form})

@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_list(request):
    qs = EmployeeIDCardRequest.objects.select_related(
        "for_user", "requested_by", "approver"
    ).order_by("-created_at")

    status = (request.GET.get("status") or "").strip()
    if status:
        qs = qs.filter(status=status)

    # 🔐 Agency HR only see their agency’s staff
    if getattr(request.user, "role", "") == "agency_hr" and getattr(request.user, "agency_id", None):
        qs = qs.filter(for_user__agency=request.user.agency)

    return render(request, "hr/idcard_request_list.html", {
        "requests": qs,
        "status_filter": status,
    })


@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_approve(request, pk):
    """
    Step 1: LSA / SOC / HR marks the request as 'Pending Photo Capture'
    and notifies the user to come and take a picture.
    """
    obj = get_object_or_404(EmployeeIDCardRequest, pk=pk)

    obj.mark_call_for_photo(request.user)

    subject = "Your ID card request is ready for photo capture"
    msg = (
        f"Your {obj.get_request_type_display()} request for your employee ID card "
        f"has been reviewed.\n\n"
        f"Status: {obj.get_status_display()}\n"
        f"Action required: Please report to Security/HR for photo capture "
        f"at the next available opportunity."
    )
    # Notify the person whose ID it is for + the requester (HR or staff)
    notify_users_direct([obj.for_user, obj.requested_by], subject, msg)

    messages.success(request, "Request moved to 'Pending Photo Capture'.")
    return redirect("accounts:idcard_request_list")



@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_reject(request, pk):
    obj = get_object_or_404(EmployeeIDCardRequest, pk=pk)
    obj.mark_rejected(request.user)

    subject = "Your ID card request has been rejected"
    msg = (
        f"Your {obj.get_request_type_display()} request for your employee ID card has been rejected.\n\n"
        f"Status: {obj.get_status_display()}"
    )
    notify_users_direct([obj.for_user, obj.requested_by], subject, msg)

    messages.warning(request, "Request rejected.")
    return redirect("accounts:idcard_request_list")


@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_mark_printed(request, pk):
    """
    Step 2: Card printed.
    """
    obj = get_object_or_404(EmployeeIDCardRequest, pk=pk)
    obj.mark_printed(request.user)

    subject = "Your ID card has been printed"
    msg = (
        f"Your ID card ({obj.get_request_type_display()}) has been printed.\n"
        f"Current status: {obj.get_status_display()}.\n\n"
        f"You will be notified once the card is ready for collection."
    )
    notify_users_direct([obj.for_user, obj.requested_by], subject, msg)

    messages.success(request, "Request marked as printed.")
    return redirect("accounts:idcard_request_list")

@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_mark_issued(request, pk):
    """
    Step 3: Card has been issued/handed over to the staff.
    """
    obj = get_object_or_404(EmployeeIDCardRequest, pk=pk)
    obj.mark_issued(request.user)

    subject = "Your ID card has been issued"
    msg = (
        f"Your ID card ({obj.get_request_type_display()}) has been issued to you.\n"
        f"Status: {obj.get_status_display()}.\n\n"
        f"If you still have an old card, please return/destroy it according to UN policy."
    )
    notify_users_direct([obj.for_user, obj.requested_by], subject, msg)

    messages.success(request, "Request marked as issued.")
    return redirect("accounts:idcard_request_list")

@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_detail(request, pk):
    """
    HR / LSA / SOC can view the full details of an ID card request.
    Agency HR is limited to their own agency staff.
    """
    qs = EmployeeIDCardRequest.objects.select_related(
        "for_user",
        "requested_by",
        "approver",
        "printed_by",
        "issued_by",
    )

    # Agency HR only sees requests for their agency’s staff
    if getattr(request.user, "role", "") == "agency_hr" and request.user.agency_id:
        qs = qs.filter(for_user__agency=request.user.agency)

    obj = get_object_or_404(qs, pk=pk)

    return render(request, "hr/idcard_request_detail.html", {"obj": obj})



