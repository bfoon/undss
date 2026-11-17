# accounts/management/commands/esign_identity.py
"""
Inspect and protect NES signing identities.

    python manage.py esign_identity --status
        How many identities exist, how many are usable, how many documents
        were signed with a personal key.

    python manage.py esign_identity --install-guard
        Install the database trigger that makes a signature ID immutable at
        the storage layer. Run this once, after migrating. PostgreSQL only;
        on other backends it explains why and changes nothing.

    python manage.py esign_identity --verify-ids
        Recompute every signature ID from its enrolment record and confirm it
        matches. Proves the IDs are the ones this office issued.

    python manage.py esign_identity --audit
        Re-verify every stored attestation from scratch, rather than trusting
        the flag written when it was created.

    python manage.py esign_identity --show NES-GM-2BE9-W79Q-MTVY-TC
        Everything on file for one identity.

Nothing here can change a person's key or mark. That is not an omission: only
the owner can, through their own signature page, and this command runs as an
administrator.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


#: Rejects any UPDATE that would change an issued identifier or its
#: derivation inputs. Model code already refuses, but `QuerySet.update()`,
#: `bulk_update`, a migration and psql all bypass model code — so the real
#: guarantee has to live here.
GUARD_SQL = """
CREATE OR REPLACE FUNCTION nes_signature_identity_immutable()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.signature_id IS DISTINCT FROM NEW.signature_id THEN
        RAISE EXCEPTION
            'NES: signature_id is issued once and is immutable (% -> %)',
            OLD.signature_id, NEW.signature_id;
    END IF;
    IF OLD.enrollment_nonce IS DISTINCT FROM NEW.enrollment_nonce THEN
        RAISE EXCEPTION 'NES: enrollment_nonce is immutable for %',
            OLD.signature_id;
    END IF;
    IF OLD.issued_at IS DISTINCT FROM NEW.issued_at THEN
        RAISE EXCEPTION 'NES: issued_at is immutable for %', OLD.signature_id;
    END IF;
    IF OLD.user_id IS DISTINCT FROM NEW.user_id THEN
        RAISE EXCEPTION
            'NES: a signing identity cannot be moved to another user (%)',
            OLD.signature_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS nes_signature_identity_immutable_trg
    ON accounts_signatureidentity;

CREATE TRIGGER nes_signature_identity_immutable_trg
    BEFORE UPDATE ON accounts_signatureidentity
    FOR EACH ROW EXECUTE FUNCTION nes_signature_identity_immutable();
"""

GUARD_CHECK_SQL = """
SELECT tgname FROM pg_trigger
WHERE tgname = 'nes_signature_identity_immutable_trg' AND NOT tgisinternal;
"""


class Command(BaseCommand):
    help = "Inspect NES signing identities and protect their identifiers."

    def add_arguments(self, parser):
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--install-guard", action="store_true")
        parser.add_argument("--verify-ids", action="store_true")
        parser.add_argument("--audit", action="store_true")
        parser.add_argument("--show", metavar="SIGNATURE_ID")
        parser.add_argument("--limit", type=int, default=500)

    def _ok(self, m):
        return self.style.SUCCESS(f"  OK    {m}")

    def _bad(self, m):
        return self.style.ERROR(f"  FAIL  {m}")

    def _warn(self, m):
        return self.style.WARNING(f"  WARN  {m}")

    def _info(self, m):
        return f"        {m}"

    def handle(self, *args, **opts):
        acted = False
        if opts.get("install_guard"):
            self.install_guard()
            acted = True
        if opts.get("status"):
            self.status()
            acted = True
        if opts.get("verify_ids"):
            self.verify_ids(opts["limit"])
            acted = True
        if opts.get("audit"):
            self.audit(opts["limit"])
            acted = True
        if opts.get("show"):
            self.show(opts["show"])
            acted = True
        if not acted:
            self.stdout.write(self.style.WARNING(
                "Nothing to do. Try --status, --install-guard, --verify-ids, "
                "--audit or --show."
            ))

    # ------------------------------------------------------------------
    def install_guard(self):
        self.stdout.write("\nSignature ID immutability guard")
        self.stdout.write("=" * 62)

        vendor = connection.vendor
        if vendor != "postgresql":
            self.stdout.write(self._warn(
                f"This database is {vendor}, not PostgreSQL, so the trigger "
                "was not installed."
            ))
            self.stdout.write(self._info(
                "Model-level protection still applies: SignatureIdentity."
                "save() refuses to change an issued ID. What is missing is "
                "protection against QuerySet.update(), bulk_update and raw "
                "SQL, which bypass model code. Production runs on PostgreSQL "
                "— run this there."
            ))
            return

        with connection.cursor() as cursor:
            cursor.execute(GUARD_SQL)
            cursor.execute(GUARD_CHECK_SQL)
            found = cursor.fetchone()

        if found:
            self.stdout.write(self._ok("trigger installed and active"))
            self.stdout.write(self._info(
                "An UPDATE that changes signature_id, enrollment_nonce, "
                "issued_at or user_id is now rejected by the database itself."
            ))
            self.stdout.write(self.style.SUCCESS(
                "\nSignature IDs are immutable at the storage layer.\n"
            ))
        else:
            self.stdout.write(self._bad(
                "the trigger does not appear in pg_trigger after creation"
            ))

    # ------------------------------------------------------------------
    def status(self):
        from accounts.models_esign_identity import (
            SignatureAttestation, SignatureIdentity, SignatureKey,
            SignatureMark,
        )
        from accounts import signature_id as sid
        from django.conf import settings

        self.stdout.write("\nNES signing identities")
        self.stdout.write("=" * 62)

        total = SignatureIdentity.objects.count()
        active = SignatureIdentity.objects.filter(
            status=SignatureIdentity.STATUS_ACTIVE).count()
        usable = sum(
            1 for i in SignatureIdentity.objects.all() if i.is_usable
        )

        self.stdout.write(self._info(f"identities        : {total}"))
        self.stdout.write(self._info(f"active            : {active}"))
        self.stdout.write(self._info(f"ready to sign     : {usable}"))
        self.stdout.write(self._info(
            f"keys on file      : {SignatureKey.objects.count()} "
            f"({SignatureKey.objects.filter(is_current=True).count()} current)"
        ))
        self.stdout.write(self._info(
            f"marks on file     : {SignatureMark.objects.count()}"
        ))

        attested = SignatureAttestation.objects.count()
        good = SignatureAttestation.objects.filter(verified=True).count()
        self.stdout.write(self._info(f"attestations      : {attested}"))
        if attested:
            self.stdout.write(
                self._ok(f"verified          : {good}") if good == attested
                else self._warn(
                    f"verified          : {good} of {attested} — "
                    f"{attested - good} did not verify"
                )
            )

        self.stdout.write(self._info(f"office prefix     : {sid.office_prefix()}"))
        if getattr(settings, "ESIGN_SIGNATURE_ID_SECRET", ""):
            self.stdout.write(self._ok("ESIGN_SIGNATURE_ID_SECRET is set"))
        else:
            self.stdout.write(self._warn(
                "ESIGN_SIGNATURE_ID_SECRET is unset — falling back to "
                "SECRET_KEY. Set it explicitly, then leave it alone."
            ))

        if getattr(settings, "ESIGN_REQUIRE_ATTESTATION", False):
            self.stdout.write(self._ok(
                "ESIGN_REQUIRE_ATTESTATION is on — enrolled users must sign "
                "with their own key"
            ))
        else:
            self.stdout.write(self._warn(
                "ESIGN_REQUIRE_ATTESTATION is off — an enrolled user can "
                "still sign with an image alone. Turn it on once people have "
                "enrolled."
            ))

        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(GUARD_CHECK_SQL)
                guard = cursor.fetchone()
            self.stdout.write(
                self._ok("ID immutability trigger is installed") if guard
                else self._warn(
                    "ID immutability trigger is NOT installed — run "
                    "--install-guard"
                )
            )
        self.stdout.write("")

    # ------------------------------------------------------------------
    def verify_ids(self, limit: int):
        from accounts import signature_id as sid
        from accounts.models_esign_identity import SignatureIdentity

        self.stdout.write("\nRecomputing signature IDs from enrolment records")
        self.stdout.write("=" * 62)

        rows = SignatureIdentity.objects.all().order_by("id")[:limit]
        bad = []
        for identity in rows:
            ok = sid.verify_signature_id(
                identity.signature_id,
                office=identity.office_code,
                user_pk=identity.user_id,
                nonce=identity.enrollment_nonce,
                issued_at_iso=identity.issued_at.isoformat(),
            )
            if not ok:
                bad.append(identity)

        checked = len(rows)
        self.stdout.write(self._info(f"checked : {checked}"))
        if not bad:
            self.stdout.write(self._ok("every ID recomputes from its record"))
        else:
            for identity in bad:
                self.stdout.write(self._bad(
                    f"{identity.signature_id} does not recompute"
                ))
            self.stdout.write(self._info(
                "If ESIGN_SIGNATURE_ID_SECRET changed, this is expected and "
                "the IDs themselves are still valid — they are stored. If it "
                "did not change, investigate: the enrolment record may have "
                "been edited."
            ))
        self.stdout.write("")

    # ------------------------------------------------------------------
    def audit(self, limit: int):
        from accounts import identity_esign as I
        from accounts.models_esign_identity import SignatureAttestation

        self.stdout.write("\nRe-verifying stored attestations")
        self.stdout.write("=" * 62)

        rows = (
            SignatureAttestation.objects
            .select_related("key", "identity", "envelope")
            .order_by("-created_at")[:limit]
        )
        good = bad = 0
        for attestation in rows:
            ok, detail = I.verify_stored_attestation(attestation)
            if ok:
                good += 1
                continue
            bad += 1
            self.stdout.write(self._bad(
                f"{attestation.signature_id} on "
                f"{attestation.envelope.envelope_id}"
            ))
            self.stdout.write(self._info(detail[:150]))
            if attestation.verified:
                self.stdout.write(self._bad(
                    "    this one was recorded as verified — it no longer is"
                ))

        self.stdout.write(self._info(f"checked : {good + bad}"))
        self.stdout.write(self._ok(f"valid   : {good}"))
        if bad:
            self.stdout.write(self._bad(f"invalid : {bad}"))
            self.stdout.write(self.style.ERROR(
                "\nAn attestation that no longer verifies means the stored "
                "statement,\nsignature or public key was altered. Do not "
                "delete anything.\n"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nEvery personal signature still verifies.\n"
            ))

    # ------------------------------------------------------------------
    def show(self, signature_id: str):
        from accounts import identity_esign as I
        from accounts import signature_id as sid

        try:
            canonical = sid.normalise(signature_id)
        except sid.SignatureIdError as exc:
            raise CommandError(str(exc))

        identity = I.identity_by_signature_id(canonical)
        if identity is None:
            raise CommandError(f"No identity has the ID {canonical}.")

        self.stdout.write(f"\n{identity.signature_id}")
        self.stdout.write("=" * 62)
        self.stdout.write(self._info(f"owner          : {identity.user} (#{identity.user_id})"))
        self.stdout.write(self._info(f"enrolled as    : {identity.enrolled_name}"))
        self.stdout.write(self._info(f"issued         : {identity.issued_at}"))
        self.stdout.write(self._info(f"office         : {identity.office_code}"))
        self.stdout.write(self._info(f"status         : {identity.get_status_display()}"))
        self.stdout.write(self._info(f"key revisions  : {identity.key_revision}"))
        self.stdout.write(self._info(f"mark revisions : {identity.mark_revision}"))
        self.stdout.write(self._info(f"documents signed: {identity.use_count}"))
        self.stdout.write(self._info(f"last used      : {identity.last_used_at or 'never'}"))

        self.stdout.write("\nKeys")
        self.stdout.write("-" * 62)
        for key in identity.keys.order_by("-revision"):
            flag = "current" if key.is_current else f"retired {key.retired_at:%Y-%m-%d}" if key.retired_at else "retired"
            self.stdout.write(
                f"  r{key.revision:<3} {key.algorithm:20s} {flag}"
            )
            self.stdout.write(self._info(f"fingerprint {key.short_fingerprint}"))

        self.stdout.write("\nMarks")
        self.stdout.write("-" * 62)
        for mark in identity.marks.order_by("-revision"):
            flag = "current" if mark.is_current else "replaced"
            self.stdout.write(
                f"  r{mark.revision:<3} {mark.get_kind_display():16s} {flag}"
            )

        self.stdout.write(self.style.WARNING(
            "\nNote: this command cannot change the key or the mark. Only "
            "the owner can,\nfrom their own signature page. That is what "
            "makes it their signature.\n"
        ))
