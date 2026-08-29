#!/usr/bin/env bash
# Bootstrap a KegPulse kiosk on Ubuntu (x86_64). Idempotent; rerun freely.
#
#   scripts/install-ubuntu-kiosk.sh [--restore /path/to/kegpulse.db] [--pin 1234]
#                                   [--lan-name NAME]... [--port 8765]
#
# Installs the source environment, restores an optional database snapshot,
# configures the administrator PIN, and installs a systemd user service that
# runs KegPulse in LAN display mode (kiosk on this machine, read-only board
# for other devices). Steps that need root (apt, serial group) use sudo and
# are skipped with a notice when sudo is unavailable.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
restore=""
pin=""
port=8765
lan_names=()
while (( $# > 0 )); do
  case "$1" in
    --restore) restore="$2"; shift 2 ;;
    --pin) pin="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --lan-name) lan_names+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }

# --- system packages -------------------------------------------------------
if ! command -v git >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1 \
   || ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  if sudo -n true 2>/dev/null; then
    say "installing system packages"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv chromium-browser >/dev/null || \
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv >/dev/null
  else
    echo "NOTICE: sudo needs a password here; run manually if missing:" >&2
    echo "  sudo apt install git python3 python3-venv chromium-browser" >&2
  fi
fi

# --- python environment ----------------------------------------------------
say "setting up the Python environment"
"$repo_root/scripts/setup-linux.sh"

# --- serial access for the Nano --------------------------------------------
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
  if sudo -n true 2>/dev/null; then
    say "adding $USER to dialout for the flow sensor's serial port"
    sudo usermod -aG dialout "$USER"
    echo "(log out and back in, or reboot, before the serial port is usable)"
  else
    echo "NOTICE: run manually, then log out/in:  sudo usermod -aG dialout $USER" >&2
  fi
fi

# --- data directory and optional restore -----------------------------------
data_root="${XDG_DATA_HOME:-$HOME/.local/share}/KegPulse"
mkdir -p -- "$data_root/backups"
if [[ -n "$restore" ]]; then
  if [[ ! -f "$restore" ]]; then echo "restore file not found: $restore" >&2; exit 2; fi
  systemctl --user stop kegpulse.service 2>/dev/null || true
  if [[ -f "$data_root/kegpulse.db" ]]; then
    stamp="$(date +%Y%m%d-%H%M%S)"
    cp -- "$data_root/kegpulse.db" "$data_root/backups/pre-restore-$stamp.db"
    say "existing database preserved at backups/pre-restore-$stamp.db"
  fi
  rm -f -- "$data_root/kegpulse.db-wal" "$data_root/kegpulse.db-shm"
  cp -- "$restore" "$data_root/kegpulse.db"
  say "database restored from $restore"
fi

# --- administrator PIN (LAN mode refuses to start without one) -------------
if [[ -n "$pin" ]]; then
  say "configuring the administrator PIN"
  KEGPULSE_DATA_DIR="$data_root" "$repo_root/.venv/bin/python" - "$pin" <<'PY'
import sys
from pathlib import Path
from kegpulse.config import AppConfig
from kegpulse.paths import get_app_paths
from kegpulse.persistence import Database, Repository
from kegpulse.security import SecurityManager

paths = get_app_paths()
paths.ensure()
database = Database(paths.database)
try:
    SecurityManager(Repository(database), AppConfig(demo=False, no_browser=True)).set_pin(sys.argv[1])
finally:
    database.close()
print("PIN configured")
PY
fi

# --- systemd user service in LAN display mode -------------------------------
say "installing the systemd user service"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p -- "$unit_dir"
primary_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
names=("$(hostname)" "${primary_ip:-}")
names+=("${lan_names[@]}")
lan_args=""
for name in "${names[@]}"; do
  [[ -n "$name" ]] || continue
  lan_args+=" --allowed-host $name --allowed-origin http://$name:$port"
done
cat > "$unit_dir/kegpulse.service" <<UNIT
[Unit]
Description=KegPulse kiosk (LAN display mode)
After=network-online.target default.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$repo_root
Environment=KEGPULSE_DATA_DIR=$data_root
ExecStart=$repo_root/.venv/bin/python -m kegpulse --no-browser --lan --lan-display --port $port$lan_args
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
UMask=0077

[Install]
WantedBy=default.target
UNIT
"$repo_root/.venv/bin/python" "$repo_root/scripts/render_systemd_units.py" \
  --repo-root "$repo_root" --data-dir "$data_root" --port "$port" --output-dir "$unit_dir/.rendered" >/dev/null
cp -- "$unit_dir/.rendered/kegpulse-kiosk.service" "$unit_dir/kegpulse-kiosk.service"
rm -rf -- "$unit_dir/.rendered"
loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now kegpulse.service
sleep 4
systemctl --user --no-pager --lines=5 status kegpulse.service || true

say "done"
echo "Kiosk:  http://127.0.0.1:$port/"
echo "Board:  http://${primary_ip:-<this-ip>}:$port/#/display"
echo "Kiosk browser autostart (after confirming a graphical session):"
echo "  systemctl --user enable --now kegpulse-kiosk.service"
