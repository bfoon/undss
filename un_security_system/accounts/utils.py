import uuid
import secrets
import random
import threading
from datetime import timedelta
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from icalendar import Calendar, Event
from datetime import datetime

from .models import OneTimeCode, TrustedDevice


def create_otp_for_user(user, device_id, ip_address=None, user_agent=""):
    """
    Create a 6-digit OTP for this user+device, valid for 10 minutes.
    Also marks previous unused OTPs for that device as used/invalid.
    """
    OneTimeCode.objects.filter(
        user=user,
        device_id=device_id,
        is_used=False,
    ).update(is_used=True)

    code = f"{secrets.randbelow(10**6):06d}"
    expires_at = timezone.now() + timedelta(minutes=10)

    otp = OneTimeCode.objects.create(
        user=user,
        device_id=device_id,
        code=code,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:500],
    )
    return otp


def _send_otp_email_sync(user, code):
    """Send an OTP email and raise if delivery fails."""
    if not user.email:
        raise ValueError("This UNPASS account does not have an email address.")

    subject = "Your UN Security login verification code"
    greeting = user.get_full_name() or user.username

    message = (
        f"Dear {greeting},\n\n"
        f"Your login verification code is: {code}\n"
        f"This code will expire in 10 minutes.\n\n"
        f"If you did not attempt to sign in, please ignore this email.\n\n"
        f"UN Security Management System"
    )

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)

    sent = send_mail(
        subject,
        message,
        from_email,
        [user.email],
        fail_silently=False,
    )
    if sent < 1:
        raise RuntimeError("The email backend did not accept the OTP message.")
    return sent


def send_otp_email(user, code):
    """
    Synchronous OTP sender.

    The mobile login API uses this so it only tells the phone that a code was
    sent after Django's configured email backend has accepted the message.
    """
    return _send_otp_email_sync(user, code)


def send_otp_email_async(user, code):
    """
    Background helper retained for web flows that do not need to wait for SMTP.
    Failures are logged instead of being silently ignored.
    """

    def _target():
        try:
            send_otp_email(user, code)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Failed to send UNPASS OTP email to user_id=%s",
                getattr(user, "pk", None),
            )

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()


def remember_device(user, device_id, user_agent="", ip_address=""):
    now = timezone.now()
    expires_at = now + timedelta(days=30)
    device, _ = TrustedDevice.objects.update_or_create(
        user=user,
        device_id=device_id,
        defaults={
            "expires_at": expires_at,
            "user_agent": user_agent[:255],
            "ip_address": ip_address[:45],
            "is_active": True,
        },
    )
    return device


def is_ict_focal_point(user):
    return user.is_authenticated and getattr(user, "role", "") == "ict_focal"


def generate_booking_ics(booking):
    cal = Calendar()
    cal.add("prodid", "-//UNPASS//Room Booking//EN")
    cal.add("version", "2.0")
    cal.add("method", "REQUEST")

    event = Event()
    if booking.series:
        event.add("rrule", {"freq": booking.series.frequency.upper(), "interval": booking.series.interval})

    event.add("uid", f"{booking.id}-{uuid.uuid4()}@unpass")
    event.add("summary", f"{booking.title} - {booking.room.name}")

    description = f"Room: {booking.room.name}\nRequested By: {booking.requested_by.get_full_name()}\n"
    if booking.description:
        description += f"Purpose: {booking.description}\n"
    if booking.virtual_meeting_link:
        description += f"\n--- VIRTUAL MEETING LINK ---\n{booking.virtual_meeting_link}"
    event.add("description", description)

    start_dt = timezone.make_aware(datetime.combine(booking.date, booking.start_time))
    end_dt = timezone.make_aware(datetime.combine(booking.date, booking.end_time))
    event.add("dtstart", start_dt)
    event.add("dtend", end_dt)
    event.add("location", booking.room.location or booking.room.name)
    event.add("organizer", f"MAILTO:{settings.DEFAULT_FROM_EMAIL}")

    if booking.room.resource_email:
        event.add("attendee", f"MAILTO:{booking.room.resource_email}", parameters={"ROLE": "CHAIR"})
    event.add("attendee", f"MAILTO:{booking.requested_by.email}", parameters={"ROLE": "REQ-PARTICIPANT"})

    if booking.attendee_emails:
        guest_emails = [email.strip() for email in booking.attendee_emails.split(',') if email.strip()]
        for email in guest_emails:
            event.add("attendee", f"MAILTO:{email}", parameters={"ROLE": "OPT-PARTICIPANT"})

    cal.add_component(event)
    return cal.to_ical()
