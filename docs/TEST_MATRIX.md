# KegPulse v1 test matrix

Evidence types: **U** unit/property, **I** integration, **E** browser, **P** package/OS, **F** firmware native/build, **M** manual hardware. `PASS-A` means the automated portion passed locally on Windows; `CI-CONFIGURED` means a target-native job exists but has not run in this workspace. `OPEN-M` cannot be closed by simulator or compile evidence.

| ID | Requirement / risk | Evidence | Status |
|---|---|---|---|
| DOM-01 | State transitions, exact arm/gap/settle boundaries, cancel semantics | `test_device_machine*`; native `test_main.cpp` | PASS-A |
| DOM-02 | Every accepted pulse is allocated once; lifetime is monotonic/saturating | device boundary/recovery tests; native saturation; physical generator | PASS-A / OPEN-M |
| DOM-03 | Duplicate API/device completion causes one durable pour/inventory effect | `test_persistence.py`; `test_measurement_recovery.py`; API journey | PASS-A |
| DOM-04 | Idle flow becomes unattributed; partial cancel retains pulses | domain, API, simulator, `test_kiosk.py` | PASS-A |
| CAL-01 | Aggregate calibration formula, density/unit validation and precision | `test_calibration.py`; numeric boundaries | PASS-A |
| CAL-02 | Ten samples, seven included, explicit robust outlier handling | calibration unit/API/browser journey | PASS-A |
| CAL-03 | Version activation and immutable historical calibration/volume | persistence/API/browser calibration tests | PASS-A |
| CAL-04 | Verification threshold never changes calibration | calibration/API/browser tests; real scale | PASS-A / OPEN-M |
| INV-01 | Ledger arithmetic, negative overrun, auditable adjustments | inventory/persistence/API tests | PASS-A |
| INV-02 | Replacement preserves old keg, pours, and calibrations | API/browser replacement journey | PASS-A |
| PRO-01 | CRC, grammar, exact/over max length, numeric bounds | protocol and result-validation unit tests | PASS-A |
| PRO-02 | Partial/concatenated/corrupt/unknown/non-ASCII recovery | protocol boundaries, simulator, native parser | PASS-A |
| PRO-03 | Shared Python/C++ golden vectors agree byte-for-byte | `frames.json`; fixture generator; native golden test | PASS-A |
| PRO-04 | Duplicate/delayed results and stale identities are safe | machine/protocol/simulator/recovery tests | PASS-A |
| FW-01 | Nano target compiles; ISR bounded; counters atomically drained | PlatformIO Nano build/native tests; real board | PASS-A / OPEN-M |
| FW-02 | Timer/counter wrap/saturation and result-store capacity | Python/native boundary tests | PASS-A |
| SER-01 | Attach only after handshake; remember/manual port selection | runtime serial-preference tests; real multi-port rig | PASS-A / OPEN-M |
| SER-02 | Busy/path change/unplug/backoff/clean shutdown | simulator integration/package shutdown; real OS ports | PASS-A / OPEN-M |
| SER-03 | Bounded overflow triggers authoritative replay, not silent loss | operational failure and queue-overflow integration | PASS-A |
| SIM-01 | Seeded attributed/unattributed/timeout/cancel/reset repeat | simulator integration/API/browser tests | PASS-A |
| SIM-02 | Corrupt/partial/duplicate/delayed/out-of-order injection | simulator integration and demo controls | PASS-A |
| DB-01 | Fresh schema and every committed migration fixture | fresh database plus released v1-with-data upgrade to v2 | PASS-A |
| DB-02 | Unique constraints/rollback prevent double finalization | persistence and recovery concurrency/idempotency tests | PASS-A |
| DB-03 | Transient write failure never causes a false ACK | injected SQLite finalize/diagnostic failures and periodic replay | PASS-A |
| DB-04 | Recovery counters checkpoint once per boot/delta and retain ordered keg/calibration context | checkpoint repository + delayed/retried context-boundary integration | PASS-A |
| DB-05 | Commit failure rolls back connection; consumed capture retry survives recapture | injected commit failure and immutable sample-revision tests | PASS-A |
| REC-01 | Provisional reconciliation across same/new boot and states | reconciliation unit and measurement-recovery integration | PASS-A |
| REC-02 | Unknown/reset gaps preserve facts and uncertainty | reconciliation and persistence recovery tests | PASS-A |
| DATA-01 | JSON/CSV export, formula mitigation, faithful raw counts | export unit/API/browser tests | PASS-A |
| DATA-02 | Atomic backup, validation, rollback, pre-restore preservation | persistence/CLI/operational failure tests | PASS-A |
| API-01 | Typed validation, limits, idempotent arm/cancel/current state | `test_api.py`; API model/unit tests | PASS-A |
| API-02 | WebSocket snapshot/reconnect/poll fallback; host outage invalidates stale UI | API socket and browser refresh/disconnect/offline tests | PASS-A |
| SEC-01 | Loopback default; exact Host/Origin/CSRF; no wildcard CORS | API LAN/loopback security tests | PASS-A |
| SEC-02 | LAN requires PIN/session; protected destructive actions | security/API/browser PIN tests | PASS-A |
| SEC-03 | Bounded bodies/queues/sockets, redacted private rotating logs | API/operational/logging tests | PASS-A |
| SEC-04 | Patched web dependencies; UNC traversal/range DoS/body-deadline regressions | security regressions + hashed lock assertions | PASS-A |
| PWA-01 | Local-only static cache; API/auth/history never cached | browser cache/offline test | PASS-A |
| UI-01 | Onboarding, required screens, timeout/partial/reassignment/settings and form-error preservation | automated primary browser journeys + manual checklist below | PASS-A / OPEN-M |
| UI-02 | 800x480, 1024x600, desktop; no overflow/touch-size failures | parametrized browser test; physical touchscreens | PASS-A / OPEN-M |
| UI-03 | Keyboard focus, labels, non-color status/live regions | browser semantic/focus checks; screen reader/contrast | PASS-A / OPEN-M |
| UI-04 | Refresh/second tab converge during an active session | browser active-session refresh/multi-page test | PASS-A |
| UI-05 | Demo-only contextual tutorial covers all persistent/live/completion screens and stays absent in hardware mode | tutorial navigation, dismiss/reopen, touch geometry, mode-isolation browser tests + screenshots | PASS-A |
| OS-01 | Fresh non-admin setup from paths with spaces | Windows setup locally; Linux jobs | Windows PASS-P / Linux CI-CONFIGURED |
| OS-02 | Occupied port, external data path, graceful shutdown | CLI unit and Windows frozen smoke | PASS-A |
| PKG-01 | Native one-folder assets; no writes inside bundle | Windows package smoke; Linux package job | Windows PASS-P / Linux CI-CONFIGURED |
| PKG-02 | Frozen demo calibrated pour/restart and checksum | Windows frozen smoke + SHA-256; Linux package job | Windows PASS-P / Linux CI-CONFIGURED |
| PI-01 | Pi source install/service/kiosk/serial guidance | scripts/docs; actual ARM64 Pi/touchscreen | DOCUMENTED / OPEN-M |
| HW-01 | D2 wiring, pull-up voltage/value, polarity/rate, exact pulse injection | M | OPEN-M |
| HW-02 | Water-rig ten-pour and installed-keg calibration/verification | M | OPEN-M |
| HW-03 | Food-contact/fittings/cleaning confirmed from actual components | M | OPEN-M |

## Documented manual UI acceptance

These presentation checks are deliberately not represented as hardware or automation evidence:

- With a screen reader on the eventual kiosk OS, traverse every navigation item, participant tile,
  active-pour phase, confirmation, calibration sample row/card, history details block, settings
  control, warning, and error toast; confirm names, order, live announcements, and focus return.
- On each physical 7- or 10-inch display, check 800×480 or 1024×600 portrait/landscape behavior,
  glare/readability, dark/light contrast, browser zoom/reflow, touch accuracy, and on-screen keyboard
  obstruction. Automated Chromium covers geometry and computed button contrast, not the panel.
- Force a non-JSON server error and an unreadable JSON error while submitting each form family;
  confirm the toast/live-region message is understandable and the entered form data remains.
- Cold-load with the host stopped and no prior cache, then repeat after one successful online load;
  confirm the former shows the browser's offline result and the latter renders only the shell plus
  an explicit unavailable-service screen, never cached measurements.
- In real hardware mode, scan multiple COM/`/dev` candidates, select and clear/reselect a
  preference, reconnect, and verify the displayed firmware/protocol/device/boot/counters and
  permission/busy diagnostics against the actual device.

## Automated gate

The complete gate is Ruff format/lint, strict mypy, pytest with at least 90% branch coverage over domain/protocol/calibration/reconciliation modules, PlatformIO native tests, Nano compilation, Playwright Chromium at the required viewports, fixture-generation checks, target-native packaging, and frozen demo smoke. Linux and Python 3.11 evidence remains target-native CI evidence until those jobs actually run; it is not inferred from this Windows/Python 3.12 workstation.

Final local Windows evidence: 259 host tests passed with 2 Linux-only skips; all named core branch
targets exceeded 90%; 19 Chromium tests passed; 17 native firmware tests passed; the Nano target
compiled at 14,664/30,720 bytes flash and 1,612/2,048 bytes RAM; and the Windows x64 frozen package
passed its calibrated-pour, restart, asset, lock, occupied-port, Unicode-path, and integrity smoke.
