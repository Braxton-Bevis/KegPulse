# KegPulse hardware and test rig

KegPulse v1 is designed for one pulse-output flow meter, one Arduino Nano-compatible
ATmega328P board, one USB host, one keg, and one tap. The repository has been compiled for
the Nano target, but the actual board, meter, wiring, fittings, and beverage line have not
been physically verified. Complete the checklist at the end before relying on measurements.

## Electrical connection

The firmware counts the sensor signal on Arduino **D2 / INT0**. Its default build expects an
**external pull-up** and configures D2 as `INPUT`; the internal pull-up is off. The default
interrupt edge is `FALLING`, and the pulse-noise gate is disabled.

```text
sensor supply  -------- verified sensor supply voltage
sensor ground  -------- Nano GND -------- host USB ground
sensor output  -------- Nano D2 / INT0
                         |
                         +--- external pull-up --- verified logic supply
```

Do not choose the pull-up supply or resistor from this diagram alone. Confirm the real
meter's datasheet and output type first. The signal presented to D2 must remain within the
ATmega328P input limits; a meter with a higher-voltage or push-pull output may require level
translation rather than a direct connection. Verify idle level, active polarity, minimum
pulse width, and maximum pulse rate with a meter or logic analyzer.

Firmware build switches are defined in [platformio.ini](../firmware/platformio.ini):

- `KEGPULSE_INTERNAL_PULLUP=0` is the default and matches the external-pull-up wiring.
- `KEGPULSE_PULSE_EDGE=FALLING` is the unverified default polarity.
- `KEGPULSE_NOISE_GATE_US=0` accepts every observed edge. Set a nonzero gate only after the
  shortest legitimate interval has been measured, then rerun native and physical pulse tests.
- `KEGPULSE_FLOW_GAP_MS=750` and `KEGPULSE_SETTLING_MS=1500` control pause detection and resume
  grace. Change them in the build environment—not firmware source—only with recorded flow tests.
- `KEGPULSE_DEVICE_ID` defaults to the development ID `4B454750554C5345`. Assign and record a
  distinct ID of exactly 16 uppercase hexadecimal digits (`0-9`, `A-F`) for each physical
  controller through its build environment. Other values fail the firmware build.

Changing any of these values requires a new firmware build and a recorded hardware test.

## Beverage line and water rig

The intended line is **3/16-inch ID × 7/16-inch OD food-grade PVC beer line**. Verify that the
actual line is suitable for the beverage, temperature, pressure, and cleaning products in use.
Also verify the meter's barb diameter and food-contact documentation. Never force an
incompatible hose onto a plastic barb.

A representative water rig is:

```text
water source/spigot
  -> appropriate GHT adapter
  -> correctly sized barb and clamp
  -> short section of the actual beer line
  -> flow meter in its documented direction
  -> short section of line
  -> collection vessel on a tared scale / bucket
```

Use leak-appropriate pressure and secure the assembly before flowing water. If both the tubing
manufacturer and meter manufacturer permit it, soften only the tubing end briefly in hot water
before fitting and clamp it correctly. Do not heat the meter body. Clean and sanitize every
beverage-contact part according to its manufacturer's instructions. These notes are not a
food-safety certification and do not establish a universal line-balance or pressure setting.

Using the final line type makes the test rig more representative, but it does not eliminate the
need to recalibrate after the meter is installed on the actual keg and line. Follow
[CALIBRATION.md](CALIBRATION.md) for the ten-pour procedure.

## Host and display topology

For initial testing, connect the Nano by USB to the existing Windows tablet or laptop and run
the host and browser on that machine. The permanent recommended topology is a Raspberry Pi OS
64-bit host with a directly connected 7- or 10-inch touchscreen:

```text
flow meter -> Nano D2 -> USB serial -> Raspberry Pi
                                      |-- KegPulse host + SQLite
                                      `-- local Chromium kiosk -> touchscreen
```

The display has no software battery assumption. The browser is replaceable: authoritative
sessions, calibration, inventory, and history live in the host database, not browser storage.
See [RASPBERRY_PI.md](RASPBERRY_PI.md) for source installation, service, kiosk, data paths, and
serial permissions.

## Physical acceptance checklist

Keep these items `OPEN-M` in [TEST_MATRIX.md](TEST_MATRIX.md) until measured. Record date,
operator, board and sensor identifiers, firmware commit/version, OS, commands, instruments,
expected counts, observed counts, and any deviations.

- Identify the exact Nano-compatible board, processor/bootloader selection, USB bridge, and
  stable device ID; upload the compiled firmware successfully.
- Confirm sensor supply range, output circuit, logic-high voltage, edge polarity, barb size,
  flow direction, pressure/temperature limits, and food-contact documentation.
- Select and document the external pull-up voltage and resistance; verify D2 voltage levels.
- Inject known pulse counts on D2 at slow, typical, and maximum plausible rates. Confirm the
  lifetime/session totals exactly and check for bounce or loss with a logic analyzer.
- Unplug and reconnect USB while idle, armed, pouring, and settling; repeat with physical reset.
  Confirm replay/reconciliation creates no duplicate and never invents a cross-boot delta.
- Run sustained plausible maximum flow and serial traffic; confirm no counter, UART, or retained
  result overflow.
- Assemble and leak-test the water rig, then perform ten varied weighed pours.
- Reinstall on the actual keg/line, recalibrate, and perform a separate weighed verification.
- Exercise the real Windows COM device, then Raspberry Pi/Linux permissions and reconnect.
- Verify the chosen touchscreen at 800×480 or 1024×600, including focus, touch targets, kiosk
  recovery, and graceful restart.

Simulator runs, native firmware tests, and board compilation are useful evidence but do not
close any of these physical checks.
