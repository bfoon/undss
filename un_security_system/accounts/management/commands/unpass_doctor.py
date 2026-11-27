# accounts/management/commands/unpass_doctor.py
"""
UN PASS — is everything that should be running, running?

Three things fail silently in this system, and this command exists because
each of them did:

1.  SCHEDULED JOBS. A job that never runs produces no error. Envelopes simply
    never expired and no signer was ever reminded, because there was no
    worker — and nothing anywhere said so.

2.  THE DATABASE. A migration that exists only on the server, a role with
    more power than it needs, a backup that was never taken: all invisible
    until the day they matter.

3.  THE AUDIT TRAIL. Damage to a hash chain is only found when someone opens
    that envelope's verify page. Most are never opened again.

So this looks at all three and says, in plain words, what is wrong:

    python manage.py unpass_doctor              everything
    python manage.py unpass_doctor --jobs       scheduled jobs only
    python manage.py unpass_doctor --db         database only
    python manage.py unpass_doctor --chain      audit trails only
    python manage.py unpass_doctor --run NAME   run one scheduled job now
    python manage.py unpass_doctor --json       for a monitoring system

Exit status is 1 when anything is FAIL, so it can drive an alert from cron or
a container healthcheck.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"


class Command(BaseCommand):
    help = "Check scheduled jobs, the database and the audit trails."

    def add_arguments(self, parser):
        parser.add_argument("--jobs", action="store_true")
        parser.add_argument("--db", action="store_true")
        parser.add_argument("--chain", action="store_true")
        parser.add_argument("--run", metavar="JOB",
                            help="Run one scheduled job now, here, and report.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Audit trails: check only the newest N.")
        parser.add_argument("--json", action="store_true")

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        self.results = []
        self.json_mode = bool(opts["json"])
        if opts["run"]:
            return self.run_now(opts["run"])

        everything = not (opts["jobs"] or opts["db"] or opts["chain"])
        if everything or opts["jobs"]:
            self.section("Scheduled jobs")
            self.check_jobs()
        if everything or opts["db"]:
            self.section("Database")
            self.check_database()
        if everything or opts["chain"]:
            self.section("Audit trails")
            self.check_chains(opts["limit"])

        fails = sum(1 for r in self.results if r["level"] == FAIL)
        warns = sum(1 for r in self.results if r["level"] == WARN)
        if opts["json"]:
            self.stdout.write(json.dumps(
                {"fail": fails, "warn": warns, "checks": self.results},
                indent=2, default=str))
        else:
            self.stdout.write("")
            line = f"  {fails} FAIL · {warns} WARN · " \
                   f"{sum(1 for r in self.results if r['level'] == OK)} OK"
            self.stdout.write(self.style.ERROR(line) if fails else
                              self.style.WARNING(line) if warns else
                              self.style.SUCCESS(line))
            self.stdout.write("")
        if fails:
            raise SystemExit(1)

    # ------------------------------------------------------------------
    def section(self, title):
        self.current = title
        if not self.json_mode:
            self.stdout.write(f"\n{title}\n" + "=" * 66)

    def report(self, level, what, detail="", fix=""):
        self.results.append({"section": self.current, "level": level,
                             "check": what, "detail": detail, "fix": fix})
        if self.json_mode:
            return
        style = {OK: self.style.SUCCESS, WARN: self.style.WARNING,
                 FAIL: self.style.ERROR, INFO: lambda s: s}[level]
        self.stdout.write(style(f"  {level:<5} {what}"))
        if detail:
            for line in str(detail).splitlines():
                self.stdout.write(f"        {line}")
        if fix and level in (WARN, FAIL):
            self.stdout.write(f"        → {fix}")

    # ==================================================================
    # 1. Scheduled jobs
    # ==================================================================
    def check_jobs(self):
        from accounts.scheduler import jobs, read_heartbeat, redis_client

        broker = getattr(settings, "CELERY_BROKER_URL", "")
        if not broker:
            self.report(FAIL, "No CELERY_BROKER_URL",
                        "Nothing scheduled can run.",
                        "Set CELERY_BROKER_URL (docker-compose.yml does).")
            return

        client = redis_client()
        try:
            client.ping()
            self.report(OK, f"Broker reachable ({broker})")
        except Exception as exc:
            self.report(FAIL, "Broker unreachable", f"{broker}: {exc}",
                        "Start the redis service: docker compose up -d redis")
            return

        # Workers: ask them. A worker that does not answer in 3s is not
        # running jobs either.
        workers = []
        try:
            from un_security_system.celery import app

            workers = list((app.control.ping(timeout=3) or []))
        except Exception as exc:
            self.report(WARN, "Could not ask for workers", str(exc))
        if workers:
            names = ", ".join(next(iter(w)) for w in workers)
            self.report(OK, f"{len(workers)} worker(s) answering", names)
        else:
            self.report(FAIL, "No worker is running",
                        "Every scheduled job below is queued and going nowhere.",
                        "docker compose up -d worker")

        try:
            queued = client.llen("celery")
            level = OK if queued < 50 else WARN
            self.report(level, f"{queued} job(s) waiting in the queue",
                        "" if queued < 50 else
                        "A backlog means workers are down or too slow.")
        except Exception:
            pass

        now = timezone.now()
        beat = read_heartbeat("scheduler-heartbeat")
        if not beat:
            self.report(FAIL, "Beat has never run a job",
                        "Without beat nothing is ever scheduled — the worker "
                        "just waits.",
                        "docker compose up -d beat   (one only, never two)")
        else:
            age = now - _parse(beat.get("finished_at"))
            if age > timedelta(minutes=15):
                self.report(FAIL, f"Beat silent for {_human(age)}",
                            "The scheduler heartbeat runs every 5 minutes.",
                            "docker compose restart beat worker")
            else:
                self.report(OK, f"Beat alive (last tick {_human(age)} ago)")

        self.stdout.write("")
        for name, job in jobs().items():
            if name == "scheduler-heartbeat":
                continue
            hb = read_heartbeat(name)
            max_age = timedelta(seconds=job["max_age"])
            label = f"{name:<28} {job['cron']:<14}"
            needs = job.get("needs")
            if needs and not getattr(settings, needs, False):
                self.report(INFO, f"{label} off ({needs} is off)")
                continue
            if not hb:
                self.report(FAIL, f"{label} has never run", job["about"],
                            f"Run it now: manage.py unpass_doctor --run {name}")
                continue
            last_ok = _parse(hb.get("last_success"))
            if not hb.get("ok"):
                self.report(FAIL, f"{label} last run FAILED",
                            (hb.get("error") or "")[:300],
                            f"See the worker log; retry with --run {name}")
            elif last_ok is None or now - last_ok > max_age:
                ago = _human(now - last_ok) if last_ok else "never"
                self.report(FAIL, f"{label} overdue — last success {ago}",
                            job["about"])
            else:
                what = hb.get("skipped") or _short(hb.get("summary"))
                self.report(OK, f"{label} {_human(now - last_ok)} ago", what)

        try:
            from un_security_system.celery import app

            extra = [k for k in (app.conf.beat_schedule or {})
                     if not k.startswith("unpass-")]
            if extra:
                self.report(INFO, "Other scheduled tasks", ", ".join(extra))
        except Exception:
            pass

    def run_now(self, name):
        from accounts.scheduler import jobs, run_job

        if name not in jobs():
            self.stderr.write(f"No job called {name!r}. Jobs: "
                              + ", ".join(jobs()))
            raise SystemExit(2)
        self.stdout.write(f"Running {name} here, now…")
        record = run_job(name)
        self.stdout.write(json.dumps(record, indent=2, default=str))
        if not record.get("ok"):
            raise SystemExit(1)

    # ==================================================================
    # 2. Database
    # ==================================================================
    def check_database(self):
        db = settings.DATABASES["default"]
        vendor = connection.vendor
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            self.report(OK, f"Connected ({vendor}, {db.get('NAME')} on "
                            f"{db.get('HOST') or 'local socket'})")
        except Exception as exc:
            self.report(FAIL, "Cannot connect", str(exc))
            return

        if vendor == "postgresql":
            self._postgres_checks(db)
        else:
            self.report(WARN, f"Running on {vendor}",
                        "The audit-trail locks and the immutability guard "
                        "are only fully effective on PostgreSQL.")

        self._migration_checks()
        self._untracked_code_checks()
        self._unmanaged_table_checks()
        self._backup_checks()

        if not getattr(settings, "USE_TZ", False):
            self.report(FAIL, "USE_TZ is off",
                        "Deadlines and audit timestamps would be naive.")
        cma = db.get("CONN_MAX_AGE", 0)
        self.report(OK if cma else INFO,
                    f"Connection reuse: CONN_MAX_AGE={cma}"
                    + (", health checks on" if db.get("CONN_HEALTH_CHECKS")
                       else ""))

    def _postgres_checks(self, db):
        with connection.cursor() as cur:
            cur.execute("SHOW server_version")
            version = cur.fetchone()[0]
            cur.execute("SELECT current_user, rolsuper FROM pg_roles "
                        "WHERE rolname = current_user")
            user, is_super = cur.fetchone()
            cur.execute("SHOW TimeZone")
            server_tz = cur.fetchone()[0]
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            size = cur.fetchone()[0]

        self.report(OK, f"PostgreSQL {version}, {size}")

        if is_super:
            self.report(
                WARN, f"The application connects as a superuser ({user})",
                "A superuser can disable or drop any trigger, including the "
                "one that makes signature IDs immutable, and can rewrite the "
                "audit trail without leaving a trace in the database itself. "
                "The cryptographic checks still catch the result, but the "
                "guard stops being a wall and becomes a speed bump.",
                "Create a role that owns nothing and can only read and write "
                "rows; keep the superuser for migrations. See "
                "docs/unpass-operations.md.")
        else:
            self.report(OK, f"Application role {user} is not a superuser")

        if db.get("PASSWORD") in ("postgres", "password", ""):
            self.report(WARN, "Database password is a default",
                        "Anyone who can reach port 5432 inside the Docker "
                        "network can log in.",
                        "Set DB_PASSWORD in .env and ALTER ROLE to match.")

        self.report(INFO, f"Server time zone {server_tz}; Django stores UTC "
                          f"and shows {settings.TIME_ZONE}")

        from accounts.db_guard import guard_status

        with connection.cursor() as cur:
            rules = guard_status(cur)
        missing = [r for r in rules if not r["present"]]
        disabled = [r for r in rules if r["present"] and not r["enabled"]]
        if disabled:
            self.report(
                FAIL, f"{len(disabled)} ledger guard rule(s) are SWITCHED OFF",
                "\n".join(f"{r['table']}: {r['trigger']}" for r in disabled),
                "Someone with the owner's password disabled them. Check "
                "`docker compose logs db | grep -iE 'trigger|alter'` before "
                "re-installing: ./scripts/owner.sh unpass_harden_db "
                "--install-guards")
        if missing:
            self.report(
                WARN, f"{len(missing)} of {len(rules)} ledger guard rule(s) "
                      "not installed",
                "Audit entries, anchors, keys and attestations can be edited "
                "or deleted by anyone who can write rows.",
                "./scripts/owner.sh unpass_harden_db --install-guards")
        if rules and not missing and not disabled:
            self.report(OK, f"Ledger guard: all {len(rules)} rules in place")

    def _migration_checks(self):
        from django.apps import apps
        from django.db.migrations.autodetector import MigrationAutodetector
        from django.db.migrations.executor import MigrationExecutor
        from django.db.migrations.state import ProjectState

        executor = MigrationExecutor(connection)
        loader = executor.loader
        plan = executor.migration_plan(loader.graph.leaf_nodes())
        if plan:
            names = ", ".join(f"{m.app_label}.{m.name}" for m, _ in plan[:6])
            self.report(FAIL, f"{len(plan)} migration(s) not applied", names,
                        "python manage.py migrate")
        else:
            self.report(OK, "Every migration on disk is applied")

        # Applied in the database but no file on disk: the schema can no
        # longer be rebuilt from the code.
        labels = {c.label for c in apps.get_app_configs()}
        ghosts = sorted(
            f"{app}.{name}" for app, name in loader.applied_migrations
            if app in labels and (app, name) not in loader.disk_migrations
        )
        if ghosts:
            self.report(FAIL, f"{len(ghosts)} applied migration(s) have no file",
                        ", ".join(ghosts[:8]),
                        "Recover the files from a backup of this server's "
                        "project folder before running makemigrations again.")

        # Model changes nobody has made a migration for.
        try:
            changes = MigrationAutodetector(
                loader.project_state(), ProjectState.from_apps(apps)
            ).changes(graph=loader.graph)
            drift = sorted(changes)
            if drift:
                self.report(WARN, "Models changed without a migration",
                            "Apps: " + ", ".join(drift),
                            "python manage.py makemigrations && migrate")
            else:
                self.report(OK, "Models and migrations agree")
        except Exception as exc:
            self.report(WARN, "Could not compare models and migrations", str(exc))

        # Migration files on this server that are not in git: the schema
        # history lives only on this machine.
        untracked = self._untracked_migrations()
        if untracked is None:
            self.report(INFO, "Git not available here — cannot check whether "
                              "migrations are committed")
        elif untracked:
            self.report(
                FAIL, f"{len(untracked)} migration file(s) exist only on "
                      "this server",
                "\n".join(untracked[:10])
                + ("\n…" if len(untracked) > 10 else ""),
                "Commit them now (git add */migrations/*.py). They are the "
                "only record of how this database was built; a new server, a "
                "colleague's machine or the next `git pull` cannot reproduce "
                "it without them.")
        else:
            self.report(OK, "Every migration file is committed to git")

    def _untracked_code_checks(self):
        """
        Code this server runs that is not in git.

        The repository imports four things it does not contain —
        accounts/view_asset_management_legacy.py, accounts/catalog.py,
        SECURITY_DASHBOARD_CODES and ForcePasswordChangeMiddleware — so a
        fresh clone cannot serve a single page. Production works, which means
        those files exist on the server and nowhere else. Losing that disk
        loses them.
        """
        base = str(settings.BASE_DIR)
        try:
            out = subprocess.run(
                ["git", "-C", base, "ls-files", "--others", "--exclude-standard",
                 "--", "*.py", "*.html", "*.js", "*.css"],
                capture_output=True, text=True, timeout=15)
            if out.returncode != 0:
                return
            files = [p for p in out.stdout.splitlines()
                     if "/migrations/" not in p and "__pycache__" not in p
                     and not p.startswith(("staticfiles/", "media/"))]
            dirty = subprocess.run(
                ["git", "-C", base, "status", "--porcelain", "--", "*.py"],
                capture_output=True, text=True, timeout=15).stdout.splitlines()
            modified = [line[3:] for line in dirty if line[:2].strip() == "M"]
        except Exception:
            return

        if files:
            self.report(
                FAIL, f"{len(files)} code file(s) exist only on this server",
                "\n".join(files[:12]) + ("\n…" if len(files) > 12 else ""),
                "Commit them. The repository cannot run without some of "
                "them — a new server or a restore from git would come up "
                "broken.")
        else:
            self.report(OK, "No untracked code on this server")
        if modified:
            self.report(
                WARN, f"{len(modified)} committed file(s) edited on this "
                      "server and not committed back",
                "\n".join(modified[:12]),
                "Commit or discard them, or the next `git pull` will refuse "
                "to run.")

    def _unmanaged_table_checks(self):
        """
        Tables created by an `ensure_*` command instead of a migration.

        Several UN PASS modules (security console, asset health, NES
        dashboards) declare `managed = False` and build their tables with a
        setup command. Migrations do not know these tables exist, so a new
        server, a restored backup taken before the command ran, or a fresh
        test database comes up without them — and the module fails at the
        first query. This lists any that are missing and which command
        creates them.
        """
        from django.apps import apps

        existing = set(connection.introspection.table_names())
        project = str(settings.BASE_DIR)
        missing = []
        for model in apps.get_models():
            meta = model._meta
            if meta.managed or meta.proxy:
                continue
            module_file = getattr(sys.modules.get(model.__module__),
                                  "__file__", "") or ""
            if not module_file.startswith(project):
                continue          # a third-party view or table: not ours
            if meta.db_table not in existing:
                missing.append((meta.db_table, model.__module__))

        if not missing:
            self.report(OK, "Tables created outside migrations are all present")
            return

        hint = {
            "security_console": "ensure_security_console",
            "asset_health": "ensure_asset_health",
            "dashboard": "ensure_esign_dashboard",
            "report": "ensure_esign_reports",
        }
        lines, commands = [], set()
        for table, module in missing:
            cmd = next((c for key, c in hint.items() if key in module), "")
            if cmd:
                commands.add(cmd)
            lines.append(f"{table}  ({module})")
        self.report(
            WARN, f"{len(missing)} table(s) the code expects do not exist",
            "\n".join(lines[:12]),
            ("Create them with: " + ", ".join(
                f"manage.py {c}" for c in sorted(commands)))
            if commands else
            "These are created by an ensure_* command, not by migrate.")

    def _untracked_migrations(self):
        base = str(settings.BASE_DIR)
        try:
            out = subprocess.run(
                ["git", "-C", base, "ls-files", "--others",
                 "--exclude-standard", "--", "*/migrations/*.py"],
                capture_output=True, text=True, timeout=15)
            if out.returncode != 0:
                return None
            return [p for p in out.stdout.splitlines()
                    if not p.endswith("__init__.py")]
        except Exception:
            return None

    def _backup_checks(self):
        folder = getattr(settings, "UNPASS_BACKUP_DIR", "") or os.path.join(
            os.path.dirname(str(settings.BASE_DIR)), "backups")
        marker = os.path.join(folder, "LAST_OK")
        if not os.path.exists(marker):
            self.report(FAIL, "No database backup found",
                        f"Looked for {marker}.",
                        "docker compose up -d db-backup")
            return
        age = timezone.now() - datetime.fromtimestamp(
            os.path.getmtime(marker), tz=timezone.get_current_timezone())
        try:
            with open(marker) as fh:
                last = fh.read().strip()
        except Exception:
            last = ""
        if age > timedelta(hours=26):
            self.report(FAIL, f"Newest backup is {_human(age)} old", last,
                        "docker compose logs db-backup")
        else:
            self.report(OK, f"Newest backup {_human(age)} ago, read back "
                            f"and verified", last)

    # ==================================================================
    # 3. Audit trails
    # ==================================================================
    def check_chains(self, limit):
        from accounts.audit_chain_esign import verify_anchor, verify_chain
        from accounts.models_esign import Envelope

        if not getattr(settings, "ESIGN_AUDIT_CHAIN", True):
            self.report(FAIL, "ESIGN_AUDIT_CHAIN is off",
                        "New audit entries are not being linked.")

        qs = Envelope.objects.order_by("-id")
        if limit:
            qs = qs[:limit]

        counts = {"clean": 0, "legacy": 0, "damaged": 0, "anchored": 0,
                  "anchor_bad": 0}
        damaged, anchor_bad = [], []
        for env in qs:
            state = verify_chain(env)
            if state["chained"] == 0:
                counts["legacy"] += 1
                continue
            if state["ok"]:
                counts["clean"] += 1
            else:
                counts["damaged"] += 1
                damaged.append(f"{env.envelope_id}: "
                               + ", ".join(sorted({p['kind'] for p in
                                                   state['problems']})))
            try:
                seal = env.seal          # OneToOne; absent on unsealed ones
            except Exception:
                seal = None
            if seal is not None and seal.audit_chain_head:
                counts["anchored"] += 1
                result = verify_anchor(env, seal)
                if not result["ok"] and result.get("present"):
                    counts["anchor_bad"] += 1
                    anchor_bad.append(f"{env.envelope_id}: "
                                      f"{result['detail'][:110]}")

        total = sum(counts[k] for k in ("clean", "legacy", "damaged"))
        self.report(INFO, f"{total} envelope(s) examined",
                    f"{counts['clean']} verify · {counts['legacy']} predate "
                    f"the chain · {counts['anchored']} anchored at completion")
        if damaged:
            self.report(FAIL, f"{len(damaged)} audit trail(s) fail verification",
                        "\n".join(damaged[:12]),
                        "Treat as an incident. Do not re-seal or edit. Compare "
                        "with the evidence package and witnessed copies. "
                        "Trails forked before the concurrency fix are listed "
                        "by the nightly nes-chain-audit job as probable "
                        "concurrency forks.")
        else:
            self.report(OK, "Every chained trail verifies")
        if anchor_bad:
            self.report(FAIL, f"{len(anchor_bad)} anchor(s) do not match",
                        "\n".join(anchor_bad[:12]))
        elif counts["anchored"]:
            self.report(OK, "Every anchored trail still matches its anchor")


# ----------------------------------------------------------------------
def _parse(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else timezone.make_aware(dt)
    except Exception:
        return None


def _human(delta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def _short(summary) -> str:
    if summary is None:
        return ""
    if isinstance(summary, dict):
        parts = []
        for k, v in summary.items():
            if isinstance(v, dict):
                inner = ", ".join(f"{ik}={iv}" for ik, iv in v.items()
                                  if not isinstance(iv, (list, dict)))
                parts.append(f"{k}: {inner}")
            elif not isinstance(v, list):
                parts.append(f"{k}={v}")
        return " · ".join(parts)[:220]
    return str(summary).splitlines()[-1][:160] if str(summary) else ""
