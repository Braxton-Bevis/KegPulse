#ifdef ARDUINO

#include <Arduino.h>
#include <EEPROM.h>
#include <util/atomic.h>

#include "kegpulse/protocol.hpp"
#include "kegpulse/session_machine.hpp"

#ifndef KEGPULSE_INTERNAL_PULLUP
#define KEGPULSE_INTERNAL_PULLUP 0
#endif

#ifndef KEGPULSE_NOISE_GATE_US
#define KEGPULSE_NOISE_GATE_US 0
#endif

#ifndef KEGPULSE_PULSE_EDGE
#define KEGPULSE_PULSE_EDGE FALLING
#endif

namespace {

constexpr uint8_t kPulsePin = 2;  // Nano D2 / INT0.
#ifndef KEGPULSE_FLOW_GAP_MS
#define KEGPULSE_FLOW_GAP_MS 750
#endif
#ifndef KEGPULSE_SETTLING_MS
#define KEGPULSE_SETTLING_MS 1500
#endif
#ifndef KEGPULSE_DEVICE_ID
#define KEGPULSE_DEVICE_ID "4B454750554C5345"
#endif

constexpr char kDeviceId[] = KEGPULSE_DEVICE_ID;
static_assert(sizeof(kDeviceId) == 17, "KEGPULSE_DEVICE_ID must be 16 ASCII characters");
static_assert(kegpulse::valid_upper_hex_identity(kDeviceId),
              "KEGPULSE_DEVICE_ID must be exactly 16 uppercase hex digits");
static_assert(sizeof(uint32_t) == 4, "KegPulse requires 32-bit uint32_t counters");
static_assert(sizeof(uint64_t) == 8, "KegPulse requires 64-bit uint64_t counters");
static_assert(kegpulse::kMaxFrameBytes == 256, "KP1 wire limit must remain 256 bytes");
static_assert(kegpulse::kMaxInboundFrameBytes == 128,
              "Nano request buffer must remain within the SRAM budget");
static_assert(kegpulse::kMaxFields == 12, "KP1 parser field bound must remain 12");
constexpr char kFirmwareVersion[] = "1.0.0";
constexpr uint16_t kBootMagic = 0x4B50;

struct BootRecord {
  uint16_t magic;
  uint32_t counter;
  uint16_t crc;
};

volatile uint32_t g_pending_pulses = 0;
volatile uint32_t g_first_pulse_ms = 0;
volatile uint32_t g_last_pulse_ms = 0;
volatile uint32_t g_last_accepted_us = 0;
volatile uint32_t g_rejected_pulses = 0;
volatile bool g_pending_saturated = false;

kegpulse::SessionMachine g_machine(15000, KEGPULSE_FLOW_GAP_MS, KEGPULSE_SETTLING_MS);
kegpulse::FrameParser g_parser;
uint32_t g_boot_counter = 1;
uint8_t g_reset_cause = 0;
uint32_t g_last_emitted_sequence = 0;

struct PendingPulseBatch {
  uint32_t count;
  uint32_t first_captured_ms;
  uint32_t last_captured_ms;
  uint32_t boundary_ms;
  bool saturated;
};

uint16_t boot_crc(const BootRecord& record) {
  return kegpulse::crc16_ccitt(
      reinterpret_cast<const uint8_t*>(&record.counter), sizeof(record.counter));
}

bool valid_boot_record(const BootRecord& record) {
  return record.magic == kBootMagic && record.counter > 0 &&
         record.crc == boot_crc(record);
}

void initialize_boot_identity() {
  BootRecord first{};
  BootRecord second{};
  EEPROM.get(0, first);
  EEPROM.get(static_cast<int>(sizeof(BootRecord)), second);
  uint32_t previous = 0;
  if (valid_boot_record(first)) {
    previous = first.counter;
  }
  if (valid_boot_record(second) && second.counter > previous) {
    previous = second.counter;
  }
  g_boot_counter = previous == UINT32_MAX ? UINT32_MAX : previous + 1;
  BootRecord next{kBootMagic, g_boot_counter, 0};
  next.crc = boot_crc(next);
  const int destination = (g_boot_counter & 1U) == 0 ? 0 : sizeof(BootRecord);
  EEPROM.put(destination, next);
}

void pulse_isr() {
  const uint32_t now_us = micros();
#if KEGPULSE_NOISE_GATE_US > 0
  if (g_last_accepted_us != 0 &&
      static_cast<uint32_t>(now_us - g_last_accepted_us) <
          static_cast<uint32_t>(KEGPULSE_NOISE_GATE_US)) {
    if (g_rejected_pulses != UINT32_MAX) {
      ++g_rejected_pulses;
    }
    return;
  }
#endif
  g_last_accepted_us = now_us;
  // Keep session timestamps in the millis() rollover domain used by tick().
  const uint32_t captured_ms = millis();
  if (g_pending_pulses != UINT32_MAX) {
    if (g_pending_pulses == 0) {
      g_first_pulse_ms = captured_ms;
    }
    ++g_pending_pulses;
  } else {
    g_pending_saturated = true;
  }
  g_last_pulse_ms = captured_ms;
}

void u64_to_ascii(uint64_t value, char output[21]) {
  char* cursor = output + 20;
  *cursor = '\0';
  do {
    *--cursor = static_cast<char>('0' + value % 10U);
    value /= 10U;
  } while (value > 0);

  char* destination = output;
  while ((*destination++ = *cursor++) != '\0') {
  }
}

void u32_to_ascii(uint32_t value, char output[11]) { ultoa(value, output, 10); }

void hex64_to_ascii(uint64_t value, char output[17]) {
  static constexpr char digits[] = "0123456789ABCDEF";
  for (int8_t index = 15; index >= 0; --index) {
    output[index] = digits[value & 0xFU];
    value >>= 4U;
  }
  output[16] = '\0';
}

bool parse_u32(const char* text, uint32_t* value) {
  if (text == nullptr || *text == '\0') {
    return false;
  }
  uint32_t parsed = 0;
  for (const char* cursor = text; *cursor != '\0'; ++cursor) {
    if (*cursor < '0' || *cursor > '9') {
      return false;
    }
    const uint8_t digit = static_cast<uint8_t>(*cursor - '0');
    if (parsed > (UINT32_MAX - digit) / 10U) {
      return false;
    }
    parsed = parsed * 10U + digit;
  }
  *value = parsed;
  return true;
}

bool boot_matches(const char* text) {
  char expected[17];
  hex64_to_ascii(g_boot_counter, expected);
  return kegpulse::valid_upper_hex_identity(text) && strcmp(text, expected) == 0;
}

void write_checked_byte(char value, uint16_t* crc) {
  *crc ^= static_cast<uint16_t>(static_cast<uint8_t>(value)) << 8U;
  for (uint8_t bit = 0; bit < 8; ++bit) {
    *crc = (*crc & 0x8000U) != 0
               ? static_cast<uint16_t>((*crc << 1U) ^ 0x1021U)
               : static_cast<uint16_t>(*crc << 1U);
  }
  Serial.write(static_cast<uint8_t>(value));
}

void write_checked_text(const char* text, uint16_t* crc) {
  while (*text != '\0') {
    write_checked_byte(*text++, crc);
  }
}

void send_fields(char kind, const char* request_id, const char* operation,
                 const kegpulse::Field* fields, uint8_t count) {
  size_t length = 4U + 1U + 1U + strlen(request_id) + 1U + strlen(operation) + 6U;
  for (uint8_t index = 0; index < count; ++index) {
    length += 2U + strlen(fields[index].key) + strlen(fields[index].value);
  }
  if (length > kegpulse::kMaxFrameBytes) {
    if (strcmp(operation, "ERROR") != 0) {
      kegpulse::Field error_fields[] = {{"code", "INTERNAL"}, {"op", operation}};
      send_fields('E', request_id, "ERROR", error_fields, 2);
    }
    return;
  }
  uint16_t crc = 0xFFFFU;
  write_checked_text("KP1|", &crc);
  write_checked_byte(kind, &crc);
  write_checked_byte('|', &crc);
  write_checked_text(request_id, &crc);
  write_checked_byte('|', &crc);
  write_checked_text(operation, &crc);
  for (uint8_t index = 0; index < count; ++index) {
    write_checked_byte('|', &crc);
    write_checked_text(fields[index].key, &crc);
    write_checked_byte('=', &crc);
    write_checked_text(fields[index].value, &crc);
  }
  static constexpr char digits[] = "0123456789ABCDEF";
  Serial.write('*');
  Serial.write(digits[(crc >> 12U) & 0x0FU]);
  Serial.write(digits[(crc >> 8U) & 0x0FU]);
  Serial.write(digits[(crc >> 4U) & 0x0FU]);
  Serial.write(digits[crc & 0x0FU]);
  Serial.write('\n');
}

void set_field(kegpulse::Field* field, const char* key, const char* value) {
  field->key = key;
  field->value = value;
}

const char* machine_error_code(kegpulse::MachineError error) {
  switch (error) {
    case kegpulse::MachineError::BUSY:
      return "BUSY";
    case kegpulse::MachineError::STALE:
      return "STALE";
    case kegpulse::MachineError::INVALID_STATE:
      return "INVALID_STATE";
    case kegpulse::MachineError::RANGE:
      return "RANGE";
    case kegpulse::MachineError::SATURATED:
      return "INTERNAL";
    case kegpulse::MachineError::NONE:
      return "INTERNAL";
  }
  return "INTERNAL";
}

void send_error(const char* request_id, const char* operation,
                const char* code) {
  kegpulse::Field fields[2]{};
  set_field(&fields[0], "code", code);
  set_field(&fields[1], "op", operation);
  send_fields('E', request_id, "ERROR", fields, 2);
}

void emit_result(const kegpulse::Result& result) {
  kegpulse::Field fields[11]{};
  char boot[17], sequence[11], pulses[21], lifetime[21], started[11], ended[11];
  hex64_to_ascii(g_boot_counter, boot);
  u32_to_ascii(result.sequence, sequence);
  u64_to_ascii(result.pulses, pulses);
  u64_to_ascii(result.lifetime, lifetime);
  u32_to_ascii(result.started_ms, started);
  u32_to_ascii(result.ended_ms, ended);
  set_field(&fields[0], "boot", boot);
  set_field(&fields[1], "seq", sequence);
  set_field(&fields[2], "sid", result.attributed ? result.session_id : "none");
  set_field(&fields[3], "attr", result.attributed ? "1" : "0");
  set_field(&fields[4], "st", kegpulse::state_name(result.status));
  set_field(&fields[5], "pulses", pulses);
  set_field(&fields[6], "life", lifetime);
  set_field(&fields[7], "start", started);
  set_field(&fields[8], "end", ended);
  set_field(&fields[9], "fault", result.fault);
  set_field(&fields[10], "dev", kDeviceId);
  send_fields('R', "00000000", "RESULT", fields, 11);
  g_last_emitted_sequence = result.sequence;
}

void emit_new_results() {
  for (uint8_t index = 0; index < g_machine.result_count(); ++index) {
    const kegpulse::Result* result = g_machine.result_at(index);
    if (result != nullptr && result->sequence > g_last_emitted_sequence) {
      emit_result(*result);
    }
  }
}

__attribute__((noinline)) void send_status(const char* request_id,
                                           uint32_t now_ms) {
  const kegpulse::Snapshot status = g_machine.snapshot(now_ms);
  // Recovery/fault diagnostics live in COUNTERS so a worst-case STATUS frame
  // (active 32-byte SID plus maximum-width counters) stays below 256 bytes.
  kegpulse::Field fields[11]{};
  char boot[17], sequence[11], pulses[21], lifetime[21], uptime[11], next[11],
      retained[4], arm_left[11];
  hex64_to_ascii(g_boot_counter, boot);
  u32_to_ascii(status.sequence, sequence);
  u64_to_ascii(status.session_pulses, pulses);
  u64_to_ascii(status.lifetime_pulses, lifetime);
  u32_to_ascii(now_ms, uptime);
  u32_to_ascii(status.next_sequence, next);
  u32_to_ascii(status.retained_results, retained);
  u32_to_ascii(status.arm_remaining_ms, arm_left);
  set_field(&fields[0], "state", kegpulse::state_name(status.state));
  set_field(&fields[1], "boot", boot);
  set_field(&fields[2], "seq", status.sequence == 0 ? "none" : sequence);
  set_field(&fields[3], "sid",
            status.attributed && status.session_id[0] != '\0' ? status.session_id
                                                               : "none");
  set_field(&fields[4], "attributed", status.attributed ? "1" : "0");
  set_field(&fields[5], "pulses", pulses);
  set_field(&fields[6], "lifetime", lifetime);
  set_field(&fields[7], "uptime", uptime);
  set_field(&fields[8], "next", next);
  set_field(&fields[9], "retained", retained);
  set_field(&fields[10], "arm_left", arm_left);
  send_fields('R', request_id, "STATUS", fields, 11);
}

__attribute__((noinline)) void send_counters(const char* request_id,
                                             uint32_t boundary_ms) {
  uint32_t rejected = 0;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { rejected = g_rejected_pulses; }
  const kegpulse::Snapshot status = g_machine.snapshot(boundary_ms);
  kegpulse::Field fields[5]{};
  char accepted[21], rejected_text[11], gate[11], recovery[21];
  u64_to_ascii(status.lifetime_pulses, accepted);
  u32_to_ascii(rejected, rejected_text);
  u32_to_ascii(static_cast<uint32_t>(KEGPULSE_NOISE_GATE_US), gate);
  u64_to_ascii(status.recovery_pulses, recovery);
  set_field(&fields[0], "accepted", accepted);
  set_field(&fields[1], "rejected", rejected_text);
  set_field(&fields[2], "noise_gate_us", gate);
  set_field(&fields[3], "recovery", recovery);
  set_field(&fields[4], "fault", status.fault);
  send_fields('R', request_id, "COUNTERS", fields, 5);
}

__attribute__((noinline)) void handle_request(
    const kegpulse::ParsedFrame& frame, uint32_t boundary_ms) {
  if (frame.kind != 'Q') {
    send_error(frame.request_id, frame.operation, "MALFORMED");
    return;
  }
  if (strcmp(frame.operation, "HELLO") == 0) {
    uint32_t minimum = 0, maximum = 0;
    if (!parse_u32(frame.get("min"), &minimum) ||
        !parse_u32(frame.get("max"), &maximum)) {
      send_error(frame.request_id, frame.operation, "MALFORMED");
      return;
    }
    if (minimum > 1 || maximum < 1) {
      send_error(frame.request_id, frame.operation, "UNSUPPORTED_VERSION");
      return;
    }
    kegpulse::Field fields[6]{};
    char boot[17], reset[8];
    hex64_to_ascii(g_boot_counter, boot);
    u32_to_ascii(g_reset_cause, reset);
    set_field(&fields[0], "proto", "1");
    set_field(&fields[1], "fw", kFirmwareVersion);
    set_field(&fields[2], "device", kDeviceId);
    set_field(&fields[3], "boot", boot);
    set_field(&fields[4], "reset", reset);
    set_field(&fields[5], "caps", "status.results.counters");
    send_fields('R', frame.request_id, "HELLO", fields, 6);
    return;
  }
  if (strcmp(frame.operation, "PING") == 0) {
    const char* nonce = frame.get("nonce");
    if (nonce == nullptr) {
      send_error(frame.request_id, frame.operation, "MALFORMED");
      return;
    }
    kegpulse::Field field{};
    set_field(&field, "nonce", nonce);
    send_fields('R', frame.request_id, "PING", &field, 1);
    return;
  }
  if (strcmp(frame.operation, "RESULTS") == 0) {
    for (uint8_t index = 0; index < g_machine.result_count(); ++index) {
      const kegpulse::Result* result = g_machine.result_at(index);
      if (result != nullptr) {
        emit_result(*result);
      }
    }
    kegpulse::Field field{};
    char count[4];
    u32_to_ascii(g_machine.result_count(), count);
    set_field(&field, "count", count);
    send_fields('R', frame.request_id, "RESULTS_END", &field, 1);
    return;
  }
  if (strcmp(frame.operation, "ARM") == 0) {
    uint32_t sequence = 0, ttl = 0;
    const char* sid = frame.get("sid");
    if (!boot_matches(frame.get("boot"))) {
      send_error(frame.request_id, frame.operation, "STALE");
      return;
    }
    if (!parse_u32(frame.get("seq"), &sequence) ||
        !parse_u32(frame.get("ttl"), &ttl) || sid == nullptr) {
      send_error(frame.request_id, frame.operation, "RANGE");
      return;
    }
    bool duplicate = false;
    const kegpulse::MachineError error =
        g_machine.arm(sid, sequence, boundary_ms, ttl, &duplicate);
    if (error != kegpulse::MachineError::NONE) {
      send_error(frame.request_id, frame.operation, machine_error_code(error));
      return;
    }
    kegpulse::Field fields[2]{};
    set_field(&fields[0], "state", "armed");
    set_field(&fields[1], "already", duplicate ? "1" : "0");
    send_fields('R', frame.request_id, "ARM", fields, 2);
    return;
  }
  if (strcmp(frame.operation, "CANCEL") == 0) {
    uint32_t sequence = 0;
    const char* sid = frame.get("sid");
    if (!boot_matches(frame.get("boot"))) {
      send_error(frame.request_id, frame.operation, "STALE");
      return;
    }
    if (!parse_u32(frame.get("seq"), &sequence) || sid == nullptr) {
      send_error(frame.request_id, frame.operation, "RANGE");
      return;
    }
    bool duplicate = false, produced = false;
    const kegpulse::MachineError error =
        g_machine.cancel(sid, sequence, boundary_ms, &duplicate, &produced);
    if (error != kegpulse::MachineError::NONE) {
      send_error(frame.request_id, frame.operation, machine_error_code(error));
      return;
    }
    emit_new_results();
    kegpulse::Field field{};
    set_field(&field, "already", duplicate ? "1" : "0");
    send_fields('R', frame.request_id, "CANCEL", &field, 1);
    return;
  }
  if (strcmp(frame.operation, "ACK") == 0) {
    uint32_t sequence = 0;
    char expected_boot[17];
    hex64_to_ascii(g_boot_counter, expected_boot);
    if (!kegpulse::ack_identity_matches(frame.get("dev"), frame.get("boot"),
                                        kDeviceId, expected_boot)) {
      send_error(frame.request_id, frame.operation, "STALE");
      return;
    }
    if (!parse_u32(frame.get("seq"), &sequence)) {
      send_error(frame.request_id, frame.operation, "RANGE");
      return;
    }
    bool already = false;
    g_machine.acknowledge(sequence, &already);
    kegpulse::Field field{};
    set_field(&field, "already", already ? "1" : "0");
    send_fields('R', frame.request_id, "ACK", &field, 1);
    return;
  }
  send_error(frame.request_id, frame.operation, "UNSUPPORTED");
}

PendingPulseBatch capture_pending_batch() {
  PendingPulseBatch batch{};
  // This bounded snapshot is the command/tick linearization point. ISR edges
  // completed before it are in this batch; an edge arriving after interrupts
  // are restored is intentionally ordered after the following command/tick.
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    batch.boundary_ms = millis();
    batch.count = g_pending_pulses;
    batch.first_captured_ms = g_first_pulse_ms;
    batch.last_captured_ms = g_last_pulse_ms;
    batch.saturated = g_pending_saturated;
    g_pending_pulses = 0;
    g_pending_saturated = false;
  }
  return batch;
}

void apply_pending_batch(const PendingPulseBatch& batch) {
  if (batch.count > 0) {
    bool produced = false;
    g_machine.add_pulse_batch(batch.count, batch.first_captured_ms,
                              batch.last_captured_ms, &produced);
    if (batch.saturated) {
      g_machine.mark_counter_saturated(batch.last_captured_ms, &produced);
    }
    emit_new_results();
  }
}

void drain_pulses() { apply_pending_batch(capture_pending_batch()); }

void advance_machine(uint32_t now_ms) {
  bool produced = false;
  g_machine.tick(now_ms, &produced);
  if (produced) {
    emit_new_results();
  }
}

__attribute__((noinline)) void handle_request_linearized(
    const kegpulse::ParsedFrame& frame) {
  const PendingPulseBatch batch = capture_pending_batch();
  apply_pending_batch(batch);
  if (frame.kind == 'Q' && strcmp(frame.operation, "STATUS") == 0) {
    advance_machine(batch.boundary_ms);
    send_status(frame.request_id, batch.boundary_ms);
    return;
  }
  if (frame.kind == 'Q' && strcmp(frame.operation, "COUNTERS") == 0) {
    send_counters(frame.request_id, batch.boundary_ms);
    return;
  }
  handle_request(frame, batch.boundary_ms);
}

void tick_linearized() {
  const PendingPulseBatch batch = capture_pending_batch();
  apply_pending_batch(batch);
  advance_machine(batch.boundary_ms);
}

}  // namespace

void setup() {
  g_reset_cause = MCUSR;
  MCUSR = 0;
  initialize_boot_identity();
  Serial.begin(115200);
#if KEGPULSE_INTERNAL_PULLUP
  pinMode(kPulsePin, INPUT_PULLUP);
#else
  pinMode(kPulsePin, INPUT);
#endif
  attachInterrupt(digitalPinToInterrupt(kPulsePin), pulse_isr, KEGPULSE_PULSE_EDGE);
}

void loop() {
  drain_pulses();
  while (Serial.available() > 0) {
    // Linearization rule: all pulses captured before a command are drained first.
    drain_pulses();
    kegpulse::ParsedFrame frame{};
    kegpulse::ParseError error = kegpulse::ParseError::NONE;
    if (g_parser.push(static_cast<char>(Serial.read()), &frame, &error)) {
      if (error == kegpulse::ParseError::NONE) {
        handle_request_linearized(frame);
      } else {
        const char* code = error == kegpulse::ParseError::BAD_CRC
                               ? "BAD_CRC"
                               : error == kegpulse::ParseError::TOO_LONG
                                     ? "TOO_LONG"
                                     : error == kegpulse::ParseError::UNSUPPORTED_VERSION
                                           ? "UNSUPPORTED_VERSION"
                                           : "MALFORMED";
        send_error("00000000", "PARSE", code);
      }
    }
  }
  tick_linearized();
}

#endif  // ARDUINO
