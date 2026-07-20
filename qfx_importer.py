#!/usr/bin/env python3
# Copyright (C) 2026 Pushkar Purohit — AGPL-3.0
"""
QFX Importer for Actual Budget — v1.2.4 (content-based routing)
============================================================
Drop QFX/OFX files (ANY filename) into the inbox. The importer reads the
<ACCTID> inside each statement block, routes transactions to the mapped
Actual Budget account, and supports multi-account files (several statement
blocks in one file). Duplicate protection is two-layer: a count-aware
(date, amount) comparison against transactions already in Actual Budget
(immune to banks that regenerate FITIDs between downloads — RBC does),
plus FITID matching. Re-dropping files or overlapping ranges is always safe.

Modes:
  python3 qfx_importer.py                # normal run (cron)
  python3 qfx_importer.py --inspect F..  # print account ids found in file(s)
                                         # and suggested ACCOUNT_MAP lines
  python3 qfx_importer.py --audit F...   # read-only duplicate report: AB vs
                                         # file counts within the file's range

Environment variables required:
  ACTUAL_API_KEY     — API key for actual-http-api
  SMTP_USER          — Gmail address for sending alerts (optional)
  SMTP_PASSWORD      — Gmail app password (optional)
  EMAIL_TO           — Destination email for alerts (optional)

Before running, fill in BUDGET_SYNC_ID and ACCOUNT_MAP below — see README.
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

PROCESSED_RETENTION_DAYS = 30

# Skip zero-amount transactions. Some banks emit informational rows with
# TRNAMT of 0 and the real figure in the memo (e.g. RBC line-of-credit
# interest notices, where the actual charge is debited from another account).
# They have no financial effect but clutter registers and the uncategorised
# count. Skipped rows are logged, so nothing disappears silently.
SKIP_ZERO_AMOUNT = True

# ── Account map: bank account-number SUFFIX → exact AB account name ───────────
# EXAMPLE ONLY — replace with your own. Run `--inspect` on a downloaded QFX
# to see the ids your bank embeds; the last 6-8 digits are usually plenty.
# Keys are matched as suffixes of the <ACCTID> in the file, so masked
# variants ("XXXX1234" vs full number) still resolve — keep keys distinctive.
ACCOUNT_MAP = {
    "111222333"  : "Your Checking Account Name",
    "4530"       : "Your Credit Card Name",     # last-4 works if unique
    "987654"     : "Your Savings Account Name",
}
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("qfx_importer")
if not log.handlers:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass  # allow --inspect to run on machines without the log path
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def mask(acctid: str) -> str:
    a = acctid.strip()
    return ("*" * max(0, len(a) - 4)) + a[-4:] if len(a) > 4 else a


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str) -> None:
    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        return
    try:
        msg = MIMEMultipart()
        msg["From"], msg["To"] = SMTP_USER, EMAIL_TO
        msg["Subject"] = f"[QFX Importer] {subject}"
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.warning(f"Email failed (non-fatal): {e}")


# ── OFX/QFX parsing ───────────────────────────────────────────────────────────

STMT_BLOCK_RE = re.compile(r"<(STMTRS|CCSTMTRS)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)


def _tag(body: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)(?:<|\r|\n|$)", body, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_transactions(body: str) -> list[dict]:
    """Statement-block body → transaction dicts (amounts in cents × 100)."""
    out = []
    for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", body, re.DOTALL | re.IGNORECASE):
        raw_date = _tag(block, "DTPOSTED")[:8]
        raw_amount = _tag(block, "TRNAMT")
        if len(raw_date) != 8 or not raw_amount:
            continue
        amount = int(round(float(raw_amount) * 100))
        date_s = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        payee  = _tag(block, "NAME")
        memo   = _tag(block, "MEMO") or ""
        if SKIP_ZERO_AMOUNT and amount == 0:
            log.info(f"  skipped {date_s} $0.00 '{payee}' — informational row"
                     + (f" (memo: {memo})" if memo else ""))
            continue
        out.append({
            "date":        date_s,
            "amount":      amount,
            "imported_id": _tag(block, "FITID"),
            "payee_name":  payee,
            "notes":       memo,
            "cleared":     True,
        })
    return out


def parse_statements(path: Path) -> list[dict]:
    """
    File → [{acctid, kind, transactions}], one entry per statement block.
    Falls back to whole-file-as-one-statement if no closing aggregate tags
    (some banks emit terse SGML OFX 1.x).
    """
    content = path.read_text(encoding="latin-1")
    blocks = [(k.upper(), b) for k, b in STMT_BLOCK_RE.findall(content)]
    if not blocks:
        blocks = [("STMTRS", content)]
    stmts = []
    for kind, body in blocks:
        acctid = _tag(body, "ACCTID")
        stmts.append({
            "acctid": acctid,
            "kind": "credit card" if kind == "CCSTMTRS" else "bank",
            "transactions": parse_transactions(body),
        })
    return stmts


# ── Account resolution ────────────────────────────────────────────────────────

def resolve_account(acctid: str) -> str | None:
    """Map an <ACCTID> to an AB account name via suffix matching."""
    aid = acctid.strip()
    if not aid:
        return None
    hits = {v for k, v in ACCOUNT_MAP.items() if aid == k or aid.endswith(k) or k.endswith(aid)}
    if len(hits) == 1:
        return hits.pop()
    if len(hits) > 1:
        log.error(f"ACCTID {mask(aid)} matches multiple map entries {sorted(hits)} — "
                  f"make map keys more specific")
    else:
        log.error(f"ACCTID {mask(aid)} not in ACCOUNT_MAP — run --inspect to add it")
    return None


# ── actual-http-api ───────────────────────────────────────────────────────────

def get_headers() -> dict:
    return {"x-api-key": API_KEY, "Content-Type": "application/json"}


def get_payee_names() -> dict:
    """payee uuid → display name; empty dict if endpoint unavailable."""
    try:
        resp = requests.get(f"{API_BASE}/budgets/{BUDGET_SYNC_ID}/payees",
                            headers=get_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        payees = data.get("data", data) if isinstance(data, dict) else data
        return {p.get("id"): p.get("name", "") for p in payees}
    except requests.RequestException:
        return {}


def get_account_id(ab_name: str) -> str | None:
    try:
        resp = requests.get(f"{API_BASE}/budgets/{BUDGET_SYNC_ID}/accounts",
                            headers=get_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        accounts = data.get("data", data) if isinstance(data, dict) else data
        for acc in accounts:
            if acc.get("name", "").strip() == ab_name.strip():
                return acc["id"]
        log.error(f"Account '{ab_name}' not found in AB")
        return None
    except requests.RequestException as e:
        log.error(f"Cannot reach actual-http-api: {e}")
        return None


def fetch_existing(account_id: str, since: str) -> list[dict] | None:
    """Existing AB transactions for the account since a date (bank-level rows
    only — split children excluded). None on API failure (caller must not
    import blind in that case)."""
    try:
        resp = requests.get(
            f"{API_BASE}/budgets/{BUDGET_SYNC_ID}/accounts/{account_id}/transactions",
            headers=get_headers(), params={"since_date": since}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        txns = data.get("data", data) if isinstance(data, dict) else data
        return [t for t in txns if not t.get("is_child") and not t.get("parent_id")]
    except requests.RequestException as e:
        log.error(f"Cannot fetch existing transactions: {e}")
        return None


def dedup_against_existing(candidates: list[dict], existing: list[dict]) -> tuple[list[dict], int]:
    """
    Count-aware (date, amount) multiset dedup. Immune to unstable bank FITIDs
    (seen in the wild: RBC regenerates FITIDs between downloads) and to payee
    renames/categorisation done in AB. If the file has two identical $8.58
    transactions on one day and AB already has two, both are skipped; if AB
    has one, exactly one imports. Returns (to_import, skipped_count).
    """
    from collections import Counter
    from datetime import date as _d
    have = Counter((t.get("date"), t.get("amount")) for t in existing)
    # Transfer-linked rows may sit on a HARMONISED date (AB aligns both legs of a
    # transfer), so an exact (date, amount) match can miss them. Track them
    # separately and match candidates within ±4 days to avoid re-importing a leg
    # that already exists under the counterpart's date.
    xfer = [t for t in existing if t.get("transfer_id")]
    xfer_used = [False] * len(xfer)

    def _days(a, b):
        try:
            ya, ma, da = map(int, a.split("-")); yb, mb, db = map(int, b.split("-"))
            return abs((_d(ya, ma, da) - _d(yb, mb, db)).days)
        except Exception:
            return 999

    out, skipped = [], 0
    for t in candidates:
        key = (t["date"], t["amount"])
        if have.get(key, 0) > 0:
            have[key] -= 1
            skipped += 1
            continue
        hit = next((i for i, x in enumerate(xfer)
                    if not xfer_used[i] and x.get("amount") == t["amount"]
                    and _days(x.get("date") or "", t["date"]) <= 4), None)
        if hit is not None:
            xfer_used[hit] = True
            skipped += 1
            log.info(f"  skipped {t['date']} {t['amount']/100:.2f} — matches a "
                     f"transfer-linked row on {xfer[hit].get('date')} (date-shifted leg)")
            continue
        out.append(t)
    return out, skipped


def import_transactions(account_id: str, transactions: list[dict]) -> tuple[bool, int, int]:
    try:
        resp = requests.post(
            f"{API_BASE}/budgets/{BUDGET_SYNC_ID}/accounts/{account_id}/transactions/import",
            headers=get_headers(), json={"transactions": transactions}, timeout=30)
        if resp.status_code in (200, 201):
            data = resp.json().get("data", resp.json())
            added = len(data.get("added", []))
            updated = len(data.get("updated", []))
            log.info(f"✓ Imported — {added} new, {updated} matched/updated")
            return True, added, updated
        log.error(f"Import failed — HTTP {resp.status_code}: {resp.text[:200]}")
        return False, 0, 0
    except requests.RequestException as e:
        log.error(f"Import request error: {e}")
        return False, 0, 0


# ── File handling ─────────────────────────────────────────────────────────────

def move(src: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}_{datetime.now().strftime('%H%M%S')}{src.suffix}"
    shutil.move(str(src), str(dest))
    log.info(f"Moved → {dest}")


def cleanup_processed() -> None:
    if not PROCESSED_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=PROCESSED_RETENTION_DAYS)
    deleted = [f for f in PROCESSED_DIR.glob("*.[qQoO][fF][xX]")
               if datetime.fromtimestamp(f.stat().st_mtime) < cutoff]
    for f in deleted:
        f.unlink()
        log.info(f"Cleanup: deleted {f.name} (>{PROCESSED_RETENTION_DAYS} days old)")


# ── Pipeline ──────────────────────────────────────────────────────────────────

def process(qfx_path: Path) -> tuple[bool, list[str]]:
    """Returns (all_ok, per-statement summaries)."""
    log.info(f"── {qfx_path.name}")
    stmts = parse_statements(qfx_path)
    summaries, all_ok = [], True

    if not any(s["transactions"] for s in stmts):
        return False, [f"{qfx_path.name}: no transactions found in any statement block"]

    for s in stmts:
        label = f"{qfx_path.name} [{s['kind']} {mask(s['acctid'])}]"
        if not s["transactions"]:
            summaries.append(f"{label}: empty statement block, skipped")
            continue
        ab_name = resolve_account(s["acctid"])
        if not ab_name:
            summaries.append(f"{label}: no ACCOUNT_MAP entry")
            all_ok = False
            continue
        account_id = get_account_id(ab_name)
        if not account_id:
            summaries.append(f"{label}: AB account '{ab_name}' not found")
            all_ok = False
            continue
        since = min(t["date"] for t in s["transactions"])
        existing = fetch_existing(account_id, since)
        if existing is None:
            summaries.append(f"{label}: cannot verify existing transactions — skipped to avoid duplicates")
            all_ok = False
            continue
        to_import, pre_skipped = dedup_against_existing(s["transactions"], existing)
        log.info(f"{label} → '{ab_name}' ({len(s['transactions'])} txns, "
                 f"{pre_skipped} already present, {len(to_import)} to import)")
        if not to_import:
            summaries.append(f"{label} → {ab_name}: 0 new, {pre_skipped} already present")
            continue
        ok, added, updated = import_transactions(account_id, to_import)
        if ok and added == 0 and updated == 0 and to_import:
            # AB keeps FITIDs of DELETED transactions (tombstones) and silently
            # drops re-imports of them. The count-aware dedup already established
            # these transactions are genuinely absent, so re-import under
            # modified ids. Future runs stay idempotent via (date, amount) dedup.
            log.warning(f"{label}: {len(to_import)} sent but 0 added — deleted-transaction "
                        f"tombstones suspected; retrying with modified import ids")
            for t in to_import:
                if t.get("imported_id"):
                    t["imported_id"] = "R:" + t["imported_id"]
            ok, added, updated = import_transactions(account_id, to_import)
        if ok:
            summaries.append(f"{label} → {ab_name}: {added} new, {updated} matched, {pre_skipped} pre-skipped")
        else:
            summaries.append(f"{label}: API error — check log")
            all_ok = False
    return all_ok, summaries


def audit(paths: list[str]) -> None:
    """
    Read-only duplicate report. For each statement block in the given file(s),
    compares (date, amount) counts in Actual Budget against the file within the
    file's date range. Within that range the bank feed is the complete record,
    so any AB excess is a duplicate. Prints exact rows to delete in the AB UI.
    Legitimate identical twins (file has 2, AB has 2) are NOT flagged.
    """
    if not API_KEY or BUDGET_SYNC_ID == "YOUR-BUDGET-SYNC-ID-HERE":
        print("Set ACTUAL_API_KEY and BUDGET_SYNC_ID first.")
        return
    from collections import Counter
    total_excess = 0
    payee_names = get_payee_names()
    def pname(t):
        raw = t.get("payee_name") or ""
        return raw or payee_names.get(t.get("payee"), t.get("payee") or "?")
    for p in paths:
        path = Path(p)
        if not path.exists():
            print(f"✗ {p}: not found")
            continue
        print(f"\n═══ {path.name}")
        for s_ in parse_statements(path):
            if not s_["transactions"]:
                continue
            ab_name = resolve_account(s_["acctid"])
            if not ab_name:
                print(f"  [{mask(s_['acctid'])}] no map entry — skipped")
                continue
            account_id = get_account_id(ab_name)
            if not account_id:
                continue
            dates = [t["date"] for t in s_["transactions"]]
            lo, hi = min(dates), max(dates)
            existing = fetch_existing(account_id, lo)
            if existing is None:
                print(f"  [{ab_name}] cannot fetch — skipped")
                continue
            # Only compare against rows plausibly sourced from THIS file.
            # Some exporters embed the export window in the id (TD:
            # "2026-06-18~2026-06-23~1808411" — first two fields are the batch
            # range). A row whose batch prefix appears nowhere in this file came
            # from a different export and must not be reported as excess.
            def _batch(v):
                v = (v or "")
                if v.startswith("R:"):
                    v = v[2:]
                parts = v.split("~")
                return "~".join(parts[:2]) if len(parts) >= 3 else None
            file_batches = {b for b in (_batch(t.get("imported_id"))
                                        for t in s_["transactions"]) if b}
            def _same_source(t):
                b = _batch(t.get("imported_id"))
                if b is None or not file_batches:
                    return True          # no batch info: fall back to counting it
                return b in file_batches
            in_range_all = [t for t in existing
                            if lo <= (t.get("date") or "") <= hi and _same_source(t)]
            foreign = [t for t in existing
                       if lo <= (t.get("date") or "") <= hi and not _same_source(t)]
            if foreign:
                print("    note: %d row(s) in range came from a different export batch "
                      "of this bank — not counted" % len(foreign))
            in_range = [t for t in in_range_all if not t.get("transfer_id")]
            xfer_rows = [t for t in in_range_all if t.get("transfer_id")]
            file_ct = Counter((t["date"], t["amount"]) for t in s_["transactions"])
            # transfer-linked rows consume matching file entries first (either on
            # their own date or a nearby shifted one), so they never count as excess
            for t in xfer_rows:
                k = (t.get("date"), t.get("amount"))
                if file_ct.get(k, 0) > 0:
                    file_ct[k] -= 1
                else:
                    near = next((fk for fk in file_ct
                                 if fk[1] == t.get("amount") and file_ct[fk] > 0), None)
                    if near:
                        file_ct[near] -= 1
                    else:
                        print(f"    note: transfer-linked row {t.get('date')} "
                              f"{(t.get('amount') or 0)/100:.2f} '{pname(t)}' has no file "
                              f"counterpart — keep (transfer leg), not counted as excess")
            ab_ct   = Counter((t.get("date"), t.get("amount")) for t in in_range)
            excess_keys, lone = {}, []
            for k in ab_ct:
                if ab_ct[k] <= file_ct.get(k, 0):
                    continue
                if ab_ct[k] == 1 and file_ct.get(k, 0) == 0:
                    lone.append(k)      # single row absent from this file: not a duplicate
                    continue
                excess_keys[k] = ab_ct[k] - file_ct.get(k, 0)
            for d, amt in sorted(lone):
                print("    note: %s %10.2f present in AB but not in this file "
                      "(single row — kept, likely from another export)" % (d, amt / 100))
            print(f"  [{ab_name}] range {lo} → {hi}: "
                  f"{sum(excess_keys.values()) or 'no'} excess transaction(s)")
            for (d, amt), n in sorted(excess_keys.items()):
                rows = [t for t in in_range if t.get("date") == d and t.get("amount") == amt]
                print(f"    {d}  {amt/100:>10.2f}  — AB has {ab_ct[(d,amt)]}, "
                      f"file has {file_ct.get((d,amt),0)} → delete {n}:")
                for t in rows:
                    print(f"        payee='{pname(t)}' imported_id={t.get('imported_id')}")
                total_excess += n
    print(f"\nTotal excess across files: {total_excess}. "
          "Delete that many in the AB UI (for identical rows, either copy is fine), "
          "then re-run --audit until it reports none.")


def inspect(paths: list[str]) -> None:
    """Print account ids found in files + ready-to-paste ACCOUNT_MAP lines."""
    for p in paths:
        path = Path(p)
        if not path.exists():
            print(f"✗ {p}: not found")
            continue
        print(f"\n{path.name}:")
        for s in parse_statements(path):
            aid = s["acctid"] or "(no ACCTID found)"
            n = len(s["transactions"])
            print(f"  {s['kind']:<12} ACCTID={aid}   ({n} transactions)")
            if s["acctid"]:
                key = s["acctid"][-6:] if len(s["acctid"]) > 6 else s["acctid"]
                print(f'  suggested map line:  "{key}" : "Your AB Account Name Here",')
    print("\nPaste the map lines into ACCOUNT_MAP (edit the AB account names), "
          "then drop files into the inbox — filenames no longer matter.")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--inspect":
        inspect(sys.argv[2:] or [])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--audit":
        audit(sys.argv[2:] or [])
        return

    for d in [INBOX_DIR, PROCESSED_DIR, FAILED_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if not API_KEY:
        log.error("ACTUAL_API_KEY not set. Exiting.")
        sys.exit(1)
    if BUDGET_SYNC_ID == "YOUR-BUDGET-SYNC-ID-HERE":
        log.error("BUDGET_SYNC_ID is still the placeholder. Edit qfx_importer.py. Exiting.")
        sys.exit(1)

    cleanup_processed()

    files = sorted(list(INBOX_DIR.glob("*.[qQ][fF][xX]")) + list(INBOX_DIR.glob("*.[oO][fF][xX]")))
    if not files:
        log.info("Inbox is empty — nothing to do.")
        return

    log.info(f"Found {len(files)} file(s) in inbox")
    successes, failures = [], []

    for f in files:
        try:
            ok, summaries = process(f)
            (successes if ok else failures).extend(summaries)
            move(f, PROCESSED_DIR if ok else FAILED_DIR)
        except Exception as e:
            msg = f"Unexpected error on {f.name}: {e}"
            log.error(msg)
            failures.append(msg)
            move(f, FAILED_DIR)

    if failures:
        body = ("These QFX statements FAILED to import:\n\n"
                + "\n".join(f"  ✗ {m}" for m in failures)
                + "\n\nNote: partially-imported files are safe to re-drop after fixing "
                  "the map — FITID dedup skips anything already imported."
                + f"\nCheck: {LOG_FILE}")
        send_email(f"⚠️ {len(failures)} import(s) FAILED", body)
    if successes:
        body = ("Imported successfully:\n\n" + "\n".join(f"  ✓ {m}" for m in successes)
                + "\n\nOpen Actual Budget to verify balances and categorise.")
        send_email(f"✓ {len(successes)} import(s) successful", body)

    log.info("Run complete.")


if __name__ == "__main__":
    main()
