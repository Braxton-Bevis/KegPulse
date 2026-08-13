#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python_bin="$repo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo 'Run scripts/setup-linux.sh first.' >&2
  exit 2
fi
for argument in "$@"; do
  if [[ "$argument" == /dev/* && -e "$argument" && ! -r "$argument" ]]; then
    device_group="$(stat -c '%G' -- "$argument")"
    echo "Serial port $argument is not readable. Inspect with: ls -l '$argument'" >&2
    echo "If appropriate, add your user to its group: sudo usermod -aG '$device_group' '$USER'" >&2
    echo 'Then log out and back in (or reboot). KegPulse will not change permissions automatically.' >&2
    exit 3
  fi
done
exec "$python_bin" -m kegpulse "$@"

