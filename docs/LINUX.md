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
creates `.venv`, installs hash-locked dependencies, and installs the package editable. Developer
setup also creates an isolated `.pio-venv` from `requirements-firmware.lock`; PlatformIO is not
installed into the host environment.

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
Only one process may use a data directory at a time, including restore operations. The
`.kegpulse.lock` file may remain after shutdown, but its kernel lock is released automatically and
the next launch can use it. Bind addresses are IPv4-only; IPv6 forms are rejected cleanly.

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

`run-linux.sh` detects an explicitly supplied `/dev` path that is not both readable and writable
and prints the exact group
action. The serial transport also turns an `EACCES`/`EPERM` open failure into read/write guidance
that names the device, its owning group when discoverable, the current user, and the exact
`usermod` plus log-out/in action. Neither path changes permissions itself. A device path may change
after reconnect, so hardware mode continues handshake-based discovery unless a manual port is
deliberately pinned.

## Target-native package

Build on Linux x86_64, never by cross-packaging from Windows:

```bash
./scripts/package-linux.sh
```

The script rejects non-x86_64 kernels and non-64-bit/non-x86_64 Python interpreters. It creates a
PyInstaller one-folder bundle, runs the frozen demo health/calibration/pour/restart smoke against
a Unicode-and-space data path, checks same-root second-instance and occupied-port rejection, then
writes `artifacts/KegPulse-linux-x86_64.tar.gz` and its SHA-256. It does not support Raspberry Pi
ARM64; use [RASPBERRY_PI.md](RASPBERRY_PI.md) there.

## Optional user service

The files under `packaging/` contain renderer placeholders and must not be copied directly. After
an interactive hardware run succeeds, render and install them for the current checkout:

```bash
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
.venv/bin/python scripts/render_systemd_units.py \
  --repo-root "$PWD" \
  --data-dir "${XDG_DATA_HOME:-$HOME/.local/share}/KegPulse" \
  --port 8765 \
  --output-dir "$unit_dir"
systemctl --user daemon-reload
systemctl --user enable --now kegpulse.service
```

It safely quotes the actual checkout/data paths and gives the host and kiosk one shared port.
Reverse both units without deleting data or running setup/dependency installation:

```bash
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
systemctl --user disable --now kegpulse-kiosk.service kegpulse.service
rm -f "$unit_dir/kegpulse.service" "$unit_dir/kegpulse-kiosk.service"
systemctl --user daemon-reload
```

## Troubleshooting

- Use `journalctl --user -u kegpulse.service` for service startup errors.
- If port 8765 is occupied, stop the other service or pass a different `--port`.
- If Chromium is missing, open the printed URL manually; normal measurement continues.
- For corrupt configuration, preserve it and validate its JSON/known keys; KegPulse refuses
  oversized, non-object, or unknown-key configuration rather than guessing.
- Preserve `~/.local/share/KegPulse` before restore or OS migration. Backups are unencrypted.
