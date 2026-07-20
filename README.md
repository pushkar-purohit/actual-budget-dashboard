# Actual Budget — Self-Hosted Dashboard & QFX Auto-Importer

A two-part toolkit for [Actual Budget](https://actualbudget.org/) self-hosters:

1. **`dashboard.html`** — a single-file, client-side household finance dashboard with tabs for Overview, Income vs Expense, Net Worth history, and Spending Trends. No backend needed beyond [`actual-http-api`](https://github.com/jhonderson/actual-http-api).
2. **`qfx_importer.py`** — a watcher script that auto-imports QFX/OFX files dropped into a folder, using each transaction's bank-assigned FITID for reliable deduplication.

Built for a Raspberry Pi running Actual Budget in Docker, but works on any Linux host running Actual Budget self-hosted.

---

## Why this exists

Actual Budget's built-in reports are solid but limited for households juggling many accounts across multiple institutions (joint + individual chequing/savings/credit, TFSA/RRSP/margin investment accounts, mortgage). This dashboard adds:

- **Liquid vs semi-liquid vs retirement net worth tiers**, with sequential liquidity-runway modeling (cash, then margin, then TFSA) under two scenarios: total income loss, and continuing at your current burn rate
- **Income/expense classification that matches Actual Budget's own logic** — a transaction is income only if tagged with an `is_income` category, not based on amount sign (this avoids miscounting refunds as income)
- **On-budget to off-budget transfers (mortgage payments, TFSA/RRSP contributions) counted as real expenses**, grouped under their actual category (Bills, Savings, etc.) rather than hidden in a generic transfer bucket
- **An Income vs Expense table** with collapsible category groups, deviation highlighting, and negative-net-month highlighting — closer to YNAB's reporting style
- **A custom date range picker** and account filter, so any tab can be sliced by period or by specific accounts
- **Light/dark theme toggle** on the dashboard itself, persisted per browser (defaults to your OS preference)
- **Skew-proof period presets**: 3 months / 6 months / 1 year always select *complete* months (on July 10, "6 months" means Jan 1 - Jun 30), so a partial current month never dilutes your monthly averages. "This month" gives explicit month-to-date.
- **A runtime tier configurator** (the "Tiers" button) -- assign every account to liquidity Tier 1/2/3, Retirement, Home, or Mortgage through dropdowns instead of editing code. Tier labels are editable (rename "TFSA" to "Roth IRA" or anything else -- only the depletion *order* matters to the runway model). New accounts automatically surface as "Unassigned" so they can't silently fall out of net worth. Settings persist per-browser in localStorage with JSON export/import to move them between devices or bake them back into the file's CONFIG defaults
- **All computation happens client-side** in the browser; one API call fetches your full transaction history once, everything else is array filtering in JavaScript. No backend logic beyond what `actual-http-api` already exposes.

---

## Architecture

```
Your server (e.g. Raspberry Pi)
- actualbudget          (Docker, port 5006)  -- Actual Budget itself
- actualhttpapi         (Docker, port 5007)  -- community REST wrapper
- nginx                 (reverse proxy, TLS) -- serves dashboard.html and proxies API
- qfx_importer.py       (cron, every 5 min)  -- watches a folder, imports QFX files
```

The dashboard is a static HTML file. It calls `actual-http-api` directly from the browser via a same-origin nginx proxy path (`/api/`), so the API key never needs CORS exceptions.

---

## Prerequisites

- Actual Budget running self-hosted (Docker recommended)
- Docker, for running `actual-http-api`
- nginx (or any reverse proxy capable of TLS plus a simple proxy_pass)
- Python 3.10+ with `requests` installed, for the QFX importer
- A way to reach your server remotely if desired (Tailscale, VPN, or just LAN-only)

---

## Setup

### 1. Deploy `actual-http-api`

```bash
docker run -d \
  --name actualhttpapi \
  --restart=unless-stopped \
  -p 127.0.0.1:5007:5007 \
  --add-host=host.docker.internal:host-gateway \
  -e ACTUAL_SERVER_URL="https://host.docker.internal:5006/" \
  -e ACTUAL_SERVER_PASSWORD="your-actual-budget-password" \
  -e API_KEY="generate-a-random-key-here" \
  -e NODE_ENV="production" \
  -e NODE_TLS_REJECT_UNAUTHORIZED="0" \
  jhonderson/actual-http-api:latest
```

> **Why `-p 127.0.0.1:5007:5007` and not `--network host`:** in testing (July 2026 image),
> `actual-http-api` did **not** enforce its `API_KEY` — requests without a key, and with a
> wrong key, were served in full. Do not rely on the wrapper's API key as a security boundary.
> Binding the port to loopback means only processes on the server itself (your reverse proxy
> and the importer) can reach it; nothing on your LAN can. If your Actual Budget server runs
> on a different host, put an authenticating reverse proxy in front of this API instead.
> (`host.docker.internal` lets the bridged container reach Actual Budget on the host; adjust
> `ACTUAL_SERVER_URL` if AB runs elsewhere.)

Generate a random key with: `python3 -c "import secrets; print(secrets.token_urlsafe(30))"`

Verify it's working:
```bash
curl -s -H "x-api-key: YOUR_KEY" http://localhost:5007/v1/budgets
```

### 2. Find your Budget Sync ID

In Actual Budget: **Settings -> Advanced -> Sync ID**. It's a UUID like `a1b2c3d4-e5f6-7890-abcd-ef1234567890`.

### 3. Get your account IDs and names

```bash
curl -s -H "x-api-key: YOUR_KEY" \
  http://localhost:5007/v1/budgets/YOUR_SYNC_ID/accounts | python3 -m json.tool
```

This gives you the exact `id` and `name` for every account, which you'll need for the dashboard config.

### 4. Configure nginx

Add a server block that serves the dashboard and proxies API calls through the same origin (this avoids CORS entirely):

```nginx
server {
    listen 3445 ssl;
    server_name your-hostname-here;
    ssl_certificate /path/to/cert.crt;
    ssl_certificate_key /path/to/cert.key;

    location /api/ {
        proxy_pass http://127.0.0.1:5007/;
        proxy_set_header x-api-key "YOUR_API_KEY";
        # No CORS header needed — the dashboard is served from the same origin.
    }

    location / {
        root /path/to/dashboard-folder;
        index dashboard.html;
        add_header Cache-Control "no-cache";
    }
}
```

> Security note: this nginx config injects the API key server-side via `proxy_set_header`, so it never appears in the dashboard's HTML source -- safer than putting the key directly in `dashboard.html`'s `CONFIG.KEY`. If you do put it in the JS config (simpler, but the key becomes visible to anyone who views page source), make sure the dashboard is only reachable over a private network or VPN (Tailscale, WireGuard, etc.), never exposed to the public internet.

### 5. Configure `dashboard.html`

Open the file and edit the `CONFIG` block near the top:

```javascript
const C = {
  API   : 'https://your-hostname:3445/api/v1',
  KEY   : 'your-api-key',          // only if not using server-side injection (see above)
  BID   : 'your-budget-sync-id',
  START : '2024-01-01',            // earliest date you want history for

  OB_ACCOUNTS: { /* your account id: name pairs */ },
  CASH_ACCOUNTS: [ /* seed defaults only -- the in-app Tiers panel supersedes these */ ],
  MARGIN_ACCOUNTS: [ /* taxable investment/margin accounts */ ],
  TFSA_ACCOUNTS: [ /* or your country's equivalent tax-free account */ ],
  PERSON1_RET: [ /* one person's RRSP/401k/retirement accounts */ ],
  PERSON2_RET: [ /* the other person's retirement accounts */ ],
  // see the file for the full set of fields to fill in
};
```

The shipped file contains a fictional worked example ("Alex" and "Sam", with fake all-zero UUIDs) -- replace the entire CONFIG block with your own account names and IDs.

### 6. Set up the QFX importer (optional but recommended)

```bash
bash setup.sh
```

This walks you through folder paths, installs dependencies, and registers a cron job. Before running, edit `qfx_importer.py`'s `BUDGET_SYNC_ID` and `ACCOUNT_MAP`.

Workflow once set up: download a QFX/OFX file from your bank and drop it in the inbox folder -- **any filename, no renaming**. The importer reads the account number(s) embedded in the file and routes transactions to the mapped Actual Budget account. Files containing multiple accounts (some banks export combined statements) are split and routed automatically. Within 5 minutes it's imported -- duplicates are skipped via FITID matching, the same mechanism Actual Budget's own QFX import uses, so re-dropping a file or overlapping date ranges is always safe.

To build your `ACCOUNT_MAP` (a one-time step), download one statement per account and run:

```bash
python3 qfx_importer.py --inspect ~/Downloads/*.qfx
```

It prints the account id found in each file plus a ready-to-paste map line -- fill in your AB account names and you're done.

---

## Security notes

- **The wrapper's API key is not a real boundary.** As noted above, `actual-http-api` was observed serving requests without any key. The loopback binding in the setup command is what actually protects you -- verify it with `ss -tlnp | grep 5007` (should show `127.0.0.1:5007`) and a keyless `curl` from another machine (should refuse to connect).
- **Keep the dashboard on a private network.** It exposes read/write access to your entire budget via the API key. Tailscale/WireGuard-only is the intended deployment; never port-forward it to the public internet.
- **Prefer nginx-side API key injection** (`proxy_set_header`, shown above) over putting the key in `dashboard.html`'s `CONFIG.KEY` -- the key then never appears in page source.
- **`NODE_TLS_REJECT_UNAUTHORIZED=0`** in the `actual-http-api` container disables TLS certificate verification for that container's outbound connections. It's needed because Actual Budget serves a self-signed cert on localhost, and the connection never leaves the host -- but be aware of what it does. If your Actual Budget instance serves plain HTTP internally, point `ACTUAL_SERVER_URL` at that and drop the flag.
- **Container env vars are visible via `docker inspect`** to anyone in the `docker` group on the host. On a single-user Pi this is fine; on shared hosts, use Docker secrets or an env file with restricted permissions instead of `-e` flags.
- **Consider self-hosting Chart.js** instead of loading it from a CDN, or add a [Subresource Integrity](https://developer.mozilla.org/en-US/docs/Web/Security/Subresource_Integrity) hash to the `<script>` tag. A finance dashboard is exactly the kind of page where you don't want a compromised CDN able to inject script.

## Changelog
- **1.2.4** -- Audit correctly ignores transactions supplied by a different export batch of the same bank (exporters such as TD embed the export window in the transaction id), and never reports a lone Actual Budget row as a duplicate. Fixes false positives when a file's date range abuts an earlier import.
- **1.2.4** -- Audit correctly ignores transactions supplied by a different export batch of the same bank (exporters such as TD embed the export window in the transaction id), and never reports a lone Actual Budget row as a duplicate. Fixes false positives when a file's date range abuts an earlier import.
- **1.2.3** -- Skip zero-amount informational transactions (e.g. RBC line-of-credit interest notices, where the figure sits in the memo and the real charge is debited elsewhere); configurable via `SKIP_ZERO_AMOUNT`, and skipped rows are logged with their memo. Audit: partial handling of rows imported from a different file — when auditing a file whose range abuts an earlier import, rows supplied by that earlier file may still be listed; check the `imported_id` before deleting.

- **1.2.2** -- Import reliability release: count-aware (date, amount) pre-import dedup, immune to banks that regenerate FITIDs between downloads (observed with RBC); read-only `--audit` duplicate report with payee names; tombstone-aware retry (Actual Budget silently drops re-imports of deleted FITIDs); transfer-linked rows recognised on date-shifted legs in both import and audit.

- **1.2.0** -- QFX importer v2: files are routed by the account number embedded in the file (`<ACCTID>`) instead of filename conventions -- no more renaming downloads. Multi-account files (several statement blocks in one QFX) are split and imported to each mapped account. New `--inspect` mode prints the account ids in a file with suggested map lines. `.ofx` extension also accepted. Account numbers are masked in logs and emails.

- **1.1.0** -- Light/dark theme toggle; complete-month period presets (partial current month no longer skews averages); off-budget accounts selectable in the account filter (drives net worth views; income/expense stays on-budget); "history only" flag extended to all tiers (closed chequing/margin/TFSA accounts stay in net-worth history, auto-defaulted for closed accounts); visual refresh; version shown in footer.
- **1.0.0** -- Initial public release: dashboard (Overview, Income v Expense, Net Worth, Spending Trends, liquidity runway, tier configurator) + QFX auto-importer + setup script.

## Known limitations

- **Net worth history reconstruction** walks forward through your transaction history from the earliest record. If an account has incomplete history (e.g. you started tracking it partway through, with no opening-balance transaction), its contribution to net worth will show as $0 until its first recorded transaction, even if you held a real balance before that. Workaround: ensure every account has an opening-balance transaction dated at when you started tracking it. Exception: the home-value account is anchored to its live balance, so it degrades to a flat (current) value for months before its first transaction rather than $0 -- reconcile adjustments to it (e.g. revaluing your home) step the history at the date they were recorded.
- **`actual-http-api`'s `run-query` endpoint silently ignores date-range filters** (`$gte`/`$lte` on the `date` field) in the version this was built against. The dashboard works around this by fetching all transactions once (unfiltered) and doing date filtering client-side -- this is actually faster anyway, but means initial load fetches your entire transaction history regardless of the period you have selected.
- Tier assignments edited in the UI are stored in the browser's localStorage, so they're per-device. Use the panel's Export/Import to sync devices, or export once and paste the arrays into CONFIG as the new defaults.
- No mobile app -- it's a responsive web page, tested on Safari iOS and Chrome Android down to about 375px width.

---

## Tech stack

- Vanilla JavaScript, no build step, no framework
- [Chart.js 4.4.1](https://www.chartjs.org/) via CDN for all charts
- [`actual-http-api`](https://github.com/jhonderson/actual-http-api) as the only backend dependency beyond Actual Budget itself

---

## License

MIT -- use, modify, and share freely. If you improve something, a pull request would be appreciated but isn't required.
