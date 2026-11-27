#!/bin/sh
# Create the NES Sentinel publication key on the HOST, in ./secrets, which the
# containers mount read-only at /etc/unpass. Run once, from the repo root.
#
#   scripts/sentinel_keygen.sh
#   scripts/owner.sh nes_sentinel status      # shows the fingerprint
#
# Back the file up with the seal key. Replacing it later makes every enrolled
# Sentinel raise "server_identity_changed" and need re-enrolling.
set -eu
umask 077
OUT=${1:-secrets/nes_sentinel_ed25519.pem}
[ -e "$OUT" ] && { echo "$OUT already exists — refusing to replace it."; exit 1; }
mkdir -p "$(dirname "$OUT")"
openssl genpkey -algorithm ed25519 -out "$OUT"
chmod 600 "$OUT"
echo "Created $OUT"
echo "Fingerprint (read this to each office that installs a Sentinel):"
openssl pkey -in "$OUT" -pubout -outform DER | tail -c 32 | sha256sum | cut -c1-32 \
  | sed 's/.\{4\}/& /g' | tr 'a-f' 'A-F'
