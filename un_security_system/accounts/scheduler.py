# accounts/scheduler.py
"""
UN PASS — runs the jobs in un_security_system/schedule.py, and remembers that
it did.

One Celery task, `run_job(name)`, runs every scheduled job. Routing them all
through one place buys three things no individual job had:

1.  **A heartbeat.** Every run records when it started, when it finished,
    whether it worked and what it did, in Redis under `unpass:job:<name>`.
    `manage.py unpass_doctor` reads those and says which job has gone quiet.
    A scheduled job that silently stops is the failure mode this whole file
    exists for — it is how envelopes went unexpired.

2.  **No overlap.** A job that takes longer than its interval, or a second
    beat started by mistake, would otherwise run it twice — two reminder
    emails, two expiry notices. Each run takes a Redis lock first; if the
    lock is held, the run is recorded as "skipped: already running".

3.  **Mail that arrives.** Everything runs inside `deliver_synchronously()`,
    so an email is sent before the job returns instead of on a thread the
    worker may kill on restart.

Jobs that depend on a feature being switched on (`needs` in the schedule)
record "skipped" when it is off, rather than failing every night.
"""
from __future__ import annotations

import importlib
import io
import json
import logging
import os
import shlex
import socket
import time

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

try:
    from celery import shared_task
except Exception:  # pragma: no cover
    def shared_task(*dargs, **dkwargs):
        def wrap(fn):
            return fn
        return wrap(dargs[0]) if dargs and callable(dargs[0]) else wrap

HEARTBEAT_PREFIX = "unpass:job:"
LOCK_PREFIX = "unpass:lock:"


# ─────────────────────────────────────────────────────────────────────────────
# Redis, for heartbeats and locks
# ─────────────────────────────────────────────────────────────────────────────

def redis_client():
    """A Redis client on the broker, or None. Never raises."""
    try:
        import redis

        url = (getattr(settings, "UNPASS_STATE_REDIS_URL", "")
               or getattr(settings, "CELERY_BROKER_URL", ""))
        if not url:
            return None
        return redis.Redis.from_url(url, socket_timeout=5,
                                    decode_responses=True)
    except Exception:
        return None


def jobs() -> dict:
    from un_security_system.schedule import JOBS

    return {job["name"]: job for job in JOBS}


def read_heartbeat(name: str) -> dict:
    client = redis_client()
    if client is None:
        return {}
    try:
        raw = client.get(HEARTBEAT_PREFIX + name)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _write_heartbeat(name: str, record: dict) -> None:
    client = redis_client()
    if client is None:
        return
    try:
        previous = read_heartbeat(name)
        # Keep the last success separately: "last run failed" and "has not
        # succeeded in nine days" are different alarms.
        if record.get("ok"):
            record["last_success"] = record.get("finished_at")
        elif previous.get("last_success"):
            record["last_success"] = previous["last_success"]
        record["runs"] = int(previous.get("runs", 0)) + 1
        client.set(HEARTBEAT_PREFIX + name, json.dumps(record, default=str),
                   ex=90 * 86400)
    except Exception:
        logger.warning("Scheduler: could not record heartbeat for %s", name,
                       exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Running a job
# ─────────────────────────────────────────────────────────────────────────────

def _call(target: str):
    """Run one target. Returns a short, JSON-safe summary of what it did."""
    if target.startswith("command:"):
        from django.core.management import call_command

        argv = shlex.split(target[len("command:"):])
        out, err = io.StringIO(), io.StringIO()
        call_command(*argv, stdout=out, stderr=err)
        text = (out.getvalue() + err.getvalue()).strip()
        # The last few lines are where every command here prints its totals.
        return "\n".join(text.splitlines()[-6:])[:1500]

    module, func = target.split(":", 1)
    result = getattr(importlib.import_module(module), func)()
    return result


@shared_task(name="accounts.scheduler.run_job", ignore_result=True)
def run_job(name: str) -> dict:
    job = jobs().get(name)
    if job is None:
        logger.error("Scheduler: no job called %r", name)
        return {"ok": False, "error": "unknown job"}

    started = timezone.now()
    record = {"name": name, "started_at": started.isoformat(),
              "host": socket.gethostname(), "pid": os.getpid()}

    needs = job.get("needs")
    if needs and not getattr(settings, needs, False):
        record.update(ok=True, skipped=f"{needs} is off",
                      finished_at=timezone.now().isoformat(), duration=0)
        _write_heartbeat(name, record)
        return record

    client = redis_client()
    lock = None
    if client is not None:
        lock = client.lock(LOCK_PREFIX + name,
                           timeout=max(300, job["max_age"]),
                           blocking=False)
        try:
            if not lock.acquire(blocking=False):
                record.update(ok=True, skipped="already running elsewhere",
                              finished_at=timezone.now().isoformat(),
                              duration=0)
                _write_heartbeat(name, record)
                return record
        except Exception:
            lock = None  # Redis hiccup: run anyway rather than not at all.

    from .asset_email import deliver_synchronously

    t0 = time.monotonic()
    try:
        with deliver_synchronously():
            summary = _call(job["target"])
        record.update(ok=True, summary=summary)
    except Exception as exc:
        logger.exception("Scheduler: job %s failed", name)
        record.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:1500])
    finally:
        record["duration"] = round(time.monotonic() - t0, 2)
        record["finished_at"] = timezone.now().isoformat()
        _write_heartbeat(name, record)
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
    return record


# ─────────────────────────────────────────────────────────────────────────────
# Job bodies that are not management commands
# ─────────────────────────────────────────────────────────────────────────────

def heartbeat() -> dict:
    """Nothing but proof of life: beat scheduled it and a worker ran it."""
    return {"alive": True}


def purge_security_events() -> str:
    """
    Retention for ICT security console events — when the console exists.

    Its tables are created by `ensure_security_console`, not by migrations, so
    on an installation that never ran it there is nothing to purge. That is
    reported as a skip rather than failing every night; `unpass_doctor` lists
    the missing tables separately.
    """
    from django.core.management import call_command

    from .security_console import event_table_exists

    if not event_table_exists():
        return "skipped: security console tables not created " \
               "(manage.py ensure_security_console)"
    out = io.StringIO()
    call_command("purge_security_events", "--days", "180", stdout=out)
    return out.getvalue().strip()[-300:]


def monthly_seal_register() -> str:
    """The seal register for the last 30 days, filed under MEDIA_ROOT."""
    from django.core.management import call_command

    folder = os.path.join(str(settings.MEDIA_ROOT), "nes-registers")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(
        folder, f"nes-seal-register-{timezone.localdate():%Y-%m}.pdf")
    out = io.StringIO()
    call_command("esign_pades", "--register", "30", "--out", path, stdout=out)
    return path


def audit_all_chains() -> dict:
    """
    Walk every envelope's hash chain and raise an alert if any is damaged.

    The verify page checks one envelope when someone opens it. That leaves
    damage nobody looks at undiscovered for as long as nobody looks — this
    looks every night.

    Forks are classified. Before the chain fix that ships with this file,
    two events written at the same instant could fork a chain with no one
    touching it. A fork whose branches were written within a few seconds of
    each other, before the fix, is reported as a probable concurrency fork,
    not as tampering — it is still listed, never hidden, and never repaired:
    rewriting a hash chain to make it look clean would destroy exactly the
    evidence it exists to keep.
    """
    from .audit_chain_esign import verify_chain
    from .models_esign import Envelope, EnvelopeEvent

    fix_date = getattr(settings, "NES_CHAIN_FIX_DATE", "2026-11-27")
    summary = {"envelopes": 0, "clean": 0, "unchained_only": 0,
               "damaged": [], "probable_concurrency_forks": []}

    for env in Envelope.objects.only("pk", "envelope_id").iterator():
        summary["envelopes"] += 1
        state = verify_chain(env)
        if state["chained"] == 0:
            summary["unchained_only"] += 1
            continue
        if state["ok"]:
            summary["clean"] += 1
            continue

        kinds = {p["kind"] for p in state["problems"]}
        if kinds == {"forked"} or kinds == {"forked", "broken_link"}:
            ats = list(EnvelopeEvent.objects.filter(
                pk__in=[p["event_id"] for p in state["problems"]]
            ).values_list("at", flat=True))
            if ats and max(ats).date().isoformat() < fix_date:
                summary["probable_concurrency_forks"].append(env.envelope_id)
                continue
        summary["damaged"].append({
            "envelope": env.envelope_id,
            "problems": sorted(kinds),
        })

    # The ledger guard: a rule that is missing or switched off is reported
    # with the damage, because it is how damage would have been let in.
    try:
        from django.db import connection

        from .db_guard import guard_status

        if connection.vendor == "postgresql":
            with connection.cursor() as cur:
                rules = guard_status(cur)
            off = [f"{r['table']}: {r['trigger']}" for r in rules
                   if r["present"] and not r["enabled"]]
            summary["guard_rules"] = len(rules)
            summary["guard_missing"] = sum(1 for r in rules if not r["present"])
            if off:
                summary["guard_disabled"] = off
                summary["damaged"].append({"envelope": "LEDGER GUARD",
                                           "problems": ["switched off: "
                                                        + "; ".join(off)]})
    except Exception:
        logger.warning("NES chain audit: could not read the guard state",
                       exc_info=True)

    if summary["damaged"]:
        _alert_chain_damage(summary["damaged"])
    return summary


def _alert_chain_damage(damaged: list[dict]) -> None:
    from .asset_email import send_email_async

    to = list(getattr(settings, "ESIGN_WITNESS_ALERT_EMAILS", []) or [])
    to += [email for _name, email in getattr(settings, "ADMINS", []) or []]
    if not to:
        logger.error("NES chain audit: %d damaged trail(s) and nobody to "
                     "tell — set ESIGN_WITNESS_ALERT_EMAILS", len(damaged))
        return
    rows = [[d["envelope"], ", ".join(d["problems"])] for d in damaged]
    send_email_async(
        subject=f"NES: {len(damaged)} audit trail(s) failed verification",
        to_emails=to,
        html_template="emails/expiry/digest.html",
        context={
            "subject": "Audit trails failed verification",
            "heading": "Audit trails failed verification",
            "intro": ("The nightly check found these envelopes' audit trails "
                      "do not verify. Treat it as an incident: do not re-seal "
                      "or edit anything, and compare against the evidence "
                      "packages and other offices' witnessed copies first."),
            "rows": rows,
            "columns": ["Envelope", "What failed"],
            "action_url": "",
            "brand": "NES",
            "footer": ("Sent by the nightly audit-trail check. It repeats "
                       "every night until the cause is understood."),
        },
    )
