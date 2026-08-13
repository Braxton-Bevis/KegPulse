#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
install_service=0
remove_service=0
for argument in "$@"; do
  [[ "$argument" == "--install-service" ]] && install_service=1
  [[ "$argument" == "--remove-service" ]] && remove_service=1
done

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "Notice: this installer targets Raspberry Pi OS 64-bit (aarch64); detected $(uname -m)." >&2
fi
"$repo_root/scripts/setup-linux.sh"
mkdir -p "$HOME/.local/share/KegPulse/backups" "$HOME/.config/systemd/user"

if [[ "$remove_service" == 1 ]]; then
  systemctl --user disable --now kegpulse.service kegpulse-kiosk.service 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/kegpulse.service" "$HOME/.config/systemd/user/kegpulse-kiosk.service"
  systemctl --user daemon-reload
  echo 'KegPulse user services removed. Data was preserved.'
  exit 0
fi

if [[ "$install_service" == 1 ]]; then
  sed "s|%h/KegPulse|$repo_root|g; s|%h|$HOME|g" "$repo_root/packaging/kegpulse.service" > "$HOME/.config/systemd/user/kegpulse.service"
  cp "$repo_root/packaging/kegpulse-kiosk.service" "$HOME/.config/systemd/user/kegpulse-kiosk.service"
  systemctl --user daemon-reload
  systemctl --user enable --now kegpulse.service
  echo 'Host service installed. Enable the kiosk after confirming Chromium and the display:'
  echo '  systemctl --user enable --now kegpulse-kiosk.service'
else
  echo 'Source installation complete. Optional reversible autostart:'
  echo '  scripts/install-raspberry-pi.sh --install-service'
  echo 'Remove it later with:'
  echo '  scripts/install-raspberry-pi.sh --remove-service'
fi

if compgen -G '/dev/ttyUSB*' >/dev/null || compgen -G '/dev/ttyACM*' >/dev/null; then
  device="$(compgen -G '/dev/ttyUSB*' | head -n1 || compgen -G '/dev/ttyACM*' | head -n1)"
  if [[ ! -r "$device" ]]; then
    group="$(stat -c '%G' -- "$device")"
    echo "Serial permission is missing for $device." >&2
    echo "After verifying its group, run: sudo usermod -aG '$group' '$USER'" >&2
    echo 'Then log out/in or reboot. This installer did not alter permissions.' >&2
  fi
fi

