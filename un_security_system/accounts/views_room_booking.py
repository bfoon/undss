from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import ListView, DetailView, CreateView, UpdateView
from django.urls import reverse_lazy, reverse
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.utils import timezone
from datetime import datetime, timedelta, date as date_cls
import threading

from django.conf import settings
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db.models import Q, Count, Prefetch
from datetime import date
from django.db import transaction

from .models import Room, RoomBooking, RoomApprover, RoomBookingSeries
from .forms import RoomBookingForm, RoomBookingApprovalForm, RoomForm, RoomSeriesApprovalForm


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

    _send_email_async(subject, message, approver_emails)


def notify_approvers_new_series(series):
    """Notify approvers about a new recurring booking series"""
    approver_emails = list(
        RoomApprover.objects.filter(
            room=series.room,
            is_active=True,
        ).select_related("user").values_list("user__email", flat=True)
    )

    requester_name = series.requested_by.get_full_name() or series.requested_by.username
    freq_display = series.get_frequency_display() if series.frequency else "One-time"
    occurrence_count = series.occurrences.count()

    subject = f"[Room Booking] New recurring series for {series.room.name}"
    message = (
        f"Dear Approver,\n\n"
        f"A new RECURRING room booking series has been submitted:\n\n"
        f"  Room: {series.room.name} ({series.room.code})\n"
        f"  Title: {series.title}\n"
        f"  Frequency: {freq_display}\n"
        f"  Start Date: {series.start_date}\n"
        f"  End Date: {series.end_date or 'No end date'}\n"
        f"  Time: {series.start_time} – {series.end_time}\n"
        f"  Number of occurrences: {occurrence_count}\n"
        f"  Requested by: {requester_name}\n\n"
        f"Please log into the booking system and review this series under 'My Approvals'.\n"
        f"You can approve/reject the entire series at once.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, approver_emails)


def notify_requester_series_submitted(series):
    if not series.requested_by.email:
        return

    occurrence_count = series.occurrences.count()
    freq_display = series.get_frequency_display() if series.frequency else "One-time"

    subject = f"[Room Booking] Recurring series submitted for {series.room.name}"
    message = (
        f"Dear {series.requested_by.get_full_name() or series.requested_by.username},\n\n"
        f"Your recurring booking series has been submitted and is pending approval.\n\n"
        f"Details:\n"
        f"  Room: {series.room.name} ({series.room.code})\n"
        f"  Title: {series.title}\n"
        f"  Frequency: {freq_display}\n"
        f"  Start Date: {series.start_date}\n"
        f"  End Date: {series.end_date or 'No end date'}\n"
        f"  Time: {series.start_time} – {series.end_time}\n"
        f"  Number of occurrences: {occurrence_count}\n"
        f"  Status: Pending approval\n\n"
        f"You will receive another email once your series is approved or rejected.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, [series.requested_by.email])


def notify_requester_series_approved(series):
    if not series.requested_by.email:
        return

    approver_name = series.approved_by.get_full_name() if series.approved_by else "Approver"
    occurrence_count = series.occurrences.count()

    subject = f"[Room Booking] Recurring series APPROVED: {series.room.name}"
    message = (
        f"Dear {series.requested_by.get_full_name() or series.requested_by.username},\n\n"
        f"Your recurring room booking series has been APPROVED.\n\n"
        f"Details:\n"
        f"  Room: {series.room.name} ({series.room.code})\n"
        f"  Title: {series.title}\n"
        f"  Start Date: {series.start_date}\n"
        f"  End Date: {series.end_date or 'No end date'}\n"
        f"  Time: {series.start_time} – {series.end_time}\n"
        f"  Number of occurrences: {occurrence_count}\n"
        f"  Approved by: {approver_name}\n\n"
        f"All {occurrence_count} bookings in this series have been approved.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, [series.requested_by.email])


def notify_requester_series_rejected(series):
    if not series.requested_by.email:
        return

    approver_name = series.approved_by.get_full_name() if series.approved_by else "Approver"

    subject = f"[Room Booking] Recurring series REJECTED: {series.room.name}"
    message = (
        f"Dear {series.requested_by.get_full_name() or series.requested_by.username},\n\n"
        f"Your recurring room booking series has been REJECTED.\n\n"
        f"Details:\n"
        f"  Room: {series.room.name} ({series.room.code})\n"
        f"  Title: {series.title}\n"
        f"  Start Date: {series.start_date}\n"
        f"  End Date: {series.end_date or 'No end date'}\n"
        f"  Time: {series.start_time} – {series.end_time}\n"
        f"  Rejected by: {approver_name}\n\n"
        f"Reason provided:\n"
        f"{series.rejection_reason or 'No reason provided.'}\n\n"
        f"If you have questions, please contact the approver or the ICT/administration team.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, [series.requested_by.email])


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

    _send_email_async(subject, message, [booking.requested_by.email])


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

    _send_email_async(subject, message, [booking.requested_by.email])


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

    _send_email_async(subject, message, [booking.requested_by.email])


def notify_approvers_booking_cancelled(booking):
    """Notify approvers when an approved booking is cancelled."""
    approver_emails = list(
        RoomApprover.objects.filter(
            room=booking.room,
            is_active=True,
        ).select_related("user").values_list("user__email", flat=True)
    )

    requester_name = booking.requested_by.get_full_name() or booking.requested_by.username
    subject = f"[Room Booking] Cancelled: {booking.room.name} on {booking.date}"
    message = (
        f"Dear Approver,\n\n"
        f"A previously approved room booking has been CANCELLED by the requester.\n\n"
        f"Details:\n"
        f"  Room: {booking.room.name} ({booking.room.code})\n"
        f"  Date: {booking.date}\n"
        f"  Time: {booking.start_time} – {booking.end_time}\n"
        f"  Title: {booking.title}\n"
        f"  Cancelled by: {requester_name}\n\n"
        f"The room is now available for this time slot.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, approver_emails)


def notify_approvers_series_cancelled(series, occurrence_count):
    """Notify approvers when an approved recurring series is cancelled."""
    approver_emails = list(
        RoomApprover.objects.filter(
            room=series.room,
            is_active=True,
        ).select_related("user").values_list("user__email", flat=True)
    )

    requester_name = series.requested_by.get_full_name() or series.requested_by.username
    freq_display = series.get_frequency_display() if series.frequency else "One-time"

    subject = f"[Room Booking] Recurring series cancelled: {series.room.name}"
    message = (
        f"Dear Approver,\n\n"
        f"A previously approved recurring booking series has been CANCELLED by the requester.\n\n"
        f"Details:\n"
        f"  Room: {series.room.name} ({series.room.code})\n"
        f"  Title: {series.title}\n"
        f"  Frequency: {freq_display}\n"
        f"  Start Date: {series.start_date}\n"
        f"  End Date: {series.end_date or 'No end date'}\n"
        f"  Time: {series.start_time} – {series.end_time}\n"
        f"  Number of occurrences: {occurrence_count}\n"
        f"  Cancelled by: {requester_name}\n\n"
        f"All {occurrence_count} bookings in this series have been cancelled.\n"
        f"The room is now available for these time slots.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, approver_emails)


# Email notification function for individual occurrence

def notify_approvers_occurrence_cancelled(occurrence):
    """Notify approvers when an individual series occurrence is cancelled."""
    approver_emails = list(
        RoomApprover.objects.filter(
            room=occurrence.room,
            is_active=True,
        ).select_related("user").values_list("user__email", flat=True)
    )

    requester_name = occurrence.series.requested_by.get_full_name() or occurrence.series.requested_by.username
    series_title = occurrence.series.title
    freq_display = occurrence.series.get_frequency_display() if occurrence.series.frequency else "Recurring"

    # Count remaining active occurrences
    active_count = occurrence.series.occurrences.exclude(status='cancelled').count()
    total_count = occurrence.series.occurrences.count()
    cancelled_count = total_count - active_count

    subject = f"[Room Booking] Single occurrence cancelled: {occurrence.room.name} on {occurrence.date}"
    message = (
        f"Dear Approver,\n\n"
        f"A single occurrence from a recurring booking series has been CANCELLED by the requester.\n\n"
        f"Cancelled Occurrence:\n"
        f"  Room: {occurrence.room.name} ({occurrence.room.code})\n"
        f"  Date: {occurrence.date}\n"
        f"  Time: {occurrence.start_time} – {occurrence.end_time}\n"
        f"  Title: {series_title}\n"
        f"  Cancelled by: {requester_name}\n\n"
        f"Series Information:\n"
        f"  Frequency: {freq_display}\n"
        f"  Total occurrences: {total_count}\n"
        f"  Active occurrences: {active_count}\n"
        f"  Cancelled occurrences: {cancelled_count}\n\n"
        f"The room is now available for this specific time slot.\n"
        f"The remaining {active_count} occurrences in the series are still active.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, approver_emails)


def room_has_active_approvers(room) -> bool:
    return room.room_approver_links.filter(is_active=True).exists()


def compute_initial_status(room):
    if room.approval_mode == "auto":
        return "approved"

    if room.approval_mode == "mixed":
        if not room.room_approver_links.filter(is_active=True).exists():
            return "approved"
        return "pending"

    return "pending"


def iter_recurrence_dates(start_date, end_date, frequency, interval=1, weekdays=None):
    """
    weekdays: list[int] for weekly, e.g. [0,2,4]
    """
    if not frequency:
        yield start_date
        return

    if not end_date:
        # safety default: 1 year max if user didn't set end date
        end_date = start_date + timedelta(days=365)

    if frequency == "daily":
        cur = start_date
        step = timedelta(days=interval)
        while cur <= end_date:
            yield cur
            cur += step

    elif frequency == "weekly":
        weekdays = sorted(weekdays or [])
        if not weekdays:
            weekdays = [start_date.weekday()]  # default: same weekday as start

        # start from the week of start_date
        cur = start_date
        while cur <= end_date:
            # for each week, yield selected weekdays
            week_start = cur - timedelta(days=cur.weekday())  # Monday
            for wd in weekdays:
                d = week_start + timedelta(days=wd)
                if d < start_date:
                    continue
                if d > end_date:
                    continue
                yield d
            cur = week_start + timedelta(days=7 * interval)

    elif frequency == "monthly":
        cur = start_date
        day = start_date.day
        while cur <= end_date:
            yield cur
            # advance by N months (safe demonstrates approach; you can use dateutil if installed)
            month = (cur.month - 1 + interval)
            year = cur.year + (month // 12)
            month = (month % 12) + 1
            # clamp day
            new_day = min(day, 28)
            cur = date_cls(year, month, new_day)

    elif frequency == "yearly":
        cur = start_date
        while cur <= end_date:
            yield cur
            cur = date_cls(cur.year + interval, cur.month, min(cur.day, 28))


# ======================= VIEWS =======================


class RoomListView(LoginRequiredMixin, ListView):
    model = Room
    template_name = "accounts/rooms/room_list.html"
    context_object_name = "rooms"

    def get_queryset(self):
        return Room.objects.filter(is_active=True).prefetch_related("amenities")


class RoomDetailView(LoginRequiredMixin, DetailView):
    model = Room
    template_name = "accounts/rooms/room_detail.html"
    context_object_name = "room"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        room = self.object

        # Upcoming approved bookings (next 30 days)
        today = timezone.localtime().date()
        future_limit = today + timedelta(days=30)

        ctx["upcoming_bookings"] = (
            room.bookings.filter(
                date__gte=today,
                date__lte=future_limit,
                status="approved",
            )
            .select_related("requested_by")
            .order_by("date", "start_time")
        )

        return ctx


class MyRoomBookingsView(LoginRequiredMixin, ListView):
    """
    Enhanced view showing user's bookings with:
    - Recurring series grouped as one item
    - Pagination
    - Search functionality
    - Advanced filters
    """
    template_name = "accounts/rooms/my_bookings.html"
    context_object_name = "items"
    paginate_by = 10  # Number of items per page

    def get_queryset(self):
        user = self.request.user

        # Get all bookings for this user
        bookings_qs = RoomBooking.objects.filter(
            requested_by=user
        ).select_related('room', 'series', 'approved_by')

        # Get all series for this user
        series_qs = RoomBookingSeries.objects.filter(
            requested_by=user
        ).select_related('room', 'approved_by').annotate(
            occurrence_count=Count('occurrences')
        ).prefetch_related(
            Prefetch(
                'occurrences',
                queryset=RoomBooking.objects.order_by('date', 'start_time')
            )
        )

        # Apply filters
        status_filter = self.request.GET.get('status', '')
        when_filter = self.request.GET.get('when', '')
        search_query = self.request.GET.get('search', '')
        date_from = self.request.GET.get('date_from', '')
        date_to = self.request.GET.get('date_to', '')
        room_filter = self.request.GET.get('room', '')

        # Status filter
        if status_filter:
            bookings_qs = bookings_qs.filter(status=status_filter)
            series_qs = series_qs.filter(status=status_filter)

        # Time filter
        today = date.today()
        if when_filter == 'upcoming':
            bookings_qs = bookings_qs.filter(date__gte=today)
            series_qs = series_qs.filter(
                Q(end_date__gte=today) | Q(end_date__isnull=True)
            )
        elif when_filter == 'past':
            bookings_qs = bookings_qs.filter(date__lt=today)
            series_qs = series_qs.filter(end_date__lt=today)

        # Search filter
        if search_query:
            bookings_qs = bookings_qs.filter(
                Q(title__icontains=search_query) |
                Q(description__icontains=search_query) |
                Q(room__name__icontains=search_query)
            )
            series_qs = series_qs.filter(
                Q(title__icontains=search_query) |
                Q(description__icontains=search_query) |
                Q(room__name__icontains=search_query)
            )

        # Date range filter
        if date_from:
            bookings_qs = bookings_qs.filter(date__gte=date_from)
            series_qs = series_qs.filter(start_date__gte=date_from)

        if date_to:
            bookings_qs = bookings_qs.filter(date__lte=date_to)
            series_qs = series_qs.filter(
                Q(end_date__lte=date_to) | Q(end_date__isnull=True)
            )

        # Room filter
        if room_filter:
            bookings_qs = bookings_qs.filter(room_id=room_filter)
            series_qs = series_qs.filter(room_id=room_filter)

        # Get standalone bookings (not part of any series)
        standalone_bookings = bookings_qs.filter(series__isnull=True)

        # Create combined list with type markers
        items = []

        # Add series
        for series in series_qs:
            items.append({
                'is_series': True,
                'series': series,
                'occurrence_count': series.occurrence_count,
                'occurrences': series.occurrences.all(),
                'sort_date': series.start_date,
                'sort_created': series.created_at,
                'title': series.title,
            })

        # Add standalone bookings
        for booking in standalone_bookings:
            items.append({
                'is_series': False,
                **booking.__dict__,
                'room': booking.room,
                'approved_by': booking.approved_by,
                'sort_date': booking.date,
                'sort_created': booking.created_at,
                'title': booking.title,
            })

        # Sort
        sort_by = self.request.GET.get('sort', '-created_at')

        if sort_by == 'date':
            items.sort(key=lambda x: x['sort_date'])
        elif sort_by == '-date':
            items.sort(key=lambda x: x['sort_date'], reverse=True)
        elif sort_by == 'title':
            items.sort(key=lambda x: x['title'].lower())
        elif sort_by == '-title':
            items.sort(key=lambda x: x['title'].lower(), reverse=True)
        elif sort_by == 'created_at':
            items.sort(key=lambda x: x['sort_created'])
        else:  # -created_at (default)
            items.sort(key=lambda x: x['sort_created'], reverse=True)

        # Convert to objects that can be used in templates
        class Item:
            def __init__(self, data):
                for key, value in data.items():
                    setattr(self, key, value)

        return [Item(item) for item in items]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        # Get all bookings and series for stats
        all_bookings = RoomBooking.objects.filter(requested_by=user)
        all_series = RoomBookingSeries.objects.filter(requested_by=user)

        today = date.today()

        # Calculate stats
        ctx['total_bookings'] = all_bookings.count() + all_series.count()
        ctx['pending_count'] = (
                all_bookings.filter(status='pending', series__isnull=True).count() +
                all_series.filter(status='pending').count()
        )
        ctx['approved_count'] = (
                all_bookings.filter(status='approved', series__isnull=True).count() +
                all_series.filter(status='approved').count()
        )
        ctx['upcoming_count'] = (
                all_bookings.filter(
                    date__gte=today,
                    status='approved',
                    series__isnull=True
                ).count() +
                all_series.filter(
                    status='approved'
                ).filter(
                    Q(end_date__gte=today) | Q(end_date__isnull=True)
                ).count()
        )
        ctx['series_count'] = all_series.count()

        # Pass filter values
        ctx['status_filter'] = self.request.GET.get('status', '')
        ctx['when_filter'] = self.request.GET.get('when', '')
        ctx['search_query'] = self.request.GET.get('search', '')
        ctx['date_from'] = self.request.GET.get('date_from', '')
        ctx['date_to'] = self.request.GET.get('date_to', '')
        ctx['room_filter'] = self.request.GET.get('room', '')
        ctx['sort_by'] = self.request.GET.get('sort', '-created_at')

        # Get available rooms for filter dropdown
        ctx['available_rooms'] = Room.objects.filter(is_active=True).order_by('name')

        return ctx


class RoomBookingCreateView(LoginRequiredMixin, CreateView):
    model = RoomBooking
    form_class = RoomBookingForm
    template_name = "accounts/rooms/booking_form.html"

    def get_success_url(self):
        return reverse("accounts:my_bookings")

    def form_valid(self, form):
        user = self.request.user
        room = form.cleaned_data["room"]

        status = compute_initial_status(room)

        # Check if recurring
        frequency = form.cleaned_data.get("frequency")
        if frequency:
            # ---- recurring booking ----
            until = form.cleaned_data.get("until")
            interval = form.cleaned_data.get("interval", 1)
            weekdays_raw = form.cleaned_data.get("weekdays", [])
            weekdays = [int(x) for x in weekdays_raw] if weekdays_raw else []

            with transaction.atomic():
                # Create series with approval status
                series = RoomBookingSeries.objects.create(
                    room=room,
                    requested_by=user,
                    title=form.cleaned_data["title"],
                    description=form.cleaned_data.get("description", ""),
                    start_date=form.cleaned_data["date"],
                    end_date=until,
                    start_time=form.cleaned_data["start_time"],
                    end_time=form.cleaned_data["end_time"],
                    frequency=frequency,
                    interval=interval,
                    weekdays_csv=",".join(str(x) for x in (weekdays or [])),
                    status=status,  # NEW: Set series status
                )

                created = 0
                for d in iter_recurrence_dates(series.start_date, series.end_date, frequency, interval, weekdays):
                    b = RoomBooking(
                        series=series,
                        room=room,
                        title=series.title,
                        description=series.description,
                        date=d,
                        start_time=series.start_time,
                        end_time=series.end_time,
                        status=status,
                        requested_by=user,
                    )
                    b.full_clean()
                    b.save()
                    created += 1

                # Send notifications
                if status == "pending":
                    notify_approvers_new_series(series)  # NEW: Series notification
                notify_requester_series_submitted(series)  # NEW: Series notification

                if status == "approved":
                    messages.success(self.request,
                                     f"Recurring booking created ({created} occurrences) and auto-approved.")
                else:
                    messages.success(self.request,
                                     f"Recurring booking series created ({created} occurrences) and awaiting approval.")

                return redirect("accounts:room_detail", pk=room.pk)

        # ---- non-recurring (single booking) ----
        form.instance.requested_by = user
        form.instance.status = status

        form.instance.full_clean()
        response = super().form_valid(form)

        if status == "pending":
            notify_approvers_new_booking(self.object)
            messages.success(self.request, "Booking request submitted and awaiting approval.")
        else:
            self.object.approve(user=None)
            if getattr(room, "auto_approve_notify_approvers", False):
                notify_approvers_new_booking(self.object)
            messages.success(self.request, "Booking auto-approved.")

        notify_requester_booking_submitted(self.object)
        return response


class MyRoomApprovalsView(LoginRequiredMixin, ListView):
    """
    Show PENDING bookings AND series where the current user is an active approver.
    """
    template_name = "accounts/rooms/approvals_list.html"
    context_object_name = "items"

    def get_queryset(self):
        """
        Return a combined list of pending individual bookings and pending series.
        We'll mark each with a type field for template rendering.
        """
        user = self.request.user

        # Get pending individual bookings (not part of a series, or series already approved)
        individual_bookings = list(
            RoomBooking.objects.filter(
                status="pending",
                room__room_approver_links__user=user,
                room__room_approver_links__is_active=True,
                series__isnull=True,  # Only non-series bookings
            )
            .select_related("room", "requested_by")
            .order_by("date", "start_time")
            .distinct()
        )

        # Get pending series
        pending_series = list(
            RoomBookingSeries.objects.filter(
                status="pending",
                room__room_approver_links__user=user,
                room__room_approver_links__is_active=True,
            )
            .select_related("room", "requested_by")
            .annotate(occurrence_count=Count("occurrences"))
            .order_by("start_date", "start_time")
            .distinct()
        )

        # Tag each item with its type
        for booking in individual_bookings:
            booking.item_type = "booking"

        for series in pending_series:
            series.item_type = "series"

        # Combine and sort
        all_items = individual_bookings + pending_series

        # Sort by date (use start_date for series, date for bookings)
        all_items.sort(key=lambda x: (
            x.start_date if hasattr(x, 'start_date') else x.date,
            x.start_time
        ))

        return all_items

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        items = ctx['items']

        # Calculate stats
        total_count = len(items)
        series_count = sum(1 for i in items if hasattr(i, 'item_type') and i.item_type == 'series')
        booking_count = total_count - series_count

        today = timezone.localtime().date()
        urgent_count = sum(1 for i in items if (
                (hasattr(i, 'date') and i.date <= today + timedelta(days=2)) or
                (hasattr(i, 'start_date') and i.start_date <= today + timedelta(days=2))
        ))

        ctx.update({
            'total_count': total_count,
            'series_count': series_count,
            'booking_count': booking_count,
            'urgent_count': urgent_count,
            'today_date': today,  # Add this for template usage
            'today_count': sum(1 for i in items if (
                    (hasattr(i, 'date') and i.date == today) or
                    (hasattr(i, 'start_date') and i.start_date == today)
            )),
            'this_week_count': sum(1 for i in items if (
                    (hasattr(i, 'date') and i.date <= today + timedelta(days=7)) or
                    (hasattr(i, 'start_date') and i.start_date <= today + timedelta(days=7))
            )),
        })

        return ctx


@login_required
def room_booking_approve_view(request, pk):
    """Approve/reject an individual booking"""
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
                notify_requester_booking_rejected(booking)
                messages.warning(request, "Booking rejected.")

            return redirect("accounts:room_approvals")
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


@login_required
def room_series_approve_view(request, pk):
    """Approve/reject an entire booking series"""
    series = get_object_or_404(
        RoomBookingSeries.objects.select_related('room', 'requested_by')
        .annotate(occurrence_count=Count('occurrences')),
        pk=pk
    )
    room = series.room

    # Only active approvers for that room (or superuser) can approve
    is_approver = RoomApprover.objects.filter(
        room=room,
        user=request.user,
        is_active=True,
    ).exists()

    if not (request.user.is_superuser or is_approver):
        messages.error(request, "You are not an approver for this room.")
        return redirect("accounts:room_detail", pk=room.pk)

    if series.status not in ("pending",):
        messages.info(request, "This series is already processed.")
        return redirect("accounts:room_approvals")

    if request.method == "POST":
        form = RoomSeriesApprovalForm(request.POST)
        if form.is_valid():
            action = form.cleaned_data["action"]
            reason = form.cleaned_data["reason"]

            if action == "approve":
                series.approve(request.user)
                notify_requester_series_approved(series)
                messages.success(
                    request,
                    f"Series approved! All {series.occurrence_count} occurrences have been approved."
                )
            else:
                if not reason.strip():
                    messages.error(request, "Please provide a reason for rejection.")
                    return render(
                        request,
                        "accounts/rooms/series_approve.html",
                        {
                            "series": series,
                            "room": room,
                            "form": form,
                        },
                    )
                series.reject(request.user, reason=reason)
                notify_requester_series_rejected(series)
                messages.warning(
                    request,
                    f"Series rejected. All {series.occurrence_count} occurrences have been rejected."
                )

            return redirect("accounts:room_approvals")
    else:
        form = RoomSeriesApprovalForm()

    # Get sample occurrences for preview
    sample_occurrences = series.occurrences.all()[:5]

    return render(
        request,
        "accounts/rooms/series_approve.html",
        {
            "series": series,
            "room": room,
            "form": form,
            "sample_occurrences": sample_occurrences,
        },
    )


@login_required
@require_POST
def cancel_booking(request, pk):
    """
    Cancel a single booking.
    Only the requester can cancel their own bookings.
    Only pending or approved bookings can be cancelled.
    """
    booking = get_object_or_404(RoomBooking, pk=pk)

    # Check permission - only requester can cancel
    if booking.requested_by != request.user:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': 'You can only cancel your own bookings.'
            }, status=403)
        else:
            messages.error(request, 'You can only cancel your own bookings.')
            return redirect('accounts:my_bookings')

    # Check if booking can be cancelled
    if booking.status not in ['pending', 'approved']:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': f'Cannot cancel a {booking.status} booking.'
            }, status=400)
        else:
            messages.error(request, f'Cannot cancel a {booking.status} booking.')
            return redirect('accounts:my_bookings')

    # Check if part of a series
    if booking.series:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': 'This booking is part of a recurring series. Please cancel the entire series instead.'
            }, status=400)
        else:
            messages.error(request,
                           'This booking is part of a recurring series. Please cancel the entire series instead.')
            return redirect('accounts:my_bookings')

    # Store info for notification
    room_name = booking.room.name
    booking_title = booking.title
    booking_date = booking.date
    was_approved = booking.status == 'approved'

    # Cancel the booking
    booking.status = 'cancelled'
    booking.save(update_fields=['status'])

    # Send notification email to approvers if it was approved
    if was_approved:
        notify_approvers_booking_cancelled(booking)

    # Return response
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Booking cancelled successfully.',
            'booking_id': booking.id
        })
    else:
        messages.success(
            request,
            f'Booking "{booking_title}" for {room_name} on {booking_date} has been cancelled.'
        )
        return redirect('accounts:my_bookings')


@login_required
@require_POST
def cancel_booking_series(request, pk):
    """
    Cancel an entire recurring booking series.
    Only the requester can cancel their own series.
    Only pending or approved series can be cancelled.
    """
    series = get_object_or_404(
        RoomBookingSeries.objects.select_related('room')
        .annotate(occurrence_count=Count('occurrences')),
        pk=pk
    )

    # Check permission - only requester can cancel
    if series.requested_by != request.user:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': 'You can only cancel your own bookings.'
            }, status=403)
        else:
            messages.error(request, 'You can only cancel your own bookings.')
            return redirect('accounts:my_bookings')

    # Check if series can be cancelled
    if series.status not in ['pending', 'approved']:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': f'Cannot cancel a {series.status} series.'
            }, status=400)
        else:
            messages.error(request, f'Cannot cancel a {series.status} series.')
            return redirect('accounts:my_bookings')

    # Store info for notification
    room_name = series.room.name
    series_title = series.title
    occurrence_count = series.occurrence_count
    was_approved = series.status == 'approved'

    # Cancel the series and all its occurrences
    with transaction.atomic():
        series.status = 'cancelled'
        series.save(update_fields=['status'])

        # Cancel all occurrences
        series.occurrences.update(status='cancelled')

    # Send notification email to approvers if it was approved
    if was_approved:
        notify_approvers_series_cancelled(series, occurrence_count)

    # Return response
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Series with {occurrence_count} occurrences cancelled successfully.',
            'series_id': series.id,
            'occurrence_count': occurrence_count
        })
    else:
        messages.success(
            request,
            f'Recurring series "{series_title}" for {room_name} with {occurrence_count} occurrences has been cancelled.'
        )
        return redirect('accounts:my_bookings')


@login_required
@require_POST
def cancel_series_occurrence(request, pk):
    """
    Cancel a single occurrence within a recurring series.
    Only the requester can cancel their own bookings.
    Only pending or approved occurrences can be cancelled.
    """
    occurrence = get_object_or_404(
        RoomBooking.objects.select_related('room', 'series', 'series__requested_by'),
        pk=pk
    )

    # Check if this booking is part of a series
    if not occurrence.series:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': 'This is not part of a recurring series. Use the regular cancel function.'
            }, status=400)
        else:
            messages.error(request, 'This is not part of a recurring series.')
            return redirect('accounts:my_bookings')

    # Check permission - only requester can cancel
    if occurrence.series.requested_by != request.user:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': 'You can only cancel your own bookings.'
            }, status=403)
        else:
            messages.error(request, 'You can only cancel your own bookings.')
            return redirect('accounts:my_bookings')

    # Check if occurrence can be cancelled
    if occurrence.status not in ['pending', 'approved']:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': f'Cannot cancel a {occurrence.status} occurrence.'
            }, status=400)
        else:
            messages.error(request, f'Cannot cancel a {occurrence.status} occurrence.')
            return redirect('accounts:my_bookings')

    # Store info for notification
    room_name = occurrence.room.name
    occurrence_date = occurrence.date
    occurrence_time = f"{occurrence.start_time} – {occurrence.end_time}"
    series_title = occurrence.series.title
    was_approved = occurrence.status == 'approved'

    # Cancel the occurrence
    occurrence.status = 'cancelled'
    occurrence.save(update_fields=['status'])

    # Check if all occurrences are now cancelled
    series = occurrence.series
    active_occurrences = series.occurrences.exclude(status='cancelled').count()

    # If no active occurrences left, mark series as cancelled too
    if active_occurrences == 0:
        series.status = 'cancelled'
        series.save(update_fields=['status'])

    # Send notification email to approvers if it was approved
    if was_approved:
        notify_approvers_occurrence_cancelled(occurrence)

    # Return response
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Occurrence on {occurrence_date} cancelled successfully.',
            'occurrence_id': occurrence.id,
            'occurrence_date': str(occurrence_date),
            'active_occurrences': active_occurrences,
            'series_cancelled': active_occurrences == 0
        })
    else:
        messages.success(
            request,
            f'Occurrence of "{series_title}" on {occurrence_date} has been cancelled. '
            f'The room is now available for this time slot.'
        )
        return redirect('accounts:my_bookings')

@method_decorator(staff_member_required, name='dispatch')
class RoomCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    """Create a new room (superuser only)."""
    model = Room
    form_class = RoomForm
    template_name = "accounts/rooms/room_form.html"
    success_url = reverse_lazy("accounts:room_list")

    def test_func(self):
        return self.request.user.is_superuser

    def form_valid(self, form):
        messages.success(self.request, f"Room '{form.instance.name}' created successfully!")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['page_title'] = 'Add New Room'
        ctx['submit_text'] = 'Create Room'
        return ctx


@method_decorator(staff_member_required, name='dispatch')
class RoomUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    """Edit an existing room (superuser only)."""
    model = Room
    form_class = RoomForm
    template_name = "accounts/rooms/room_form.html"

    def test_func(self):
        return self.request.user.is_superuser

    def form_valid(self, form):
        messages.success(self.request, f"Room '{form.instance.name}' updated successfully!")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("accounts:room_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['page_title'] = f'Edit Room: {self.object.name}'
        ctx['submit_text'] = 'Update Room'
        ctx['is_edit'] = True
        return ctx


@staff_member_required
def room_delete_view(request, pk):
    """Delete/deactivate a room (superuser only)."""
    if not request.user.is_superuser:
        messages.error(request, "You don't have permission to delete rooms.")
        return redirect("accounts:room_list")

    room = get_object_or_404(Room, pk=pk)

    if request.method == "POST":
        room_name = room.name
        # Soft delete - just deactivate
        room.is_active = False
        room.save()
        messages.warning(request, f"Room '{room_name}' has been deactivated.")
        return redirect("accounts:room_list")

    return render(request, "accounts/rooms/room_confirm_delete.html", {"room": room})