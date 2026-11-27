# accounts/tasks.py
"""
NES envelope deadlines: expiry and signer reminders.

Run by the scheduler (accounts/scheduler.py) every hour. Safe to run more
often: reminders are spaced by each envelope's `reminder_days` and an expired
envelope is expired exactly once.

What changed from the original, and why
---------------------------------------
* Nothing ran this. It was a Celery task in a project with no Celery app, no
  broker setting and no worker — so envelopes never expired and no signer was
  ever reminded. The scheduler now runs it.
* Expiry was silent: the status flipped and nobody was told, and nothing went
  into the audit trail. It now writes an `expired` event (hash-linked like
  every other) and emails the sender who is still outstanding.
* `last_reminded_at` was stamped even when nobody could be reminded, which
  pushed the next attempt a full interval away for no reason. It is now set
  only when a reminder actually went out.
* Mail was handed to a daemon thread. In a worker or a cron run that can exit
  before the thread sends — it now goes out before the job returns.
* One broken envelope no longer stops the run: each is handled on its own and
  failures are counted, so the job's heartbeat says how many failed.
"""
import logging

from django.db import transaction
from django.utils import timezone

from . import esign_notify
from .asset_email import deliver_synchronously
from .models_esign import Envelope
from .utils_esign import due_for_reminder, envelope_is_expired, log_event

logger = logging.getLogger(__name__)

try:
    from celery import shared_task
except Exception:  # pragma: no cover - Celery is optional for the web process
    def shared_task(*dargs, **dkwargs):
        def wrap(fn):
            return fn
        return wrap(dargs[0]) if dargs and callable(dargs[0]) else wrap


def _expire(env) -> bool:
    """Expire one envelope, once. Returns True when this call expired it."""
    with transaction.atomic():
        # Re-read under a lock so two overlapping runs cannot both expire it
        # and both email the sender.
        locked = (Envelope.objects.select_for_update()
                  .filter(pk=env.pk, status=Envelope.STATUS_SENT).first())
        if locked is None:
            return False
        locked.status = Envelope.STATUS_EXPIRED
        locked.save(update_fields=["status"])
    log_event(env, "expired",
              note=f"Expired at its deadline, "
                   f"{timezone.localtime(env.expires_at):%Y-%m-%d %H:%M}.")
    esign_notify.notify_expired(locked)
    return True


def _remind(env) -> int:
    """Remind every signer whose turn it is. Returns how many were emailed."""
    sent = 0
    for recipient in env.signers():
        if recipient.can_sign_now():
            esign_notify.notify_reminder(None, recipient)
            sent += 1
    if sent:
        env.last_reminded_at = timezone.now()
        env.save(update_fields=["last_reminded_at"])
    return sent


@shared_task(name="accounts.esign.send_due_reminders")
def esign_send_due_reminders() -> dict:
    summary = {"examined": 0, "expired": 0, "reminded_envelopes": 0,
               "reminders_sent": 0, "failed": 0}
    with deliver_synchronously():
        for env in (Envelope.objects.filter(status=Envelope.STATUS_SENT)
                    .select_related("created_by").iterator()):
            summary["examined"] += 1
            try:
                if envelope_is_expired(env):
                    if _expire(env):
                        summary["expired"] += 1
                    continue
                if due_for_reminder(env):
                    n = _remind(env)
                    if n:
                        summary["reminded_envelopes"] += 1
                        summary["reminders_sent"] += n
            except Exception:
                summary["failed"] += 1
                logger.exception("NES deadlines: envelope %s failed", env.pk)
    return summary
