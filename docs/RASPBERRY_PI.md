# Raspberry Pi OS 64-bit deployment

Raspberry Pi OS 64-bit Bookworm on `aarch64`, Python 3.11, and a directly connected Chromium
touchscreen are the intended long-term topology. This is a **source deployment**, not an x86_64
bundle. It is documented and CI-independent; no Pi, ARM package, real serial device, or display
was available in the current build session, so complete the manual checks before unattended use.

## Prepare and install

Install the minimal OS packages with an administrator account, then perform application setup as
the unprivileged kiosk user:

```bash
sudo apt update
sudo apt install python3 python3-venv chromium
git clone <your-kegpulse-repository-url> ~/KegPulse
cd ~/KegPulse
./scripts/install-raspberry-pi.sh
```

The script creates the local `.venv` and durable
`~/.local/share/KegPulse/backups` directory. It rejects unknown/conflicting arguments, does not use
sudo, does not alter serial permissions, and refuses non-`aarch64` machines rather than implying
that this Pi deployment was tested on another architecture.

Test interactively first:

```bash
./scripts/run-linux.sh --demo --kiosk
./scripts/run-linux.sh --serial-port /dev/ttyACM0 --kiosk
```

Confirm the real device handshake, boot identity, counted pulses, reconnect behavior, touch
layout, and calibration before enabling startup services.

## USB serial access

Inspect the actual Nano device node and group:

```bash
ls -l /dev/ttyACM0
stat -c '%G' /dev/ttyACM0
```

Only after confirming the group, add the kiosk user (example group `dialout`) and reboot/log in:

```bash
sudo usermod -aG dialout "$USER"
```

Do not run KegPulse as root, add global read/write rules, or assume every USB bridge uses the
same path. Both read and write access are required. Handshake discovery handles `/dev` path
changes when no explicit port is pinned.

## Reversible user autostart

After interactive validation:

```bash
cd ~/KegPulse
./scripts/install-raspberry-pi.sh --install-service --port 8765
systemctl --user enable --now kegpulse-kiosk.service
```

The installer renders both user units with safely quoted checkout/data paths, including spaces,
Unicode, `%`, `$`, and shell metacharacters. The selected port is shared by the host and kiosk.
`kegpulse.service` starts the host; `kegpulse-kiosk.service` requires a bounded JSON health
response identifying KegPulse before it launches Chromium. No shell command interprets the
rendered paths. To let user services start without an interactive login, an administrator may
choose:

```bash
sudo loginctl enable-linger "$USER"
```

That OS policy is optional and should be understood before enabling it. Remove both KegPulse
services while preserving all data:

```bash
./scripts/install-raspberry-pi.sh --remove-service
```

Removal is available before setup and without dependency/network access, removes both rendered
units, preserves data, and tolerates an unavailable user service manager. Disable linger
separately if it was enabled: `sudo loginctl disable-linger "$USER"`.

## Display and kiosk behavior

Use a directly connected 7- or 10-inch touchscreen, with the host and browser on the same Pi.
Test the actual panel resolution (especially 800×480 or 1024×600), touch targets, focus,
on-screen keyboard, rotation, blanking/power policy, and recovery after Chromium or Pi restart.
KegPulse assumes no display battery and stores no authoritative state in the browser.

The service binds to IPv4 loopback, so a separate LAN is unnecessary for the attached display.
IPv6 bind forms are currently rejected rather than formatted or probed ambiguously.
Trusted-LAN mode is optional and requires a configured PIN and exact allowlists; see
[SECURITY.md](SECURITY.md). Keep loopback mode for the simplest and safest kiosk.

## Data, backup, and update

Durable data defaults to `~/.local/share/KegPulse/`:

```text
kegpulse.db
logs/kegpulse.log*
backups/
exports/
```

Back up this directory to storage appropriate for personal consumption history. Backups are not
encrypted. Before updating source, stop the user services, create/copy a backup, update the
checkout and locked environment, run tests or demo smoke, then restart:

```bash
systemctl --user stop kegpulse-kiosk.service kegpulse.service
cd ~/KegPulse
git pull --ff-only
./scripts/setup-linux.sh
systemctl --user start kegpulse.service kegpulse-kiosk.service
```

Do not overwrite local changes or perform a destructive database import during an update.

## Manual commissioning

Complete every Raspberry Pi and physical item in [HARDWARE.md](HARDWARE.md), including sustained
pulse-rate testing, USB unplug/reset in every state, real `/dev` permission recovery, ten water
pours, installed-keg recalibration, weighed verification, kiosk resolution/touch, and unexpected
power-loss recovery. Until then, the Pi deployment remains documented but hardware-unverified.
