#!/usr/bin/env bash
# ⚠️ LEGACY: VPS/systemd resident-route setup (pre-CVM era). Production is
# all-CVM now; for the current self-hosting path use the docker-compose flow
# in docs-site/content/docs/self-hosting.mdx. Kept for the legacy runbook
# deploy/SELF_HOSTING.md, which carries the same banner.
#
# Feedling VPS setup script
# Run as ubuntu user on the EC2 instance
# Usage: bash deploy/setup.sh [--install-caddy]
#
# By default this script:
#   1. Creates a Python venv
#   2. Installs deps
#   3. Writes ~/feedling.env (multi-tenant mode — no shared API key)
#   4. Installs the systemd units:
#        feedling-backend, feedling-chat-resident
#   5. Starts the backend immediately
#   6. Starts feedling-chat-resident if ~/feedling-chat-resident.env exists
# Pass --install-caddy to also install Caddy and enable HTTPS.

set -e

REPO_DIR="$HOME/feedling-mcp"
VENV_DIR="$HOME/feedling-venv"
DATA_DIR="$HOME/feedling-data"
ENV_FILE="$HOME/feedling.env"
RESIDENT_ENV="$HOME/feedling-chat-resident.env"
INSTALL_CADDY=0
for arg in "$@"; do
    case "$arg" in
        --install-caddy) INSTALL_CADDY=1 ;;
    esac
done

echo "=== 1. Create Python venv and install deps ==="
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/backend/requirements.txt"

echo "=== 2. Create data dir ==="
mkdir -p "$DATA_DIR"

echo "=== 3. Ensure env file exists ==="
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
# REQUIRED: all per-user data lives in PostgreSQL now (see backend/db.py).
# The backend will not boot without DATABASE_URL. Point it at your Postgres;
# for an external/managed instance add ?sslmode=require.
DATABASE_URL=postgresql://feedling:CHANGE_ME@127.0.0.1:5432/feedling
FEEDLING_DATA_DIR=$DATA_DIR
FEEDLING_FLASK_URL=http://127.0.0.1:5001
EOF
    chmod 600 "$ENV_FILE"
    echo "    wrote $ENV_FILE (multi-tenant — users register via iOS and receive per-user api_keys)"
    echo "    NOTE: edit DATABASE_URL in $ENV_FILE — Postgres is now required."
else
    echo "    $ENV_FILE already exists — leaving alone"
fi

echo "=== 4. Install all systemd service files ==="
sudo cp "$REPO_DIR/deploy/feedling-backend.service"       /etc/systemd/system/
sudo cp "$REPO_DIR/deploy/feedling-chat-resident.service" /etc/systemd/system/
sudo systemctl daemon-reload

echo "=== 5. Enable and start backend ==="
sudo systemctl enable feedling-backend
sudo systemctl restart feedling-backend

echo "=== 6. Chat resident (agent auto-reply) ==="
if [ -f "$RESIDENT_ENV" ]; then
    sudo systemctl enable feedling-chat-resident
    sudo systemctl restart feedling-chat-resident
    echo "    feedling-chat-resident started."
else
    echo "    $RESIDENT_ENV not found — skipping start (see checklist below)."
fi

if [ "$INSTALL_CADDY" = "1" ]; then
    echo "=== 7. Install Caddy (HTTPS reverse proxy) ==="
    sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list
    sudo apt-get update
    sudo apt-get install -y caddy
    sudo cp "$REPO_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
    sudo systemctl enable --now caddy
    echo "    Caddy installed. Point DNS for api.<domain> and mcp.<domain>"
    echo "    at this VPS, then 'sudo systemctl reload caddy'."
fi

# ─────────────────────────────────────────────────────────────────────
# POST-SETUP CHECKLIST — complete ALL steps before testing
# ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║         POST-SETUP CHECKLIST — complete before testing          ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  [✓]  feedling-backend        running                           ║"
if [ -f "$RESIDENT_ENV" ]; then
echo "║  [✓]  feedling-chat-resident  running  (agent auto-reply live)  ║"
else
echo "║  [ ]  feedling-chat-resident  NOT STARTED ← required for reply  ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  To enable agent auto-reply:                                    ║"
echo "║    1. cp $REPO_DIR/deploy/chat_resident.env.example            ║"
echo "║          ~/feedling-chat-resident.env && chmod 600 it           ║"
echo "║    2. Fill in FEEDLING_API_KEY and a real HTTP/CLI agent entry ║"
echo "║    3. sudo systemctl enable --now feedling-chat-resident        ║"
echo "║    4. sudo systemctl status feedling-chat-resident              ║"
fi
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "Check service status:"
echo "  sudo systemctl status feedling-backend feedling-chat-resident"
echo "  curl -s http://127.0.0.1:5001/healthz"
