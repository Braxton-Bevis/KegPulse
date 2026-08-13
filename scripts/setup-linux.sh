#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
dev=0
case "$#:${1:-}" in
  0:) ;;
  1:--dev) dev=1 ;;
  *) echo 'Usage: scripts/setup-linux.sh [--dev]' >&2; exit 2 ;;
esac

python_bin=""
for candidate in python3.11 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,13)' 2>/dev/null; then
    python_bin="$candidate"
    break
  fi
done
if [[ -z "$python_bin" ]]; then
  echo 'KegPulse requires CPython 3.11 or 3.12.' >&2
  exit 2
fi
if ! "$python_bin" -m venv --help >/dev/null 2>&1; then
  echo "Python venv support is missing. On Debian/Raspberry Pi OS: sudo apt install python3-venv" >&2
  exit 2
fi
"$python_bin" -m venv "$repo_root/.venv" || {
  echo "Virtual environment creation failed. On Debian/Raspberry Pi OS: sudo apt install python3-venv" >&2
  exit 2
}
venv_python="$repo_root/.venv/bin/python"
"$venv_python" -m pip install --upgrade 'pip==25.2'
lock="$repo_root/requirements.lock"
[[ "$dev" == 1 ]] && lock="$repo_root/requirements-dev.lock"
"$venv_python" -m pip install --require-hashes -r "$lock"
"$venv_python" -m pip install --no-deps -e "$repo_root"
if ! "$venv_python" -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("platformio") is None else 1)'; then
  echo 'The host .venv contains PlatformIO. Remove .venv and rerun setup; PlatformIO belongs only in .pio-venv.' >&2
  exit 2
fi
echo "KegPulse environment ready at $repo_root/.venv"
if [[ "$dev" == 1 ]]; then
  export PLATFORMIO_SETTING_ENABLE_TELEMETRY=no
  "$python_bin" -m venv "$repo_root/.pio-venv" || {
    echo 'Isolated PlatformIO virtual environment creation failed.' >&2
    exit 2
  }
  pio_python="$repo_root/.pio-venv/bin/python"
  "$pio_python" -m pip install --upgrade 'pip==25.2'
  "$pio_python" -m pip install --require-hashes -r "$repo_root/requirements-firmware.lock"
  echo "Isolated PlatformIO environment ready at $repo_root/.pio-venv"
fi
