# Changelog

## Unreleased

- Put calibration and verification back behind the administrator PIN, including
  the page itself; the pour and completion screens stay neutral so a capture
  can route through them without dropping the session.
- Fixed a cancelled pour inheriting a calibration sample that was still waiting
  for its scale mass, which wrongly prompted for a weight instead of ending
  quietly.
- Hardened the whole stack after a four-agent audit: restored the administrator
  PIN on calibration/verification activation and encrypted-DB backup download,
  bound automatic avatars to the participant currently pouring, and made the
  request-size middleware actually return 413/408 instead of a 500.
- Fixed a migration that could permanently fail to start once a corrupt recovery
  pour had been reassigned: its ledger, charges, audit rows, and device-result
  pointer are now cleared and the charge refunded before the pour is removed,
  and poisoned recovery checkpoints keep their watermark so they fail closed.
- Bounded the calibration factor to a plausible range so one mistyped sample can
  no longer mis-meter volume and money; reported variation as n/a below two
  samples and stopped stale outlier flags from lingering.
- Fixed the PIN keypad dropping input when its form re-rendered, scoped the admin
  relock to actually leaving admin tabs, armed the completion auto-return once so
  it fires on hardware, and stopped pour-video recorders from leaking.
- Firmware: enabled the 150 us noise gate for the open-collector sensor, moved
  the recovery counter and fault pointer to the low end of memory so a stack
  overrun hits scratch state first, and sized number buffers to their contract.
- Serial: a device-reported command error no longer kills the reader thread, and
  a single dropped periodic poll retries once before forcing a DTR reset.
- Captured photo and video evidence for unattributed flow (nobody selected): photos join the
  management evidence grid labeled Unattributed, and videos share the five-slot on-device pool.
- Relaxed the administrator PIN to 4-20 digits.
- Replaced browser-savable PIN fields with an on-screen keypad dialog; admin unlock now expires
  when leaving a tab, and the Settings and Participants tabs require the PIN outright.
- Showed each person's account balance beside their name on the home screen and added a
  twelve-ounce "beers left" estimate to the home and keg views.
- Recorded a WebM video of each pour while the camera is armed, keeping the five most recent in
  the user's Videos folder (photos continue for every pour).
- Added automatic face-cropped profile photos captured the first time a person pours, shown
  beside their name and editable or removable in management.

- Hardened measurement integrity end to end: reduced Nano stack usage so protocol formatting cannot
  overwrite the recovery counter, added host-side counter-relationship and pulse-rate-envelope
  validation, and quarantined semantically invalid RESULT/COUNTERS/status frames as durable
  measurement anomalies with acknowledgement so poisoned retained results cannot flood retries.
- Migrated previously stored corrupt recovery pours out of pour history and inventory (schema v4),
  preserving each raw reading as anomaly evidence and resetting only the poisoned recovery
  checkpoints; the dashboard clamps out-of-envelope volume instead of rendering it.
- Displayed pour volume in US fluid ounces, milliliters, and estimated grams during live flow,
  completion, recent pours, and history.
- Generalized explicit provisional calibration activation from exactly one sample to any partial
  run (one to nine samples) using the included-sample aggregate factor, analyzed partial runs in
  the review screen (predicted volume, residuals, MAD outlier flags from three included samples),
  and stopped labeling unjudgeable samples "Consistent".

## 1.0.0 — 2026-08-12

- Added the local-first FastAPI host, versioned SQLite persistence, bounded serial manager,
  full-state WebSocket stream, polling recovery, local PWA shell, and platform data paths.
- Added participant/guest pours, durable unattributed flow and reassignment audit, keg inventory,
  replacement/adjustment history, CSV/JSON export, atomic backup, and validated restore CLI.
- Added ten-pour density-aware calibration with explicit outlier review, immutable versions, and
  weighed drift verification.
- Added the KP1 checked ASCII serial protocol, deterministic simulator/fault injection, and
  Arduino Nano ATmega328P firmware with autonomous attributed/unattributed state handling.
- Added loopback-first Host/Origin/CSRF controls, optional scrypt PIN and explicit LAN mode,
  rotating structured logs, bounded inputs/queues/sockets, and static-only service-worker cache.
- Added touch/keyboard kiosk UI, Windows/Linux source scripts, Raspberry Pi source/service
  deployment, target-native PyInstaller configurations, package smoke tests, and CI workflows.
- Added Python unit/integration/browser suites, shared protocol vectors, native firmware tests,
  Nano compilation, traceability documentation, and manual hardware commissioning checks.
- Hardened final-review boundaries: durable firmware overflow-counter consumption, pre-command ARM
  binding, retry-safe calibration/verification commits, captured boot identity, aggregate body
  deadlines, live WebSocket authorization revocation, patched web dependencies, network-first PWA
  updates, complete paged exports, per-data-root process locks, and target-architecture guards.
- Added authoritative arming countdown and timeout/uncertainty screens, inline pour reassignment,
  measurement details, editable install time and serial settings, diagnostics, focus preservation,
  and dark-theme contrast regressions.
- Added the additive schema-v2 upgrade with a released-v1 data fixture, immutable calibration sample
  revisions, commit-failure rollback recovery, and identity-bound device acknowledgements.
- Preserved admission-time keg/calibration context across delayed results and ordered recovery
  counter retries, including context changes during transient database failures.
- Backfilled unambiguous already-consumed schema-v1 capture receipts and made every unverifiable
  pre-v2 crash candidate fail closed without claiming unrelated evidence or creating duplicates.
- Made host outages visibly invalidate stale kiosk state, recovered expired security context,
  preserved failed-form input, exposed unattributed device flow, and completed calibration evidence.
- Synchronized PIN lock/unlock controls without discarding unrelated form edits and made active or
  superseded calibration evidence explicitly read-only.
- Added a demo-only contextual tutorial on every persistent, live-pour, and completion screen with
  accessible hide/reopen controls, guided navigation, responsive styling, and screenshot coverage.

Hardware-dependent characteristics—sensor electrical behavior, plumbing/food-contact suitability,
physical pulse accuracy, Windows COM and Raspberry Pi deployment—remain explicitly unverified
until the commissioning checklist is performed.
