#pragma once

#include <stddef.h>
#include <stdint.h>

namespace kegpulse {

constexpr size_t kMaxFrameBytes = 256;
constexpr uint8_t kMaxFields = 12;

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
  char buffer_[kMaxFrameBytes + 1];
  size_t length_;
  bool discarding_;
};

}  // namespace kegpulse
