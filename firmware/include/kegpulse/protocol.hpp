#pragma once

#include <stddef.h>
#include <stdint.h>

namespace kegpulse {

constexpr size_t kMaxFrameBytes = 256;
// Every defined KP1 host command fits in 113 bytes. The Nano keeps a smaller
// inbound buffer while responses retain the full protocol frame allowance.
constexpr size_t kMaxInboundFrameBytes = 128;
constexpr uint8_t kMaxFields = 12;

constexpr bool upper_hex_digit(char value) {
  return (value >= '0' && value <= '9') ||
         (value >= 'A' && value <= 'F');
}

// Device and boot identities have one canonical wire representation. Keeping
// this constexpr lets board builds reject an invalid configured device ID.
constexpr bool valid_upper_hex_identity_at(const char* value, uint8_t index) {
  return index == 16
             ? value[index] == '\0'
             : upper_hex_digit(value[index]) &&
                   valid_upper_hex_identity_at(value,
                                               static_cast<uint8_t>(index + 1U));
}

constexpr bool valid_upper_hex_identity(const char* value) {
  return value != nullptr && valid_upper_hex_identity_at(value, 0);
}

enum class ParseError : uint8_t {
  NONE,
  MALFORMED,
  BAD_CRC,
  TOO_LONG,
  UNSUPPORTED_VERSION,
};

struct Field {
  const char* key;
  const char* value;
};

struct ParsedFrame {
  char kind;
  char request_id[9];
  char operation[17];
  Field fields[kMaxFields];
  uint8_t field_count;

  const char* get(const char* key) const;
};

uint16_t crc16_ccitt(const uint8_t* data, size_t length);
// KP1 proto 1 originally identified ACKs by boot and sequence. Device is
// optional only for that legacy wire form; whenever present it is a strict
// additional identity gate.
bool ack_identity_matches(const char* supplied_device,
                          const char* supplied_boot,
                          const char* expected_device,
                          const char* expected_boot);
bool encode_frame(char* destination, size_t capacity, char kind,
                  const char* request_id, const char* operation,
                  const Field* fields, uint8_t field_count,
                  size_t* written);
ParseError parse_frame(char* line, size_t length, ParsedFrame* output);

class FrameParser {
 public:
  FrameParser();
  bool push(char byte, ParsedFrame* output, ParseError* error);

 private:
  char buffer_[kMaxInboundFrameBytes + 1];
  size_t length_;
  bool discarding_;
};

}  // namespace kegpulse
