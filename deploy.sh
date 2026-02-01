#!/bin/bash
# Deploy sports-arb to test server
set -e

SERVER="192.168.1.251"
USER="marmok"
REMOTE_DIR="/home/marmok/sports-arb"

echo "==> Syncing project to $SERVER..."
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.pytest_cache' --exclude 'sports_arb.db' \
    -e "sshpass -p 'gimgimlil' ssh -o StrictHostKeyChecking=no" \
    /Users/ildarflame/Desktop/sports-arb/ \
    ${USER}@${SERVER}:${REMOTE_DIR}/

echo "==> Setting up on server..."
sshpass -p 'gimgimlil' ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} << 'REMOTE_SCRIPT'
set -e
cd /home/marmok/sports-arb

# Install uv if not present
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
export PATH="$HOME/.local/bin:$PATH"

# Copy .env from example if not exists
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit it with your API keys"
fi

# Install dependencies
uv sync

# Create systemd user service
mkdir -p ~/.config/systemd/user/
cat > ~/.config/systemd/user/sports-arb.service << 'EOF'
[Unit]
Description=Sports Arbitrage Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/marmok/sports-arb
ExecStart=/home/marmok/.local/bin/uv run python -m src.main
Restart=on-failure
RestartSec=5
Environment=PATH=/home/marmok/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

# Reload and start
systemctl --user daemon-reload
systemctl --user enable sports-arb
systemctl --user restart sports-arb

echo "==> Service started. Dashboard at http://192.168.1.251:8000"
systemctl --user status sports-arb --no-pager || true
REMOTE_SCRIPT

echo "==> Deploy complete!"
echo "Dashboard: http://192.168.1.251:8000"
