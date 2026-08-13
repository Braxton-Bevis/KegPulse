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
  };
  TEST_ASSERT_TRUE(kegpulse::encode_frame(encoded, sizeof(encoded), 'R', "FFFFFFFF",
                                          "STATUS", status_fields, 10, &written));
  TEST_ASSERT_EQUAL_UINT32(234, written);
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
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::SETTLING, machine.snapshot().state);
  machine.add_pulses(2, 130, &produced);
  TEST_ASSERT_EQUAL(kegpulse::DeviceState::POURING, machine.snapshot().state);
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

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_crc_known_vector);
  RUN_TEST(test_protocol_round_trip);
  RUN_TEST(test_all_shared_protocol_vectors);
  RUN_TEST(test_protocol_rejects_invalid_request_ids_and_duplicate_fields);
  RUN_TEST(test_parser_recovers_after_oversize);
  RUN_TEST(test_counters_command_and_worst_case_response_round_trip);
  RUN_TEST(test_worst_case_status_and_compact_result_fit_frame_limit);
  RUN_TEST(test_attributed_session_boundary_and_completion);
  RUN_TEST(test_timeout_cancel_and_unattributed);
  RUN_TEST(test_cancel_after_flow_never_erases_pulses);
  RUN_TEST(test_pending_counter_saturation_interrupts_with_lower_bound);
  return UNITY_END();
}
