# Linux x86_64 installation and operation

Ubuntu 22.04-compatible Linux x86_64 is a source, CI, and target-native package target. The
Linux CI/package workflows are defined in the repository; this Windows development session did
not execute a Linux runner, so local Linux outcomes must not be inferred from Windows evidence.

## Source setup

Install CPython 3.11 or 3.12 with venv support. On Debian/Ubuntu this is typically:

```bash
sudo apt install python3 python3-venv
./scripts/setup-linux.sh
```

For tests and packaging:

```bash
./scripts/setup-linux.sh --dev
python -m playwright install --with-deps chromium
./scripts/test-linux.sh
```

Setup itself does not invoke `sudo`, alter serial permissions, or install a system service. It
creates `.venv`, installs hash-locked dependencies, and installs the package editable.

## Demo and hardware mode

```bash
./scripts/run-linux.sh --demo
./scripts/run-linux.sh --demo --no-browser
./scripts/run-linux.sh
./scripts/run-linux.sh --serial-port /dev/ttyACM0
./scripts/run-linux.sh --kiosk
```

Normal mode enumerates ports and attaches only after a KP1 handshake. The launch helper prefers
Chromium/Chrome kiosk/app mode and falls back to the default browser. A missing browser does not
stop the service; use the printed `http://127.0.0.1:8765` URL.

Data is in the current user's platform data directory, normally
`~/.local/share/KegPulse/`. Override it with `--data-dir` or `KEGPULSE_DATA_DIR`. The install
directory may be read-only because runtime data, logs, and backups are kept outside it.

## Serial permissions

Do not run KegPulse as root and do not make the device world-writable. Inspect the actual node:

```bash
ls -l /dev/ttyACM0
stat -c '%G' /dev/ttyACM0
```

If the verified device group is, for example, `dialout`, add only the intended user and then log
out/in or reboot:

```bash
sudo usermod -aG dialout "$USER"
```

`run-linux.sh` detects an explicitly supplied unreadable `/dev` path and prints the exact group
action; it never changes permissions itself. A path may change after reconnect, so hardware mode
continues handshake-based discovery unless a manual port is deliberately pinned.

## Target-native package

Build on Linux x86_64, never by cross-packaging from Windows:

```bash
./scripts/package-linux.sh
```

This creates a PyInstaller one-folder bundle, runs the frozen demo health/calibration/pour/data
path/shutdown smoke test, then writes `artifacts/KegPulse-linux-x86_64.tar.gz` and its SHA-256.
It does not support Raspberry Pi ARM64; use [RASPBERRY_PI.md](RASPBERRY_PI.md) there.

## Optional user service

The provided `packaging/kegpulse.service` is a template. Copy and adjust its repository path,
then install it as a user service only after an interactive hardware run succeeds:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/kegpulse.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kegpulse.service
```

Reverse it without deleting data:

```bash
systemctl --user disable --now kegpulse.service
rm ~/.config/systemd/user/kegpulse.service
systemctl --user daemon-reload
```

## Troubleshooting

- Use `journalctl --user -u kegpulse.service` for service startup errors.
- If port 8765 is occupied, stop the other service or pass a different `--port`.
- If Chromium is missing, open the printed URL manually; normal measurement continues.
- For corrupt configuration, preserve it and validate its JSON/known keys; KegPulse refuses
  oversized, non-object, or unknown-key configuration rather than guessing.
- Preserve `~/.local/share/KegPulse` before restore or OS migration. Backups are unencrypted.
