# UN PASS — operations: scheduled jobs, the database, the audit trail

What has to be running for UN PASS to keep its promises, how to tell whether
it is, and what to do when it is not.

    python manage.py unpass_doctor

is the one command to remember. It checks all three areas below and exits
non-zero when anything is wrong, so it can drive an alert.

---

## 1. Scheduled jobs

### What was wrong

UN PASS had the code for its time-based work and nothing that ran it.

* `esign_send_due_reminders` was a Celery task, but the project had **no
  Celery app**, no broker setting and no worker or beat container. Envelopes
  never expired and no signer was ever reminded.
* `requirements.txt` pinned `celery==5.3.4` and `redis==5.0.1` but not
  `kombu`. pip resolves kombu 5.7, whose Redis transport refuses redis 5.0.1 —
  so a worker, had one been started, would have died on its first connection.
* Emails were sent on a daemon thread with `fail_silently=True`. From a cron
  command or a worker restart, the process can exit before the thread sends:
  mail lost, no error anywhere.
* An envelope that expired changed status silently — no audit entry, nobody
  told.
* Parking cards, staff ID cards and module licences all carry expiry dates and
  nothing ever warned anyone. Each was discovered on the day it failed.

### What runs now

`docker compose up -d` starts two new containers beside `web`:

| Container | Does |
|---|---|
| `worker` | runs the jobs |
| `beat` | decides when. **Exactly one, ever** — two would run every job twice |

The jobs, all in `un_security_system/schedule.py`:

| Job | When (Banjul) | What |
|---|---|---|
| `esign-deadlines` | hourly :07 | expire overdue envelopes (audit entry + email to sender); remind signers |
| `expiry-warnings` | daily 07:13 | parking cards, staff IDs, module licences — 30/14/7/1 days, on the day, once after |
| `esign-workflow-maintenance` | daily 07:23 | workflow step reminders, workbench retention |
| `nes-refresh-ltv` | daily 02:17 | archival timestamp refresh (only seals that are due) |
| `nes-seal-register` | 1st, 02:40 | monthly sealed register into `MEDIA_ROOT/nes-registers/` |
| `nes-witness-send` | every 15 min | push fingerprints to other offices |
| `nes-witness-audit` | daily 01:25 | compare stored documents against witnessed copies |
| `nes-identity-audit` | Mondays 06:10 | signature IDs still recompute |
| `nes-chain-audit` | daily 03:45 | every envelope's audit trail verifies; alert email if not |
| `security-events-purge` | daily 03:05 | 180-day retention for security console events |
| `scheduler-heartbeat` | every 5 min | proof that beat and a worker are both alive |

Jobs that depend on a feature (`needs` in the schedule) record "skipped" when
it is off, rather than failing every night.

Every run records a heartbeat in Redis — started, finished, ok, what it did.
That is what `unpass_doctor` reads, and how it can say "esign-deadlines has
not succeeded in 3 days" instead of nothing.

Every run takes a lock first, so a slow job or an accidental second beat
cannot send the same reminder twice. Expiry warnings remember which warnings
went out (per record, per expiry date, per distance), so a missed day is
caught up the next day and nothing is ever sent twice; renewing a card resets
its warnings.

### Running one by hand

    python manage.py unpass_doctor --run esign-deadlines

Runs it here, now, with the same lock and heartbeat as the scheduler.

---

## 2. The database

### Backups

A new `db-backup` container writes a compressed dump to `./backups` every
night at 01:50 and **reads each one back with `pg_restore --list` before
keeping it** — a dump truncated by a full disk otherwise looks exactly like a
good one. Fourteen are kept. `unpass_doctor` fails if the newest is more than
26 hours old.

Copy `./backups` off the machine as well. A backup on the same disk survives
a bad migration, not a dead disk.

Restore:

    docker compose exec -T db pg_restore -U postgres -d un_security --clean \
        < backups/un_security-YYYYMMDD-HHMM.dump

### Migrations exist only on the server — fix this first

Most apps have **no migration files in git**: `accounts` has one, from
September 2025, while its models have changed many times since. Each
deployment has been running `makemigrations` on the server, so the only record
of how the production database was built is a set of untracked files in the
server's project folder.

That breaks three things: a new server cannot be built to match, a backup
cannot be restored onto a fresh checkout, and the next time a migration file
*is* committed, `git pull` on the server refuses with "untracked working tree
files would be overwritten" — the error you have already seen for other
files.

`unpass_doctor` lists them. To fix it, **on the production server**, once:

    cd /path/to/undss
    git status --short -- '*/migrations/*.py'     # the files only this server has
    tar czf ~/migrations-backup-$(date +%F).tgz $(git ls-files --others --exclude-standard -- '*/migrations/*.py')
    git add -- '*/migrations/*.py'
    git commit -m "Commit the migrations the production database was built from"
    git push

From then on, run `makemigrations` on a development machine, commit the
result, and only `migrate` on the server.

**The same is true of four pieces of code.** The repository imports
`accounts/view_asset_management_legacy.py`, `accounts/catalog.py`,
`SECURITY_DASHBOARD_CODES` and `ForcePasswordChangeMiddleware`, none of which
are in git — a fresh clone cannot serve a single page. Production works, so
they exist on the server. `unpass_doctor` lists every code file that exists
only on the server, and every committed file edited there and never committed
back. Commit them in the same pass:

    git status --short -- '*.py' '*.html'

### The application should not connect as a superuser

It connects as `postgres`. A superuser can disable or drop any trigger —
including the one that makes signature IDs immutable — and can rewrite the
audit trail directly. The cryptographic checks still catch the result, but
the guard becomes a speed bump.

Verified on PostgreSQL 16: a role that can only read and write rows is
refused `DROP TRIGGER` and `ALTER TABLE … DISABLE TRIGGER`; `postgres` is not.

Create the role once, as `postgres`:

```sql
CREATE ROLE unpass_app LOGIN PASSWORD 'choose-a-long-one';
GRANT CONNECT ON DATABASE un_security TO unpass_app;
GRANT USAGE ON SCHEMA public TO unpass_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO unpass_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO unpass_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO unpass_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO unpass_app;
```

Then point `web`, `worker` and `beat` at it (`DB_USER=unpass_app` and its
password), and keep `postgres` for anything that changes the schema:

    docker compose run --rm -e DB_USER=postgres -e DB_PASSWORD=... web python manage.py migrate
    docker compose run --rm -e DB_USER=postgres -e DB_PASSWORD=... web python manage.py ensure_security_console

The reports page used to issue `CREATE TABLE IF NOT EXISTS` on every visit,
which a restricted role is refused even when the table exists. That is fixed
in this change; it was the one thing that would have broken.

### Tables created outside migrations

Some modules declare `managed = False` and build their tables with an
`ensure_*` command. `migrate` does not create them. `unpass_doctor` lists any
that are missing and the command that creates each.

### The default password

`docker-compose.yml` now reads `DB_PASSWORD` from the `.env` beside it, and
falls back to the old value so nothing changes until you set one. Setting it
does **not** change the password inside an existing database — the Postgres
image only reads it when the volume is first created. Do both:

    docker compose exec db psql -U postgres -c "ALTER ROLE postgres PASSWORD '...';"
    # then put DB_PASSWORD=... in .env and: docker compose up -d

---

## 3. The audit trail (`prev_hash` chain)

### Two bugs, both false alarms

**Concurrent writes forked the chain.** Each new entry was linked to "the
chained entry with the highest id". When two events on one envelope are
written at the same moment — two signers, or a view and a reminder — the
second insert can take the lock first, and the next entry is then linked to
the wrong predecessor. The verifier walked rows in id order and reported
tampering on envelopes nobody had touched. On PostgreSQL, 24 simultaneous
writers per envelope produced 40-odd forks per round and a false alarm on
every envelope.

Fixed by defining the chain by its links, as a hash chain is meant to be:
the head is the entry nothing points to, the verifier follows links from the
start, and whoever holds the lock chains every pending entry in insertion
order. Same test: zero forks, zero false alarms. Real edits, deletions and
forged insertions are all still caught — a forged entry slotted into the
middle now shows up specifically as a fork.

**Every download broke the anchor.** The anchor signed at completion records
the trail's length and the hash it ended on. The check required the trail to
*still* end there — but every download of the signed copy appends an entry.
So every completed envelope failed its anchor check the first time anyone
downloaded it. The check now confirms the anchored entry is exactly where the
anchor says, with later entries chained after it, and flags any signing
activity recorded after completion.

### Envelopes damaged before the fix

Never repair them. Rewriting a hash chain to make it look clean destroys
exactly the evidence it exists to keep. The nightly `nes-chain-audit` job
lists forks whose branches were written within seconds of each other before
the fix date as *probable concurrency forks*, separately from damage it
cannot explain, and emails `ESIGN_WITNESS_ALERT_EMAILS` about the rest.

---

## 4. Deploying this change

First commit the server's existing migration files (section 2), so the one
new migration this change needs lands on top of the real history:

    git pull
    docker compose build
    docker compose run --rm web python manage.py makemigrations accounts   # one new choice on EnvelopeEvent
    docker compose run --rm web python manage.py migrate
    git add -- '*/migrations/*.py' && git commit -m "Migration: expired event" && git push
    docker compose up -d
    docker compose exec web python manage.py unpass_doctor

This change cannot ship that migration itself: the repository does not hold
the migrations your database was built from, so a file generated here would
not fit them. That is the problem section 2 fixes for good.

Expect FAILs for jobs that have "never run" for the first hour. Run any of
them at once with `--run NAME`, or wait for its slot.
