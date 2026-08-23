# KegPulse architecture decisions

Decisions are intentionally conservative: retain evidence, avoid guessed measurements, and keep the local system small.

## ADR-001: Authority and concurrency

Firmware owns accepted pulses and its boot-relative session state. A single asynchronous host coordinator owns all application mutations and serial event processing. SQLite writes are serialized. The browser never owns an active session. This prevents multiple tabs, duplicate frames, or concurrent HTTP calls from finalizing twice.

## ADR-002: Durable identifiers and acknowledgement

Host entities use UUIDv4. Attributed sessions use a host UUID. Firmware has a stable device ID, a boot ID, and a monotonically increasing event sequence. Device results are unique by `(device_id, boot_id, event_seq)` and sessions are also unique by host UUID. The host commits a result transactionally before sending `ACK`. The current host supplies the device ID from its active `HELLO`, and firmware validates device, boot, and event sequence. For compatibility with the original KP1 proto-1 wire contract, firmware also accepts a legacy ACK that omits `dev` but still requires matching boot and sequence; any present `dev` is validated strictly. Duplicate results and duplicate API idempotency keys return the existing outcome.

## ADR-003: Measurement and reset evidence

Raw pulse counts are immutable. Firmware keeps a 64-bit boot-lifetime total and saturating counters rather than silently wrapping. A reset changes boot identity. Same-boot unexplained positive deltas become recovered, unattributed, `needs_review` evidence; cross-boot deltas are never invented. A cancel after the first pulse produces an interrupted partial result.

## ADR-004: Firmware electrical defaults

The input is D2/INT0, configured as `INPUT`; the external pull-up is required. `KEGPULSE_INTERNAL_PULLUP` defaults off. The default interrupt edge is `FALLING`, subject to physical sensor verification. The noise gate defaults to zero/off because no plausible sensor frequency has yet been measured.

## ADR-005: Timers and boundaries

Default arm timeout is 15 seconds, flow-gap transition is 750 ms, and settling completion is 1,500 ms. Values are bounded below half the unsigned 32-bit timer range. At an exact deadline, an already captured pulse is processed before timeout/completion. The ISR retains the first and last timestamp of each pending batch and the machine applies the first edge separately, preventing later batched edges from moving it across an arm/settle deadline. Immediately before a command mutation or timer tick, firmware takes a bounded atomic snapshot: that batch is applied first, while edges arriving after the snapshot are ordered after the command/tick. STATUS exposes the rollover-safe authoritative remaining arm time as `arm_left`.

## ADR-006: Protocol

Serial is 115200 8-N-1. KP1 frames are ASCII/LF and protected by CRC-16/CCITT-FALSE. Device responses
are at most 256 bytes including LF; Nano-bound host requests are at most 128 bytes, with every defined
request fitting in 113 bytes. Device and boot identities use exactly 16 uppercase hexadecimal digits.
Request IDs correlate replies; semantic idempotency uses device/boot/event/session identity. Partial
and concatenated frames are handled by newlines; an oversized frame is discarded through its newline
and parsing then resumes. Full grammar is in `docs/PROTOCOL.md`.

## ADR-007: Bounded recovery

Firmware holds four unacknowledged terminal results in a fixed array. When full it rejects attributed arms and retains later lifetime pulses in a recovery bucket exposed by `COUNTERS`. Host serial events use a bounded queue of 256; changed COUNTERS snapshots are events through that same bound. Overflow forces visible degraded state and a status/results resynchronization; it is never treated as proof that no pulse occurred. A boot mismatch in STATUS or RESULT forces a fresh handshake before new-boot results are accepted. Reconnect backoff is bounded from 250 ms to 15 seconds with jitter.

## ADR-008: Calibration

Canonical calibration is pulses per milliliter computed as `sum(pulses) / sum(mass_g / density_g_per_ml)`, never the mean of ratios. A run stores ten samples and requires at least seven included samples to activate. Inputs must be finite and within documented bounds. Outliers use a median absolute deviation rule on sample ratios (modified z-score greater than 3.5; when MAD is zero, any unequal ratio is flagged) and require explicit user inclusion/exclusion. Calculations retain full precision; displays round only at presentation.

## ADR-009: Pre-calibration behavior

Calibration capture remains possible before an active K-factor. Ordinary flow is not erased or assigned a guessed factor: it is stored with raw pulses, null volume, and `needs_review`. The kiosk prominently directs setup through calibration before normal pouring. Historical pours are never recomputed when a later calibration activates.

## ADR-010: Participants and unattributed flow

`Guest / Unattributed` means `participant_id = NULL`; it is not a hidden participant row. With zero active profiles the home screen presents one `Start pour` action that is explicitly unattributed. Reassignment changes only participant attribution and writes an audit entry.

## ADR-011: Kegs and inventory

Only one keg is open. A pour captures keg and calibration IDs when armed or first observed. Remaining volume is derived from starting milliliters minus finalized known-volume pours plus signed adjustments. Unknown-volume pours and negative remainder are visibly flagged. Replacement closes but never deletes the old keg or its records. Keg replacement and calibration activation are rejected while flow or unresolved finalization is active.

## ADR-012: Storage and paths

SQLite uses foreign keys, WAL, a 5-second busy timeout, and explicit transactional numbered migrations. Writable files use `platformdirs`, overridable by `KEGPULSE_DATA_DIR`; no state is written beside source or a frozen executable. Backups use SQLite's online backup API and atomic rename. Restore validation occurs on a copy after an automatic pre-restore backup.

## ADR-013: Time and units

Durable timestamps are RFC 3339 UTC. Device uptime remains monotonic device data, not wall time. Canonical volume is milliliters; UI supports mL/L and US fluid ounces (1 US fl oz = 29.5735295625 mL). Decimal-backed domain calculations precede float serialization.

## ADR-014: HTTP security

The default bind is `127.0.0.1`. Exact Host and Origin checks protect mutation routes and WebSockets; CORS is disabled. Mutations are JSON-only and use a same-origin CSRF token. LAN mode requires explicit configuration, host/origin allowlists, and an admin PIN. PINs use versioned `hashlib.scrypt` with a random 16-byte-or-longer salt and constant-time comparison; opaque sessions are server-side and bounded. Plain HTTP LAN mode is documented as unsuitable for hostile/shared networks.

## ADR-015: Resource and privacy bounds

JSON bodies are capped at 64 KiB; input lengths, list pages, serial lines/queues, and WebSocket clients/outboxes are bounded. Logs rotate and omit participant names, notes, bodies, tokens, PINs, and raw frame floods. CSV neutralizes formula-leading content. Demo endpoints and controls are registered only in explicit demo mode, which cannot be combined with LAN mode.

## ADR-016: PWA and browser state

Static assets are local and loaded through package resources. The service worker cache contains only exact versioned app-shell files. API/auth/history/export data is never cached. WebSockets publish full snapshots with monotonic revisions; polling is the visible fallback and reconnect always refreshes a complete snapshot. Client storage is limited to harmless display preferences.

## ADR-017: Runtime and release support

Source support targets CPython 3.11 and 3.12 on Windows x64 and Ubuntu 22.04-compatible Linux x86_64. Releases use target-native PyInstaller one-folder bundles. Raspberry Pi OS 64-bit Bookworm/Python 3.11 is source-install documentation pending hardware verification. macOS, Python 3.13+, and ARM64 frozen bundles are not claimed.

## ADR-018: Physical assumptions still unverified

The actual sensor voltage, polarity, pulse width/rate, K-factor, food-contact rating, barb diameter, fittings, external pull-up value, Nano bootloader/USB bridge behavior, line hydraulics, scale, Windows COM behavior, and Raspberry Pi/touchscreen behavior require physical checks. Simulator and compile evidence cannot close those items.

## ADR-019: Durable overflow-counter consumption

`COUNTERS.recovery` is a cumulative, boot-scoped count of accepted pulses that firmware could not
place in its fixed result store. The host checkpoints it transactionally by `(device_id, boot_id)`.
Each increase creates one deterministic, unattributed `estimated_recovered` delta pour and advances
the checkpoint in the same transaction; equality is a replay and a decrease is rejected. Serial
events capture device/boot identity plus the applicable keg/calibration context when admitted, so a
delayed measurement is never relabeled with a newer device, keg, or calibration. Duplicate results
retain the first captured context. Recovery counters stay ordered by cumulative boot-scoped value;
an increase across a keg/calibration boundary is retained as a separate delta instead of being
coalesced into the newer context. Coordinator event exceptions are isolated and retried through a
bounded queue; result commit failures remain unacknowledged for firmware's periodic replay.

## ADR-020: Arm binding and workflow consumption

The host binds a provisional UUID to the exact device, boot, next event sequence, and confirmed
lifetime before transmitting `ARM`. An explicit device rejection marks that binding failed; a
transport failure or lost acknowledgement leaves it active for status/result reconciliation because
the device may have armed. Calibration and verification capture consumption writes the measured
entity and its durable `consumed_entity_id` pointer atomically, so retry returns the original entity
instead of duplicating it.

## ADR-021: Dependency and instance isolation

The runtime pins patched FastAPI/Starlette releases in hashed host locks. PlatformIO is constrained
by a different dependency graph, so firmware tooling uses its own hashed `requirements-firmware.lock`
and `.pio-venv`, with telemetry disabled; it is absent from the host environment and frozen bundle.
One kernel-held lock per resolved data directory covers normal startup and offline restore. A stale
lock file is harmless because ownership is the OS lock, not file existence.
