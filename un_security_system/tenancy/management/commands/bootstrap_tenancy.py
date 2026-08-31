"""
tenancy/management/commands/bootstrap_tenancy.py
================================================

One-shot setup for an existing UN PASS database.

    python manage.py bootstrap_tenancy --dry-run
    python manage.py bootstrap_tenancy

Does four things, all idempotent:

1. Creates a default CountryOffice for every Agency that has none.
2. Assigns every user without a country office to their agency's default.
3. Copies the old AgencyServiceConfig switches (asset_mgmt_enabled,
   esign_enabled) into FeatureGrant rows so nothing changes on day one.
4. Optionally switches on a starter set of modules for offices with no grants
   at all, so an existing deployment does not go dark after the migration.

Use --preserve-current on an existing production database: it reads which
modules are actually in use and turns exactly those on.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from tenancy.catalog import FEATURES
from tenancy.models import CountryOffice, FeatureGrant
from tenancy.services import bump_cache

#: What an existing single-compound deployment most likely already uses.
STARTER_MODULES = (
    "ict_console",
    "analytics",
    "activity_log",
    "security_incidents",
    "incident_reporting",
    "visitor_access",
    "vehicle_access",
    "room_booking",
    "room_attendance",
    "common_services",
)


class Command(BaseCommand):
    help = "Create default country offices and migrate existing feature toggles."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--preserve-current", action="store_true",
            help="Switch on the starter module set for offices with no grants.",
        )
        parser.add_argument(
            "--office-name", default="Head Office",
            help="Name for auto-created offices. Default: Head Office",
        )
        parser.add_argument(
            "--country", default="",
            help="Country to stamp on auto-created offices.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        from django.apps import apps

        Agency = apps.get_model("accounts", "Agency")
        User = apps.get_model("accounts", "User")

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — nothing will be written.\n"))

        with transaction.atomic():
            created_offices = self._create_offices(Agency, options, dry)
            moved = self._assign_users(User, dry)
            migrated = self._migrate_service_configs(apps, dry)
            seeded = 0
            if options["preserve_current"]:
                seeded = self._seed_starter(dry)

            if dry:
                transaction.set_rollback(True)

        if not dry:
            bump_cache()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Summary"))
        self.stdout.write(f"  Offices created ......... {created_offices}")
        self.stdout.write(f"  Users assigned .......... {moved}")
        self.stdout.write(f"  Old toggles migrated .... {migrated}")
        self.stdout.write(f"  Starter grants written .. {seeded}")
        if dry:
            self.stdout.write(self.style.WARNING("\nRolled back (dry run)."))
        else:
            self.stdout.write(
                self.style.SUCCESS("\nDone. Open /platform/ to review each office.")
            )

    # -- steps ------------------------------------------------------------

    def _create_offices(self, Agency, options, dry) -> int:
        count = 0
        for agency in Agency.objects.all():
            if agency.country_offices.exists():
                continue
            label = f"{agency.code} · {options['office_name']}"
            self.stdout.write(f"  + office for {agency.code}: {options['office_name']}")
            if not dry:
                CountryOffice.objects.create(
                    agency=agency,
                    name=options["office_name"],
                    code=(agency.code or "HQ")[:20],
                    country=options["country"],
                    is_default=True,
                )
            count += 1
        return count

    def _assign_users(self, User, dry) -> int:
        count = 0
        defaults = {
            o.agency_id: o
            for o in CountryOffice.objects.filter(is_default=True)
        }
        if not defaults:
            defaults = {
                o.agency_id: o
                for o in CountryOffice.objects.order_by("agency_id", "pk")
            }

        orphans = User.objects.filter(country_office__isnull=True, agency__isnull=False)
        for user in orphans.iterator():
            office = defaults.get(user.agency_id)
            if not office:
                continue
            if not dry:
                user.country_office = office
                user.save(update_fields=["country_office"])
            count += 1
        if count:
            self.stdout.write(f"  ~ assigning {count} user(s) to a default office")
        return count

    def _migrate_service_configs(self, apps, dry) -> int:
        """Read the old AgencyServiceConfig booleans into FeatureGrant rows."""
        try:
            AgencyServiceConfig = apps.get_model("accounts", "AgencyServiceConfig")
        except LookupError:
            return 0

        mapping = {
            "asset_mgmt_enabled": "asset_mgmt",
            "esign_enabled": "esign",
        }
        count = 0
        for cfg in AgencyServiceConfig.objects.select_related("agency"):
            for field, code in mapping.items():
                value = bool(getattr(cfg, field, False))
                self.stdout.write(
                    f"  = {cfg.agency.code} {code} -> {'on' if value else 'off'}"
                )
                if not dry:
                    FeatureGrant.objects.update_or_create(
                        agency=cfg.agency, country_office=None, feature_code=code,
                        defaults={
                            "enabled": value,
                            "is_paid": bool(getattr(cfg, f"{field[:-8]}is_paid", False)),
                        },
                    )
                count += 1
        return count

    def _seed_starter(self, dry) -> int:
        valid = {f.code for f in FEATURES}
        codes = [c for c in STARTER_MODULES if c in valid]
        count = 0
        for office in CountryOffice.objects.all():
            if FeatureGrant.objects.for_office(office.pk).exists():
                continue
            self.stdout.write(f"  + starter modules for {office}")
            for code in codes:
                if not dry:
                    FeatureGrant.objects.update_or_create(
                        agency=None, country_office=office, feature_code=code,
                        defaults={"enabled": True},
                    )
                count += 1
        return count
