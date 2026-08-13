#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
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
pytest_args=(--cov=kegpulse --cov-branch --cov-report=term-missing --cov-report=xml:coverage.xml)
[[ "$skip_browser" == 1 ]] && pytest_args+=(--ignore "$repo_root/tests/e2e")
"$venv_bin/python" -m pytest "${pytest_args[@]}"
if [[ "$skip_firmware" == 0 ]]; then
  "$venv_bin/platformio" test -d "$repo_root/firmware" -e native
  "$venv_bin/platformio" run -d "$repo_root/firmware" -e nanoatmega328
fi

