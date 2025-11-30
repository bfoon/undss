import uuid
import secrets
import random
from datetime import timedelta
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings

from .models import OneTimeCode, TrustedDevice



def create_otp_for_user(user, device_id, ip_address=None, user_agent=""):
    """
    Create a 6-digit OTP for this user+device, valid for 10 minutes.
    Also marks previous unused OTPs for that device as used/invalid.
    """
    # Invalidate older unused OTPs for safety
    OneTimeCode.objects.filter(
        user=user,
        device_id=device_id,
        is_used=False,
    ).update(is_used=True)

    # 6-digit numeric OTP
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


def send_otp_email(user, code):
    """
    Send the OTP to the user's email.
    Make sure EMAIL_BACKEND & SMTP settings are configured in settings.py.
    """
    if not user.email:
        # You might want to log this or show a message instead
        return

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

    # If something is misconfigured, we WANT to see the error => fail_silently=False
    send_mail(
        subject,
        message,
        from_email,
        [user.email],
        fail_silently=False,
    )


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
    # Adjust to your real logic
    return user.is_authenticated and getattr(user, "role", "") == "ict_focal"