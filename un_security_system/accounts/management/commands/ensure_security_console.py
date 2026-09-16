from django.core.management.base import BaseCommand
from django.db import connection

from accounts.security_console import (
    DEFAULT_GLOBAL_POLICY,
    SCOPE_GLOBAL,
    SecurityAuthEvent,
    SecurityPasswordPolicy,
)


class Command(BaseCommand):
    help = (
        "Create ICT Security Console telemetry and password-policy tables "
        "without using Django migrations."
    )

    def handle(self, *args, **options):
        existing = set(connection.introspection.table_names())

        for model in (SecurityAuthEvent, SecurityPasswordPolicy):
            table = model._meta.db_table
            if table in existing:
                self.stdout.write(
                    self.style.SUCCESS(f"Table already exists: {table}")
                )
                continue

            with connection.schema_editor() as editor:
                editor.create_model(model)

            self.stdout.write(
                self.style.SUCCESS(f"Created table: {table}")
            )
            existing.add(table)

        SecurityPasswordPolicy.objects.get_or_create(
            scope_kind=SCOPE_GLOBAL,
            scope_id=0,
            defaults={
                "inherit_global": False,
                **DEFAULT_GLOBAL_POLICY,
            },
        )

        self.stdout.write(
            self.style.SUCCESS("Global password policy is ready.")
        )
        self.stdout.write(
            "Country Offices inherit the global policy until an authorized "
            "CO admin saves an override."
        )
