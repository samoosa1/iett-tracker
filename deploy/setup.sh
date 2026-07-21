#!/usr/bin/env bash
# STEP 2 — turnkey install on a fresh Ubuntu/Debian VPS.
# Put iett_tracker.py next to this script, then run:  bash setup.sh
# (Re-runnable; safe to run again to update.)
set -euo pipefail

APPDIR="$HOME/iett-takip"
PORT="${PORT:-8731}"
LINES="${LINES:-522,M7}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$HERE/iett_tracker.py" ]; then
  echo "ERROR: iett_tracker.py must sit next to this script ($HERE)."; exit 1
fi

echo "==> installing Python (sudo may prompt for your password)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip curl >/dev/null

echo "==> copying app to $APPDIR"
mkdir -p "$APPDIR"
cp "$HERE/iett_tracker.py" "$APPDIR/"

echo "==> creating virtualenv + installing dependencies"
python3 -m venv "$APPDIR/.venv"
"$APPDIR/.venv/bin/pip" install -q --upgrade pip
"$APPDIR/.venv/bin/pip" install -q requests rich qrcode

echo "==> installing systemd service (auto-start + auto-restart, no caffeinate needed on a server)"
sudo tee /etc/systemd/system/iett-takip.service >/dev/null <<EOF
[Unit]
Description=IETT bus tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$APPDIR/.venv/bin/python $APPDIR/iett_tracker.py --map $LINES --lan --port $PORT --no-open
WorkingDirectory=$APPDIR
Restart=always
RestartSec=5
User=$USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now iett-takip
sleep 3

echo "==> seeding ETA baselines once (schedule + traffic prior for 522)"
"$APPDIR/.venv/bin/python" "$APPDIR/iett_tracker.py" --seed-traffic 522 || true

echo
echo "==> DONE. Service status:"
systemctl --no-pager status iett-takip | head -6 || true
echo
echo "Map URL:  http://$(curl -s --max-time 8 ifconfig.me || echo '<server-ip>'):$PORT/"
echo
echo "IMPORTANT — security: the app has NO password and binds to all interfaces."
echo "  • Best: install Tailscale on the server + your phone, reach it privately."
echo "  • Or: restrict the firewall so only YOUR ip can hit port $PORT."
echo "  Do NOT leave port $PORT open to the whole internet long-term."
echo
echo "Manage:  sudo systemctl {status|restart|stop} iett-takip"
echo "Logs:    journalctl -u iett-takip -f"
echo "Learned: $APPDIR/.venv/bin/python $APPDIR/iett_tracker.py --eta-stats"
