from django.core.management.base import BaseCommand
from django.db import connection

from accounts.dashboard_esign_access import DashboardAccessGrant


class Command(BaseCommand):
    help = (
        "Create the eSign / Forms / Flow dashboard access table without "
        "using Django migrations."
    )

    def handle(self, *args, **options):
        table = DashboardAccessGrant._meta.db_table
        existing = connection.introspection.table_names()

        if table in existing:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dashboard access table already exists: {table}"
                )
            )
            return

        with connection.schema_editor() as editor:
            editor.create_model(DashboardAccessGrant)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created dashboard access table: {table}"
            )
        )
        self.stdout.write(
            "Active Country Office admins automatically receive Level 4 "
            "for their own CO and do not require a grant row."
        )
