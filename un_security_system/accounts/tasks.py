# accounts/tasks.py
from celery import shared_task
from .models_esign import Envelope
from .utils_esign import due_for_reminder, envelope_is_expired
from . import esign_notify
from django.utils import timezone

@shared_task
def esign_send_due_reminders():
    for env in Envelope.objects.filter(status=Envelope.STATUS_SENT):
        if envelope_is_expired(env):
            env.status = Envelope.STATUS_EXPIRED
            env.save(update_fields=["status"])
            continue
        if due_for_reminder(env):
            for r in env.signers():
                if r.can_sign_now():
                    esign_notify.notify_reminder(None, r)
            env.last_reminded_at = timezone.now()
            env.save(update_fields=["last_reminded_at"])