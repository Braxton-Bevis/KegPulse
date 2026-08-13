# KegPulse security and privacy

This document describes the security controls that are implemented in KegPulse v1. It does not
claim that a kiosk, USB sensor, operating system, or local network is tamper resistant.

## Security goals and trust boundaries

KegPulse protects two kinds of local data:

- measurement and inventory evidence, including raw pulse counts, calibration factors, keg history,
  adjustments, and attribution audit records;
- personal data, principally participant display names and timestamped pour history.

The browser is an untrusted presentation client. The host process and SQLite database are
authoritative for durable state. Serial input is also treated as untrusted input: protocol frames
are bounded, ASCII-only, checksummed, validated, and deduplicated by device, boot, and event
identity before they affect durable records.

The following are outside the v1 security boundary:

- an administrator, malware, or another process running with access to the KegPulse OS account;
- physical modification of the flow sensor, wiring, Nano, USB connection, or storage device;
- confidentiality against a party that can observe plain HTTP traffic in LAN mode;
- encryption of the database, backups, exports, or logs at rest;
- legal-for-trade or tamper-evident measurement.

An attacker with any of those capabilities can read private history or forge, suppress, or alter
measurements. Use OS account security, full-disk encryption, physical controls, and a trusted
network where those risks matter.

## Network defaults

The default bind address is exactly `127.0.0.1` on port `8765`. Configuration validation rejects a
non-loopback bind unless `lan_mode` is explicitly enabled. Uvicorn proxy-header trust is disabled,
and the normal runner disables access logs and the server-identification header.

Every HTTP request is checked against an exact Host allowlist. The built-in entries are
`127.0.0.1`, `localhost`, and `[::1]`; configured LAN host names or addresses are added explicitly.
The comparison removes a numeric port and lowercases the host. This check is important even on
loopback because it limits DNS-rebinding attacks against a local service.

KegPulse does not install CORS middleware and sends no permissive CORS headers. For `POST`, `PUT`,
`PATCH`, and `DELETE`, middleware additionally requires a non-null Origin equal to either the
request Host with `http` or `https`, or an explicitly configured origin. Mutation handlers then
require the session's CSRF value in `X-KegPulse-CSRF`. GET routes do not mutate durable product
records; the security-context GET may create an in-memory session and set its cookie.

`Content-Type: application/json` is used by the client for mutations, and Pydantic rejects unknown
model fields, non-finite numbers, and values outside each route's declared bounds. HTTP bodies are
limited to 65,536 bytes using both declared `Content-Length` and streamed-byte accounting. One
absolute 15-second deadline covers the complete streamed request body, so many small chunks cannot
reset the timer. There is no general HTTP request-rate limiter; the runner's keep-alive timeout is
five seconds.

### Loopback authorization

Loopback mode relies on the local OS boundary for confidentiality. Any process able to reach the
loopback port can read status, participants, settings, calibration data, history, exports, and
existing backup downloads. Those read routes do not require an administrator PIN in loopback mode.

All mutations require a server-issued session plus CSRF token. If no PIN exists, a new loopback
session is administrative. Once a PIN is configured, participant, keg, calibration, verification,
reassignment, settings, backup creation, PIN, demo, and test-shutdown changes require an unlocked
administrator session. Arming and cancelling a normal pour remain available to a CSRF-valid local
session so the kiosk can perform its primary task while administrator settings stay locked.

The health route, static application shell, security-context route, simple local API documentation,
and OpenAPI JSON are not PIN protected. They do not intentionally contain pour history, although
OpenAPI exposes the API shape.

### Trusted-LAN mode

LAN mode is deliberately opt-in. It requires all of the following before the app will start:

- `--lan`, which uses `0.0.0.0` unless another host is supplied;
- at least one exact `--allowed-host` entry;
- at least one exact `--allowed-origin` entry, including scheme and port where applicable;
- an administrator PIN that was configured previously while running on loopback.

For example, after configuring the PIN locally and stopping KegPulse:

```powershell
python -m kegpulse --lan --allowed-host 192.168.1.50 `
  --allowed-origin http://192.168.1.50:8765 --no-browser
```

In LAN mode, status, history, exports, backup downloads, settings, serial-port listings, and all
other protected API data require an authenticated administrator session. The WebSocket requires
the same session before the server accepts it. Health, the static shell, security context, API
documentation, and OpenAPI remain reachable after a valid Host check so a client can load and log
in.

The built-in LAN server is plain HTTP. The PIN, CSRF token, session cookie, history, and live state
are therefore not confidential against network sniffing or an active local-network attacker. LAN
mode is suitable only for a private, trusted network with no untrusted clients and no router port
forwarding. Prefer the same-device loopback kiosk. KegPulse's runner does not configure TLS, and a
reverse-proxy/TLS deployment is not part of the tested v1 launch path.

Demo mode cannot be combined with LAN mode. Production mode installs a catch-all 404 route for demo
API paths rather than registering simulator controls.

## PINs, sessions, and CSRF

Administrator PINs must contain 6 to 20 ASCII digits. The PIN itself is never stored. The database
contains a versioned verifier with:

- a random 16-byte salt;
- `hashlib.scrypt` parameters `N=16384`, `r=8`, `p=1`, and a 32-byte result;
- Base64-encoded salt and digest;
- constant-time digest comparison through `hmac.compare_digest`.

Changing or removing the PIN invalidates every in-memory session. Successful login also removes the
old cookie session and issues a new one. Login attempts are limited in a rolling 60-second window to
five per client address and 30 globally; the sixth attempt in either window is rejected. This state
is memory-only and resets when the service restarts, so the control slows online guessing but does
not make a numeric PIN high entropy or prevent offline guessing of a copied database.

Session and CSRF values are independently generated with `secrets.token_urlsafe(32)` and retained
only in server memory. Sessions expire after 30 minutes idle or 12 hours absolute and are pruned by
recent use; the configured table target is 128 sessions and can transiently contain one additional
new session before the next prune. Restarting the service invalidates all sessions.

The session cookie is `HttpOnly`, `SameSite=Strict`, scoped to `/`, and has no Domain attribute. It
is marked `Secure` only when the request URL scheme is HTTPS. CSRF tokens are returned by the
security-context/login response and kept in JavaScript memory, not browser persistent storage.
Neither value is placed in a URL.

## WebSocket and browser controls

`/api/v1/ws` checks Host and Origin before acceptance. LAN mode additionally requires the
administrator cookie. The socket is server-to-client only; any client data message closes it with a
policy violation. The normal runner caps incoming WebSocket messages at 16 KiB. The app allows at
most 16 subscribers, gives each a queue of two full snapshots, drops an older queued snapshot when
necessary, and sends a current full snapshot after a 20-second quiet interval. These bounds prevent
a slow browser from creating an unbounded event queue. Admission is serialized so simultaneous
connections cannot exceed the cap. In LAN mode the server revalidates the administrator session
before every snapshot or heartbeat; logout, PIN change, idle expiry, or absolute expiry closes an
already accepted socket.

Responses include these browser-facing headers:

- a Content Security Policy that keeps scripts and styles same-origin and includes
  `object-src 'none'`, `base-uri 'none'`, and `frame-ancestors 'none'`; its `connect-src` also
  permits `ws:` and `wss:`, while the KegPulse WebSocket endpoint independently checks Host and
  Origin;
- `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`;
- `Referrer-Policy: no-referrer`;
- a Permissions Policy disabling camera, microphone, geolocation, and payment.

API responses receive `Cache-Control: no-store`. The service worker caches only an exact list of
same-origin shell resources under the versioned `kegpulse-shell-v2-demo-guide` cache. It bypasses
every `/api/` request and every non-GET request, uses a cached `/` only as the offline navigation
fallback, and deletes only obsolete caches whose names begin with `kegpulse-shell-`. Shell requests
are network-first, and install/activation immediately replaces an old worker so a cached shell
cannot remain pinned to an incompatible API. Plain-HTTP LAN origins
normally do not qualify as browser secure contexts, so service-worker/PWA behavior is only expected
on localhost or an HTTPS deployment.

The frontend uses no `localStorage`, `sessionStorage`, or IndexedDB. Live status, personal history,
and the CSRF token exist only in page memory. Runtime assets are packaged locally; there is no CDN,
telemetry, cloud account, ad network, or runtime internet dependency.

## Data privacy, logs, and exports

SQLite, configuration, logs, and backups live under the per-user data root selected by
`platformdirs`, or under an explicit `--data-dir`/`KEGPULSE_DATA_DIR` override. On POSIX, KegPulse
sets its data directories to `0700` and database, configuration, log, and backup files it creates to
`0600`; custom parent directories and externally copied files still need operator review. Files are
not encrypted. Database, WAL, log, backup, export, and `.env` patterns are excluded by the
repository `.gitignore`, but that does not protect copies made elsewhere.

Local logs are structured JSON, rotate at 2 MiB, and retain five rotated files. The formatter
replaces CR/LF in messages, truncates messages to 1,000 characters, and records only an exception's
type rather than its traceback. Current application log calls record connection/error types, not
participant names, notes, request bodies, PINs, cookies, or tokens. The formatter is not a general
secret scrubber, so future log messages must preserve that discipline. Database diagnostics are
bounded to the latest 500 rows; context JSON is truncated to 2,000 characters.

CSV export prefixes an apostrophe when a string begins with tab or carriage return, or when the
first non-whitespace character is `=`, `+`, `-`, or `@`. This mitigates spreadsheet formula
execution; CSV quoting alone would not. JSON export does not transform text. The API streams the
complete pour ledger in bounded database pages and includes joined participant and keg labels, so
both formats are sensitive personal records. In LAN mode an administrator session is required. In
loopback mode the local OS boundary is the only read-access control.

## Backups and restore

Backup creation is an administrator mutation when a PIN is configured. SQLite's online backup API
runs while holding the repository's database lock, writes a sibling `.tmp` file, sets the KegPulse
application ID, commits and fsyncs the file, then replaces the named destination atomically. The
HTTP response reports the filename, size, and SHA-256 digest; no separate checksum manifest is
written. Backups are raw, unencrypted SQLite databases and include participant history, notes,
settings, and the administrator PIN verifier. Store and transfer them as sensitive files.

Restore is an offline command-line operation, not an HTTP upload:

```text
python -m kegpulse --data-dir <data-directory> --restore <backup.db>
```

The source must be an existing non-symlink regular file, must differ from the live database, and is
limited to 256 MiB. Validation opens it read-only and checks the KegPulse SQLite application ID,
supported schema range, integrity, foreign keys, and required table set. If a live database exists,
KegPulse first creates a unique timestamped online backup in the backup directory. It then copies
the source to a unique private candidate, validates that copy, moves the prior database to a private
rollback path, atomically installs the candidate, opens/checkpoints/revalidates it, and exits.

If installation or reopen validation fails, KegPulse first attempts to preserve the failed candidate
in `backups/`, then restores and reopens the prior database even if that diagnostic archival move
fails. If the rollback itself fails, the command reports that distinct terminal error and retains
the private rollback path for manual recovery. Validation checks version-specific required tables
and migration-critical v2 columns, but not every column definition. Restore authorization remains the ability to run the local
process and write its data directory, not the web administrator PIN.

Stop the service before restore, keep the reported pre-restore backup, and start the service once to
verify health and history before discarding any older copy.

## Configuration, dependencies, and operational guidance

Configuration JSON is limited to 64 KiB, must be an object, rejects unknown keys, and is replaced
through a temporary file. Host/origin allowlists contain at most 16 entries each. The config file
does not contain the PIN verifier or session secrets; the verifier is in SQLite and sessions are
memory-only.

Runtime dependencies and their transitive hashes are pinned in `requirements.lock`. The host pins
FastAPI 0.139.2 and Starlette 1.5.1, including absolute-path rejection, efficient range merging,
inverted-range rejection, and the 100-range FileResponse cap; local regression tests exercise these
boundaries.
PlatformIO has a separate hashed lock and `.pio-venv`, is never included in the host environment or
frozen runtime, and has telemetry disabled by setup/test scripts and CI. Normal runtime dependencies
are FastAPI, Uvicorn, Pydantic, pyserial, platformdirs, and websockets; browser assets are
repository-owned. Run KegPulse as an unprivileged OS user, keep the data directory private, do not
expose port 8765 through a router, and protect offline backups separately.

On POSIX systems KegPulse applies mode `0700` to its data/log/backup/export directories and `0600`
to database, backup, config, and current log files. On Windows, confidentiality relies on the
current user's directory ACLs; review inherited ACLs when using a custom data directory.

If a kiosk or data copy may be compromised, stop LAN exposure, preserve a forensic copy if needed,
change the PIN after returning to loopback, and review pour attribution, inventory adjustments,
calibrations, backups, and diagnostics. A PIN change invalidates live sessions but does not revoke
or encrypt previously copied databases or backups.
