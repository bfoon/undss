from datetime import timedelta
import os

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import ListView, CreateView

from .notifications import notify_users_by_role, notify_users_direct
from .forms import EmployeeIDCardRequestForm, EmployeeIDCardAdminRequestForm
from .models import EmployeeIDCardRequest

User = get_user_model()


# ---------------------------------------------------------------------------
# Role helpers
# ---------------------------------------------------------------------------

def is_lsa_soc_or_hr(user):
    """
    Check if user is LSA, SOC, Agency HR or superuser.
    """
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return getattr(user, "role", "") in ("lsa", "soc", "agency_hr")


class LsaSocHrRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return is_lsa_soc_or_hr(self.request.user)


def get_idcard_qs_for_user(user):
    """
    Base queryset for ID card requests, restricted by Agency HR scope:

    - LSA / SOC / superuser: all requests
    - Agency HR: only requests for staff in their own agency
    """
    qs = EmployeeIDCardRequest.objects.all()
    role = getattr(user, "role", "")
    if role == "agency_hr" and getattr(user, "agency_id", None):
        qs = qs.filter(for_user__agency=user.agency)
    return qs


# ---------------------------------------------------------------------------
# 5.1. Employees with expiring / expired IDs
# ---------------------------------------------------------------------------

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
            # 🔐 Restrict to their own agency only
            qs = qs.filter(agency=self.request.user.agency)

        return qs.order_by("agency__name", "employee_id_expiry", "last_name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["today"] = timezone.localdate()
        ctx["days"] = int(self.request.GET.get("days") or 30)
        return ctx


# ---------------------------------------------------------------------------
# Self-service: staff requests (renewal / replacement only)
# ---------------------------------------------------------------------------

@login_required
def my_idcard_request(request):
    """
    Staff request ID card for themselves.
    - Self-service users can only request RENEWAL or REPLACEMENT (no NEW).
    """
    target = request.user
    allowed_self_service_types = {"renewal", "replacement"}

    if request.method == "POST":
        form = EmployeeIDCardRequestForm(request.POST, request.FILES)

        # Restrict request_type choices for self-service (POST)
        if "request_type" in form.fields:
            form.fields["request_type"].choices = [
                (value, label)
                for value, label in form.fields["request_type"].choices
                if value in allowed_self_service_types
            ]

        if form.is_valid():
            obj = form.save(commit=False)

            # Extra safety check
            if obj.request_type not in allowed_self_service_types:
                form.add_error(
                    "request_type",
                    "You can only request a renewal or replacement. "
                    "New ID cards must be requested through your Agency HR / HR Focal Point.",
                )
            else:
                obj.for_user = target
                obj.requested_by = request.user
                obj.save()

                # Notify LSA/SOC/Agency HR
                display_name = target.get_full_name() or target.username
                subject = f"ID Card {obj.get_request_type_display()} request for {display_name}"
                msg = (
                    f"An ID card {obj.get_request_type_display()} request was submitted.\n\n"
                    f"Employee: {display_name}\n"
                    f"Employee ID: {target.employee_id or 'N/A'}\n"
                    f"Agency: {getattr(target, 'un_agency', '') or 'N/A'}\n"
                    f"Reason: {obj.reason or '—'}"
                )
                notify_users_by_role(["lsa", "soc", "agency_hr"], subject, msg)

                messages.success(request, "Your ID card request has been submitted.")
                return redirect("accounts:my_idcard_requests")
    else:
        form = EmployeeIDCardRequestForm()

        # Restrict request_type choices for self-service (GET)
        if "request_type" in form.fields:
            form.fields["request_type"].choices = [
                (value, label)
                for value, label in form.fields["request_type"].choices
                if value in allowed_self_service_types
            ]

    return render(request, "hr/my_idcard_request_form.html", {"form": form})


@login_required
def my_id_card_requests(request):
    """
    Show ID card requests related to the currently logged-in user.
    - Requests they submitted (requested_by)
    - Requests where they are the subject (for_user)
    Includes stats and filters.
    """
    qs = (
        EmployeeIDCardRequest.objects
        .filter(
            Q(requested_by=request.user) |
            Q(for_user=request.user)
        )
        .select_related("for_user", "requested_by", "approver", "printed_by", "issued_by")
        .order_by("-created_at")
    )

    # Filters
    status = (request.GET.get("status") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()
    q = (request.GET.get("q") or "").strip()

    if status:
        qs = qs.filter(status=status)

    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)

    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    if q:
        qs = qs.filter(
            Q(reason__icontains=q) |
            Q(for_user__first_name__icontains=q) |
            Q(for_user__last_name__icontains=q) |
            Q(for_user__username__icontains=q) |
            Q(requested_by__first_name__icontains=q) |
            Q(requested_by__last_name__icontains=q) |
            Q(requested_by__username__icontains=q)
        )

    # Stats
    total_requests = qs.count()
    pending_count = qs.filter(status__in=["submitted", "photo_pending"]).count()
    printed_count = qs.filter(status="printed").count()
    issued_count = qs.filter(status="issued").count()
    rejected_count = qs.filter(status="rejected").count()
    approved_count = qs.filter(status__in=["photo_pending", "printed", "issued"]).count()

    # Pagination
    paginator = Paginator(qs, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "total_requests": total_requests,
        "pending_count": pending_count,
        "approved_count": approved_count,
        "printed_count": printed_count,
        "issued_count": issued_count,
        "rejected_count": rejected_count,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
        "q": q,
    }
    return render(request, "hr/my_id_card_requests.html", context)


@login_required
def my_id_card_request_detail(request, pk):
    """
    Show full details for a single ID card request belonging to the
    currently logged-in user.
    """
    card_request = get_object_or_404(
        EmployeeIDCardRequest,
        Q(requested_by=request.user) |
        Q(for_user=request.user),
        pk=pk,
    )
    return render(
        request,
        "hr/my_id_card_request_detail.html",
        {"card_request": card_request},
    )


# ---------------------------------------------------------------------------
# Admin / HR / LSA / SOC views
# ---------------------------------------------------------------------------

@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_for_user(request):
    """
    HR/LSA/SOC initiate a card request for any employee.

    - LSA / SOC / superuser: can pick any active user
    - Agency HR: ONLY staff in their own agency

    Optional ?user=<id> to preselect the employee.
    """
    role = getattr(request.user, "role", "")
    is_agency_hr = (role == "agency_hr")
    has_agency = getattr(request.user, "agency_id", None) is not None

    # Base queryset for for_user field
    if is_agency_hr and has_agency:
        base_user_qs = User.objects.filter(
            agency=request.user.agency,
            is_active=True,
        )
    else:
        base_user_qs = User.objects.filter(is_active=True)

    # Optional preselect via ?user=<id>
    preselect_id = request.GET.get("user")
    initial = {}
    if preselect_id:
        target = base_user_qs.filter(pk=preselect_id).first()
        if target:
            initial["for_user"] = target

    if request.method == "POST":
        form = EmployeeIDCardAdminRequestForm(request.POST, request.FILES)
        # Limit choices for Agency HR on POST
        if "for_user" in form.fields:
            form.fields["for_user"].queryset = base_user_qs

        if form.is_valid():
            obj = form.save(commit=False)
            obj.requested_by = request.user

            # Extra safety check for Agency HR: only their own agency
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
                subject = (
                    f"ID Card {obj.get_request_type_display()} request created "
                    f"for {obj.for_user.get_full_name() or obj.for_user.username}"
                )
                msg = (
                    f"An ID card {obj.get_request_type_display()} request was created.\n\n"
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
    """
    Admin / LSA / SOC / Agency HR list of all requests.
    Agency HR only sees requests for their own agency.
    """
    qs = get_idcard_qs_for_user(request.user).select_related(
        "for_user", "requested_by", "approver"
    ).order_by("-created_at")

    status = (request.GET.get("status") or "").strip()
    if status:
        qs = qs.filter(status=status)

    return render(request, "hr/idcard_request_list.html", {
        "requests": qs,
        "status_filter": status,
    })


# ---------------------------------------------------------------------------
# Download attached request form (single, permission-checked view)
# ---------------------------------------------------------------------------

@login_required
def idcard_request_download_form(request, pk):
    """
    Allow access to the attached request form with proper permissions:

    - Owner (for_user) or requester (requested_by)
    - LSA / SOC / superuser
    - Agency HR for same agency as the employee
    """
    obj = get_object_or_404(EmployeeIDCardRequest, pk=pk)
    user = request.user
    role = getattr(user, "role", "")

    allowed = False

    # Owner / requester
    if user == obj.for_user or user == obj.requested_by:
        allowed = True
    # LSA & SOC & superuser
    elif role in ("lsa", "soc") or user.is_superuser:
        allowed = True
    # Agency HR for same agency
    elif role == "agency_hr" and user.agency_id == getattr(obj.for_user, "agency_id", None):
        allowed = True

    if not allowed:
        return HttpResponseForbidden("You are not allowed to access this file.")

    if not obj.request_form:
        raise Http404("No form uploaded for this request.")

    return FileResponse(
        obj.request_form.open("rb"),
        as_attachment=True,
        filename=obj.request_form_filename or "request_form",
    )


# ---------------------------------------------------------------------------
# Edit / approve / reject / printed / issued / detail (admin side)
# ---------------------------------------------------------------------------

@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_edit(request, pk):
    """
    Edit an existing ID card request (for_user, request_type, reason, request_form).
    LSA / SOC can edit all;
    Agency HR can only edit requests for staff in their own agency.
    """
    qs = get_idcard_qs_for_user(request.user)
    obj = get_object_or_404(qs, pk=pk)

    if request.method == "POST":
        form = EmployeeIDCardAdminRequestForm(
            request.POST,
            request.FILES,
            instance=obj,
        )

        # Limit employee choices for Agency HR
        if getattr(request.user, "role", "") == "agency_hr":
            form.fields["for_user"].queryset = User.objects.filter(
                is_active=True,
                agency=request.user.agency,
            ).order_by("last_name", "first_name")

        if form.is_valid():
            form.save()
            messages.success(request, "ID card request updated.")
            return redirect("accounts:idcard_request_detail", pk=obj.pk)
    else:
        form = EmployeeIDCardAdminRequestForm(instance=obj)

        # Limit employee choices for Agency HR
        if getattr(request.user, "role", "") == "agency_hr":
            form.fields["for_user"].queryset = User.objects.filter(
                is_active=True,
                agency=request.user.agency,
            ).order_by("last_name", "first_name")

    return render(
        request,
        "hr/idcard_admin_request_form.html",
        {
            "form": form,
            "obj": obj,
            "is_edit": True,
        },
    )


@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_approve(request, pk):
    """
    Step 1: LSA / SOC / HR marks the request as 'Pending Photo Capture'
    and notifies the user to come and take a picture.
    """
    qs = get_idcard_qs_for_user(request.user)
    obj = get_object_or_404(qs, pk=pk)

    obj.mark_call_for_photo(request.user)

    subject = "Your ID card request is ready for photo capture"
    msg = (
        f"Your {obj.get_request_type_display()} request for your employee ID card "
        f"has been reviewed.\n\n"
        f"Status: {obj.get_status_display()}\n"
        f"Action required: Please report to Security/HR for photo capture "
        f"at the next available opportunity."
    )
    notify_users_direct([obj.for_user, obj.requested_by], subject, msg)

    messages.success(request, "Request moved to 'Pending Photo Capture'.")
    return redirect("accounts:idcard_request_list")


@login_required
@user_passes_test(is_lsa_soc_or_hr)
def idcard_request_reject(request, pk):
    """
    Reject a request.
    """
    qs = get_idcard_qs_for_user(request.user)
    obj = get_object_or_404(qs, pk=pk)

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
    qs = get_idcard_qs_for_user(request.user)
    obj = get_object_or_404(qs, pk=pk)

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
    qs = get_idcard_qs_for_user(request.user)
    obj = get_object_or_404(qs, pk=pk)

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
    qs = get_idcard_qs_for_user(request.user).select_related(
        "for_user",
        "requested_by",
        "approver",
        "printed_by",
        "issued_by",
    )

    obj = get_object_or_404(qs, pk=pk)

    return render(request, "hr/idcard_request_detail.html", {"obj": obj})
