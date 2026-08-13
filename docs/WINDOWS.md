# Windows installation and operation

Windows x64 is a source and packaged-release target. The local evidence for v1 was produced on
Windows with CPython 3.12; CI also defines CPython 3.11. The actual Nano/COM device remains a
manual hardware check.

## Source setup

Install 64-bit CPython 3.11 or 3.12. A non-administrator PowerShell is sufficient. From the
repository root—even when its path contains spaces—run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
```

For tests and packaging tools:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -Dev
```

The script creates `.venv`, installs hash-locked dependencies, and installs KegPulse editable
without writing user data into the repository. Developer setup also creates an isolated
`.pio-venv` from `requirements-firmware.lock`; PlatformIO is never installed into the host
environment because its optional web dependencies conflict with the patched host web stack.

## Demo and hardware mode

```powershell
# Complete simulator; opens Edge/Chrome app mode or the default browser
.\scripts\run-windows.ps1 -Demo

# Simulator without opening a browser
.\scripts\run-windows.ps1 -Demo -NoBrowser

# Hardware auto-discovery
.\scripts\run-windows.ps1

# Explicit serial port and HTTP port
.\scripts\run-windows.ps1 -SerialPort COM5
.\scripts\run-windows.ps1 -Port 8877

# Fullscreen kiosk preference
.\scripts\run-windows.ps1 -Kiosk
```

The terminal always prints the URL, normally `http://127.0.0.1:8765`. If no supported browser
can be launched, the service stays running and prints the URL. If that port belongs to another
service, KegPulse exits with a clear conflict instead of attaching to it. A second launch that
finds a healthy KegPulse instance exits without opening a duplicate tab. Omitting `-Port` leaves
the saved `config.json` port in control; the wrapper only supplies `--port` when `-Port` was
explicitly bound. Bind addresses are IPv4-only; IPv6 forms are rejected as configuration errors.

Data defaults to the per-user `platformdirs` location (normally under local AppData). Override
it for diagnostics or removable test profiles:

```powershell
.\scripts\run-windows.ps1 -Demo -DataDir 'D:\KegPulse data'
```

The directory contains `kegpulse.db`, `.kegpulse.lock`, `config.json` when saved, `logs\`,
`backups\`, and `exports\`. The kernel-held lock permits only one process per data directory,
including restore operations; a leftover lock file after a crash is harmless. Back the directory
up as sensitive personal data. Stop with Ctrl+C; the host stops its serial thread, checkpoints
SQLite, and closes resources.

## Tests, firmware, and package

Install Playwright Chromium once after developer setup:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
.\scripts\test-windows.ps1
```

Connect the Nano and upload only after confirming the board/bootloader and port:

```powershell
$env:PLATFORMIO_SETTING_ENABLE_TELEMETRY = 'no'
.\.pio-venv\Scripts\platformio.exe run -d firmware -e nanoatmega328 -t upload --upload-port COM5
```

Build the target-native one-folder release and run its automated demo smoke test:

```powershell
.\scripts\package-windows.ps1
```

The script refuses non-AMD64 or 32-bit Python builds. The zip and SHA-256 manifest are written
under `artifacts\`. The smoke test uses a temporary external data directory containing Unicode
and spaces, verifies same-root second-instance and occupied-port rejection, and confirms that the
frozen bundle is unchanged.

## Backup and restore

Use **Create atomic backup** in Device & settings. Backups are unencrypted SQLite files. Restore
while KegPulse is stopped:

```powershell
.\.venv\Scripts\python.exe -m kegpulse --data-dir 'D:\KegPulse data' --restore 'D:\Backups\kegpulse-20260812T120000Z.db'
```

The command validates the candidate and creates a pre-restore backup when a current database
exists. Keep an independent copy until the restored instance has been checked.

## Troubleshooting

- **COM access denied/busy:** close Arduino Serial Monitor and other programs using the port,
  unplug/replug, then restart. Do not run the whole app as administrator.
- **No device detected:** confirm the correct firmware, USB cable, driver, and D2 wiring. Use the
  settings port scan; auto-discovery accepts a port only after a KP1 handshake.
- **Device repeatedly resets:** some Nano USB bridges toggle DTR when serial opens. Record the
  boot ID behavior and complete the reset/recovery checks in [HARDWARE.md](HARDWARE.md).
- **Browser unavailable:** use the printed URL in any modern local browser.
- **Data problem:** preserve the data directory before repair. Logs rotate at 2 MiB with five
  retained files and intentionally omit PINs/request bodies.
