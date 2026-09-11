# accounts/management/commands/esign_studio_maintenance.py
"""
eSign Studio housekeeping. Run it once a day (cron, Celery beat, or a
Kubernetes CronJob):

    python manage.py esign_studio_maintenance
    python manage.py esign_studio_maintenance --dry-run

1. Reminders — a workflow step still pending after ESIGN_WF_REMINDER_DAYS
   (default 3), or past its due date, gets a reminder email. Each task is
   reminded at most once per that interval. Signature steps are skipped: the
   envelope's own eSign reminders already cover them.

2. Retention — PDF workbench files nobody has changed for
   ESIGN_STUDIO_RETENTION_DAYS (default 90; 0 keeps them forever) are deleted
   with all their versions. Workflow runs, submissions and envelopes are
   records and are never touched.

Email links need SITE_URL (e.g. "https://unpass.example.org"), the same
setting eSign uses, because there is no request to build them from.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone


class Command(BaseCommand):
    help = "Send workflow step reminders and remove expired PDF workbench files."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would happen without doing it.")
        parser.add_argument("--skip-reminders", action="store_true")
        parser.add_argument("--skip-retention", action="store_true")
        parser.add_argument("--reminder-days", type=int, default=None,
                            help="Override ESIGN_WF_REMINDER_DAYS for this run.")
        parser.add_argument("--retention-days", type=int, default=None,
                            help="Override ESIGN_STUDIO_RETENTION_DAYS for this run (0 disables).")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        if not getattr(settings, "SITE_URL", ""):
            self.stderr.write(self.style.WARNING(
                "SITE_URL is not set — reminder emails will contain relative links."))
        if not opts["skip_reminders"]:
            self._reminders(dry, opts["reminder_days"])
        if not opts["skip_retention"]:
            self._retention(dry, opts["retention_days"])

    # ------------------------------------------------------------------ reminders
    def _reminders(self, dry, override):
        from accounts import workflow_notify_esign as notify
        from accounts.models_esign_studio import WorkflowRun, WorkflowTask
        from accounts.workflow_engine_esign import log_run

        days = override if override is not None else int(getattr(settings, "ESIGN_WF_REMINDER_DAYS", 3))
        if days <= 0:
            self.stdout.write("Reminders are off (ESIGN_WF_REMINDER_DAYS = 0).")
            return
        now = timezone.now()
        cutoff = now - timedelta(days=days)

        due = (
            WorkflowTask.objects.filter(status=WorkflowTask.STATUS_PENDING, run__status__in=WorkflowRun.OPEN_STATUSES)
            .exclude(kind=WorkflowTask.KIND_SIGNATURE)
            .exclude(email="")
            .filter(Q(created_at__lte=cutoff) | Q(due_at__lte=now))
            .filter(Q(reminded_at__isnull=True) | Q(reminded_at__lte=cutoff))
            .select_related("run")
        )
        sent = 0
        for task in due:
            label = f"{task.run.reference} · {task.node_label} → {task.email}"
            if dry:
                self.stdout.write(f"  would remind {label}")
                sent += 1
                continue
            if notify.reminder(task):
                task.reminded_at = now
                task.save(update_fields=["reminded_at"])
                log_run(task.run, "notified", task=task, node_id=task.node_id,
                        note=f"Reminder sent to {task.name} about “{task.node_label}”.")
                sent += 1
                self.stdout.write(f"  reminded {label}")
        verb = "would send" if dry else "sent"
        self.stdout.write(self.style.SUCCESS(f"Reminders: {verb} {sent}."))

    # ------------------------------------------------------------------ retention
    def _retention(self, dry, override):
        from accounts.models_esign_studio import StudioFile

        days = override if override is not None else int(getattr(settings, "ESIGN_STUDIO_RETENTION_DAYS", 90))
        if days <= 0:
            self.stdout.write("Retention is off (ESIGN_STUDIO_RETENTION_DAYS = 0).")
            return
        cutoff = timezone.now() - timedelta(days=days)
        stale = StudioFile.objects.filter(updated_at__lt=cutoff).prefetch_related("versions")
        removed = freed = 0
        for sf in stale.iterator(chunk_size=200):
            versions = list(sf.versions.all())
            size = sum(v.size for v in versions)
            if dry:
                self.stdout.write(f"  would delete “{sf.name}” ({len(versions)} version(s), {size // 1024} KB) of {sf.owner_id}")
            else:
                for v in versions:
                    try:
                        v.file.delete(save=False)
                    except Exception:  # noqa: BLE001 - a missing file must not stop the sweep
                        pass
                sf.delete()
            removed += 1
            freed += size
        verb = "would delete" if dry else "deleted"
        self.stdout.write(self.style.SUCCESS(
            f"Retention: {verb} {removed} file(s), {freed / (1024 * 1024):.1f} MB, untouched for {days}+ days."))
