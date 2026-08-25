#!/usr/bin/env bash
# Installs the AskCruz email harvester on a 2-hourly *user* crontab.
#
# This is the no-root alternative to deploy/install.sh. Use it when you can log
# into the VPS but are not in the sudoers file -- systemd units need root, a
# user crontab does not.
#
#   bash deploy/install-cron.sh
#
# Trade-offs versus the systemd unit (deploy/install.sh), which is still the
# real deployment:
#   - runs as YOU, not as deploy, so it dies with your account
#   - no Persistent=true equivalent: a window missed while the box is down is
#     skipped, not caught up on boot
#   - logs to a file here instead of journald, outside the journalctl -u
#     workflow every other service on this box uses
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$APP_DIR/.venv/bin/python"
LOG="$APP_DIR/harvest.log"
# Marker so re-running this replaces our line instead of stacking duplicates.
MARKER="# askcruz-email-harvester"

if [[ ! -f "$APP_DIR/fetch_and_store.py" ]]; then
    echo "ERROR: fetch_and_store.py not found in $APP_DIR" >&2
    exit 1
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "ERROR: $APP_DIR/.env not found -- copy .env.example to .env and fill it in." >&2
    exit 1
fi

# The mailbox and database passwords live here; don't leave them group/world readable.
chmod 600 "$APP_DIR/.env"

if [[ ! -x "$PY" ]]; then
    echo "Creating virtualenv..."
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# cron runs with a near-empty PATH and no cwd, so use absolute paths throughout.
# Without the redirect cron would mail the output into a void nobody reads, and a
# failing run would be completely invisible.
CRON_LINE="0 */2 * * * cd $APP_DIR && $PY fetch_and_store.py >> $LOG 2>&1 $MARKER"

echo "Installing crontab entry..."
( crontab -l 2>/dev/null | grep -vF "$MARKER" || true; echo "$CRON_LINE" ) | crontab -

echo
echo "Installed. Current crontab:"
crontab -l | grep -F "$MARKER"

echo
echo "Run once now to verify:"
echo "  cd $APP_DIR && $PY fetch_and_store.py"
echo
echo "Watch the scheduled runs:"
echo "  tail -f $LOG"
