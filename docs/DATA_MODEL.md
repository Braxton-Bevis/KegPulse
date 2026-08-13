# KegPulse data model

KegPulse v1 uses one SQLite database with current schema version `2`. The authoritative migrations
are [`001_initial.sql`](../src/kegpulse/migrations/001_initial.sql) and
[`002_initial.sql`](../src/kegpulse/migrations/002_initial.sql); migration and connection behavior
are implemented in [`database.py`](../src/kegpulse/persistence/database.py), and application data
rules are implemented in [`repository.py`](../src/kegpulse/persistence/repository.py).

## Storage conventions

- Host-created durable IDs and attributed session IDs are lowercase hyphenated UUIDv4 text.
- An unattributed device result receives a deterministic UUIDv5 session ID derived from its device
  ID, boot ID, and event sequence.
- Device result identity is the composite `(device_id, boot_id, event_seq)`.
- Durable wall-clock timestamps are RFC 3339 UTC strings with millisecond precision and a `Z`
  suffix. Device start/end values are boot-relative integer milliseconds and are stored separately.
- Exact decimal values, including milliliters, mass, density, calibration factor, and error values,
  are stored as decimal strings in SQLite `TEXT` columns. Domain calculations use `Decimal`.
- Per-event raw pulses and sequences fit signed SQLite `INTEGER`; the unsigned 64-bit confirmed
  lifetime counter is decimal text. Boolean values are `INTEGER` constrained to `0` or `1` where
  the schema declares a check.
- Foreign-key enforcement is enabled for the application connection. No foreign key declares a
  cascading delete; the application exposes no record-deletion API.

The database normally lives at `kegpulse.db` under the per-user `platformdirs` data directory. The
root can be overridden with `--data-dir` or `KEGPULSE_DATA_DIR`. Logs and backups occupy sibling
directories; the config is a sibling `config.json` file.

## Relationships

```text
participants ----< provisional_sessions >---- kegs
     |                       |                   |
     |                       v                   |
     +------------------< pour_events >---------+
                              |  |
                              |  +----< attribution_audit >---- participants
                              +-------< device_results

calibrations ----< calibration_samples
      |
      +----------< verification_checks >-------- kegs
      +----------< provisional_sessions
      +----------< pour_events

kegs ------------< inventory_adjustments

device/boot ----- device_recovery_checkpoints ---- pour_events
```

Lines represent logical foreign-key relationships. Several references are nullable so KegPulse can
retain evidence when attribution, a keg, or a usable calibration is unavailable.

## Tables

### `participants`

Participant profiles. `id` is the primary key; `display_name` is 1 to 80 characters; `active` is a
checked boolean; `created_at` and `updated_at` are UTC timestamps. Deactivation preserves every
historical reference. Guest/unattributed flow is represented by a null participant, not a hidden
participant row.

Referenced by `provisional_sessions.participant_id`, `pour_events.participant_id`, and both the old
and new participant columns in `attribution_audit`.

### `kegs`

Keg versions. `id` is the primary key. Each row stores a 1-to-120-character label, exact
`starting_volume_ml`, `opened_at`, optional `closed_at`, and notes up to 1,000 characters. The
partial unique index `one_open_keg` permits at most one row whose `closed_at` is null. Replacing a
keg closes the current row and inserts a new one in one transaction. The API accepts an optional
timezone-aware `installed_at`, canonicalizes it to UTC in `opened_at`, and defaults it to the current
time. A replacement cannot precede the open keg's installation, and the prior keg's `closed_at`
equals the replacement's `opened_at`.

Referenced optionally by provisional sessions, pours, and verification checks, and mandatorily by
inventory adjustments.

### `calibrations`

Immutable calibration versions after activation. `id` is the primary key. A row stores liquid,
default density, optional computed `pulses_per_ml`, status (`draft`, `active`, or `superseded`),
notes, creation time, and optional activation time. The partial unique index
`one_active_calibration` permits at most one active row.

Activation requires ten stored samples with at least seven explicitly included. It supersedes the
previous active row and writes the aggregate factor to the draft. Activated and superseded samples
cannot be edited through the repository.

### `calibration_samples`

The raw evidence for a calibration. `id` is the primary key and `calibration_id` is required. Each
row contains an ordinal from 1 through 10, positive `raw_pulses`, mass, density, derived volume,
included and suspected-outlier flags, capture time, and optional supersession time. A partial unique
index permits one current row (`superseded_at IS NULL`) per `(calibration_id, ordinal)`.

While a calibration is a draft, recapturing an ordinal marks the prior row superseded and inserts a
new current row. The immutable prior row remains addressable by an already-consumed session, so a
retry returns the original measurements after a legitimate recapture. Calibration detail and
analysis use current rows only. Outlier status never silently changes the `included` flag.

### `verification_checks`

Periodic weighed checks. `id` is the primary key and `calibration_id` is required; `keg_id` is
optional. Each row preserves positive raw pulses, entered mass and density, predicted and actual
volume, absolute and percentage error, a checked warning flag, and creation time. A verification
never changes the active calibration automatically.

### `provisional_sessions`

The host's durable workflow and crash-recovery record. `session_id` is the primary key and
`idempotency_key` is globally unique. `purpose` is checked as `pour`, `calibration`, or
`verification`. Nullable participant, keg, and calibration references capture the associations
known when the session is created. `target_ordinal` supports calibration capture.

After device binding, the row records device ID, boot ID, event sequence, and the confirmed lifetime
pulse total. `(device_id, boot_id, event_seq)` is unique when those values are non-null.
`captured_raw_pulses` stores completed calibration/verification capture evidence. `status` and
created/updated timestamps describe workflow progress.
`consumed_entity_id` records the exact calibration sample or verification check produced when a
completed capture is consumed. The entity insert, pointer, and `consumed` status commit in one
transaction, so a retried commit returns the original durable row without replacing measurements.

Unlike `purpose`, the `status` column has no database CHECK constraint. The current application uses
values including `arming`, `armed`, `pouring`, `settling`, `finalizing`, `complete`, `timed_out`,
`cancelled`, `failed`, `consumed`, and `interrupted_uncertain`. Active-session queries treat only
`arming`, `armed`, `pouring`, `settling`, and `finalizing` as active. Completed rows are retained.

### `pour_events`

The permanent measurement history. `id` is the primary key and `session_id` is unique. Participant,
keg, calibration, computed `volume_ml`, and event sequence are nullable; device ID, boot ID, raw
pulses, attribution flag, quality, host timestamps, device milliseconds, and fault are retained.
`raw_pulses` must be nonnegative. Quality is checked as `complete`, `unattributed`, `interrupted`,
`estimated_recovered`, or `needs_review`.

`(device_id, boot_id, event_seq)` is unique when `event_seq` exists, independently of the unique
session ID. A same-boot counter-delta recovery has no device result sequence and is identified
idempotently by a deterministic recovery session UUID. The calibration
ID and computed volume are captured when the event is finalized. Later calibration activation does
not recompute an old pour. If no usable factor exists, volume remains null and quality is
`needs_review` so raw evidence is not guessed or erased.

Reassignment updates only `participant_id` and the attribution flag and creates an
`attribution_audit` row. It does not change raw pulses, volume, calibration, keg, or timestamps.

### `device_results`

The durable serial-result idempotency ledger. Its composite primary key is
`(device_id, boot_id, event_seq)`. It stores the optional session ID, terminal device status, raw
pulses, optional linked `pour_id`, and commit time.

A row may have no pour: zero-pulse timeouts and calibration/verification captures are still recorded
so replayed device results can be recognized. The coordinator commits this ledger and any related
pour or workflow update in one SQLite transaction before sending the device `ACK`. Duplicate serial
results return the prior outcome instead of inserting another pour.

An attributed result is accepted only when its session row is already durably bound to the exact
device ID, boot ID, and event sequence carried by the result. A mismatched or unknown attributed
session rolls back without writing either the result ledger or a pour, leaving a later valid replay
able to commit normally.

### `device_recovery_checkpoints`

One row per `(device_id, boot_id)` checkpoints the firmware's cumulative recovery-pulse counter as
an unsigned decimal string and links the most recently materialized pour. A strictly increasing
counter is converted atomically into one unattributed `estimated_recovered` pour containing only
the delta, using a deterministic session UUID derived from device, boot, old count, and new count.
The checkpoint advances in the same transaction. An equal counter returns the prior outcome without
another inventory effect; a same-boot decrease is rejected rather than interpreted as a wrap.

### `inventory_adjustments`

Auditable signed changes to a keg. `id` is the primary key; `keg_id` is required. `amount_ml` is an
exact decimal string, reason is 1 to 500 characters, and `created_at` is UTC. Zero adjustments are
rejected by the repository.

Inventory is derived, not stored:

```text
remaining = starting_volume - sum(known pour volumes) + sum(signed adjustments)
```

A negative remainder is retained as overrun rather than clamped. A pour with null volume sets the
derived `has_unknown_pours` flag and is not silently assigned an estimated volume.

### `attribution_audit`

Append-only attribution-change evidence. `id` is the primary key; `pour_id` is required; old and new
participant IDs are nullable foreign keys; reason is 1 to 500 characters; and `created_at` is UTC.
The current API reassigns to an existing participant, so `new_participant_id` is populated in normal
operation.

### `settings`

A key/value store with `key` as primary key, JSON text, and update time. Repository writes limit keys
to 80 characters and encoded values to 16,384 characters and reject non-finite JSON numbers. Current
uses include display units, completion delay, verification warning percentage, arm timeout,
serial-port preference, and `admin_pin_verifier`. The PIN verifier is salted scrypt material, not
plaintext.

There is no foreign key or schema-level allowlist for setting names; API settings expose and modify
only the explicitly selected public keys.

### `device_diagnostics`

A bounded local diagnostic trail with an autoincrement integer primary key, creation time, level,
code, and JSON context. Repository insertion truncates level to 16 characters, code to 80, and
encoded context to 2,000 characters, then deletes all but the newest 500 rows. This retention bound
is applied when diagnostics are inserted. Repository/API listing is newest-first and independently
bounded to at most 500 rows; JSON context is decoded into an object before it is returned.

## Indexes and uniqueness

In addition to primary-key and unique-constraint indexes, the current schema defines:

- `one_open_keg`, a partial unique index for the single open keg;
- `one_active_calibration`, a partial unique index for the single active calibration;
- `one_current_calibration_sample`, a partial unique index for one current sample per ordinal;
- `calibration_sample_history` for ordered sample revisions;
- `diagnostics_created` on diagnostic time descending;
- `pours_ended` on pour end time descending;
- `pours_participant` on participant and pour end time descending.

Important idempotency constraints are the unique provisional idempotency key, unique pour session
ID, unique device identity on provisional sessions and pours, and the device-results composite
primary key.

## Transaction and finalization rules

KegPulse uses one SQLite connection guarded by a reentrant lock. Reads and writes are serialized at
the repository boundary. Writes use `BEGIN IMMEDIATE`, commit on success, and roll back on either a
body or commit exception. The connection enables foreign keys, WAL journaling,
`synchronous=FULL`, a 5-second
SQLite connection timeout, and `busy_timeout=5000`.

Finalizing a device result performs these decisions in one transaction:

1. Look up its composite device identity; if present, return the existing result.
2. For attributed results, require an exact match to the provisional session's durable device,
   boot, and event-sequence binding.
3. For a calibration or verification capture, write the result ledger and update the provisional
   row with captured pulses, without creating a pour or changing inventory.
4. For a zero-pulse timeout, write the result ledger and mark the provisional session timed out,
   without creating a pour.
5. Otherwise use the keg and calibration captured when the frame was admitted (or the binding on a
   linked provisional session), compute volume once when a factor exists, insert the pour and result
   ledger, and mark a linked provisional session complete. The direct repository fallback to the
   current context exists only for legacy callers that do not supply captured context.

The coordinator acknowledges the firmware only after this transaction returns successfully. If the
ACK fails, firmware replay is safe because the composite key recognizes the already committed
result.

## Migration policy

SQLite `PRAGMA user_version` is the schema version and `PRAGMA application_id` is `0x4B50554C`.
`CURRENT_SCHEMA` is currently `2`. A fresh version-zero database applies packaged migrations 001
and 002 in separate explicit immediate transactions. Migration 002 adds durable capture-consumption
links, immutable calibration sample revisions, and recovery-counter checkpoints. Opening a database
newer than the running code fails rather than attempting a downgrade. Automated fixture coverage
opens released schema-v1 databases with existing data, validates them, and upgrades them to v2
without losing prior rows. For already-consumed v1 captures, migration 002 backfills an entity link
only for a one-to-one match: calibration ID, ordinal, raw pulses, and capture time for samples; or
calibration ID, keg ID, raw pulses, and creation time for verification checks. The entity timestamp
must fall inside a consumed provisional session's lifetime. For a v1 crash between the entity insert
and status update, any still-complete session beside a plausible later entity is marked consumed with
a null link. Even a one-to-one match fails closed because v1 also allowed direct sample/check entry,
so fields and timestamps cannot prove provenance. This avoids either claiming unrelated evidence or
writing another durable entity. A complete session with no plausible entity remains complete and can
be consumed normally.

Future changes must be additive numbered migrations, must retain raw/history evidence, and must be
tested both from a fresh database and from every prior committed schema. Existing migrations must
not be edited after release because that would make installations with the same `user_version`
structurally ambiguous.

## Backup, validation, and restore

`Database.backup()` uses SQLite's online backup API while holding the database lock. It writes a
temporary sibling, sets the application ID, commits and fsyncs that file, then uses `os.replace` for
an atomic destination update. The HTTP backup endpoint creates a UTC-timestamped `.db` name and
reports its size and SHA-256 digest. The digest is returned to the caller but is not stored in a
manifest.

`Database.validate_backup()` requires a regular file of at least 100 bytes, opens it read-only, and
checks its KegPulse application ID, supported schema range, SQLite integrity result, foreign keys,
and the required-table set for that version. For v2 it also verifies the two migration-critical
columns. It does not validate every table's complete column definition.

Command-line restore is performed before the HTTP service starts. The source is capped at 256 MiB.
Symlink sources and the live database itself are rejected. If a live database exists, a unique
`pre-restore-<UTC timestamp>-<token>.db` backup is created first. The source is copied to a unique
private candidate, validated again, and atomically placed at the live path while the old file is
held at a private rollback path. The restored database is opened so supported migrations run,
closed, and revalidated. If installation or reopen fails, the failed candidate is preserved under
`backups/` and the prior live database is automatically restored and reopened. Restore is
authorized by OS filesystem/process access, not by the web PIN.

Database files and backups are not encrypted and contain personal history plus the PIN verifier.
Keep them in a private OS account or encrypted volume, copy them through a protected channel, stop
the service before restore, and retain at least one known-good copy until the restored service and
history have been checked.

On POSIX systems, application data directories are set to mode `0700` and database/backup/config/
current-log files to `0600`. Windows uses the current user's inherited filesystem ACLs.

## Export and retention

The export API streams every pour as JSON or CSV from stable, bounded newest-first database pages,
including participant and keg labels from left joins. Raw pulses, original calibration/keg IDs,
quality, timestamps, and computed volume remain in each row. CSV string cells with
spreadsheet-control prefixes are neutralized with a leading apostrophe; JSON preserves the original
strings. The interactive history endpoint remains independently capped at 500 rows.

KegPulse v1 has no automatic deletion or age-based retention for participants, kegs, calibrations,
samples, verifications, sessions, pours, results, adjustments, audits, or settings. Only diagnostic
rows have a numeric retention cap. Closing/deactivating/superseding preserves history. Database
retention therefore lasts until an operator, backup policy, filesystem event, or external SQLite
tool removes it; direct database editing is outside the supported workflow and can invalidate audit
or measurement integrity.
