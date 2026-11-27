#!/bin/sh
# UN PASS — nightly PostgreSQL backup.
#
# Runs in the db-backup container (docker-compose.yml). Once a day at
# BACKUP_AT it writes a compressed custom-format dump to /backups, then READS
# IT BACK with pg_restore before keeping it. A backup that has never been
# read is a hope, not a backup: a dump truncated by a full disk looks exactly
# like a good one until the day it is needed.
#
# Keeps BACKUP_KEEP_DAYS days. Writes /backups/LAST_OK on success, which
# `manage.py unpass_doctor` reads to say how old the newest good backup is.
#
# Restore with:
#   docker compose exec -T db pg_restore -U postgres -d un_security --clean \
#       < backups/un_security-YYYYMMDD-HHMM.dump
#
# Copy ./backups OFF this machine as well. A backup on the same disk as the
# database survives a bad migration, not a dead disk.

set -u
KEEP="${BACKUP_KEEP_DAYS:-14}"
AT="${BACKUP_AT:-01:50}"
DIR=/backups

mkdir -p "$DIR"

backup() {
    stamp=$(date +%Y%m%d-%H%M)
    out="$DIR/${PGDATABASE}-${stamp}.dump"
    tmp="$out.partial"

    if ! pg_dump --format=custom --compress=6 --no-owner --file="$tmp"; then
        echo "$(date -Is) backup FAILED: pg_dump" >&2
        rm -f "$tmp"
        return 1
    fi
    # Prove it is readable end to end before trusting it.
    if ! pg_restore --list "$tmp" > /dev/null; then
        echo "$(date -Is) backup FAILED: dump does not read back" >&2
        rm -f "$tmp"
        return 1
    fi
    mv "$tmp" "$out"
    size=$(du -h "$out" | cut -f1)
    printf '%s %s %s\n' "$(date -Is)" "$(basename "$out")" "$size" > "$DIR/LAST_OK"
    echo "$(date -Is) backup ok: $(basename "$out") ($size)"

    find "$DIR" -name "${PGDATABASE}-*.dump" -mtime +"$KEEP" -print -delete
    find "$DIR" -name "*.partial" -mmin +120 -delete
    return 0
}

seconds_until() {
    # Seconds from now until the next HH:MM, today or tomorrow.
    now=$(date +%s)
    target=$(date -d "$(date +%Y-%m-%d) $1" +%s 2>/dev/null) || target=$now
    [ "$target" -le "$now" ] && target=$((target + 86400))
    echo $((target - now))
}

# One immediately on first start, so a new installation is never without one.
[ -f "$DIR/LAST_OK" ] || backup

while true; do
    sleep "$(seconds_until "$AT")"
    backup || { sleep 900; backup; }   # one retry, fifteen minutes later
done
