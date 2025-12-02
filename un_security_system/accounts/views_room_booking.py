from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import ListView, DetailView, CreateView
from django.urls import reverse_lazy, reverse
from django.contrib import messages
from django.utils import timezone
from datetime import datetime
import threading

from django.conf import settings
from django.core.mail import send_mail

from .models import Room, RoomBooking, RoomApprover
from .forms import RoomBookingForm, RoomBookingApprovalForm


# ======================= EMAIL HELPERS =======================

def _send_email_async(subject, message, recipients):
    """
    Send email in the background using a thread.
    """
    recipients = [e for e in recipients if e]  # filter empty
    if not recipients:
        return

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)
    if not from_email:
        return

    def _task():
        try:
            send_mail(subject, message, from_email, recipients, fail_silently=True)
        except Exception:
            # Optionally log here
            pass

    # fire-and-forget background thread
    threading.Thread(target=_task, daemon=True).start()


def notify_approvers_new_booking(booking):
    approver_emails = list(
        RoomApprover.objects.filter(
            room=booking.room,
            is_active=True,
        ).select_related("user").values_list("user__email", flat=True)
    )

    requester_name = booking.requested_by.get_full_name() or booking.requested_by.username
    subject = f"[Room Booking] New request for {booking.room.name} on {booking.date}"
    message = (
        f"Dear Approver,\n\n"
        f"A new room booking request has been submitted for:\n\n"
        f"  Room: {booking.room.name} ({booking.room.code})\n"
        f"  Date: {booking.date}\n"
        f"  Time: {booking.start_time} – {booking.end_time}\n"
        f"  Title: {booking.title}\n"
        f"  Requested by: {requester_name}\n\n"
        f"Please log into the booking system and review this request under 'My Approvals'.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, approver_emails)  # 👈


def notify_requester_booking_submitted(booking):
    if not booking.requested_by.email:
        return

    subject = f"[Room Booking] Request submitted for {booking.room.name} on {booking.date}"
    message = (
        f"Dear {booking.requested_by.get_full_name() or booking.requested_by.username},\n\n"
        f"Your booking request has been submitted and is pending approval.\n\n"
        f"Details:\n"
        f"  Room: {booking.room.name} ({booking.room.code})\n"
        f"  Date: {booking.date}\n"
        f"  Time: {booking.start_time} – {booking.end_time}\n"
        f"  Title: {booking.title}\n"
        f"  Status: Pending approval\n\n"
        f"You will receive another email once your request is approved or rejected.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, [booking.requested_by.email])  # 👈


def notify_requester_booking_approved(booking):
    if not booking.requested_by.email:
        return

    approver_name = booking.approved_by.get_full_name() if booking.approved_by else "Approver"
    subject = f"[Room Booking] Approved: {booking.room.name} on {booking.date}"
    message = (
        f"Dear {booking.requested_by.get_full_name() or booking.requested_by.username},\n\n"
        f"Your room booking request has been APPROVED.\n\n"
        f"Details:\n"
        f"  Room: {booking.room.name} ({booking.room.code})\n"
        f"  Date: {booking.date}\n"
        f"  Time: {booking.start_time} – {booking.end_time}\n"
        f"  Title: {booking.title}\n"
        f"  Approved by: {approver_name}\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, [booking.requested_by.email])  # 👈


def notify_requester_booking_rejected(booking):
    if not booking.requested_by.email:
        return

    approver_name = booking.approved_by.get_full_name() if booking.approved_by else "Approver"
    subject = f"[Room Booking] Rejected: {booking.room.name} on {booking.date}"
    message = (
        f"Dear {booking.requested_by.get_full_name() or booking.requested_by.username},\n\n"
        f"Your room booking request has been REJECTED.\n\n"
        f"Details:\n"
        f"  Room: {booking.room.name} ({booking.room.code})\n"
        f"  Date: {booking.date}\n"
        f"  Time: {booking.start_time} – {booking.end_time}\n"
        f"  Title: {booking.title}\n"
        f"  Rejected by: {approver_name}\n\n"
        f"Reason provided:\n"
        f"{booking.rejection_reason or 'No reason provided.'}\n\n"
        f"If you have questions, please contact the approver or the ICT/administration team.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, [booking.requested_by.email])  # 👈



# ======================= VIEWS =======================


class RoomListView(LoginRequiredMixin, ListView):
    model = Room
    template_name = "accounts/rooms/room_list.html"
    context_object_name = "rooms"

    def get_queryset(self):
        return Room.objects.filter(is_active=True).order_by("name")

class RoomDetailView(LoginRequiredMixin, DetailView):
    model = Room
    template_name = "accounts/rooms/room_detail.html"
    context_object_name = "room"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        room = self.object
        user = self.request.user

        today = timezone.localdate()

        # ---------- APPROVED BOOKINGS (UPCOMING) ----------
        approved_bookings = list(
            room.bookings.filter(
                status="approved",
                date__gte=today,
            )
            .select_related("requested_by", "approved_by")
            .order_by("date", "start_time")
        )

        # compute duration & class
        for b in approved_bookings:
            minutes = 0
            if b.date and b.start_time and b.end_time:
                try:
                    start_dt = datetime.combine(b.date, b.start_time)
                    end_dt = datetime.combine(b.date, b.end_time)
                    duration = end_dt - start_dt
                    minutes = max(int(duration.total_seconds() // 60), 0)
                except Exception:
                    minutes = 0

            b.duration_minutes = minutes

            if minutes >= 180:
                b.duration_class = "heavy"
            elif minutes >= 60:
                b.duration_class = "medium"
            else:
                b.duration_class = "light"

        # ---------- TODAY'S BOOKINGS FOR TIMELINE ----------
        timeline_bookings_today = (
            room.bookings.filter(
                status="approved",
                date=today,
            )
            .select_related("requested_by")
            .order_by("start_time")
        )

        # ---------- MY PENDING BOOKINGS (SIDEBAR) ----------
        my_pending_bookings = (
            room.bookings.filter(
                status="pending",
                requested_by=user,
                date__gte=today,
            )
            .select_related("requested_by")
            .order_by("date", "start_time")
        )

        # ---------- EXTRA STATS FOR HEADER ----------
        bookings_today_count = timeline_bookings_today.count()
        utilization_rate = None  # placeholder

        # ---------- APPROVER FLAG (using RoomApprover) ----------
        is_approver = (
            user.is_superuser
            or RoomApprover.objects.filter(
                room=room,
                user=user,
                is_active=True,
            ).exists()
        )

        timeline_hours = range(8, 20)  # 08:00–19:00

        ctx["approved_bookings"] = approved_bookings
        ctx["timeline_bookings_today"] = timeline_bookings_today  # 👈 NEW
        ctx["my_pending_bookings"] = my_pending_bookings
        ctx["bookings_today"] = bookings_today_count
        ctx["utilization_rate"] = utilization_rate
        ctx["is_approver"] = is_approver
        ctx["timeline_hours"] = timeline_hours

        return ctx



class MyRoomBookingsView(LoginRequiredMixin, ListView):
    """
    Show all bookings requested by the current user.

    - Default: all their bookings, newest first
    - ?status=pending/approved/rejected/cancelled
    - ?when=upcoming/past/today
    """
    model = RoomBooking
    template_name = "accounts/rooms/my_bookings.html"
    context_object_name = "bookings"
    paginate_by = 20

    def get_queryset(self):
        user = self.request.user
        qs = (
            RoomBooking.objects.filter(requested_by=user)
            .select_related("room")
            .order_by("-date", "-start_time")
        )

        # --- Status filter ---
        status = (self.request.GET.get("status") or "").strip()
        valid_statuses = {choice[0] for choice in getattr(RoomBooking, "STATUS_CHOICES", [])}
        if status and status in valid_statuses:
            qs = qs.filter(status=status)

        # --- Time filter: upcoming / past / today ---
        when = (self.request.GET.get("when") or "").strip()
        today = timezone.localdate()

        if when == "upcoming":
            qs = qs.filter(date__gte=today)
        elif when == "past":
            qs = qs.filter(date__lt=today)
        elif when == "today":
            qs = qs.filter(date=today)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["when_filter"] = self.request.GET.get("when", "")
        ctx["status_choices"] = getattr(RoomBooking, "STATUS_CHOICES", [])
        return ctx


class RoomBookingCreateView(LoginRequiredMixin, CreateView):
    model = RoomBooking
    form_class = RoomBookingForm
    template_name = "accounts/rooms/booking_form.html"

    def get_initial(self):
        initial = super().get_initial()
        room_id = self.request.GET.get("room")
        if room_id:
            initial["room"] = room_id
        return initial

    def form_valid(self, form):
        form.instance.requested_by = self.request.user
        try:
            form.instance.full_clean()
        except Exception as e:
            form.add_error(None, e)
            return self.form_invalid(form)

        response = super().form_valid(form)

        # 💌 Send notifications
        notify_approvers_new_booking(self.object)
        notify_requester_booking_submitted(self.object)

        messages.success(self.request, "Booking request submitted and awaiting approval.")
        return response

    def get_success_url(self):
        # After creating, go back to the room detail
        return reverse("accounts:room_detail", kwargs={"pk": self.object.room.pk})


class MyRoomApprovalsView(LoginRequiredMixin, ListView):
    """
    Show PENDING bookings where the current user is an active approver
    for that room (via RoomApprover).
    """
    model = RoomBooking
    template_name = "accounts/rooms/approvals_list.html"
    context_object_name = "bookings"

    def get_queryset(self):
        user = self.request.user
        return (
            RoomBooking.objects.filter(
                status="pending",
                room__room_approver_links__user=user,
                room__room_approver_links__is_active=True,
            )
            .select_related("room", "requested_by")
            .order_by("date", "start_time")
            .distinct()
        )


@login_required
def room_booking_approve_view(request, pk):
    booking = get_object_or_404(RoomBooking, pk=pk)
    room = booking.room

    # Only active approvers for that room (or superuser) can approve
    is_approver = RoomApprover.objects.filter(
        room=room,
        user=request.user,
        is_active=True,
    ).exists()

    if not (request.user.is_superuser or is_approver):
        messages.error(request, "You are not an approver for this room.")
        return redirect("accounts:room_detail", pk=room.pk)

    if booking.status not in ("pending",):
        messages.info(request, "This booking is already processed.")
        return redirect("accounts:room_detail", pk=room.pk)

    if request.method == "POST":
        form = RoomBookingApprovalForm(request.POST)
        if form.is_valid():
            action = form.cleaned_data["action"]
            reason = form.cleaned_data["reason"]

            if action == "approve":
                booking.approve(request.user)
                # 💌 notify requester
                notify_requester_booking_approved(booking)
                messages.success(request, "Booking approved.")
            else:
                if not reason.strip():
                    messages.error(request, "Please provide a reason for rejection.")
                    return render(
                        request,
                        "accounts/rooms/booking_approve.html",
                        {
                            "booking": booking,
                            "room": room,
                            "form": form,
                        },
                    )
                booking.reject(request.user, reason=reason)
                # 💌 notify requester
                notify_requester_booking_rejected(booking)
                messages.warning(request, "Booking rejected.")

            return redirect("accounts:room_detail", pk=room.pk)
    else:
        form = RoomBookingApprovalForm()

    return render(
        request,
        "accounts/rooms/booking_approve.html",
        {
            "booking": booking,
            "room": room,
            "form": form,
        },
    )
