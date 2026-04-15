from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import ListView, DetailView, CreateView, UpdateView
from django.urls import reverse_lazy, reverse
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.contrib import messages
from django.utils import timezone
from datetime import datetime, timedelta, date as date_cls
from datetime import datetime, date, time
import threading
from django.shortcuts import render

from django.conf import settings
from django.core.mail import send_mail, EmailMessage, EmailMultiAlternatives
from .utils import generate_booking_ics
from django.core.paginator import Paginator
from django.db.models import Q, Count, Prefetch
from datetime import date
from django.db import transaction

from .models import Room, RoomBooking, RoomApprover, RoomBookingSeries, MeetingAttendee
from .forms import RoomBookingForm, RoomBookingApprovalForm, RoomForm, RoomSeriesApprovalForm, MeetingAttendeeForm

# ICT focal lookup — graceful so the file works if AgencyAssetRoles isn't present.
try:
    from .models import AgencyAssetRoles, User as _User

    _HAS_AGENCY_ROLES = True
except ImportError:
    _HAS_AGENCY_ROLES = False


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

    approver_name = (
        series.approved_by.get_full_name() or series.approved_by.username
        if series.approved_by
        else "System (auto-approved)"
    )
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
        f"Please remember to leave the room neat and tidy as you found it.\n\n"
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
    registration_link = request.build_absolute_uri(
        reverse('accounts:meeting_registration', args=[booking.registration_code]))

    approver_name = (
        booking.approved_by.get_full_name() or booking.approved_by.username
        if booking.approved_by
        else "System (auto-approved)"
    )
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
        f"Attendee Registration Link: {registration_link}\n\n"
        f"Please remember to leave the room neat and tidy as you found it.\n\n"
        f"Thank you."
    )

    _send_email_async(subject, message, [booking.requested_by.email])

def notify_attendee_of_registration(attendee):
    subject = f"Registration Confirmed: {attendee.booking.title}"
    message = (
        f"Dear {attendee.name},\n\n"
        f"Thank you for registering for the meeting: '{attendee.booking.title}'.\n\n"
        f"Date: {attendee.booking.date.strftime('%A, %B %d, %Y')}\n"
        f"Time: {attendee.booking.start_time.strftime('%I:%M %p')}\n"
        f"Room: {attendee.booking.room.name}\n\n"
        "We look forward to seeing you."
    )
    _send_email_async(subject, message, [attendee.email])

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


def send_booking_calendar_invite(booking):
    """
    Sends a calendar invite to the room resource, the requester, and all guests.
    """
    # Do not send an invite if the room is not configured for calendar synchronization.
    if not getattr(booking.room, 'calendar_sync_enabled', False):
        return

    # Generate the .ics calendar file content
    ics_bytes = generate_booking_ics(booking)

    # --- Build the list of all recipients ---
    recipients = set()

    # 1. Add the requester
    if booking.requested_by and booking.requested_by.email:
        recipients.add(booking.requested_by.email)

    # 2. Add the room's resource email (if it exists)
    if booking.room.resource_email:
        recipients.add(booking.room.resource_email)

    # 3. Add all guest emails
    if booking.attendee_emails:
        guest_emails = [email.strip() for email in booking.attendee_emails.split(',') if email.strip()]
        recipients.update(guest_emails)

    if not recipients:
        return # No one to send to

    # --- Create and send the email ---
    subject = f"Booking: {booking.title} ({booking.room.name})"
    body = "This email contains a calendar invitation for your meeting."
    from_email = settings.DEFAULT_FROM_EMAIL

    msg = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=from_email,
        to=list(recipients) # Convert set to list for sending
    )

    # Attach the calendar data. The content type is crucial for Outlook/O365.
    msg.attach("invite.ics", ics_bytes, "text/calendar; method=REQUEST")

    # Send the email in a background thread to not block the web request
    threading.Thread(target=lambda: msg.send(fail_silently=True)).start()



def _get_ict_emails_for_booking(booking):
    """
    Collect ICT focal-point emails for a booking.
    Priority order:
      1. Any RoomApprover for this room whose User has role=ict_focal
      2. AgencyAssetRoles.ict_custodian for the requester agency
      3. Any User with role=ict_focal in the same agency (fallback)
    """
    emails = []
    room = booking.room

    # 1. Room-level approvers who are ICT focal
    emails.extend(list(
        RoomApprover.objects.filter(
            room=room,
            is_active=True,
            user__role="ict_focal",
        ).select_related("user").values_list("user__email", flat=True)
    ))

    # 2 & 3. Agency-level ICT roles
    if _HAS_AGENCY_ROLES:
        agency = getattr(booking.requested_by, "agency", None)
        if agency:
            try:
                roles = AgencyAssetRoles.objects.get(agency=agency)
                emails.extend(list(roles.ict_custodian.values_list("email", flat=True)))
            except AgencyAssetRoles.DoesNotExist:
                pass
            emails.extend(list(
                _User.objects.filter(
                    agency=agency, role="ict_focal", is_active=True,
                ).exclude(email="").values_list("email", flat=True)
            ))

    return list(dict.fromkeys(e for e in emails if e))


def notify_ict_support_requested(booking):
    """
    Email ICT focal points when a booking requests ICT support.
    Fires at submission time so ICT can plan ahead.
    """
    ict_emails = _get_ict_emails_for_booking(booking)
    if not ict_emails:
        return

    requester_name = booking.requested_by.get_full_name() or booking.requested_by.username
    support_label = {
        "setup": "Before meeting — Setup / AV configuration",
        "during": "During meeting — Live technical support",
    }.get(getattr(booking, "ict_support", ""), "ICT support requested")

    subject = f"[ICT Support Requested] {booking.room.name} — {booking.date}"
    message = (
        "Dear ICT Team,\n\n"
        "A room booking has been submitted that requires ICT support.\n\n"
        f"  Room:         {booking.room.name} ({booking.room.code})\n"
        f"  Date:         {booking.date}\n"
        f"  Time:         {booking.start_time} – {booking.end_time}\n"
        f"  Title:        {booking.title}\n"
        f"  Requested by: {requester_name}\n"
        f"  ICT Support:  {support_label}\n\n"
        "The booking is currently pending approval. "
        "You may want to reach out to the requester in advance "
        "to clarify any technical requirements.\n\n"
        "Thank you."
    )
    _send_email_async(subject, message, ict_emails)


def notify_ict_support_requested_series(series):
    """Same as notify_ict_support_requested but for a recurring series."""

    class _FakeBooking:
        pass

    fake = _FakeBooking()
    fake.room = series.room
    fake.requested_by = series.requested_by

    ict_emails = _get_ict_emails_for_booking(fake)
    if not ict_emails:
        return

    requester_name = series.requested_by.get_full_name() or series.requested_by.username
    freq_display = series.get_frequency_display() if series.frequency else "One-time"
    support_label = {
        "setup": "Before meeting — Setup / AV configuration",
        "during": "During meeting — Live technical support",
    }.get(getattr(series, "ict_support", ""), "ICT support requested")

    subject = f"[ICT Support Requested] Recurring — {series.room.name}"
    message = (
        "Dear ICT Team,\n\n"
        "A RECURRING room booking series has been submitted that requires ICT support.\n\n"
        f"  Room:          {series.room.name} ({series.room.code})\n"
        f"  Title:         {series.title}\n"
        f"  Frequency:     {freq_display}\n"
        f"  Start Date:    {series.start_date}\n"
        f"  End Date:      {series.end_date or 'No end date'}\n"
        f"  Time:          {series.start_time} – {series.end_time}\n"
        f"  Requested by:  {requester_name}\n"
        f"  ICT Support:   {support_label}\n\n"
        "The series is currently pending approval. "
        "Please coordinate with the requester before the first occurrence.\n\n"
        "Thank you."
    )
    _send_email_async(subject, message, ict_emails)


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


def _nth_weekday_of_month(year, month, weekday, n):
    """
    Return the date of the nth occurrence of `weekday` (0=Mon..6=Sun) in year/month.
    n=1 → first, n=2 → second, n=3 → third, n=4 → fourth, n=-1 → last.
    Returns None if that occurrence doesn't exist (e.g. 5th Monday in a short month).
    """
    import calendar as _cal
    if n == -1:
        last_day = _cal.monthrange(year, month)[1]
        d = date_cls(year, month, last_day)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d
    # Find the first occurrence of weekday in the month
    first = date_cls(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    first_occ = first + timedelta(days=delta)
    target = first_occ + timedelta(weeks=n - 1)
    if target.month != month:
        return None  # e.g. "5th Monday" doesn't exist this month
    return target


def _advance_months(d, interval):
    """Advance date d by `interval` months, clamping day to last day of target month."""
    import calendar as _cal
    m = (d.month - 1 + interval)
    year = d.year + (m // 12)
    month = (m % 12) + 1
    last = _cal.monthrange(year, month)[1]
    return date_cls(year, month, min(d.day, last))


def iter_recurrence_dates(
        start_date, end_date, frequency, interval=1, weekdays=None,
        monthly_type="day", monthly_week=None, monthly_weekday=None,
):
    """
    Yield dates in the recurrence series.

    Parameters
    ----------
    weekdays        : list[int] for weekly, e.g. [0, 2, 4]  (Mon=0 … Sun=6)
    monthly_type    : 'day'     → fixed day-of-month (legacy behaviour)
                      'weekday' → nth weekday of month (e.g. last Thursday)
    monthly_week    : 1/2/3/4/-1  (used when monthly_type='weekday')
    monthly_weekday : 0–6         (used when monthly_type='weekday')
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
        if monthly_type == "weekday" and monthly_week is not None and monthly_weekday is not None:
            # ---- nth weekday of month (e.g. "last Thursday") ----
            cur_year, cur_month = start_date.year, start_date.month
            while True:
                d = _nth_weekday_of_month(cur_year, cur_month, monthly_weekday, monthly_week)
                if d is not None and d >= start_date and d <= end_date:
                    yield d
                # Advance by `interval` months
                m = (cur_month - 1 + interval)
                cur_year = cur_year + (m // 12)
                cur_month = (m % 12) + 1
                # Stop if the start of the next candidate month is already past end_date
                if date_cls(cur_year, cur_month, 1) > end_date:
                    break
        else:
            # ---- fixed day-of-month (legacy) ----
            cur = start_date
            day = start_date.day
            while cur <= end_date:
                yield cur
                cur = _advance_months(date_cls(cur.year, cur.month, day), interval)

    elif frequency == "yearly":
        cur = start_date
        while cur <= end_date:
            yield cur
            import calendar as _cal
            last = _cal.monthrange(cur.year + interval, cur.month)[1]
            cur = date_cls(cur.year + interval, cur.month, min(cur.day, last))


# ======================= VIEWS =======================


class RoomListView(LoginRequiredMixin, ListView):
    model = Room
    template_name = "accounts/rooms/room_list.html"
    context_object_name = "rooms"

    def get_queryset(self):
        return Room.objects.filter(is_active=True).prefetch_related("amenities")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        import json
        from datetime import datetime as _dt

        user = self.request.user
        today = timezone.localtime().date()
        rooms = ctx["rooms"]

        # ── Stats for the banner ──────────────────────────────────────────
        ctx["total_capacity"] = sum(r.capacity or 0 for r in rooms)
        ctx["available_now"] = sum(1 for r in rooms if getattr(r, "is_available_now", False))
        ctx["bookings_today"] = RoomBooking.objects.filter(
            room__in=rooms, date=today, status="approved"
        ).count()

        # ── Pending approvals badge ───────────────────────────────────────
        ctx["pending_approvals"] = RoomBooking.objects.filter(
            status="pending",
            room__room_approver_links__user=user,
            room__room_approver_links__is_active=True,
            series__isnull=True,
        ).distinct().count()

        # ── Per-room today bookings as JSON for live JS ───────────────────
        # Single query — no N+1
        today_bookings = (
            RoomBooking.objects
            .filter(room__in=rooms, date=today, status="approved")
            .values("room_id", "start_time", "end_time", "title")
        )

        bookings_by_room = {}
        for b in today_bookings:
            rid = str(b["room_id"])  # JS uses String(roomId)
            bookings_by_room.setdefault(rid, []).append({
                "start": b["start_time"].strftime("%H:%M"),
                "end": b["end_time"].strftime("%H:%M"),
                "title": b["title"],
            })

        ctx["bookings_by_room_json"] = json.dumps(bookings_by_room)
        return ctx


class RoomDetailView(LoginRequiredMixin, DetailView):
    model = Room
    template_name = "accounts/rooms/room_detail.html"
    context_object_name = "room"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        room = self.object
        user = self.request.user

        today = timezone.localtime().date()
        future_limit = today + timedelta(days=30)

        # ── Approved upcoming bookings (list view) ──────────────────────
        approved_bookings = list(
            room.bookings.filter(
                date__gte=today,
                date__lte=future_limit,
                status="approved",
            )
            .select_related("requested_by")
            .order_by("date", "start_time")
        )

        from datetime import datetime as _dt
        for b in approved_bookings:
            start = _dt.combine(today, b.start_time)
            end = _dt.combine(today, b.end_time)
            mins = max(int((end - start).total_seconds() / 60), 0)
            b.duration_minutes = mins
            b.duration_class = (
                "heavy" if mins > 180 else
                "medium" if mins > 60 else
                "light"
            )

        ctx["approved_bookings"] = approved_bookings
        ctx["upcoming_bookings"] = approved_bookings

        # ── Timeline: today's approved bookings ─────────────────────────
        TIMELINE_START_HOUR = 7
        timeline_bookings = list(
            room.bookings.filter(date=today, status="approved")
            .select_related("requested_by")
            .order_by("start_time")
        )

        for b in timeline_bookings:
            top = (b.start_time.hour - TIMELINE_START_HOUR) * 60 + b.start_time.minute
            start = _dt.combine(today, b.start_time)
            end = _dt.combine(today, b.end_time)
            mins = max(int((end - start).total_seconds() / 60), 15)
            b.timeline_top = max(top, 0)
            b.timeline_height = mins
            b.duration_minutes = mins

        ctx["timeline_bookings_today"] = timeline_bookings
        ctx["timeline_hours"] = list(range(TIMELINE_START_HOUR, 22))

        # ── Sidebar: my pending bookings ────────────────────────────────
        ctx["my_pending_bookings"] = list(
            room.bookings.filter(requested_by=user, status="pending")
            .order_by("date", "start_time")[:5]
        )

        # ── Is current user an approver for this room? ──────────────────
        ctx["is_approver"] = room.room_approver_links.filter(
            user=user, is_active=True
        ).exists()

        # ── Stats ────────────────────────────────────────────────────────
        ctx["bookings_today"] = room.bookings.filter(
            date=today, status="approved"
        ).count()

        total_mins = sum(b.duration_minutes for b in timeline_bookings)
        ctx["utilization_rate"] = min(round(total_mins / 480 * 100), 100)

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

        # --- Automatic Survey Logic ---
        now = timezone.now()
        # Find approved bookings that ended in the last 7 days and haven't been surveyed
        unsurveyed_bookings = RoomBooking.objects.filter(
            requested_by=user,
            status='approved',
            date__lt=now.date(),
            survey_sent_at__isnull=True
        ).select_related('room')

        for booking in unsurveyed_bookings:
            # Placeholder for sending email; in a real app, you'd use a proper function
            print(f"Sending survey for booking #{booking.id} for room {booking.room.name}")
            # notify_user_of_survey(booking) # This function would be defined in your email helpers

            # Mark as surveyed
            booking.survey_sent_at = now
            booking.save(update_fields=['survey_sent_at'])

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

    def get_form_kwargs(self):
        kwargs = super(RoomBookingCreateView, self).get_form_kwargs()
        # Pass the selected room to the form if available
        room_id = self.request.GET.get('room')
        if room_id:
            kwargs['room'] = get_object_or_404(Room, pk=room_id)
        return kwargs

    def get_success_url(self):
        return reverse("accounts:my_bookings")

    def form_valid(self, form):
        user = self.request.user
        room = form.cleaned_data["room"]

        status = compute_initial_status(room)

        # Extract newly added fields
        attendee_emails = form.cleaned_data.get("attendee_emails", "")
        virtual_meeting_link = form.cleaned_data.get("virtual_meeting_link", "")
        selected_amenities = form.cleaned_data.get("selected_amenities")

        # Check if recurring
        frequency = form.cleaned_data.get("frequency")
        if frequency:
            # ---- recurring booking ----
            until = form.cleaned_data.get("until")
            interval = form.cleaned_data.get("interval", 1)
            weekdays_raw = form.cleaned_data.get("weekdays", [])
            weekdays = [int(x) for x in weekdays_raw] if weekdays_raw else []

            # Monthly-weekday fields
            monthly_type = form.cleaned_data.get("monthly_type", "day") or "day"
            monthly_week_raw = form.cleaned_data.get("monthly_week")
            monthly_weekday_raw = form.cleaned_data.get("monthly_weekday")
            monthly_week = int(monthly_week_raw) if monthly_week_raw not in (None, "") else None
            monthly_weekday = int(monthly_weekday_raw) if monthly_weekday_raw not in (None, "") else None

            ict_support = form.cleaned_data.get("ict_support", "none") or "none"

            with transaction.atomic():
                # Create series with approval status AND new fields
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
                    monthly_type=monthly_type,
                    monthly_week=monthly_week,
                    monthly_weekday=monthly_weekday,
                    status=status,
                    ict_support=ict_support,
                    attendee_emails=attendee_emails,
                    virtual_meeting_link=virtual_meeting_link,
                )

                created = 0
                first_booking = None
                for d in iter_recurrence_dates(
                        series.start_date, series.end_date, frequency, interval, weekdays,
                        monthly_type=monthly_type,
                        monthly_week=monthly_week,
                        monthly_weekday=monthly_weekday,
                ):
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
                        ict_support=ict_support,
                        attendee_emails=attendee_emails,
                        virtual_meeting_link=virtual_meeting_link,
                    )
                    b.full_clean()
                    b.save()

                    # Add Many-to-Many selected amenities to each generated booking
                    if selected_amenities:
                        b.selected_amenities.set(selected_amenities)

                    if created == 0:
                        first_booking = b

                    created += 1

                # Send notifications — use the right email for pending vs auto-approved
                if status == "pending":
                    notify_approvers_new_series(series)
                    notify_requester_series_submitted(series)
                else:
                    # Auto-approved: optionally still notify approvers for visibility
                    if getattr(room, "auto_approve_notify_approvers", False):
                        notify_approvers_new_series(series)
                    notify_requester_series_approved(series)

                    # Send Calendar Invite for auto-approved recurring series
                    # (Passing the first booking is sufficient, ICS generation pulls the RRULE from the attached series)
                    if first_booking:
                        send_booking_calendar_invite(first_booking)

                # Notify ICT if support was requested
                if ict_support and ict_support != "none":
                    notify_ict_support_requested_series(series)

                if status == "approved":
                    messages.success(self.request,
                                     f"Recurring booking created ({created} occurrences) and auto-approved. Calendar invites sent.")
                else:
                    messages.success(self.request,
                                     f"Recurring booking series created ({created} occurrences) and awaiting approval.")

                return redirect("accounts:room_detail", pk=room.pk)

        # ---- non-recurring (single booking) ----
        ict_support = form.cleaned_data.get("ict_support", "none") or "none"
        form.instance.requested_by = user
        form.instance.status = status
        form.instance.ict_support = ict_support

        # For single bookings, `attendee_emails`, `virtual_meeting_link` and M2M `selected_amenities`
        # are automatically saved by `super().form_valid(form)` since they are part of the ModelForm.
        form.instance.full_clean()
        response = super().form_valid(form)

        if status == "pending":
            notify_approvers_new_booking(self.object)
            notify_requester_booking_submitted(self.object)
            messages.success(self.request, "Booking request submitted and awaiting approval.")
        else:
            self.object.approve(user=None)
            if getattr(room, "auto_approve_notify_approvers", False):
                notify_approvers_new_booking(self.object)
            notify_requester_booking_approved(self.object)

            # Send Calendar Invite for auto-approved single booking
            send_booking_calendar_invite(self.object)

            messages.success(self.request, "Booking auto-approved. Calendar invites sent.")

        # Notify ICT if support was requested (fires regardless of approval status)
        if ict_support and ict_support != "none":
            notify_ict_support_requested(self.object)

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
    """
    Approve or reject an individual booking and confirm available amenities.
    """
    booking = get_object_or_404(RoomBooking, pk=pk)
    room = booking.room

    # Permission check: User must be a superuser or an active approver for this room.
    is_approver = request.user.is_superuser or RoomApprover.objects.filter(
        room=room,
        user=request.user,
        is_active=True,
    ).exists()

    if not is_approver:
        messages.error(request, "You are not an approver for this room.")
        return redirect("accounts:room_detail", pk=room.pk)

    if booking.status != "pending":
        messages.info(request, "This booking has already been processed.")
        return redirect("accounts:room_approvals")

    if request.method == "POST":
        # We pass the booking instance to the form
        form = RoomBookingApprovalForm(request.POST, instance=booking)

        # The form is valid if the submitted amenity IDs are valid
        if form.is_valid():
            action = request.POST.get('action')  # 'approve' or 'reject' from button name

            if action == "approve":
                # Save the form to update the `approved_amenities` on the booking instance
                booking = form.save()

                # Finalize the approval
                booking.approve(request.user)

                # Send notifications
                notify_requester_booking_approved(request, booking)
                send_booking_calendar_invite(booking)

                messages.success(request, "Booking approved and calendar invite sent.")
                return redirect("accounts:room_approvals")

            elif action == "reject":
                rejection_reason = form.cleaned_data.get('rejection_reason', '').strip()
                if not rejection_reason:
                    messages.error(request, "A reason is required to reject a booking.")
                    # Re-render the page with the error
                    return render(request, "accounts/rooms/booking_approve.html", {
                        "booking": booking, "room": room, "form": form
                    })

                booking.reject(request.user, reason=rejection_reason)
                # notify_requester_booking_rejected(booking) # Assumes this function exists
                messages.warning(request, "Booking has been rejected.")
                return redirect("accounts:room_approvals")

            else:
                messages.error(request, "Invalid action specified.")

    else:
        # For a GET request, initialize the form with the booking instance.
        # This will pre-populate the 'approved_amenities' checklist.
        form = RoomBookingApprovalForm(instance=booking)

    return render(request, "accounts/rooms/booking_approve.html", {
        "booking": booking,
        "room": room,
        "form": form,
    })


@login_required
def room_series_approve_view(request, pk):
    """Approve/reject an entire booking series. This view remains unchanged."""
    series = get_object_or_404(
        RoomBookingSeries.objects.select_related('room', 'requested_by')
        .annotate(occurrence_count=Count('occurrences')),
        pk=pk
    )
    room = series.room

    is_approver = request.user.is_superuser or RoomApprover.objects.filter(
        room=room,
        user=request.user,
        is_active=True,
    ).exists()

    if not is_approver:
        messages.error(request, "You are not an approver for this room.")
        return redirect("accounts:room_detail", pk=room.pk)

    if series.status != "pending":
        messages.info(request, "This series has already been processed.")
        return redirect("accounts:room_approvals")

    if request.method == "POST":
        form = RoomSeriesApprovalForm(request.POST)
        if form.is_valid():
            action = form.cleaned_data["action"]
            reason = form.cleaned_data["reason"]

            if action == "approve":
                series.approve(request.user)
                # notify_requester_series_approved(request, series)

                # Send invite for the first occurrence, which contains the recurrence rule
                first_booking = series.occurrences.order_by('date', 'start_time').first()
                if first_booking:
                    send_booking_calendar_invite(first_booking)

                messages.success(request,
                                 f"Series approved! All {series.occurrences.count()} occurrences have been approved.")
            else:
                if not reason.strip():
                    messages.error(request, "Please provide a reason for rejection.")
                    return render(request, "accounts/rooms/series_approve.html",
                                  {"series": series, "room": room, "form": form})

                series.reject(request.user, reason=reason)
                # notify_requester_series_rejected(series)
                messages.warning(request,
                                 f"Series rejected. All {series.occurrences.count()} occurrences have been rejected.")

            return redirect("accounts:room_approvals")
    else:
        form = RoomSeriesApprovalForm()

    sample_occurrences = series.occurrences.all()[:5]

    return render(request, "accounts/rooms/series_approve.html", {
        "series": series,
        "room": room,
        "form": form,
        "sample_occurrences": sample_occurrences,
    })


@login_required
def booking_detail_view(request, pk):
    booking = get_object_or_404(RoomBooking.objects.prefetch_related('attendees', 'approved_amenities'), pk=pk)
    # Add permission check here if needed (e.g., only requester or approver)

    registration_link = request.build_absolute_uri(
        reverse('accounts:meeting_registration', args=[booking.registration_code])
    )

    return render(request, 'accounts/rooms/booking_detail.html', {
        'booking': booking,
        'registration_link': registration_link
    })


# --- Attendee Registration Views ---

def meeting_registration_view(request, registration_code):
    booking = get_object_or_404(RoomBooking.objects.select_related('room'), registration_code=registration_code)

    if request.method == 'POST':
        form = MeetingAttendeeForm(request.POST)
        if form.is_valid():
            attendee = form.save(commit=False)
            attendee.booking = booking
            try:
                attendee.save()
                notify_attendee_of_registration(attendee)
                messages.success(request, "Thank you for registering! A confirmation email has been sent.")
                return redirect('accounts:meeting_registration_success')
            except IntegrityError:
                messages.error(request, "The email address you entered has already been registered for this meeting.")
    else:
        form = MeetingAttendeeForm()

    return render(request, 'accounts/rooms/meeting_registration.html', {'form': form, 'booking': booking})


def meeting_registration_success_view(request):
    return render(request, 'accounts/rooms/meeting_registration_success.html')


# --- FULLY IMPLEMENTED QR CODE VIEW ---
def meeting_qr_code_view(request, registration_code):
    """
    Generates and returns a PNG image of a QR code for the given meeting registration link.
    """
    # 1. Construct the full URL that the QR code will point to.
    # This URL leads to the attendee registration form.
    registration_url = request.build_absolute_uri(
        reverse('accounts:meeting_registration', args=[registration_code])
    )

    # 2. Configure and generate the QR code image.
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,  # L = Low error correction
        box_size=10,  # Size of each box in pixels
        border=4,  # Width of the border
    )
    qr.add_data(registration_url)
    qr.make(fit=True)

    # Create an image from the QR Code instance
    img = qr.make_image(fill_color="black", back_color="white")

    # 3. Save the image to a memory buffer.
    # We use a BytesIO buffer to avoid saving the file to disk.
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)  # Rewind the buffer to the beginning before reading

    # 4. Create an HTTP response with the image data and the correct content type.
    # This tells the browser to display it as an image.
    return HttpResponse(buffer, content_type="image/png")

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


def _parse_iso_dt(value: str):
    """
    FullCalendar sends ISO datetimes like:
      2026-02-19T00:00:00Z
      2026-02-19T00:00:00+00:00
      2026-02-19T00:00:00
    """
    if not value:
        return None

    value = value.strip()

    # Handle Zulu time (Z)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None

    # If naive, assume current timezone
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())

    return dt


@login_required
def room_bookings_calendar(request):
    """
    Calendar page (Day/Week/Month/List).
    Loads bookings via AJAX from room_bookings_events endpoint.
    """
    rooms = Room.objects.filter(is_active=True).order_by("name")

    # Optional: restrict rooms per agency if you have agency field
    # if hasattr(Room, "agency_id") and request.user.agency_id:
    #     rooms = rooms.filter(agency_id=request.user.agency_id)

    context = {
        "rooms": rooms,
        "status_choices": RoomBooking.STATUS_CHOICES,
    }
    return render(request, "accounts/rooms/room_bookings_calendar.html", context)


@require_GET
@login_required
def room_bookings_events(request):
    """
    JSON endpoint for FullCalendar.
    Query params:
      start=ISO datetime
      end=ISO datetime
      mine=1 (optional)
      room_id= (optional)
      status= (optional)
      future_only=1 (optional default on frontend)
    """
    start_dt = _parse_iso_dt(request.GET.get("start"))
    end_dt = _parse_iso_dt(request.GET.get("end"))

    mine = (request.GET.get("mine") or "").strip() == "1"
    room_id = (request.GET.get("room_id") or "").strip()
    status = (request.GET.get("status") or "").strip()
    future_only = (request.GET.get("future_only") or "1").strip() == "1"

    # Base queryset
    qs = RoomBooking.objects.select_related("room", "requested_by").all()

    # Optional: agency restriction if your models have agency
    # if hasattr(RoomBooking, "agency_id") and request.user.agency_id:
    #     qs = qs.filter(room__agency_id=request.user.agency_id)

    if mine:
        qs = qs.filter(requested_by=request.user)

    if room_id.isdigit():
        qs = qs.filter(room_id=int(room_id))

    if status:
        qs = qs.filter(status=status)

    # Date range from FullCalendar
    if start_dt and end_dt:
        qs = qs.filter(date__gte=start_dt.date(), date__lte=end_dt.date())

    # Today/future only (default ON)
    if future_only:
        qs = qs.filter(date__gte=timezone.localdate())

    # Build events list
    events = []
    tz = timezone.get_current_timezone()

    for b in qs:
        start = datetime.combine(b.date, b.start_time)
        end = datetime.combine(b.date, b.end_time)

        if timezone.is_naive(start):
            start = timezone.make_aware(start, tz)
        if timezone.is_naive(end):
            end = timezone.make_aware(end, tz)

        # Status color classes (FullCalendar can use classNames)
        # You can style these in CSS.
        class_names = [f"bk-status-{b.status}"]

        events.append({
            "id": b.id,
            "title": f"{b.room.name} • {b.title}",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "allDay": False,
            "classNames": class_names,
            "extendedProps": {
                "room": b.room.name,
                "status": b.status,
                "requested_by": (b.requested_by.get_full_name() or b.requested_by.username),
                "description": b.description or "",
            }
        })

    return JsonResponse(events, safe=False)
