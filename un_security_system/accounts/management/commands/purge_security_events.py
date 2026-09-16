from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.security_console import SecurityAuthEvent, event_table_exists


class Command(BaseCommand):
    help = "Delete ICT Security Console authentication events older than N days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=180,
            help="Retain this many days of events (default: 180).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 30:
            raise CommandError("--days must be at least 30.")

        if not event_table_exists():
            raise CommandError("Security event table does not exist.")

        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = SecurityAuthEvent.objects.filter(at__lt=cutoff).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} authentication event row(s) older than {days} days."
            )
        )
