#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "Linux packages must be built natively on x86_64; detected $(uname -m)." >&2
  exit 2
fi
if ! "$repo_root/.venv/bin/python" -c 'import platform, struct, sys; sys.exit(0 if struct.calcsize("P") == 8 and platform.machine().lower() in {"x86_64", "amd64"} else 1)'; then
  echo 'Linux packages require 64-bit x86_64 CPython.' >&2
  exit 2
fi
mkdir -p "$repo_root/artifacts"
"$repo_root/.venv/bin/pyinstaller" --noconfirm --clean --distpath "$repo_root/dist" --workpath "$repo_root/build/pyinstaller" "$repo_root/packaging/KegPulse.spec"
"$repo_root/.venv/bin/python" "$repo_root/scripts/smoke-package.py" --executable "$repo_root/dist/KegPulse/KegPulse"
tar -C "$repo_root/dist" -czf "$repo_root/artifacts/KegPulse-linux-x86_64.tar.gz" KegPulse
(cd "$repo_root/artifacts" && sha256sum KegPulse-linux-x86_64.tar.gz > SHA256SUMS.txt)
echo "Created $repo_root/artifacts/KegPulse-linux-x86_64.tar.gz"
