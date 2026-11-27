# accounts/management/commands/unpass_harden_db.py
"""
Make the database hold the line by itself.

    manage.py unpass_harden_db                       what is protected now
    manage.py unpass_harden_db --install-guards      add the ledger rules
    manage.py unpass_harden_db --create-app-role     a role that can only
                                                     read and write rows

Both changing options must run as the database OWNER (today: postgres),
because only the owner can create triggers and roles. scripts/owner.sh does
that without changing anything else:

    ./scripts/owner.sh unpass_harden_db --install-guards
    ./scripts/owner.sh unpass_harden_db --create-app-role

Then switch the application to the new role (the command prints the two
lines for .env) and restart. From then on the application cannot remove the
rules it is held to — and neither can anyone who steals its password.

See accounts/db_guard.py for what each rule allows and why.
"""
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from accounts import db_guard as G


class Command(BaseCommand):
    help = "Install the NES ledger guard and create a restricted app role."

    def add_arguments(self, parser):
        parser.add_argument("--install-guards", action="store_true",
                            help="Create or refresh every ledger rule.")
        parser.add_argument("--create-app-role", nargs="?", const="unpass_app",
                            metavar="NAME",
                            help="Create (or reset the password of) a role "
                                 "that can only read and write rows. "
                                 "Default name: unpass_app.")
        parser.add_argument("--password", default="",
                            help="With --create-app-role. Generated if omitted.")

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise CommandError("These rules are PostgreSQL triggers; this "
                               f"database is {connection.vendor}.")

        with connection.cursor() as cur:
            cur.execute("SELECT current_user, rolsuper FROM pg_roles "
                        "WHERE rolname = current_user")
            self.user, self.is_super = cur.fetchone()

        if opts["install_guards"]:
            self.install()
        if opts["create_app_role"]:
            self.create_role(opts["create_app_role"], opts["password"])
        self.status()

    # ------------------------------------------------------------------
    def _need_owner(self, what):
        if not self.is_super:
            raise CommandError(
                f"{what} needs the database owner, and this connection is "
                f"'{self.user}'. Run it with ./scripts/owner.sh so it "
                "connects as the owner for this one command.")

    def install(self):
        self._need_owner("Installing the guards")
        tables = set(connection.introspection.table_names())
        with transaction.atomic(), connection.cursor() as cur:
            for sql in G.install_sql(tables):
                cur.execute(sql)
            # If a restricted role already exists, the detach log must stay
            # append-only for it even though default privileges grant writes.
            cur.execute("SELECT rolname FROM pg_roles WHERE NOT rolsuper "
                        "AND rolcanlogin AND has_table_privilege(rolname, "
                        "'accounts_envelopeevent', 'INSERT')")
            for (role,) in cur.fetchall():
                cur.execute(f'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON '
                            f'nes_guard_detached FROM "{role}"')
                cur.execute(f'GRANT SELECT ON nes_guard_detached TO "{role}"')
        self.stdout.write(self.style.SUCCESS("Ledger guard installed."))

    def create_role(self, role, password):
        self._need_owner("Creating a role")
        password = password or secrets.token_urlsafe(32)
        db = settings.DATABASES["default"]["NAME"]
        if "nes_guard_detached" not in connection.introspection.table_names():
            raise CommandError("Install the guards first (--install-guards): "
                               "the role's grants include the guard's log.")
        with transaction.atomic(), connection.cursor() as cur:
            for sql in G.create_app_role_sql(role, password, db, self.user):
                cur.execute(sql)
        self.stdout.write(self.style.SUCCESS(f"Role {role} ready."))
        self.stdout.write(
            "\nPut these two lines in the .env beside docker-compose.yml, then\n"
            "`docker compose up -d` (web, worker and beat pick them up):\n")
        self.stdout.write(f"    APP_DB_USER={role}")
        self.stdout.write(f"    APP_DB_PASSWORD={password}\n")
        self.stdout.write(
            "Keep DB_PASSWORD as it is: that stays the owner's password, used\n"
            "only by ./scripts/owner.sh for migrate and ensure_* commands.\n")

    # ------------------------------------------------------------------
    def status(self):
        self.stdout.write("\nNES ledger guard")
        self.stdout.write("=" * 62)
        with connection.cursor() as cur:
            rules = G.guard_status(cur)
        missing = [r for r in rules if not r["present"]]
        disabled = [r for r in rules if r["present"] and not r["enabled"]]
        for r in rules:
            mark = ("OK   " if r["present"] and r["enabled"] else
                    "OFF  " if r["present"] else "MISSING")
            line = f"  {mark:<8}{r['table']:<34}{r['trigger']}"
            self.stdout.write(self.style.SUCCESS(line) if mark.startswith("OK")
                              else self.style.ERROR(line))
        if missing:
            self.stdout.write(self.style.WARNING(
                f"\n  {len(missing)} rule(s) missing — "
                "./scripts/owner.sh unpass_harden_db --install-guards"))
        if disabled:
            self.stdout.write(self.style.ERROR(
                f"\n  {len(disabled)} rule(s) DISABLED. Someone with the "
                "owner's password switched them off. Check the database log "
                "(docker compose logs db | grep -i trigger) before re-installing."))

        self.stdout.write("")
        if self.is_super:
            self.stdout.write(self.style.WARNING(
                f"  This connection is '{self.user}', a superuser. Rules hold "
                "against the application only once it connects as a "
                "restricted role (--create-app-role)."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"  This connection is '{self.user}', not a superuser: it "
                "cannot remove these rules."))
        self.stdout.write("")
