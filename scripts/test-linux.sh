#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$repo_root"
mkdir -p "$repo_root/artifacts"
venv_bin="$repo_root/.venv/bin"
skip_browser=0
skip_firmware=0
for argument in "$@"; do
  [[ "$argument" == "--skip-browser" ]] && skip_browser=1
  [[ "$argument" == "--skip-firmware" ]] && skip_firmware=1
done
"$venv_bin/ruff" format --check "$repo_root/src" "$repo_root/tests"
"$venv_bin/ruff" check "$repo_root/src" "$repo_root/tests"
"$venv_bin/mypy" "$repo_root/src/kegpulse"
"$venv_bin/python" -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("platformio") is None else 1)' || {
  echo 'PlatformIO must not be installed in the host .venv.' >&2
  exit 2
}
pytest_args=(--ignore "$repo_root/tests/e2e" --junitxml=artifacts/pytest-linux.xml --cov=kegpulse --cov-branch --cov-report=term-missing --cov-report=xml:coverage.xml --cov-report=json:coverage.json)
"$venv_bin/python" -m pytest "${pytest_args[@]}"
"$venv_bin/python" "$repo_root/scripts/check_core_coverage.py" "$repo_root/coverage.json" --minimum 90
if [[ "$skip_browser" == 0 ]]; then
  "$venv_bin/python" -m pytest "$repo_root/tests/e2e" --junitxml=artifacts/pytest-browser-linux.xml
fi
if [[ "$skip_firmware" == 0 ]]; then
  export PLATFORMIO_SETTING_ENABLE_TELEMETRY=no
  pio="$repo_root/.pio-venv/bin/platformio"
  [[ -x "$pio" ]] || { echo 'Run scripts/setup-linux.sh --dev first.' >&2; exit 2; }
  "$pio" test -d "$repo_root/firmware" -e native
  "$pio" run -d "$repo_root/firmware" -e nanoatmega328
fi
