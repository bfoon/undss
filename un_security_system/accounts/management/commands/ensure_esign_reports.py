# accounts/management/commands/ensure_esign_reports.py
from django.core.management.base import BaseCommand

from accounts.report_bi_esign import ensure_report_tables


class Command(BaseCommand):
    help = "Create the isolated eSign Report Builder table if it does not exist."

    def handle(self, *args, **options):
        ensure_report_tables()
        self.stdout.write(self.style.SUCCESS("eSign Report Builder table is ready."))
