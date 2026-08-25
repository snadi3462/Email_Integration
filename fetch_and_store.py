import imaplib
import email
import os
import sys
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    print(
        "python-dotenv is not installed -- run: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise

# Under systemd the variables arrive via EnvironmentFile=, so the environment is
# already populated and there may be no .env next to this file at all. Run by hand
# and nothing would load it, which used to fail with a bare "Missing required
# environment variables" naming every one of them. override=False so a value
# already exported (systemd, or `set -a; source .env`) always wins over the file.
# Anchored to this file's own directory so the run works from any cwd.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

MAILCOW_HOST = os.environ.get("MAILCOW_HOST")
MAILCOW_PORT = int(os.environ.get("MAILCOW_PORT", "993"))
MAILCOW_USERNAME = os.environ.get("MAILCOW_USERNAME")
MAILCOW_PASSWORD = os.environ.get("MAILCOW_PASSWORD")
# Which IMAP folders to harvest. Received mail alone misses half of any
# conversation, so Sent and Archive are included by default; Junk/Trash/Drafts
# deliberately are not -- spam and half-written text aren't business records.
MAILCOW_FOLDERS = [
    f.strip() for f in os.environ.get("MAILCOW_FOLDERS", "INBOX,Sent,Archive").split(",")
    if f.strip()
]

DB_HOST = os.environ.get("DB_HOST")
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))

REQUIRED_VARS = {
    "MAILCOW_HOST": MAILCOW_HOST,
    "MAILCOW_USERNAME": MAILCOW_USERNAME,
    "MAILCOW_PASSWORD": MAILCOW_PASSWORD,
    "DB_HOST": DB_HOST,
    "DB_NAME": DB_NAME,
    "DB_USER": DB_USER,
    "DB_PASSWORD": DB_PASSWORD,
}


def check_env():
    missing = [name for name, value in REQUIRED_VARS.items() if not value]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def decode_str(value):
    if value is None:
        return ""
    decoded, encoding = decode_header(value)[0]
    if isinstance(decoded, bytes):
        return decoded.decode(encoding or "utf-8", errors="replace")
    return decoded


def extract_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                return payload.decode(errors="replace") if payload else ""
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                return payload.decode(errors="replace") if payload else ""
        return ""
    else:
        payload = msg.get_payload(decode=True)
        return payload.decode(errors="replace") if payload else ""


def fetch_folder(mail, folder):
    """Fetch every message in one IMAP folder. Returns [] if the folder is absent.

    A missing folder is not an error worth aborting the whole run for -- mailbox
    layouts differ, and losing Sent because Archive was renamed would be worse
    than skipping one folder with a warning.
    """
    status, _ = mail.select(f'"{folder}"', readonly=True)
    if status != "OK":
        print(f"  {folder}: not found, skipping.", file=sys.stderr)
        return []

    status, data = mail.search(None, "ALL")
    if status != "OK":
        raise RuntimeError(f"IMAP search failed in {folder}: {status}")

    message_nums = data[0].split()
    print(f"  {folder}: {len(message_nums)} message(s).")

    records = []
    for num in message_nums:
        status, msg_data = mail.fetch(num, "(RFC822)")
        if status != "OK" or not msg_data or msg_data[0] is None:
            continue
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        message_id = msg.get("Message-ID")
        if not message_id:
            # Fall back to a synthetic id so we never silently drop a message,
            # but this should be rare -- most MTAs always set Message-ID. The
            # folder is part of the id because message numbers restart per folder.
            message_id = f"<no-message-id-{folder}-{num.decode()}@{MAILCOW_USERNAME}>"

        from_addr = decode_str(msg["From"])
        subject = decode_str(msg["Subject"])
        date_header = msg["Date"]
        body = extract_body(msg)

        records.append((message_id, from_addr, subject, body, date_header, folder))

    return records


def fetch_all_emails():
    mail = imaplib.IMAP4_SSL(MAILCOW_HOST, MAILCOW_PORT)
    mail.login(MAILCOW_USERNAME, MAILCOW_PASSWORD)

    print(f"Harvesting folders: {', '.join(MAILCOW_FOLDERS)}")
    records = []
    try:
        for folder in MAILCOW_FOLDERS:
            records.extend(fetch_folder(mail, folder))
    finally:
        mail.logout()

    # The same Message-ID can legitimately appear in two folders (a message
    # copied rather than moved). message_id is the table's UNIQUE key, so one
    # email stays one row -- first folder listed wins. Deduping here rather than
    # leaning on ON CONFLICT keeps the reported insert count honest.
    seen = set()
    deduped = []
    for rec in records:
        if rec[0] in seen:
            continue
        seen.add(rec[0])
        deduped.append(rec)

    dropped = len(records) - len(deduped)
    if dropped:
        print(f"Collapsed {dropped} cross-folder duplicate(s).")
    print(f"Found {len(deduped)} unique message(s) across {len(MAILCOW_FOLDERS)} folder(s).")
    return deduped


def store_emails(records):
    if not records:
        print("No messages fetched, nothing to store.")
        return 0

    conn = psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS askcruz_emails (
                        id SERIAL PRIMARY KEY,
                        message_id TEXT UNIQUE,
                        from_address TEXT,
                        subject TEXT,
                        body TEXT,
                        received_at TEXT,
                        folder TEXT,
                        fetched_at TIMESTAMPTZ DEFAULT now()
                    )
                    """
                )
                # Additive, so an askcruz_emails table created by an earlier
                # INBOX-only version of this script picks up the new column
                # instead of silently failing the INSERT below.
                cur.execute(
                    "ALTER TABLE askcruz_emails ADD COLUMN IF NOT EXISTS folder TEXT"
                )
                # execute_values() batches at page_size rows per statement, so
                # cur.rowcount afterwards only reflects the LAST batch. Insert in
                # explicit chunks and sum, so the reported count is the real total.
                BATCH = 500
                inserted = 0
                for start in range(0, len(records), BATCH):
                    chunk = records[start:start + BATCH]
                    execute_values(
                        cur,
                        """
                        INSERT INTO askcruz_emails
                            (message_id, from_address, subject, body, received_at, folder)
                        VALUES %s
                        ON CONFLICT (message_id) DO NOTHING
                        """,
                        chunk,
                        page_size=len(chunk),
                    )
                    inserted += cur.rowcount
        print(f"Inserted {inserted} new message(s) (skipped duplicates).")
        return inserted
    finally:
        conn.close()


def main():
    check_env()
    records = fetch_all_emails()
    store_emails(records)


if __name__ == "__main__":
    main()
