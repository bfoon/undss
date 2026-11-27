# un_security_system/schedule.py
"""
UN PASS — every job that has to run on a clock, in one place.

Before this file, UN PASS had the code for a dozen time-based jobs and nothing
that ran any of them: Celery tasks with no Celery app, management commands
documented as "put this in cron" that nobody had. Envelopes never expired,
signers were never reminded, archival timestamps were never refreshed, and
nothing said so — a job that never runs fails silently by definition.

So this list is used three ways, and that is the point of it living here:

    celery beat     reads it to know WHEN to run each job
    the worker      reads it to know WHAT each job is
    unpass_doctor   reads it to know how STALE each job is allowed to get,
                    and says so loudly when one has stopped

Times are in TIME_ZONE (Africa/Banjul). Minutes are deliberately not :00 —
everything else on the internet runs on the hour.

Fields
------
name       stable identifier; also the Redis heartbeat key
target     "module:function" called with no arguments, or
           "command:<name> <args...>" for a management command
cron       minute hour day-of-month month day-of-week
max_age    seconds after which a missing successful run is an alarm
needs      a setting that must be truthy for the job to do anything; when it
           is falsy the job records "skipped" rather than failing
about      one line, shown by the doctor
"""

JOBS = [
    {
        "name": "esign-deadlines",
        "target": "accounts.tasks:esign_send_due_reminders",
        "cron": "7 * * * *",
        "max_age": 2 * 3600,
        "about": "Expires overdue envelopes and reminds signers whose turn it is.",
    },
    {
        "name": "expiry-warnings",
        "target": "accounts.expiry_notify:run",
        "cron": "13 7 * * *",
        "max_age": 26 * 3600,
        "about": "Advance notice for parking cards, staff ID cards and module licences.",
    },
    {
        "name": "esign-workflow-maintenance",
        "target": "command:esign_studio_maintenance",
        "cron": "23 7 * * *",
        "max_age": 26 * 3600,
        "about": "Workflow step reminders and PDF workbench retention.",
    },
    {
        "name": "nes-refresh-ltv",
        "target": "command:esign_pades --refresh-ltv",
        "cron": "17 2 * * *",
        "max_age": 26 * 3600,
        "needs": "ESIGN_PADES_ENABLED",
        "about": "Renews archival timestamps that are due. A lapsed chain cannot be repaired.",
    },
    {
        "name": "nes-seal-register",
        "target": "accounts.scheduler:monthly_seal_register",
        "cron": "40 2 1 * *",
        "max_age": 32 * 86400,
        "needs": "ESIGN_PADES_ENABLED",
        "about": "Sealed register of everything sealed in the last month.",
    },
    {
        "name": "nes-witness-send",
        "target": "command:esign_witness --send-pending",
        "cron": "*/15 * * * *",
        "max_age": 3600,
        "needs": "ESIGN_WITNESS_ENABLED",
        "about": "Pushes new fingerprints to participating offices.",
    },
    {
        "name": "nes-witness-audit",
        "target": "command:esign_witness --audit",
        "cron": "25 1 * * *",
        "max_age": 26 * 3600,
        "needs": "ESIGN_WITNESS_ENABLED",
        "about": "Checks stored documents against what other offices witnessed.",
    },
    {
        "name": "nes-identity-audit",
        "target": "command:esign_identity --audit",
        "cron": "10 6 * * 1",
        "max_age": 8 * 86400,
        "about": "Confirms every signature ID still recomputes and nothing immutable moved.",
    },
    {
        "name": "nes-key-watch",
        "target": "accounts.key_watch:sweep",
        "cron": "40 6 * * *",
        "max_age": 26 * 3600,
        "about": "Emails anyone whose signature gained a key they were not told about.",
    },
    {
        "name": "nes-chain-audit",
        "target": "accounts.scheduler:audit_all_chains",
        "cron": "45 3 * * *",
        "max_age": 26 * 3600,
        "about": "Walks every envelope's hash chain; emails an alert if one is damaged.",
    },
    {
        "name": "security-events-purge",
        "target": "accounts.scheduler:purge_security_events",
        "cron": "5 3 * * *",
        "max_age": 26 * 3600,
        "about": "Removes ICT security console events older than 180 days.",
    },
    {
        "name": "scheduler-heartbeat",
        "target": "accounts.scheduler:heartbeat",
        "cron": "*/5 * * * *",
        "max_age": 15 * 60,
        "about": "Proves beat and a worker are both alive. Silence here means nothing runs.",
    },
]


def beat_schedule() -> dict:
    """CELERY_BEAT_SCHEDULE built from JOBS. Imported by settings."""
    from celery.schedules import crontab

    out = {}
    for job in JOBS:
        minute, hour, dom, month, dow = job["cron"].split()
        out[f"unpass-{job['name']}"] = {
            "task": "accounts.scheduler.run_job",
            "schedule": crontab(minute=minute, hour=hour, day_of_month=dom,
                                month_of_year=month, day_of_week=dow),
            "args": (job["name"],),
            # A job that missed its slot by more than its own window is not
            # worth running late in a burst; the next slot will do it.
            "options": {"expires": max(60, min(job["max_age"] // 2, 6 * 3600))},
        }
    return out
