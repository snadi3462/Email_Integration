#!/usr/bin/env bash
# Installs the AskCruz email harvester as a 2-hourly systemd timer.
#
# Run ON the VPS, as any user with sudo (you do not have to BE deploy):
#   bash deploy/install.sh
#
# The service itself runs as deploy, matching every other unit on this box.
# Expects the repo already cloned to APP_DIR with a filled-in .env alongside it.
set -euo pipefail

APP_DIR=/home/deploy/askcruz-email-harvester
RUN_AS=deploy

if [[ ! -f "$APP_DIR/fetch_and_store.py" ]]; then
    echo "ERROR: $APP_DIR/fetch_and_store.py not found -- clone the repo there first." >&2
    exit 1
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "ERROR: $APP_DIR/.env not found -- copy .env.example to .env and fill it in." >&2
    exit 1
fi

# Everything the service touches must be owned by the user it runs as, and the
# secrets file must not be world-readable. Use sudo so this works whether the
# installer is deploy or a separate sudo-capable admin account.
sudo chown -R "$RUN_AS:$RUN_AS" "$APP_DIR"
sudo chmod 600 "$APP_DIR/.env"

# Its own venv, matching how eoxs-wiki-db isolates its dependencies.
if [[ ! -x "$APP_DIR/.venv/bin/python3" ]]; then
    echo "Creating virtualenv..."
    sudo -u "$RUN_AS" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$RUN_AS" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$RUN_AS" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "Installing systemd units..."
sudo cp "$APP_DIR/deploy/askcruz-email.service" /etc/systemd/system/
sudo cp "$APP_DIR/deploy/askcruz-email.timer"   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now askcruz-email.timer

echo
echo "Installed. Next scheduled run:"
systemctl list-timers askcruz-email.timer --no-pager

echo
echo "Trigger one run now and watch it:"
echo "  sudo systemctl start askcruz-email.service"
echo "  journalctl -u askcruz-email.service -n 50 --no-pager"
