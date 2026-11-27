# accounts/management/commands/esign_reissue.py
"""
Find — and repair — completed NES documents that are missing their Envelope
ID and signatures (see accounts/esign_finalize.py for why that happened).

    manage.py esign_reissue                    list affected documents (changes nothing)
    manage.py esign_reissue --since 2026-11-27 only those completed since then
    manage.py esign_reissue --apply            rebuild, re-seal, record it in the trail
    manage.py esign_reissue --apply --email    ...and e-mail everyone the corrected copy
    manage.py esign_reissue --envelope 0D0E38… one envelope
    manage.py esign_reissue --stuck            also finish envelopes signed but never completed
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts import esign_finalize as F
from accounts.models_esign import Envelope


class Command(BaseCommand):
    help = "List or repair completed NES documents missing their Envelope ID and signatures."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--email", action="store_true")
        parser.add_argument("--since", help="YYYY-MM-DD")
        parser.add_argument("--envelope")
        parser.add_argument("--stuck", action="store_true")

    def handle(self, *args, **o):
        from accounts.utils_esign import stamp_page  # noqa: F401 — the fixed code must be in place

        ok, detail = F.stamp_selftest()
        if not ok:
            raise CommandError(f"Stamping still does not work here ({detail}). "
                               "Rebuild the image with the pinned requirements first.")
        self.stdout.write(self.style.SUCCESS(f"Stamping works ({detail})."))

        if o["stuck"]:
            stuck = F.stuck_envelopes()
            self.stdout.write(f"{len(stuck)} envelope(s) fully signed but not completed.")
            if o["apply"] and stuck:
                self.stdout.write(str(F.retry_stuck()))

        qs = Envelope.objects.filter(status=Envelope.STATUS_COMPLETED).order_by("completed_at")
        if o["envelope"]:
            qs = qs.filter(envelope_id__iexact=o["envelope"].strip())
        if o["since"]:
            try:
                day = datetime.strptime(o["since"], "%Y-%m-%d")
            except ValueError:
                raise CommandError("--since takes YYYY-MM-DD")
            qs = qs.filter(completed_at__gte=timezone.make_aware(day))

        affected = []
        for env in qs.iterator():
            if F.missing_stamp(env):
                affected.append(env)
                self.stdout.write(f"  MISSING  {env.envelope_id}  {env.completed_at:%Y-%m-%d %H:%M}  {env.subject[:60]}")
        self.stdout.write(f"{len(affected)} completed document(s) without their Envelope ID.")

        if not o["apply"]:
            if affected:
                self.stdout.write("Nothing changed. Re-run with --apply (and --email to send the corrected copies).")
            return
        fixed = 0
        for env in affected:
            try:
                result = F.reissue(env, email=o["email"])
                still = F.missing_stamp(env)
                mark = self.style.SUCCESS("FIXED") if still is False else self.style.WARNING("CHECK")
                self.stdout.write(f"  {mark}  {env.envelope_id}  sealed={result['sealed']}")
                fixed += still is False
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"  FAILED {env.envelope_id}: {exc}"))
        self.stdout.write(f"{fixed} of {len(affected)} re-issued.")
