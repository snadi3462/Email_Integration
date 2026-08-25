# AskCruz Email Harvester

Fetches mail from the AskCruz Mailcow mailbox over IMAP and stores it in
Postgres, on a 2-hourly schedule.

## What this does

1. Connects to `ayan@askcruz.com` on Mailcow over IMAP (SSL, port 993).
2. Fetches every message in `INBOX`, `Sent`, and `Archive` (configurable via
   `MAILCOW_FOLDERS`). `Junk`/`Trash`/`Drafts` are deliberately excluded --
   spam and half-written text aren't business records.
3. Extracts sender, subject, date, and body for each.
4. Inserts new messages into `askcruz_emails`, skipping ones already stored
   (deduplicated on `message_id`). A message present in two folders stays one
   row -- the first folder in `MAILCOW_FOLDERS` wins, recorded in `folder`.
5. Runs every 2 hours via a systemd timer on the EOXS VPS.

## Requirements

- Python 3
- `psycopg2-binary` and `python-dotenv` (see `requirements.txt`)
- An IMAP app password for the mailbox (Mailcow -> mailbox -> App Passwords)
- A Postgres role with CREATE + INSERT on the target database

## Environment variables

All connection details come from environment variables.
**Never hardcode credentials in the script or commit them to this repo.**

| Variable | Description |
|---|---|
| `MAILCOW_HOST` | IMAP server hostname (e.g. `mailcow.askcruz.com`) |
| `MAILCOW_PORT` | IMAP port (default `993`) |
| `MAILCOW_USERNAME` | Mailbox address (e.g. `ayan@askcruz.com`) |
| `MAILCOW_PASSWORD` | The mailbox's IMAP app password (not the account password) |
| `MAILCOW_FOLDERS` | Comma-separated folders to harvest (default `INBOX,Sent,Archive`) |
| `DB_HOST` | Postgres host |
| `DB_PORT` | Postgres port (default `5432`) |
| `DB_NAME` | Postgres database (e.g. `eoxs_wiki_staging`) |
| `DB_USER` | Postgres role |
| `DB_PASSWORD` | Postgres password |

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored.

The script loads `.env` itself (from its own directory, so any working directory
works), which is what makes a manual run possible at all -- under systemd the
same values arrive via `EnvironmentFile=` instead. Anything already exported in
the environment wins over the file, so `set -a; source .env; set +a` still
behaves as you'd expect.

**Quote any value containing `#`.** systemd reads `.env` via `EnvironmentFile=`,
and an unquoted `#` can truncate the value, producing a confusing auth failure
against a password that looks correct in the file.

## Database

The script creates its table on first run:

```sql
CREATE TABLE IF NOT EXISTS askcruz_emails (
    id SERIAL PRIMARY KEY,
    message_id TEXT UNIQUE,
    from_address TEXT,
    subject TEXT,
    body TEXT,
    received_at TEXT,
    folder TEXT,
    fetched_at TIMESTAMPTZ DEFAULT now()
);
```

`folder` is added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` on every run, so
a table created by an earlier INBOX-only version upgrades in place.

## Running locally

Postgres on the EOXS VPS listens on `127.0.0.1` only, so reaching it from a
laptop needs an SSH tunnel in its own terminal:

```bash
ssh -L 5433:127.0.0.1:5432 deploy@5.223.44.95 -N
```

Then set `DB_HOST=127.0.0.1` and `DB_PORT=5433` in `.env` and run:

```bash
pip install -r requirements.txt
python fetch_and_store.py
```

If this exits with `Missing required environment variables:` listing everything,
`.env` isn't being found -- confirm it sits next to `fetch_and_store.py` and that
`python-dotenv` actually installed.

## Deployment

Runs on the VPS as a systemd timer, matching the shape of the existing
`eoxs-sweep.timer` in `eoxs-wiki-db`.

```bash
# on the server, from /home/deploy/askcruz-email-harvester
bash deploy/install.sh
```

That creates a venv, installs `deploy/askcruz-email.service` and
`deploy/askcruz-email.timer`, and enables the timer
(`OnCalendar=00/2:00:00`, `Persistent=true` so a reboot doesn't skip a window).

Check it:

```bash
systemctl list-timers askcruz-email.timer
sudo systemctl start askcruz-email.service    # run once now
journalctl -u askcruz-email.service -n 50 --no-pager
```

Logs go to journald under `SyslogIdentifier=askcruz-email`; there is no
separate log file.

## Current status

- [x] IMAP fetch working (verified against the live mailbox)
- [x] Multi-folder harvest (INBOX/Sent/Archive), cross-folder dedup
- [x] Postgres insert with dedup
- [x] systemd service + timer written
- [ ] Installed and running on the VPS
- [ ] Migrate to the production database once validated on staging
- [ ] Expand beyond a single mailbox to a shared/general inbox
