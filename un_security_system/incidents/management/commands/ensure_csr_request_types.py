from django.core.management.base import BaseCommand
from django.db import connection

from incidents.models import CommonServiceRequestType


DEFAULT_TYPES = [
    ("common_premises", "Common Premises / General", "General common premises and shared-facility support.", False, 10),
    ("un_all_staff_group", "UN All Staff Group", "Requests related to the UN All Staff Group.", False, 20),
    ("cash_power", "Cash Power Refill", "Cash power / electricity credit refill.", False, 30),
    ("facility_notice", "Facility Work Notice (Noise/Disruption)", "Planned facility work or disruption notice.", True, 40),
    ("electrical", "Electrical (Bulbs, Switches, Outlets, Failover)", "Electrical and power-related support.", False, 50),
    ("plumbing", "Plumbing / Toilets", "Plumbing, water and toilet support.", False, 60),
    ("cleaning", "Cleaning Services", "Cleaning and housekeeping support.", False, 70),
    ("waste", "Dumpster / Waste Disposal", "Waste collection and disposal support.", False, 80),
    ("grounds", "Grounds (Trees Trim/Cut)", "Grounds and outdoor maintenance.", False, 90),
    ("solar", "Solar Issue", "Solar power system support.", False, 100),
    ("cctv", "CCTV Issue", "CCTV infrastructure support.", False, 110),
    ("other", "Other", "Any Common Service Request that does not fit another type.", False, 999),
]


class Command(BaseCommand):
    help = (
        "Safely create the dynamic CSR request-type table if it is missing, "
        "then seed the default request types. No Django migration is required."
    )

    def handle(self, *args, **options):
        table = CommonServiceRequestType._meta.db_table
        existing_tables = connection.introspection.table_names()

        if table not in existing_tables:
            self.stdout.write(f"Creating table: {table}")
            with connection.schema_editor() as schema_editor:
                schema_editor.create_model(CommonServiceRequestType)
            self.stdout.write(self.style.SUCCESS(f"Created table: {table}"))
        else:
            self.stdout.write(f"Table already exists: {table}")

        created_count = 0
        existing_count = 0

        for code, name, description, requires_window, sort_order in DEFAULT_TYPES:
            obj, created = CommonServiceRequestType.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "description": description,
                    "requires_disruption_window": requires_window,
                    "sort_order": sort_order,
                    "is_active": True,
                },
            )

            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"Created: {name}"))
            else:
                existing_count += 1
                self.stdout.write(f"Exists: {obj.name}")

        self.stdout.write(
            self.style.SUCCESS(
                f"CSR request types ready. Created {created_count}; "
                f"already existed {existing_count}."
            )
        )
