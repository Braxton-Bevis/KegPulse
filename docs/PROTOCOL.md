# KegPulse KP1 serial protocol

KP1 is a bounded ASCII protocol for a Nano-compatible ATmega328P and a single host. Serial settings are 115200 baud, 8 data bits, no parity, one stop bit.

## Frame grammar

```abnf
frame = prefix "|" kind "|" request-id "|" operation fields "*" crc LF
prefix = "KP1"
kind = "Q" / "R" / "E"
request-id = 8HEXDIG ; uppercase; events use 00000000
operation = 1*16(ALPHA / DIGIT / "_") ; uppercase
fields = *("|" key "=" value)
key = 1*16(lalpha / DIGIT / "_")
value = 1*64(ALPHA / DIGIT / "." / "_" / ":" / "-")
crc = 4HEXDIG ; uppercase
LF = %x0A ; an optional CR immediately before LF is accepted
```

The maximum encoded device response is 256 bytes including LF. Nano-bound host requests are limited
to 128 bytes including LF; the longest defined request is a maximum-width `ARM` at 113 bytes. CRC is
CRC-16/CCITT-FALSE: polynomial `0x1021`, initial value `0xFFFF`, no reflection, no final XOR. It
covers bytes beginning with `K` and ending immediately before `*`.

`Q` is a host request, `R` a response or device result, and `E` an error. Keys may appear only once. Integers are unsigned base-10 unless a field says otherwise. Device and boot IDs are 16 uppercase hex digits; session IDs are UUIDs without hyphens (32 lowercase hex digits).

Example (the checksum shown in code/tests is authoritative):

```text
KP1|Q|12AB34CD|PING|nonce=42*CBA4\n
```

## Commands and responses

| Operation | Required request fields | Successful response/result fields |
|---|---|---|
| `HELLO` | `min`, `max` | `proto`, `fw`, `device`, `boot`, `reset`, `caps` |
| `STATUS` | none | `state`, `boot`, `seq`, `sid`, `attributed`, `pulses`, `lifetime`, `uptime`, `next`, `retained`, `arm_left` |
| `COUNTERS` | none | `accepted`, `rejected`, `noise_gate_us`, `recovery`, `fault` |
| `ARM` | `boot`, `seq`, `sid`, `ttl` | `state`, `already` |
| `CANCEL` | `boot`, `seq`, `sid` | `already` |
| `ACK` | `boot`, `seq` | `already` |
| `RESULTS` | none | all retained `RESULT` frames followed by `RESULTS_END(count=...)` |
| `PING` | `nonce` | identical `nonce` |

Terminal device events use request ID `00000000`, operation `RESULT`, and fields:

```text
dev boot seq sid attr st pulses life start end fault
```

An unattributed result uses `sid=none` and `attr=0`; an attributed result uses a 32-character lowercase hexadecimal `sid` and `attr=1`. `st` is `complete`, `timed_out`, or `interrupted`. `pulses` is capped at `2^63-1` so it is exactly representable in SQLite; `life` is an unsigned 64-bit lifetime counter. `start` and `end` are boot-relative unsigned 32-bit milliseconds and duration uses rollover-safe subtraction. A timeout result has zero pulses and never affects inventory. Firmware retains terminal results until a matching ACK.

`ACK.dev` is optional solely for compatibility with the original KP1 proto-1
`boot`+`seq` form. The current host always supplies the device ID learned from its active `HELLO`
handshake. Firmware validates any supplied `dev` as exactly 16 uppercase hexadecimal digits and
requires it to match the configured controller; an invalid or mismatched present value is `STALE`.
The `boot` and `seq` fields remain required for both forms.

`STATUS.arm_left` is the authoritative unsigned milliseconds remaining before an armed session
times out. It is rollover-safe and is `0` outside `ARMED` (and at the exact deadline). The compact
name keeps the maximum STATUS response at 254 bytes including LF.

## Errors

An error frame has operation `ERROR`, required fields `code` and `op`, and an optional bounded `detail` token.

| Code | Meaning |
|---|---|
| `MALFORMED` | Invalid framing, token, duplicate key, or field shape |
| `BAD_CRC` | CRC did not match |
| `TOO_LONG` | An encoded Nano-bound request exceeds 128 bytes including LF, or another KP1 frame exceeds 256 bytes |
| `UNSUPPORTED_VERSION` | Protocol range excludes KP1 |
| `UNSUPPORTED` | Unknown command or unsupported optional behavior |
| `BUSY` | Another active session or retained-result capacity prevents the command |
| `STALE` | Boot, event sequence, or session identity is old/mismatched |
| `INVALID_STATE` | Command is not valid in the current state |
| `RANGE` | A numeric or length bound is invalid |
| `INTERNAL` | A bounded internal failure was surfaced |

## Parsing and replay rules

- Bytes are accumulated until LF. Multiple frames in a read are split by LF; partial frames wait for more bytes.
- When the applicable maximum is exceeded, bytes are discarded through the next LF, one `TOO_LONG`
  condition is emitted, and parsing resumes.
- Non-ASCII/control bytes, unknown versions, malformed CRCs, duplicate keys, missing fields, and overflowing integers are rejected deterministically.
- Timing gaps do not delimit frames. A corrupted device response is ignored by the host and recovered with `STATUS`/`RESULTS`.
- Request IDs correlate a response. Repeating the last identical state-changing request returns its semantic result with `already=1`.
- `ARM` requires the current boot and `seq == next`. Reusing `(boot, seq, sid)` is idempotent; changing the SID for an allocated sequence is `STALE`.
- Duplicate `CANCEL` and matching `ACK` requests never affect a newer event. Current-host ACKs are
  gated by device, boot, and sequence; legacy proto-1 ACKs without `dev` are gated by boot and sequence.
- The ISR retains the first and last timestamps of each bounded drained pulse batch. The first edge
  is applied separately, so an edge captured at/before an arm or settle deadline cannot be moved
  across that deadline merely because later edges arrived before the main loop drained the batch.
- Immediately before a completed command changes machine state, and immediately before a timer
  tick, firmware takes one bounded atomic snapshot of the pending ISR batch. Edges already captured
  in that snapshot are applied first. Interrupts are then restored; later edges are deliberately
  ordered after that command/tick and remain pending for the next boundary.

## Device state model

| Current | Input | Next | Result |
|---|---|---|---|
| `IDLE` | valid `ARM` | `ARMED` | none |
| `IDLE` | pulse | `POURING` | starts unattributed event |
| `ARMED` | first pulse at/before deadline | `POURING` | starts attributed measurement |
| `ARMED` | deadline | `TIMED_OUT` then `IDLE` | retained zero-pulse result |
| `ARMED` | cancel | `IDLE` | no pour result |
| `POURING` | gap elapsed | `SETTLING` | none |
| `SETTLING` | pulse at/before deadline | `POURING` | resumes same event |
| `SETTLING` | deadline | `COMPLETE` then `IDLE` | retained result |
| `POURING`/`SETTLING` | cancel | `INTERRUPTED` then `IDLE` | retained partial result |

`COMPLETE`, `TIMED_OUT`, and `INTERRUPTED` describe retained outcomes; operational readiness may return to `IDLE` while those outcomes await ACK. Exactly one attributed session may be active. The lifetime count increases for every accepted pulse independent of state.

## Reconnect and reset

On reconnect the host performs `HELLO`, then `STATUS`, then `RESULTS`. It commits every unseen result before sending an ACK with that result's exact device, boot, and sequence identity. If a periodic STATUS or RESULT reveals an in-place boot change, the host rejects that frame for the stale handshake and forces a new `HELLO` before accepting new-boot results. On the same boot, an unexplained lifetime increase is preserved as an unattributed recovered event marked `needs_review`. A new boot identity prevents cross-reset delta inference; any provisional host session is closed as interrupted/uncertain using only already confirmed pulses.

Firmware keeps a fixed four-result store. If it fills, new attributed `ARM` requests return `BUSY`; accepted pulses still increment lifetime and are surfaced through the `COUNTERS.recovery` count. Device faults are also reported by `COUNTERS.fault`. Event sequence exhaustion is a fault and never wraps.

## Shared fixtures and compatibility

Canonical encoded vectors live in `tests/fixtures/protocol/frames.json`. Python tests and PlatformIO native tests validate the same CRC, grammar, and state semantics. Firmware and host accept protocol `1` only for v1. A future incompatible grammar must use a new prefix/version and explicit negotiation.
