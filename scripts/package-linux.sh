#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
mkdir -p "$repo_root/artifacts"
"$repo_root/.venv/bin/pyinstaller" --noconfirm --clean --distpath "$repo_root/dist" --workpath "$repo_root/build/pyinstaller" "$repo_root/packaging/KegPulse.spec"
"$repo_root/.venv/bin/python" "$repo_root/scripts/smoke-package.py" --executable "$repo_root/dist/KegPulse/KegPulse"
tar -C "$repo_root/dist" -czf "$repo_root/artifacts/KegPulse-linux-x86_64.tar.gz" KegPulse
(cd "$repo_root/artifacts" && sha256sum KegPulse-linux-x86_64.tar.gz > SHA256SUMS.txt)
echo "Created $repo_root/artifacts/KegPulse-linux-x86_64.tar.gz"

