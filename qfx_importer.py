#!/usr/bin/env python3
"""
QFX Importer for Actual Budget
==============================
Watches /mnt/samsungt5/bank-imports/inbox/ for QFX/qfx files,
imports them via actual-http-api, sends email alerts on
success/failure, and cleans up processed files older than 30 days.

File naming: <Account Name>_DD-MM-YYYY.qfx (or .QFX)
Example:     Joint Chequing_13-06-2026.qfx

Environment variables required:
  ACTUAL_API_KEY     — API key for actual-http-api
  SMTP_USER          — Gmail address for sending alerts (optional)
  SMTP_PASSWORD      — Gmail app password (optional)
  EMAIL_TO           — Destination email for alerts (optional)

Before running, fill in BUDGET_SYNC_ID and ACCOUNT_MAP below with your
own values — see README.md "Configuration" section.
"""

import os
import re
import sys
import shutil
import logging
import smtplib
import requests
from pathlib import Path
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Configuration ─────────────────────────────────────────────────────────────
INBOX_DIR     = Path("/mnt/samsungt5/bank-imports/inbox")
PROCESSED_DIR = Path("/mnt/samsungt5/bank-imports/processed")
FAILED_DIR    = Path("/mnt/samsungt5/bank-imports/failed")
LOG_FILE      = Path("/mnt/samsungt5/bank-imports/import.log")

API_BASE       = "http://localhost:5007/v1"
API_KEY        = os.environ.get("ACTUAL_API_KEY", "")

# REQUIRED: Find this in Actual Budget under Settings > Advanced > Sync ID
BUDGET_SYNC_ID = "YOUR-BUDGET-SYNC-ID-HERE"

# Email — optional, uses Gmail SMTP. Leave env vars unset to disable.
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO      = os.environ.get("EMAIL_TO", "")

# Delete processed files older than this many days
PROCESSED_RETENTION_DAYS = 30

# ── Account map: lowercase filename prefix → exact AB account name ─────────────
# EXAMPLE ONLY — replace with your own accounts.
# The key is what appears before the underscore in your QFX filename
# (e.g. "Joint Chequing_13-06-2026.qfx" → key is "joint chequing").
# The value must match the EXACT account name shown in Actual Budget's sidebar.
ACCOUNT_MAP = {
    "checking account"       : "Your Checking Account Name",
    "credit card"             : "Your Credit Card Name",
    "savings account"         : "Your Savings Account Name",
    # Add one line per account you want to auto-import
}
# ─────────────────────────────────────────────────────────────────────────────

# Single logger setup — avoids duplicate log lines
log = logging.getLogger("qfx_importer")
if not log.handlers:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


# ── Email notifications ───────────────────────────────────────────────────────

def send_email(subject: str, body: str) -> None:
    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        return
    try:
        msg = MIMEMultipart()
        msg["From"]    = SMTP_USER
        msg["To"]      = EMAIL_TO
        msg["Subject"] = f"[QFX Importer] {subject}"
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.warning(f"Email failed (non-fatal): {e}")


# ── QFX Parser ────────────────────────────────────────────────────────────────

def parse_qfx(path: Path) -> list[dict]:
    """
    Parse QFX file into transaction dicts for actual-http-api.
    Amount: QFX decimal dollars → cents × 100 (what actual-http-api expects).
    """
    content = path.read_text(encoding="latin-1")
    blocks  = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", content, re.DOTALL)
    transactions = []
    for block in blocks:
        def get(tag):
            m = re.search(rf"<{tag}>(.*?)(?:<|\r|\n|$)", block)
            return m.group(1).strip() if m else ""
        raw_date = get("DTPOSTED")[:8]
        if len(raw_date) != 8:
            continue
        date_str   = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        raw_amount = get("TRNAMT")
        if not raw_amount:
            continue
        transactions.append({
            "date"        : date_str,
            "amount"      : int(round(float(raw_amount) * 100)),
            "imported_id" : get("FITID"),
            "payee_name"  : get("NAME"),
            "notes"       : get("MEMO") or "",
            "cleared"     : True,
        })
    log.info(f"Parsed {len(transactions)} transactions from {path.name}")
    return transactions


# ── actual-http-api helpers ───────────────────────────────────────────────────

def get_headers() -> dict:
    return {"x-api-key": API_KEY, "Content-Type": "application/json"}


def get_account_id(ab_name: str) -> str | None:
    try:
        resp = requests.get(
            f"{API_BASE}/budgets/{BUDGET_SYNC_ID}/accounts",
            headers=get_headers(), timeout=10,
        )
        resp.raise_for_status()
        data     = resp.json()
        accounts = data.get("data", data) if isinstance(data, dict) else data
        for acc in accounts:
            if acc.get("name", "").strip() == ab_name.strip():
                return acc["id"]
        log.error(f"Account '{ab_name}' not found in AB")
        return None
    except requests.RequestException as e:
        log.error(f"Cannot reach actual-http-api: {e}")
        return None


def import_transactions(account_id: str, transactions: list[dict]) -> tuple[bool, int, int]:
    try:
        resp = requests.post(
            f"{API_BASE}/budgets/{BUDGET_SYNC_ID}/accounts/{account_id}/transactions/import",
            headers=get_headers(),
            json={"transactions": transactions},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data    = resp.json().get("data", resp.json())
            added   = len(data.get("added",   []))
            updated = len(data.get("updated", []))
            log.info(f"✓ Import done — {added} new, {updated} matched/updated")
            return True, added, updated
        else:
            log.error(f"Import failed — HTTP {resp.status_code}: {resp.text[:200]}")
            return False, 0, 0
    except requests.RequestException as e:
        log.error(f"Import request error: {e}")
        return False, 0, 0


# ── File routing ──────────────────────────────────────────────────────────────

def parse_filename(name: str) -> tuple[str, str] | None:
    stem  = Path(name).stem
    match = re.match(r"^(.+?)_(\d{2}-\d{2}-\d{4})$", stem)
    if not match:
        log.warning(f"'{name}' doesn't match <Account Name>_DD-MM-YYYY.qfx")
        return None
    return match.group(1).strip(), match.group(2)


def resolve_ab_name(raw: str) -> str | None:
    key = raw.lower().strip()
    if key in ACCOUNT_MAP:
        return ACCOUNT_MAP[key]
    for map_key, ab_name in ACCOUNT_MAP.items():
        if key.startswith(map_key) or map_key.startswith(key):
            log.info(f"Fuzzy matched '{raw}' → '{ab_name}'")
            return ab_name
    log.error(f"No mapping for '{raw}'. Known keys: {list(ACCOUNT_MAP.keys())}")
    return None


def move(src: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        ts   = datetime.now().strftime("%H%M%S")
        dest = dest_dir / f"{src.stem}_{ts}{src.suffix}"
    shutil.move(str(src), str(dest))
    log.info(f"Moved → {dest}")


# ── Cleanup old processed files ───────────────────────────────────────────────

def cleanup_processed() -> None:
    if not PROCESSED_DIR.exists():
        return
    cutoff  = datetime.now() - timedelta(days=PROCESSED_RETENTION_DAYS)
    deleted = [
        f for f in PROCESSED_DIR.glob("*.[qQ][fF][xX]")
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff
    ]
    for f in deleted:
        f.unlink()
        log.info(f"Cleanup: deleted {f.name} (>{PROCESSED_RETENTION_DAYS} days old)")
    if deleted:
        log.info(f"Cleanup: removed {len(deleted)} file(s)")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process(qfx_path: Path) -> tuple[bool, str]:
    log.info(f"── {qfx_path.name}")

    parsed = parse_filename(qfx_path.name)
    if not parsed:
        move(qfx_path, FAILED_DIR)
        return False, f"Bad filename: {qfx_path.name}"

    account_raw, date_str = parsed
    ab_name = resolve_ab_name(account_raw)
    if not ab_name:
        move(qfx_path, FAILED_DIR)
        return False, f"No account mapping for '{account_raw}'"

    account_id = get_account_id(ab_name)
    if not account_id:
        move(qfx_path, FAILED_DIR)
        return False, f"Account '{ab_name}' not found in AB"

    transactions = parse_qfx(qfx_path)
    if not transactions:
        move(qfx_path, FAILED_DIR)
        return False, f"No transactions in {qfx_path.name}"

    ok, added, updated = import_transactions(account_id, transactions)
    if ok:
        move(qfx_path, PROCESSED_DIR)
        return True, f"{qfx_path.name}: {added} new, {updated} matched"
    else:
        move(qfx_path, FAILED_DIR)
        return False, f"API error for {qfx_path.name} — check log"


def main() -> None:
    for d in [INBOX_DIR, PROCESSED_DIR, FAILED_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if not API_KEY:
        log.error("ACTUAL_API_KEY not set. Exiting.")
        sys.exit(1)

    if BUDGET_SYNC_ID == "YOUR-BUDGET-SYNC-ID-HERE":
        log.error("BUDGET_SYNC_ID is still set to the placeholder value. "
                   "Edit qfx_importer.py and set your real Budget Sync ID. Exiting.")
        sys.exit(1)

    cleanup_processed()

    files = sorted(INBOX_DIR.glob("*.[qQ][fF][xX]"))
    if not files:
        log.info("Inbox is empty — nothing to do.")
        return

    log.info(f"Found {len(files)} file(s) in inbox")
    successes, failures = [], []

    for f in files:
        try:
            ok, summary = process(f)
            (successes if ok else failures).append(summary)
        except Exception as e:
            msg = f"Unexpected error on {f.name}: {e}"
            log.error(msg)
            failures.append(msg)
            move(f, FAILED_DIR)

    if failures:
        body = "The following QFX files FAILED to import:\n\n"
        body += "\n".join(f"  ✗ {m}" for m in failures)
        body += f"\n\nCheck: {LOG_FILE}"
        send_email(f"⚠️ {len(failures)} import(s) FAILED", body)

    if successes:
        body = "The following QFX files imported successfully:\n\n"
        body += "\n".join(f"  ✓ {m}" for m in successes)
        body += "\n\nOpen Actual Budget to verify balances and categorise."
        send_email(f"✓ {len(successes)} import(s) successful", body)

    log.info("Run complete.")


if __name__ == "__main__":
    main()
