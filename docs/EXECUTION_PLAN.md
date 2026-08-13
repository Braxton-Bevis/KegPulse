# KegPulse v1 execution plan

This is the integration plan for the greenfield KegPulse repository. It incorporates the six independent initial reviews completed on 2026-08-12: requirements architecture, embedded/protocol, Windows/Linux/Raspberry Pi release, kiosk UX/accessibility, security/privacy, and adversarial testing.

## Quality objective

KegPulse v1 is complete when the testable acceptance criteria in the product brief pass, the quality gates are green, final specialist review has no unresolved critical/high/medium finding, and every physical check that could not be performed is identified precisely.

## Integration order

- [x] Inspect repository and toolchain baseline (empty workspace; Python 3.12 and Git available; PlatformIO absent initially).
- [x] Complete six independent read-only initial reviews and reconcile them.
- [x] Implement pure domain models, calibration/inventory math, protocol, device state machine, and tests.
- [x] Implement firmware core/native tests and compile the Nano target.
- [x] Implement deterministic simulator through the same protocol/transport contract.
- [x] Implement SQLite migrations/repositories, backup/export, and recovery.
- [x] Implement single-writer coordinator, serial discovery/reconnect, FastAPI API, WebSocket snapshots, and security controls.
- [x] Implement the local semantic HTML/CSS/JavaScript kiosk PWA and browser tests.
- [x] Implement Windows/Linux/Pi scripts, PyInstaller packaging, and CI.
- [x] Complete operating, hardware, calibration, protocol, security, and data-model documentation.
- [x] Run the initial Ruff, mypy, pytest/coverage, browser, firmware native/build, and Windows package smoke gates.
- [x] Run the six final code reviews, fix all valid critical/high/medium findings, add regressions, and rerun affected/full gates.

## Architecture and critical path

```text
D2 pulse -> firmware ISR/counter -> firmware session machine
         -> checked KP1 serial result -> bounded transport queue
         -> single-writer host coordinator -> SQLite transaction
         -> result ACK -> monotonic full snapshot -> WebSocket/browser
```

Firmware owns live pulse counting, boot-relative state, and recoverable results. The host owns durable identity, calibration, attribution, keg assignment, inventory, and audit history. The browser is a replaceable projection of host state.

## Work ownership

The root agent retains integration ownership and reviews every shared result. Specialist work used
bounded, non-overlapping review, documentation, firmware, and test paths; overlapping runtime edits
were explicitly sequenced before integration and the complete gate.

## Reconciled conflicts

1. Firmware is authoritative for pulse intervals; the host is authoritative for persistence and inventory. A result is acknowledged only after its database commit.
2. Before calibration, raw pulses are persisted with `volume_ml = NULL` and `needs_review`. First-run UI directs users to calibration and clearly warns that inventory cannot yet be reduced by an unknown volume.
3. With no configured participants, `Start pour` creates an explicitly unattributed session. No synthetic person is silently created.
4. An idle pulse starts a firmware unattributed session; the host stores `participant_id = NULL` and retains the same raw measurement evidence as an attributed pour.
5. Exactly ten samples are collected for a calibration run; activation requires at least seven included samples. Suspected outliers are flagged but never excluded automatically.
6. Inventory is a ledger calculation and may be negative. Negative remaining volume is an overrun needing review, never silently clamped.
7. Default networking is exact loopback. LAN mode requires explicit enablement, an allowlist, and an admin PIN; plain HTTP LAN confidentiality is not claimed.
8. The PWA caches only repository-owned versioned shell assets. API, history, authentication, and exports are network-only.
9. Windows/Linux x86_64 packages are built natively. Raspberry Pi OS 64-bit is a source deployment and is not represented as compatible with x86_64 bundles.

## Evidence retained

Test reports, coverage XML/HTML, browser traces/screenshots where available, native firmware output, board compilation output, package-smoke logs, and artifact SHA-256 values are generated under `artifacts/` or CI. Hardware-only items remain marked manual until recorded on the actual sensor, Nano-compatible board, line, scale, Windows host, and Pi.

The final Windows/Python 3.12 gate completed with 259 host tests passing and 2 Linux-only skips,
16 Chromium journeys passing, 17 native firmware tests passing, a successful Nano build, and a
successful frozen Windows package smoke. Windows package, process-lock, restart, asset, and
external-data-path checks passed. Linux/Python 3.11 jobs are configured for target-native CI but
were not executed on this workstation. The final specialist reviews left no unresolved
critical/high/medium software finding; all physical commissioning evidence remains open.
