#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
install_service=0
remove_service=0
port=8765
port_set=0

usage() {
  cat <<'EOF'
Usage: scripts/install-raspberry-pi.sh [--install-service [--port PORT] | --remove-service]

With no arguments, install the source environment only. Service removal never runs setup or
dependency installation and always preserves the KegPulse data directory.
EOF
}

fail_usage() {
  echo "Error: $1" >&2
  usage >&2
  exit 2
}

validate_port() {
  [[ "$1" =~ ^[0-9]+$ ]] || fail_usage "port must be an integer"
  (( ${#1} <= 5 )) || fail_usage "port must be between 1024 and 65535"
  (( 10#$1 >= 1024 && 10#$1 <= 65535 )) || fail_usage "port must be between 1024 and 65535"
  port="$((10#$1))"
  port_set=1
}

while (( $# > 0 )); do
  case "$1" in
    --install-service)
      (( install_service == 0 )) || fail_usage "--install-service was supplied more than once"
      install_service=1
      shift
      ;;
    --remove-service)
      (( remove_service == 0 )) || fail_usage "--remove-service was supplied more than once"
      remove_service=1
      shift
      ;;
    --port)
      (( port_set == 0 )) || fail_usage "--port was supplied more than once"
      (( $# >= 2 )) || fail_usage "--port requires a value"
      validate_port "$2"
      shift 2
      ;;
    --port=*)
      (( port_set == 0 )) || fail_usage "--port was supplied more than once"
      validate_port "${1#--port=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail_usage "unknown argument: $1"
      ;;
  esac
done

(( install_service == 0 || remove_service == 0 )) || fail_usage "choose only one service action"
(( port_set == 0 || install_service == 1 )) || fail_usage "--port requires --install-service"

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if (( remove_service == 1 )); then
  manager_available=0
  removal_failed=0
  if systemctl --user show-environment >/dev/null 2>&1; then
    manager_available=1
    if ! systemctl --user disable --now kegpulse-kiosk.service kegpulse.service; then
      echo 'Error: the user service manager could not stop/disable KegPulse.' >&2
      removal_failed=1
    fi
  else
    echo 'User service manager is unavailable; removing enablement offline.' >&2
  fi
  rm -f -- "$unit_dir/kegpulse.service" "$unit_dir/kegpulse-kiosk.service"
  rm -f -- \
    "$unit_dir/default.target.wants/kegpulse.service" \
    "$unit_dir/default.target.wants/kegpulse-kiosk.service" \
    "$unit_dir/graphical-session.target.wants/kegpulse.service" \
    "$unit_dir/graphical-session.target.wants/kegpulse-kiosk.service"
  if (( manager_available == 1 )) && ! systemctl --user daemon-reload; then
    echo 'Error: the user service manager could not reload after removal.' >&2
    removal_failed=1
  fi
  (( removal_failed == 0 )) || exit 1
  echo 'KegPulse user services removed. Data was preserved.'
  exit 0
fi

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "This installer requires Raspberry Pi OS 64-bit (aarch64); detected $(uname -m)." >&2
  exit 2
fi

"$repo_root/scripts/setup-linux.sh"
data_root="${XDG_DATA_HOME:-$HOME/.local/share}/KegPulse"
mkdir -p -- "$data_root/backups" "$unit_dir"

if (( install_service == 1 )); then
  "$repo_root/.venv/bin/python" "$repo_root/scripts/render_systemd_units.py" \
    --repo-root "$repo_root" \
    --data-dir "$data_root" \
    --port "$port" \
    --output-dir "$unit_dir"
  systemctl --user daemon-reload
  systemctl --user enable --now kegpulse.service
  echo "Host service installed on http://127.0.0.1:$port/."
  echo 'Enable the kiosk after confirming Chromium and the display:'
  echo '  systemctl --user enable --now kegpulse-kiosk.service'
else
  echo 'Source installation complete. Optional reversible autostart:'
  echo '  scripts/install-raspberry-pi.sh --install-service [--port 8765]'
  echo 'Remove it later with:'
  echo '  scripts/install-raspberry-pi.sh --remove-service'
fi

shopt -s nullglob
serial_devices=(/dev/ttyUSB* /dev/ttyACM*)
shopt -u nullglob
for device in "${serial_devices[@]}"; do
  if [[ ! -r "$device" || ! -w "$device" ]]; then
    group="$(stat -c '%G' -- "$device")"
    echo "Serial read/write permission is missing for $device." >&2
    echo "After verifying its group, run: sudo usermod -aG '$group' '$USER'" >&2
    echo 'Then log out/in or reboot. This installer did not alter permissions.' >&2
  fi
done
