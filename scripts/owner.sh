#!/bin/sh
# Run one manage.py command as the database OWNER, then go back to normal.
#
# Once the application connects as a restricted role (see
# `manage.py unpass_harden_db --create-app-role`), anything that changes the
# schema — migrate, the ensure_* commands, installing the ledger guard — has
# to run as the owner instead. This does exactly that for one command, in a
# throwaway container, without touching the running ones:
#
#   ./scripts/owner.sh migrate
#   ./scripts/owner.sh ensure_security_console
#   ./scripts/owner.sh unpass_harden_db --install-guards
#
# It reads DB_PASSWORD from the .env beside docker-compose.yml — the owner's
# password, which the running containers no longer have.
set -eu
cd "$(dirname "$0")/.."

if [ $# -eq 0 ]; then
    echo "usage: ./scripts/owner.sh <manage.py command> [args...]" >&2
    exit 2
fi

OWNER_PASSWORD="postgres"
if [ -f .env ]; then
    line=$(grep -E '^DB_PASSWORD=' .env | tail -1 || true)
    [ -n "$line" ] && OWNER_PASSWORD="${line#DB_PASSWORD=}"
fi

exec docker compose run --rm --no-deps \
    -e DB_USER=postgres \
    -e DB_PASSWORD="$OWNER_PASSWORD" \
    web python manage.py "$@"
