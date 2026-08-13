#include <string.h>

#include <unity.h>

#include "kegpulse/protocol.hpp"
#include "kegpulse/session_machine.hpp"
#include "protocol_vectors.hpp"

void setUp() {}
void tearDown() {}

void test_crc_known_vector() {
  const char* input = "123456789";
  TEST_ASSERT_EQUAL_HEX16(
      0x29B1,
      kegpulse::crc16_ccitt(reinterpret_cast<const uint8_t*>(input), strlen(input)));
}

void test_protocol_round_trip() {
  kegpulse::Field field{"nonce", "42"};
  char encoded[kegpulse::kMaxFrameBytes + 1]{};
  size_t written = 0;
  TEST_ASSERT_TRUE(kegpulse::encode_frame(encoded, sizeof(encoded), 'Q', "12AB34CD",
                                          "PING", &field, 1, &written));
  TEST_ASSERT_EQUAL_STRING("KP1|Q|12AB34CD|PING|nonce=42*CBA4\n", encoded);
  kegpulse::ParsedFrame decoded{};
  TEST_ASSERT_EQUAL(kegpulse::ParseError::NONE,
                    kegpulse::parse_frame(encoded, written, &decoded));
  TEST_ASSERT_EQUAL_CHAR('Q', decoded.kind);
  TEST_ASSERT_EQUAL_STRING("12AB34CD", decoded.request_id);
  TEST_ASSERT_EQUAL_STRING("PING", decoded.operation);
  TEST_ASSERT_EQUAL_STRING("42", decoded.get("nonce"));
}

void test_all_shared_protocol_vectors() {
  for (size_t index = 0; index < kegpulse::fixtures::k_vector_count; ++index) {
    const kegpulse::fixtures::GoldenVector& vector = kegpulse::fixtures::k_vectors[index];
    char encoded[kegpulse::kMaxFrameBytes + 1]{};
    size_t written = 0;
    TEST_ASSERT_TRUE_MESSAGE(
        kegpulse::encode_frame(encoded, sizeof(encoded), vector.kind,
                               vector.request_id, vector.operation, vector.fields,
                               vector.field_count, &written),
        vector.name);
    TEST_ASSERT_EQUAL_STRING_MESSAGE(vector.encoded, encoded, vector.name);
    kegpulse::ParsedFrame decoded{};
    TEST_ASSERT_EQUAL_MESSAGE(kegpulse::ParseError::NONE,
                              kegpulse::parse_frame(encoded, written, &decoded),
                              vector.name);
    TEST_ASSERT_EQUAL_CHAR(vector.kind, decoded.kind);
    TEST_ASSERT_EQUAL_STRING(vector.request_id, decoded.request_id);
    TEST_ASSERT_EQUAL_STRING(vector.operation, decoded.operation);
  }
}

void test_protocol_rejects_invalid_request_ids_and_duplicate_fields() {
  char encoded[kegpulse::kMaxFrameBytes + 1]{};
  size_t written = 0;
  kegpulse::Field duplicate[] = {{"nonce", "1"}, {"nonce", "2"}};
  TEST_ASSERT_FALSE(kegpulse::encode_frame(encoded, sizeof(encoded), 'Q',
                                           "lowerabc", "PING", duplicate, 1,
                                           &written));
  TEST_ASSERT_FALSE(kegpulse::encode_frame(encoded, sizeof(encoded), 'Q',
                                           "00000001", "PING", duplicate, 2,
                                           &written));
  TEST_ASSERT_FALSE(kegpulse::encode_frame(encoded, sizeof(encoded), 'Q',
                                           "00000001", "PING", nullptr, 1,
                                           &written));

  char lowercase_crc[] = "KP1|Q|12AB34CD|PING|nonce=42*Cba4\n";
  kegpulse::ParsedFrame decoded{};
  TEST_ASSERT_EQUAL(kegpulse::ParseError::MALFORMED,
                    kegpulse::parse_frame(lowercase_crc,
                                          strlen(lowercase_crc), &decoded));
}

void test_upper_hex_identity_and_ack_wire_binding() {
  TEST_ASSERT_TRUE(kegpulse::valid_upper_hex_identity("0123456789ABCDEF"));
  TEST_ASSERT_FALSE(kegpulse::valid_upper_hex_identity("0123456789abcdef"));
  TEST_ASSERT_FALSE(kegpulse::valid_upper_hex_identity("GGGGGGGGGGGGGGGG"));
  TEST_ASSERT_FALSE(kegpulse::valid_upper_hex_identity("0123456789ABCDE"));
  TEST_ASSERT_FALSE(kegpulse::valid_upper_hex_identity("0123456789ABCDEF0"));
  TEST_ASSERT_FALSE(kegpulse::valid_upper_hex_identity(nullptr));

  TEST_ASSERT_TRUE(kegpulse::ack_identity_matches(
      "0123456789ABCDEF", "1111111111111111", "0123456789ABCDEF",
      "1111111111111111"));
  TEST_ASSERT_TRUE(kegpulse::ack_identity_matches(
      nullptr, "1111111111111111", "0123456789ABCDEF",
      "1111111111111111"));
  TEST_ASSERT_FALSE(kegpulse::ack_identity_matches(
      "FEDCBA9876543210", "1111111111111111", "0123456789ABCDEF",
      "1111111111111111"));
  TEST_ASSERT_FALSE(kegpulse::ack_identity_matches(
      "0123456789ABCDEF", "2222222222222222", "0123456789ABCDEF",
      "1111111111111111"));
  TEST_ASSERT_FALSE(kegpulse::ack_identity_matches(
      nullptr, "2222222222222222", "0123456789ABCDEF",
      "1111111111111111"));
  TEST_ASSERT_FALSE(kegpulse::ack_identity_matches(
      nullptr, nullptr, "0123456789ABCDEF", "1111111111111111"));
  TEST_ASSERT_FALSE(kegpulse::ack_identity_matches(
      "0123456789abcdef", "1111111111111111", "0123456789ABCDEF",
      "1111111111111111"));
  TEST_ASSERT_FALSE(kegpulse::ack_identity_matches(
      "0123456789ABCDE", "1111111111111111", "0123456789ABCDEF",
      "1111111111111111"));

  kegpulse::Field fields[] = {
      {"dev", "0123456789ABCDEF"},
      {"boot", "1111111111111111"},
      {"seq", "4294967295"},
  };
  char encoded[kegpulse::kMaxFrameBytes + 1]{};
  size_t written = 0;
  TEST_ASSERT_TRUE(kegpulse::encode_frame(encoded, sizeof(encoded), 'Q',
                                          "A0C00001", "ACK", fields, 3,
                                          &written));
  kegpulse::ParsedFrame decoded{};
  TEST_ASSERT_EQUAL(kegpulse::ParseError::NONE,
                    kegpulse::parse_frame(encoded, written, &decoded));
  TEST_ASSERT_EQUAL_STRING("ACK", decoded.operation);
  TEST_ASSERT_EQUAL_STRING("0123456789ABCDEF", decoded.get("dev"));
  TEST_ASSERT_EQUAL_STRING("1111111111111111", decoded.get("boot"));
  TEST_ASSERT_EQUAL_STRING("4294967295", decoded.get("seq"));

  kegpulse::Field legacy_fields[] = {
      {"boot", "1111111111111111"},
      {"seq", "4294967295"},
  };
  char legacy_encoded[kegpulse::kMaxFrameBytes + 1]{};
  size_t legacy_written = 0;
  TEST_ASSERT_TRUE(kegpulse::encode_frame(
      legacy_encoded, sizeof(legacy_encoded), 'Q', "A0C00002", "ACK",
      legacy_fields, 2, &legacy_written));
  kegpulse::ParsedFrame legacy_decoded{};
  TEST_ASSERT_EQUAL(kegpulse::ParseError::NONE,
                    kegpulse::parse_frame(legacy_encoded, legacy_written,
                                          &legacy_decoded));
  TEST_ASSERT_NULL(legacy_decoded.get("dev"));
  TEST_ASSERT_EQUAL_STRING("1111111111111111", legacy_decoded.get("boot"));
  TEST_ASSERT_EQUAL_STRING("4294967295", legacy_decoded.get("seq"));
}

void test_parser_recovers_after_oversize() {
  kegpulse::FrameParser parser;
  kegpulse::ParsedFrame frame{};
  kegpulse::ParseError error = kegpulse::ParseError::NONE;
  bool yielded = false;
  for (size_t index = 0; index <= kegpulse::kMaxFrameBytes; ++index) {
    yielded = parser.push('x', &frame, &error);
    TEST_ASSERT_FALSE(yielded);
  }
  TEST_ASSERT_TRUE(parser.push('\n', &frame, &error));
  TEST_ASSERT_EQUAL(kegpulse::ParseError::TOO_LONG, error);
}

void test_counters_command_and_worst_case_response_round_trip() {
  char encoded[kegpulse::kMaxFrameBytes + 1]{};
  size_t written = 0;
  TEST_ASSERT_TRUE(kegpulse::encode_frame(encoded, sizeof(encoded), 'Q', "C0A17E45",
                                          "COUNTERS", nullptr, 0, &written));
  TEST_ASSERT_EQUAL_STRING("KP1|Q|C0A17E45|COUNTERS*8D34\n", encoded);

  kegpulse::ParsedFrame decoded{};
  TEST_ASSERT_EQUAL(kegpulse::ParseError::NONE,
                    kegpulse::parse_frame(encoded, written, &decoded));
  TEST_ASSERT_EQUAL_STRING("COUNTERS", decoded.operation);
  TEST_ASSERT_EQUAL_UINT8(0, decoded.field_count);

  kegpulse::Field fields[] = {
      {"accepted", "18446744073709551615"},
      {"rejected", "4294967295"},
      {"noise_gate_us", "4294967295"},
      {"recovery", "18446744073709551615"},
      {"fault", "lifetime_saturated"},
  };
  TEST_ASSERT_TRUE(kegpulse::encode_frame(encoded, sizeof(encoded), 'R', "C0A17E45",
                                          "COUNTERS", fields, 5, &written));
  TEST_ASSERT_EQUAL_UINT32(159, written);
  TEST_ASSERT_LESS_OR_EQUAL_UINT32(kegpulse::kMaxFrameBytes, written);
  TEST_ASSERT_EQUAL_CHAR('\n', encoded[written - 1]);
}

void test_worst_case_status_and_compact_result_fit_frame_limit() {
  char encoded[kegpulse::kMaxFrameBytes + 1]{};
  size_t written = 0;
  kegpulse::Field status_fields[] = {
      {"state", "interrupted"},
      {"boot", "FFFFFFFFFFFFFFFF"},
      {"seq", "4294967295"},
      {"sid", "ffffffffffffffffffffffffffffffff"},
      {"attributed", "1"},
      {"pulses", "9223372036854775807"},
      {"lifetime", "18446744073709551615"},
      {"uptime", "4294967295"},
      {"next", "4294967295"},
      {"retained", "4"},
      {"arm_left", "4294967295"},
  };
  TEST_ASSERT_TRUE(kegpulse::encode_frame(encoded, sizeof(encoded), 'R', "FFFFFFFF",
                                          "STATUS", status_fields, 11, &written));
  TEST_ASSERT_EQUAL_UINT32(254, written);
  TEST_ASSERT_LESS_OR_EQUAL_UINT32(kegpulse::kMaxFrameBytes, written);
  TEST_ASSERT_EQUAL_CHAR('\n', encoded[written - 1]);

  kegpulse::Field result_fields[] = {
      {"dev", "FFFFFFFFFFFFFFFF"},
      {"boot", "FFFFFFFFFFFFFFFF"},
      {"seq", "4294967295"},
      {"sid", "ffffffffffffffffffffffffffffffff"},
      {"attr", "1"},
      {"st", "interrupted"},
      {"pulses", "9223372036854775807"},
      {"life", "18446744073709551615"},
      {"start", "4294967295"},
      {"end", "4294967295"},
      {"fault", "lifetime_saturated"},
  };
  TEST_ASSERT_TRUE(kegpulse::encode_frame(encoded, sizeof(encoded), 'R', "00000000",
                                          "RESULT", result_fields, 11, &written));
  // Session/result pulses are capped at SQLite's signed 64-bit maximum. Lifetime
  // remains unsigned 64-bit, and the destination includes an extra byte for NUL.
  TEST_ASSERT_LESS_OR_EQUAL_UINT32(kegpulse::kMaxFrameBytes, written);
  TEST_ASSERT_EQUAL_CHAR('\n', encoded[written - 1]);

  kegpulse::ParsedFrame decoded{};
  TEST_ASSERT_EQUAL(kegpulse::ParseError::NONE,
                    kegpulse::parse_frame(encoded, written, &decoded));
  TEST_ASSERT_EQUAL_STRING("RESULT", decoded.operation);
  TEST_ASSERT_EQUAL_UINT8(11, decoded.field_count);
  TEST_ASSERT_EQUAL_STRING("18446744073709551615", decoded.get("life"));
  TEST_ASSERT_EQUAL_STRING("9223372036854775807", decoded.get("pulses"));
  TEST_ASSERT_EQUAL_STRING("lifetime_saturated", decoded.get("fault"));
}

void test_attributed_session_boundary_and_completion() {
  kegpulse::SessionMachine machine(100, 10, 20);
  bool duplicate = false;
  TEST_ASSERT_EQUAL(kegpulse::MachineError::NONE,
                    machine.arm("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 1, 0, 100,
                                &duplicate));
  bool produced = false;
  TEST_ASSERT_EQUAL(kegpulse::MachineError::NONE,
                    machine.add_pulses(10, 100, &produced));
  TEST_ASSERT_FALSE(produced);
  machine.tick(110, &produced);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::SETTLING, machine.snapshot(110).state);
  machine.add_pulses(2, 130, &produced);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::POURING, machine.snapshot(130).state);
  machine.tick(160, &produced);
  TEST_ASSERT_TRUE(produced);
  TEST_ASSERT_EQUAL_UINT64(12, machine.result_at(0)->pulses);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::COMPLETE, machine.result_at(0)->status);
}

void test_timeout_cancel_and_unattributed() {
  kegpulse::SessionMachine machine(100, 10, 20);
  bool duplicate = false, produced = false;
  machine.arm("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 1, 0, 100, &duplicate);
  machine.tick(100, &produced);
  TEST_ASSERT_TRUE(produced);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::TIMED_OUT, machine.result_at(0)->status);
  bool already = false;
  machine.acknowledge(1, &already);
  machine.add_pulses(5, 101, &produced);
  machine.tick(131, &produced);
  TEST_ASSERT_TRUE(produced);
  TEST_ASSERT_FALSE(machine.result_at(0)->attributed);
  TEST_ASSERT_EQUAL_UINT64(5, machine.result_at(0)->pulses);
}

void test_cancel_after_flow_never_erases_pulses() {
  kegpulse::SessionMachine machine(100, 10, 20);
  bool duplicate = false, produced = false;
  machine.arm("cccccccccccccccccccccccccccccccc", 1, 0, 100, &duplicate);
  machine.add_pulses(7, 1, &produced);
  machine.cancel("cccccccccccccccccccccccccccccccc", 1, 2, &duplicate,
                 &produced);
  TEST_ASSERT_TRUE(produced);
  TEST_ASSERT_EQUAL_UINT64(7, machine.result_at(0)->pulses);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::INTERRUPTED,
                    machine.result_at(0)->status);
}

void test_pending_counter_saturation_interrupts_with_lower_bound() {
  kegpulse::SessionMachine machine(100, 10, 20);
  bool duplicate = false, produced = false;
  machine.arm("dddddddddddddddddddddddddddddddd", 1, 0, 100, &duplicate);
  machine.add_pulses(9, 1, &produced);
  TEST_ASSERT_EQUAL(kegpulse::MachineError::SATURATED,
                    machine.mark_counter_saturated(2, &produced));
  TEST_ASSERT_TRUE(produced);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::INTERRUPTED,
                    machine.result_at(0)->status);
  TEST_ASSERT_EQUAL_STRING("counter_saturated", machine.result_at(0)->fault);
  TEST_ASSERT_EQUAL_UINT64(9, machine.result_at(0)->pulses);
}

void test_batch_first_edge_at_arm_deadline_preserves_attribution() {
  kegpulse::SessionMachine machine(100, 10, 20);
  bool duplicate = false, produced = false;
  machine.arm("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", 1, 0, 100, &duplicate);

  TEST_ASSERT_EQUAL(kegpulse::MachineError::NONE,
                    machine.add_pulse_batch(3, 100, 101, &produced));
  TEST_ASSERT_FALSE(produced);
  const kegpulse::Snapshot status = machine.snapshot(101);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::POURING, status.state);
  TEST_ASSERT_TRUE(status.attributed);
  TEST_ASSERT_EQUAL_UINT32(1, status.sequence);
  TEST_ASSERT_EQUAL_UINT64(3, status.session_pulses);
  TEST_ASSERT_EQUAL_UINT64(3, status.lifetime_pulses);
  TEST_ASSERT_EQUAL_UINT8(0, machine.result_count());
}

void test_batch_first_edge_at_settle_deadline_resumes_original_event() {
  kegpulse::SessionMachine machine(100, 10, 20);
  bool duplicate = false, produced = false;
  machine.arm("ffffffffffffffffffffffffffffffff", 1, 0, 100, &duplicate);
  machine.add_pulses(4, 1, &produced);
  machine.tick(11, &produced);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::SETTLING, machine.snapshot(11).state);

  TEST_ASSERT_EQUAL(kegpulse::MachineError::NONE,
                    machine.add_pulse_batch(3, 31, 32, &produced));
  TEST_ASSERT_FALSE(produced);
  const kegpulse::Snapshot status = machine.snapshot(32);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::POURING, status.state);
  TEST_ASSERT_TRUE(status.attributed);
  TEST_ASSERT_EQUAL_UINT32(1, status.sequence);
  TEST_ASSERT_EQUAL_UINT64(7, status.session_pulses);
  TEST_ASSERT_EQUAL_UINT8(0, machine.result_count());
}

void test_linearized_batch_precedes_tick_and_cancel_boundary() {
  kegpulse::SessionMachine deadline_machine(100, 10, 20);
  bool duplicate = false, produced = false;
  deadline_machine.arm("10101010101010101010101010101010", 1, 0, 100,
                       &duplicate);

  // This is the order used by main.cpp after its atomic pending-batch
  // snapshot: apply all earlier ISR evidence, then advance the deadline.
  deadline_machine.add_pulse_batch(1, 100, 100, &produced);
  deadline_machine.tick(100, &produced);
  TEST_ASSERT_FALSE(produced);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::POURING,
                    deadline_machine.snapshot(100).state);
  TEST_ASSERT_EQUAL_UINT64(1,
                           deadline_machine.snapshot(100).session_pulses);

  kegpulse::SessionMachine cancel_machine(100, 10, 20);
  cancel_machine.arm("20202020202020202020202020202020", 1, 0, 100,
                     &duplicate);
  cancel_machine.add_pulse_batch(2, 1, 1, &produced);
  cancel_machine.cancel("20202020202020202020202020202020", 1, 2,
                        &duplicate, &produced);
  TEST_ASSERT_TRUE(produced);
  TEST_ASSERT_EQUAL_UINT64(2, cancel_machine.result_at(0)->pulses);
  TEST_ASSERT_TRUE(cancel_machine.result_at(0)->attributed);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::INTERRUPTED,
                    cancel_machine.result_at(0)->status);

  kegpulse::SessionMachine after_cancel(100, 10, 20);
  after_cancel.arm("30303030303030303030303030303030", 1, 0, 100,
                   &duplicate);
  after_cancel.cancel("30303030303030303030303030303030", 1, 2,
                      &duplicate, &produced);
  after_cancel.add_pulse_batch(1, 2, 2, &produced);
  TEST_ASSERT_FALSE(after_cancel.snapshot(2).attributed);
  TEST_ASSERT_EQUAL_UINT32(2, after_cancel.snapshot(2).sequence);
}

void test_snapshot_reports_rollover_safe_authoritative_arm_remaining() {
  kegpulse::SessionMachine machine(100, 10, 20);
  bool duplicate = false;
  machine.arm("0123456789abcdef0123456789abcdef", 1, UINT32_MAX - 50U, 100,
              &duplicate);

  TEST_ASSERT_EQUAL_UINT32(100, machine.snapshot(UINT32_MAX - 50U).arm_remaining_ms);
  TEST_ASSERT_EQUAL_UINT32(1, machine.snapshot(48).arm_remaining_ms);
  TEST_ASSERT_EQUAL_UINT32(0, machine.snapshot(49).arm_remaining_ms);
}

void test_full_result_store_batch_preserves_all_pulses_in_recovery_counter() {
  kegpulse::SessionMachine machine(100, 10, 20);
  bool produced = false;
  for (uint32_t event = 0; event < kegpulse::kResultCapacity; ++event) {
    const uint32_t start = event * 31U;
    machine.add_pulses(1, start, &produced);
    machine.tick(start + 30U, &produced);
    TEST_ASSERT_TRUE(produced);
  }

  TEST_ASSERT_EQUAL(kegpulse::MachineError::BUSY,
                    machine.add_pulse_batch(3, 124, 125, &produced));
  const kegpulse::Snapshot status = machine.snapshot(125);
  TEST_ASSERT_EQUAL_UINT64(7, status.lifetime_pulses);
  TEST_ASSERT_EQUAL_UINT64(3, status.recovery_pulses);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_crc_known_vector);
  RUN_TEST(test_protocol_round_trip);
  RUN_TEST(test_all_shared_protocol_vectors);
  RUN_TEST(test_protocol_rejects_invalid_request_ids_and_duplicate_fields);
  RUN_TEST(test_upper_hex_identity_and_ack_wire_binding);
  RUN_TEST(test_parser_recovers_after_oversize);
  RUN_TEST(test_counters_command_and_worst_case_response_round_trip);
  RUN_TEST(test_worst_case_status_and_compact_result_fit_frame_limit);
  RUN_TEST(test_attributed_session_boundary_and_completion);
  RUN_TEST(test_timeout_cancel_and_unattributed);
  RUN_TEST(test_cancel_after_flow_never_erases_pulses);
  RUN_TEST(test_pending_counter_saturation_interrupts_with_lower_bound);
  RUN_TEST(test_batch_first_edge_at_arm_deadline_preserves_attribution);
  RUN_TEST(test_batch_first_edge_at_settle_deadline_resumes_original_event);
  RUN_TEST(test_linearized_batch_precedes_tick_and_cancel_boundary);
  RUN_TEST(test_snapshot_reports_rollover_safe_authoritative_arm_remaining);
  RUN_TEST(test_full_result_store_batch_preserves_all_pulses_in_recovery_counter);
  return UNITY_END();
}
