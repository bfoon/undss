#!/bin/sh
# NES Sentinel — Linux installer.
#
#   sudo ./install-linux.sh                      # installs and starts nothing yet
#   sudo -u nes-sentinel NES_SENTINEL_HOME=/var/lib/nes-sentinel \
#        /opt/nes-sentinel/nes-sentinel enroll --server https://unpass.example.org --code NES-…
#   sudo systemctl enable --now nes-sentinel
#
# Uses the prebuilt binary `nes-sentinel` next to this script if present;
# otherwise installs nes_sentinel.py into a private virtualenv.
set -eu

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)."; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX=/opt/nes-sentinel
STATE=/var/lib/nes-sentinel

if [ -f "$HERE/SHA256SUMS" ] && command -v sha256sum >/dev/null; then
  (cd "$HERE" && sha256sum --ignore-missing -c SHA256SUMS) || { echo "Checksum mismatch — do not install."; exit 1; }
fi

id nes-sentinel >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin nes-sentinel
install -d -m 0755 "$PREFIX"
install -d -m 0700 -o nes-sentinel -g nes-sentinel "$STATE"

if [ -x "$HERE/nes-sentinel" ]; then
  install -m 0755 "$HERE/nes-sentinel" "$PREFIX/nes-sentinel"
else
  command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
  python3 -m venv "$PREFIX/venv"
  "$PREFIX/venv/bin/pip" install --quiet --upgrade pip
  "$PREFIX/venv/bin/pip" install --quiet -r "$HERE/requirements.txt"
  install -m 0644 "$HERE/nes_sentinel.py" "$PREFIX/nes_sentinel.py"
  cat > "$PREFIX/nes-sentinel" <<SH
#!/bin/sh
exec $PREFIX/venv/bin/python -I $PREFIX/nes_sentinel.py "\$@"
SH
  chmod 0755 "$PREFIX/nes-sentinel"
fi
[ -f "$HERE/README.md" ] && install -m 0644 "$HERE/README.md" "$PREFIX/README.md"
install -m 0644 "$HERE/nes-sentinel.service" /etc/systemd/system/nes-sentinel.service
ln -sf "$PREFIX/nes-sentinel" /usr/local/bin/nes-sentinel
systemctl daemon-reload

cat <<MSG

Installed. Now, with the one-time code ICT gave you:

  sudo -u nes-sentinel NES_SENTINEL_HOME=$STATE nes-sentinel enroll \\
       --server https://YOUR-UNPASS-ADDRESS --code NES-XXXXX-XXXXX-XXXXX-XXXXX

  sudo -u nes-sentinel NES_SENTINEL_HOME=$STATE nes-sentinel set-email \\
       --host smtp.your-office.org --port 587 --user sentinel@your-office.org \\
       --from sentinel@your-office.org --to ict@your-office.org

  sudo systemctl enable --now nes-sentinel
  journalctl -u nes-sentinel -f
MSG
