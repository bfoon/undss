# accounts/db_guard.py
"""
UN PASS — NES ledger guard: PostgreSQL rules that make the evidence add-only.

The hash chain DETECTS a changed audit trail. This makes most changes
IMPOSSIBLE in the first place, for anyone connecting as the application —
which is also what an attacker who takes the application's credentials
connects as. Each rule lives in the database, so it holds against the app,
against `QuerySet.update()`, against a Django shell and against psql.

What each table accepts
-----------------------
    audit entries      added, never edited or deleted — once an envelope
                       has been sent. Drafts that were never sent stay
                       editable: nothing happened that needs a record.
    anchors            set once at completion, then fixed
    envelopes          a sent one cannot be deleted (void it instead)
    signing identities never deleted; the ID never changes
    keys and marks     never deleted; may only be retired, never revived
    attestations       never deleted; only the verification result changes
    witness records    what other offices sent us: never changed at all
    every table above  no TRUNCATE

One change is allowed on purpose: a reference DETACHING. When a staff account
or an envelope recipient is deleted, Django sets the audit entries that
pointed at them to NULL. Those ids are part of each entry's hash, so before
this guard, deleting one user silently broke every trail they had touched —
the verifier reported tampering and the record of who signed was gone. Now
the guard lets the reference go to NULL and writes the original value to
`nes_guard_detached`, an append-only table; the verifier puts it back when it
recomputes the hash, and the trail still verifies, exactly.

What it cannot do
-----------------
The table owner — `postgres` — can drop these rules. That is why the
application must not connect as `postgres` (see `create_app_role_sql`, and
`manage.py unpass_harden_db`). With the app on a restricted role, removing a
guard takes the owner's password, and the database logs it
(`log_statement=ddl` in docker-compose.yml).
"""
from __future__ import annotations

import re

#: Statuses that mean an envelope has left draft at least once. Together with
#: "has a `sent` audit entry", this is what makes an envelope evidence. The
#: second test matters: a returned envelope can be reworked back to draft,
#: and its earlier round must stay on record.
EVIDENCE_SQL = """
CREATE OR REPLACE FUNCTION nes_envelope_is_evidence(env_id bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM accounts_envelope
        WHERE id = env_id AND status <> 'draft'
    ) OR EXISTS (
        SELECT 1 FROM accounts_envelopeevent
        WHERE envelope_id = env_id AND event = 'sent'
    );
$$;
"""

DETACH_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS nes_guard_detached (
    id          bigserial PRIMARY KEY,
    table_name  text        NOT NULL,
    row_id      bigint      NOT NULL,
    column_name text        NOT NULL,
    old_value   text,
    detached_at timestamptz NOT NULL DEFAULT now(),
    by_role     text        NOT NULL DEFAULT current_user
);
CREATE INDEX IF NOT EXISTS nes_guard_detached_row
    ON nes_guard_detached (table_name, row_id);
REVOKE ALL ON nes_guard_detached FROM PUBLIC;
"""

# One generic body. Every rule is expressed as "these columns may change,
# under this condition", over to_jsonb(row) — so adding a column to a model
# never silently opens a hole: a new column is protected by default.
FUNCTIONS_SQL = r"""
CREATE OR REPLACE FUNCTION nes_guard_changed(o jsonb, n jsonb)
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(array_agg(k ORDER BY k), '{}')
    FROM (SELECT jsonb_object_keys(n) AS k) keys
    WHERE (n -> k) IS DISTINCT FROM (o -> k);
$$;

CREATE OR REPLACE FUNCTION nes_guard_refuse(what text, hint text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'NES ledger guard: %', what
        USING ERRCODE = 'insufficient_privilege', HINT = hint;
END;
$$;

-- Audit entries --------------------------------------------------------
CREATE OR REPLACE FUNCTION nes_guard_envelopeevent()
RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    changed text[];
    col text;
    rest text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF nes_envelope_is_evidence(OLD.envelope_id) THEN
            PERFORM nes_guard_refuse(
                format('audit entry %s belongs to an envelope that was sent; '
                       'it cannot be deleted', OLD.id),
                'Void the envelope instead. Its audit trail is the record of '
                'what happened.');
        END IF;
        RETURN OLD;
    END IF;

    changed := nes_guard_changed(to_jsonb(OLD), to_jsonb(NEW));
    IF cardinality(changed) = 0 OR NOT nes_guard_envelope_check(OLD.envelope_id) THEN
        RETURN NEW;
    END IF;

    rest := changed;
    -- A reference may detach (go to NULL) when the person it names is
    -- deleted. The original value is kept so the hash still verifies.
    FOREACH col IN ARRAY ARRAY['recipient_id', 'actor_id'] LOOP
        IF col = ANY(changed) AND (to_jsonb(NEW) -> col) = 'null'::jsonb THEN
            INSERT INTO nes_guard_detached (table_name, row_id, column_name, old_value)
            VALUES ('accounts_envelopeevent', OLD.id, col, to_jsonb(OLD) ->> col);
            rest := array_remove(rest, col);
        END IF;
    END LOOP;

    -- Linking an entry into the chain: the hashes are set once.
    IF OLD.entry_hash = '' THEN
        rest := array_remove(array_remove(rest, 'prev_hash'), 'entry_hash');
    END IF;

    IF cardinality(rest) > 0 THEN
        PERFORM nes_guard_refuse(
            format('audit entry %s of a sent envelope cannot be changed '
                   '(attempted: %s)', OLD.id, array_to_string(rest, ', ')),
            'Audit entries are added, never edited. If a user or recipient '
            'is being removed, deactivate the account instead of changing '
            'the entries that name it.');
    END IF;
    RETURN NEW;
END;
$$;

-- Evidence status for an UPDATE: the same test, kept separate so a future
-- refinement does not have to touch the trigger body.
CREATE OR REPLACE FUNCTION nes_guard_envelope_check(env_id bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT nes_envelope_is_evidence(env_id);
$$;

-- Envelopes ------------------------------------------------------------
CREATE OR REPLACE FUNCTION nes_guard_envelope()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF nes_envelope_is_evidence(OLD.id) THEN
        PERFORM nes_guard_refuse(
            format('envelope %s was sent and cannot be deleted', OLD.envelope_id),
            'Void it instead: that notifies everyone and keeps the record.');
    END IF;
    RETURN OLD;
END;
$$;

-- Seals: the audit anchor is set once ----------------------------------
CREATE OR REPLACE FUNCTION nes_guard_envelopeseal()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    touched boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF nes_envelope_is_evidence(OLD.envelope_id) THEN
            PERFORM nes_guard_refuse(
                format('the seal record of envelope %s cannot be deleted',
                       OLD.envelope_id), 'Seal records are evidence.');
        END IF;
        RETURN OLD;
    END IF;
    IF coalesce(OLD.audit_chain_head, '') = '' THEN
        RETURN NEW;
    END IF;
    -- The anchor signature may be ADDED once to a head that was recorded
    -- unsigned (no certificate at completion); it can never be replaced.
    touched := nes_guard_changed(to_jsonb(OLD), to_jsonb(NEW))
               && ARRAY['audit_chain_head', 'audit_chain_length',
                        'audit_anchored_at']::text[]
               OR (coalesce(OLD.audit_anchor, '') <> ''
                   AND coalesce(NEW.audit_anchor, '') IS DISTINCT FROM OLD.audit_anchor);
    IF touched THEN
        PERFORM nes_guard_refuse(
            format('the audit anchor of envelope %s was set at completion and '
                   'cannot be changed', OLD.envelope_id),
            'The anchor is what proves the trail was not cut short.');
    END IF;
    RETURN NEW;
END;
$$;

-- Generic: "only these columns may change", plus no deletes -------------
CREATE OR REPLACE FUNCTION nes_guard_columns()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    allowed text[] := TG_ARGV[0]::text[];
    changed text[];
    rest text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM nes_guard_refuse(
            format('%s row %s cannot be deleted', TG_TABLE_NAME, OLD.id),
            'Signing identities, keys, marks, attestations and witness records '
            'are kept for life. Deactivate the user account instead of '
            'deleting it.');
        RETURN OLD;
    END IF;
    changed := nes_guard_changed(to_jsonb(OLD), to_jsonb(NEW));
    SELECT coalesce(array_agg(c), '{}') INTO rest
    FROM unnest(changed) c WHERE NOT c = ANY(allowed);
    IF cardinality(rest) > 0 THEN
        PERFORM nes_guard_refuse(
            format('%s row %s: %s cannot be changed', TG_TABLE_NAME, OLD.id,
                   array_to_string(rest, ', ')),
            'Only the fields that record a later event may change here.');
    END IF;
    -- A retired key or mark stays retired: no reviving an old key to sign
    -- with after the person has replaced it.
    IF 'is_current' = ANY(changed)
       AND (to_jsonb(OLD) ->> 'is_current')::boolean = false THEN
        PERFORM nes_guard_refuse(
            format('%s row %s was retired and cannot be made current again',
                   TG_TABLE_NAME, OLD.id),
            'Enrol a new key or mark instead.');
    END IF;
    RETURN NEW;
END;
$$;

-- No TRUNCATE anywhere guarded ------------------------------------------
CREATE OR REPLACE FUNCTION nes_guard_no_truncate()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM nes_guard_refuse(format('%s cannot be truncated', TG_TABLE_NAME),
                             'These tables are evidence.');
    RETURN NULL;
END;
$$;

-- The detach log itself is append-only ----------------------------------
CREATE OR REPLACE FUNCTION nes_guard_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM nes_guard_refuse(format('%s is append-only', TG_TABLE_NAME),
                             'Rows here are evidence.');
    RETURN NULL;
END;
$$;
"""

#: (table, trigger name, timing/events, function call). Order matters only
#: for readability; each is dropped and recreated, so installing twice is
#: harmless.
TRIGGERS = [
    ("accounts_envelopeevent", "nes_guard_envelopeevent_trg",
     "BEFORE UPDATE OR DELETE", "FOR EACH ROW", "nes_guard_envelopeevent()"),
    ("accounts_envelope", "nes_guard_envelope_trg",
     "BEFORE DELETE", "FOR EACH ROW", "nes_guard_envelope()"),
    ("accounts_envelopeseal", "nes_guard_envelopeseal_trg",
     "BEFORE UPDATE OR DELETE", "FOR EACH ROW", "nes_guard_envelopeseal()"),
    ("accounts_signatureidentity", "nes_guard_signatureidentity_del_trg",
     "BEFORE DELETE", "FOR EACH ROW", "nes_guard_columns('{}')"),
    ("accounts_signaturekey", "nes_guard_signaturekey_trg",
     "BEFORE UPDATE OR DELETE", "FOR EACH ROW",
     "nes_guard_columns('{is_current,retired_at,retired_reason}')"),
    ("accounts_signaturemark", "nes_guard_signaturemark_trg",
     "BEFORE UPDATE OR DELETE", "FOR EACH ROW",
     "nes_guard_columns('{is_current,replaced_at}')"),
    ("accounts_signatureattestation", "nes_guard_signatureattestation_trg",
     "BEFORE UPDATE OR DELETE", "FOR EACH ROW",
     "nes_guard_columns('{verified,verify_detail,verified_at}')"),
    ("accounts_witnessrecord", "nes_guard_witnessrecord_trg",
     "BEFORE UPDATE OR DELETE", "FOR EACH ROW", "nes_guard_columns('{}')"),
    ("accounts_sentinelblock", "nes_guard_sentinelblock_trg",
     "BEFORE UPDATE OR DELETE", "FOR EACH ROW", "nes_guard_append_only()"),
    ("accounts_sentinelalert", "nes_guard_sentinelalert_trg",
     "BEFORE UPDATE OR DELETE", "FOR EACH ROW",
     "nes_guard_columns('{notified_at,resolved_at,resolved_by_id,resolution_note}')"),
    ("nes_guard_detached", "nes_guard_detached_trg",
     "BEFORE UPDATE OR DELETE", "FOR EACH ROW", "nes_guard_append_only()"),
]

TRUNCATE_TABLES = [
    "accounts_envelopeevent", "accounts_envelope", "accounts_envelopeseal",
    "accounts_signatureidentity", "accounts_signaturekey",
    "accounts_signaturemark", "accounts_signatureattestation",
    "accounts_witnessrecord", "nes_guard_detached",
    "accounts_sentinelblock", "accounts_sentinelalert",
]

#: The identity rule that already shipped (esign_identity --install-guard).
#: Kept by name so both commands manage the same trigger.
IDENTITY_TRIGGER = "nes_signature_identity_immutable_trg"


def _truncate_name(table: str) -> str:
    return f"nes_guard_{table}_trunc_trg"[:63]


def expected_triggers() -> list[tuple[str, str]]:
    """(table, trigger) for every rule, including the identity one."""
    out = [(t, name) for t, name, *_rest in TRIGGERS]
    out += [(t, _truncate_name(t)) for t in TRUNCATE_TABLES]
    out.append(("accounts_signatureidentity", IDENTITY_TRIGGER))
    return out


def install_sql(existing_tables: set[str]) -> list[str]:
    """Every statement needed, skipping tables this database does not have."""
    from .management.commands.esign_identity import GUARD_SQL as IDENTITY_SQL

    statements = [DETACH_TABLE_SQL, EVIDENCE_SQL, FUNCTIONS_SQL]
    existing = set(existing_tables) | {"nes_guard_detached"}
    for table, name, timing, each, call in TRIGGERS:
        if table not in existing:
            continue
        statements.append(
            f"DROP TRIGGER IF EXISTS {name} ON {table};\n"
            f"CREATE TRIGGER {name} {timing} ON {table} {each} "
            f"EXECUTE FUNCTION {call};")
    for table in TRUNCATE_TABLES:
        if table not in existing:
            continue
        name = _truncate_name(table)
        statements.append(
            f"DROP TRIGGER IF EXISTS {name} ON {table};\n"
            f"CREATE TRIGGER {name} BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION nes_guard_no_truncate();")
    if "accounts_signatureidentity" in existing:
        statements.append(IDENTITY_SQL)
    return statements


def guard_status(cursor) -> list[dict]:
    """Each expected rule: present, and enabled ('O' = fires normally)."""
    cursor.execute(
        "SELECT c.relname, t.tgname, t.tgenabled FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal "
        "AND t.tgname LIKE 'nes_%'")
    found = {(rel, name): enabled for rel, name, enabled in cursor.fetchall()}
    cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    tables = {r[0] for r in cursor.fetchall()}
    out = []
    for table, name in expected_triggers():
        if table not in tables:
            continue
        state = found.get((table, name))
        out.append({"table": table, "trigger": name,
                    "present": state is not None,
                    "enabled": state in ("O", "A")})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# The restricted application role
# ─────────────────────────────────────────────────────────────────────────────

_ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def create_app_role_sql(role: str, password: str, database: str,
                        owner: str) -> list[str]:
    """
    A role that can read and write rows and do nothing else.

    It owns no table, so it cannot drop, disable or alter a guard; it has no
    CREATE on the schema, so it cannot add objects; and default privileges
    give it rows in tables the owner creates later, so a future `migrate`
    run as the owner needs no extra grant.
    """
    for name in (role, owner):
        if not _ROLE.match(name):
            raise ValueError(f"Not a safe role name: {name!r}")
    if not re.match(r"^[A-Za-z0-9_]{1,63}$", database):
        raise ValueError(f"Not a safe database name: {database!r}")
    pw = password.replace("'", "''")
    return [
        f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN PASSWORD '{pw}';
            ELSE
                ALTER ROLE {role} LOGIN PASSWORD '{pw}';
            END IF;
        END $$;""",
        f"ALTER ROLE {role} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
        f"NOBYPASSRLS;",
        f"GRANT CONNECT ON DATABASE {database} TO {role};",
        f"REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
        f"GRANT USAGE ON SCHEMA public TO {role};",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        f"TO {role};",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role};",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role};",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {role};",
        # The detach log: readable by the app, written only by the guard.
        f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON nes_guard_detached "
        f"FROM {role};",
        f"GRANT SELECT ON nes_guard_detached TO {role};",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# For the verifier
# ─────────────────────────────────────────────────────────────────────────────

def detached_values(event_ids) -> dict[int, dict[str, str | None]]:
    """
    {event id: {column: original value}} for references the guard saw go to
    NULL. Empty where the guard is not installed (SQLite, or not yet run).
    """
    from django.db import connection

    ids = [int(i) for i in event_ids]
    if not ids or connection.vendor != "postgresql":
        return {}
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT row_id, column_name, old_value FROM nes_guard_detached "
                "WHERE table_name = 'accounts_envelopeevent' AND row_id = ANY(%s) "
                "ORDER BY id", [ids])
            out: dict[int, dict] = {}
            for row_id, column, value in cur.fetchall():
                # The FIRST detachment is the value the hash was taken over.
                out.setdefault(row_id, {}).setdefault(column, value)
            return out
    except Exception:
        return {}
