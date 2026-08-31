"""
tenancy/management/commands/backfill_office.py
==============================================

Fills in `country_office` on records that predate the field, by looking at who
created each one.

    python manage.py backfill_office --dry-run
    python manage.py backfill_office
    python manage.py backfill_office --model visitors.Visitor
    python manage.py backfill_office --model vehicles.Vehicle --office 3

Every model in BACKFILL_MAP names the user fields to try, in order. The first
one that is set and whose user has a country office wins.

Two models have no user field at all — `vehicles.Vehicle` and `vehicles.Key`
are compound-level physical items — so they fall back to their most recent
movement or log entry, and failing that need `--office` to assign them
somewhere explicitly.

Run this after adding OfficeOwnedModel to the models you want scoped. It is
idempotent: rows that already have an office are left alone unless you pass
--force.
"""

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

#: model label -> user fields to try, in priority order
BACKFILL_MAP = {
    "visitors.Visitor": ["registered_by", "approved_by"],
    "visitors.VisitorCard": ["issued_by", "returned_by"],
    "vehicles.ParkingCard": ["created_by"],
    "vehicles.ParkingCardRequest": ["requested_by", "decided_by"],
    "vehicles.AssetExit": ["requester", "agency_approver", "lsa_user"],
    "vehicles.VehicleMovement": ["recorded_by"],
    "vehicles.KeyLog": ["issued_by", "received_by"],
    "vehicles.Package": ["logged_by", "reception_received_by", "agency_received_by"],
    "vehicles.UserSignature": ["user"],
}

#: models with no user field — derive from a related record instead
RELATED_FALLBACK = {
    # model label: (related_name, user field on the related model)
    "vehicles.Vehicle": ("movements", "recorded_by"),
    "vehicles.Key": ("logs", "issued_by"),
}


class Command(BaseCommand):
    help = "Stamp country_office onto records created before the field existed."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change without writing.")
        parser.add_argument("--model", default="",
                            help="Limit to one model, e.g. visitors.Visitor")
        parser.add_argument("--office", type=int, default=None,
                            help="CountryOffice pk to use when no user can be found.")
        parser.add_argument("--force", action="store_true",
                            help="Overwrite rows that already have an office.")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        only = options["model"].strip()
        force = options["force"]

        fallback_office = None
        if options["office"]:
            CountryOffice = apps.get_model("tenancy", "CountryOffice")
            fallback_office = CountryOffice.objects.filter(pk=options["office"]).first()
            if not fallback_office:
                raise CommandError(f"No country office with pk={options['office']}.")

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — nothing will be written.\n"))

        targets = dict(BACKFILL_MAP)
        targets.update({k: None for k in RELATED_FALLBACK})
        if only:
            if only not in targets:
                raise CommandError(
                    f"{only} is not in the backfill map. Known: {', '.join(sorted(targets))}"
                )
            targets = {only: targets[only]}

        totals = {"stamped": 0, "skipped": 0, "unresolved": 0}

        with transaction.atomic():
            for label in targets:
                try:
                    model = apps.get_model(label)
                except LookupError:
                    self.stdout.write(self.style.WARNING(f"  {label}: model not installed, skipping"))
                    continue

                if not self._has_office_field(model):
                    self.stdout.write(self.style.WARNING(
                        f"  {label}: no country_office field yet — add OfficeOwnedModel first"
                    ))
                    continue

                res = self._backfill_model(model, label, fallback_office, dry, force)
                for k in totals:
                    totals[k] += res[k]

            if dry:
                transaction.set_rollback(True)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Summary"))
        self.stdout.write(f"  Stamped .......... {totals['stamped']}")
        self.stdout.write(f"  Already set ...... {totals['skipped']}")
        self.stdout.write(f"  Could not resolve  {totals['unresolved']}")
        if totals["unresolved"]:
            self.stdout.write(self.style.WARNING(
                "\nRe-run with --office <pk> to assign the unresolved rows explicitly."
            ))
        if dry:
            self.stdout.write(self.style.WARNING("\nRolled back (dry run)."))

    # -- helpers -----------------------------------------------------------

    def _has_office_field(self, model) -> bool:
        return any(f.name == "country_office" for f in model._meta.get_fields())

    def _office_from_user(self, user):
        return getattr(user, "country_office_id", None) if user else None

    def _backfill_model(self, model, label, fallback_office, dry, force):
        user_fields = BACKFILL_MAP.get(label)
        related = RELATED_FALLBACK.get(label)

        qs = model.objects.all()
        if not force:
            qs = qs.filter(country_office__isnull=True)

        total = qs.count()
        if not total:
            self.stdout.write(f"  {label}: nothing to do")
            return {"stamped": 0, "skipped": 0, "unresolved": 0}

        stamped = unresolved = 0

        select = [f for f in (user_fields or []) if self._is_fk(model, f)]
        for obj in qs.select_related(*select).iterator():
            office_id = None

            for field in user_fields or []:
                office_id = self._office_from_user(getattr(obj, field, None))
                if office_id:
                    break

            if not office_id and related:
                rel_name, rel_user_field = related
                rel_qs = getattr(obj, rel_name, None)
                if rel_qs is not None:
                    latest = rel_qs.select_related(rel_user_field).order_by("-pk").first()
                    if latest:
                        office_id = self._office_from_user(getattr(latest, rel_user_field, None))

            if not office_id and fallback_office:
                office_id = fallback_office.pk

            if not office_id:
                unresolved += 1
                continue

            if not dry:
                model.objects.filter(pk=obj.pk).update(country_office_id=office_id)
            stamped += 1

        skipped = model.objects.exclude(country_office__isnull=True).count() if not force else 0
        self.stdout.write(
            f"  {label}: {stamped} stamped, {unresolved} unresolved (of {total})"
        )
        return {"stamped": stamped, "skipped": skipped, "unresolved": unresolved}

    def _is_fk(self, model, field_name) -> bool:
        try:
            f = model._meta.get_field(field_name)
            return f.is_relation and f.many_to_one
        except Exception:
            return False
