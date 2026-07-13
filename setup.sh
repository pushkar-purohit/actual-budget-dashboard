#!/bin/bash
# Copyright (C) 2026 Pushkar Purohit — AGPL-3.0
# setup.sh — One-time setup for QFX importer on your Pi/server
# Run with: bash setup.sh
#
# BEFORE RUNNING: edit qfx_importer.py and set BUDGET_SYNC_ID and ACCOUNT_MAP
# to match your own Actual Budget setup. See README.md for details.
set -e

echo ""
echo "========================================"
echo "   QFX Importer — setup"
echo "========================================"
echo ""

read -p "Path to your bank-imports folder (e.g. /mnt/your-drive/bank-imports): " IMPORT_BASE
read -p "Path to install qfx_importer.py (e.g. /home/youruser/qfx-importer): " INSTALL_DIR

# 1. Folder structure
echo "[1/5] Creating folder structure..."
mkdir -p "$IMPORT_BASE/inbox"
mkdir -p "$IMPORT_BASE/processed"
mkdir -p "$IMPORT_BASE/failed"
echo "      ✓ ${IMPORT_BASE}/{inbox,processed,failed}"

# 2. Importer files
echo "[2/5] Copying importer files..."
mkdir -p "$INSTALL_DIR"
cp qfx_importer.py "$INSTALL_DIR/"
echo "      ✓ ${INSTALL_DIR}/qfx_importer.py"
echo ""
echo "      ⚠️  Now edit ${INSTALL_DIR}/qfx_importer.py and:"
echo "         1. Replace INBOX_DIR/PROCESSED_DIR/FAILED_DIR paths with: $IMPORT_BASE"
echo "         2. Set BUDGET_SYNC_ID (find it in Actual Budget > Settings > Advanced)"
echo "         3. Fill in ACCOUNT_MAP with your own account names"
read -p "      Press Enter once you've edited the file..."

# 3. Python dependency
echo "[3/5] Installing Python requests library..."
pip3 install requests --break-system-packages -q
echo "      ✓ requests installed"

# 4. actual-http-api container
echo "[4/5] Setting up actual-http-api..."
echo ""

API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(30))")
echo "      Generated API key: $API_KEY"
echo "      Save this somewhere safe — you'll need it for the dashboard config too."
echo ""

read -p "      Press Enter once Actual Budget server password is ready to enter..."
read -s -p "      Enter your Actual Budget server password: " AB_PASS
echo ""

if docker ps --format '{{.Names}}' | grep -q "^actualhttpapi$"; then
    echo "      actualhttpapi container already running — stopping it first..."
    docker stop actualhttpapi && docker rm actualhttpapi
fi

echo "      Starting actual-http-api container..."
# Bound to loopback ONLY: the wrapper's API_KEY is not reliably enforced
# (verified July 2026), so network isolation is the real security boundary.
docker run -d \
    --name actualhttpapi \
    --restart=unless-stopped \
    -p 127.0.0.1:5007:5007 \
    --add-host=host.docker.internal:host-gateway \
    -e ACTUAL_SERVER_URL="https://host.docker.internal:5006/" \
    -e ACTUAL_SERVER_PASSWORD="${AB_PASS}" \
    -e API_KEY="${API_KEY}" \
    -e NODE_ENV="production" \
    -e NODE_TLS_REJECT_UNAUTHORIZED="0" \
    jhonderson/actual-http-api:latest

echo "      Waiting 15s for container to initialise..."
sleep 15

HTTP_CODE=$(curl -sk -o /dev/null -w "%{http_code}" \
    -H "x-api-key: ${API_KEY}" \
    http://localhost:5007/v1/budgets 2>/dev/null || echo "000")

if [ "$HTTP_CODE" = "200" ]; then
    echo "      ✓ actual-http-api is up and responding"
else
    echo "      ✗ API returned HTTP $HTTP_CODE — check: docker logs actualhttpapi"
fi

# 5. Cron job
echo ""
echo "[5/5] Registering cron job (every 5 minutes)..."

CRON_CMD="*/5 * * * * ACTUAL_API_KEY=${API_KEY} python3 ${INSTALL_DIR}/qfx_importer.py >> ${IMPORT_BASE}/import.log 2>&1"
( crontab -l 2>/dev/null | grep -v "qfx_importer"; echo "$CRON_CMD" ) | crontab -

echo "      ✓ Cron job registered"
echo ""
echo "========================================"
echo "  Setup complete!"
echo ""
echo "  WORKFLOW"
echo "  --------"
echo "  1. Download QFX/OFX from your bank"
echo "  2. Rename to:  AccountName_DD-MM-YYYY.qfx"
echo "     (AccountName must match a key you set in ACCOUNT_MAP)"
echo "  3. Drop into:  ${IMPORT_BASE}/inbox/"
echo "  4. Wait up to 5 minutes"
echo "  5. Open Actual Budget and verify balance"
echo ""
echo "  Your API key (save this for dashboard.html config too):"
echo "  ${API_KEY}"
echo ""
echo "  Logs: ${IMPORT_BASE}/import.log"
echo "========================================"
