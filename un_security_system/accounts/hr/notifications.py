import threading
from typing import Iterable, Sequence

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail


def _send_email_background(subject: str, message: str, recipients: Sequence[str]):
    if not recipients:
        return
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)
    if not from_email:
        # you already saw this error earlier – avoid sending with empty from
        return

    def _worker():
        try:
            send_mail(
                subject,
                message,
                from_email,
                list(recipients),
                fail_silently=True,
            )
        except Exception:
            # swallow or log if you have logging set up
            pass

    threading.Thread(target=_worker, daemon=True).start()


def notify_users_by_role(roles: Iterable[str], subject: str, message: str):
    """
    Notify all active users whose `role` is in `roles`.
    """
    User = get_user_model()
    qs = User.objects.filter(is_active=True, role__in=list(roles)).exclude(email__isnull=True).exclude(email="")
    emails = list(qs.values_list("email", flat=True))
    _send_email_background(subject, message, emails)


def notify_users_direct(users: Iterable, subject: str, message: str):
    emails = [
        u.email
        for u in users
        if getattr(u, "email", "") not in (None, "")
    ]
    _send_email_background(subject, message, emails)