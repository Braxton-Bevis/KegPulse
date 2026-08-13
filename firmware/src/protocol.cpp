#include "kegpulse/protocol.hpp"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

namespace kegpulse {

uint16_t crc16_ccitt(const uint8_t* data, size_t length) {
  uint16_t crc = 0xFFFF;
  for (size_t index = 0; index < length; ++index) {
    const uint16_t input = static_cast<uint16_t>(
        static_cast<uint16_t>(data[index]) << 8U);
    crc = static_cast<uint16_t>(crc ^ input);
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x8000U) != 0
                ? static_cast<uint16_t>((crc << 1) ^ 0x1021U)
                : static_cast<uint16_t>(crc << 1);
    }
  }
  return crc;
}

bool ack_identity_matches(const char* supplied_device,
                          const char* supplied_boot,
                          const char* expected_device,
                          const char* expected_boot) {
  if (!valid_upper_hex_identity(supplied_boot) ||
      !valid_upper_hex_identity(expected_device) ||
      !valid_upper_hex_identity(expected_boot) ||
      strcmp(supplied_boot, expected_boot) != 0) {
    return false;
  }
  return supplied_device == nullptr ||
         (valid_upper_hex_identity(supplied_device) &&
          strcmp(supplied_device, expected_device) == 0);
}

static bool valid_identifier(const char* text, size_t minimum, size_t maximum,
                             bool uppercase) {
  if (text == nullptr) {
    return false;
  }
  const size_t length = strlen(text);
  if (length < minimum || length > maximum) {
    return false;
  }
  for (size_t index = 0; index < length; ++index) {
    const unsigned char value = static_cast<unsigned char>(text[index]);
    if (!(isdigit(value) || value == '_' ||
          (uppercase ? isupper(value) : islower(value)))) {
      return false;
    }
  }
  return true;
}

static bool valid_value(const char* text) {
  if (text == nullptr) {
    return false;
  }
  const size_t length = strlen(text);
  if (length < 1 || length > 64) {
    return false;
  }
  for (size_t index = 0; index < length; ++index) {
    const unsigned char value = static_cast<unsigned char>(text[index]);
    if (!(isalnum(value) || value == '.' || value == '_' || value == ':' ||
          value == '-')) {
      return false;
    }
  }
  return true;
}

const char* ParsedFrame::get(const char* key) const {
  for (uint8_t index = 0; index < field_count; ++index) {
    if (strcmp(fields[index].key, key) == 0) {
      return fields[index].value;
    }
  }
  return nullptr;
}

bool encode_frame(char* destination, size_t capacity, char kind,
                  const char* request_id, const char* operation,
                  const Field* fields, uint8_t field_count, size_t* written) {
  if (destination == nullptr || written == nullptr || capacity < 1 ||
      (kind != 'Q' && kind != 'R' && kind != 'E') ||
      request_id == nullptr || strlen(request_id) != 8 ||
      !valid_identifier(operation, 1, 16, true) || field_count > kMaxFields ||
      (field_count > 0 && fields == nullptr)) {
    return false;
  }
  for (uint8_t index = 0; index < 8; ++index) {
    if (!isxdigit(static_cast<unsigned char>(request_id[index])) ||
        islower(static_cast<unsigned char>(request_id[index]))) {
      return false;
    }
  }
  size_t used = static_cast<size_t>(snprintf(destination, capacity, "KP1|%c|%s|%s",
                                             kind, request_id, operation));
  if (used >= capacity) {
    return false;
  }
  for (uint8_t index = 0; index < field_count; ++index) {
    if (!valid_identifier(fields[index].key, 1, 16, false) ||
        !valid_value(fields[index].value)) {
      return false;
    }
    for (uint8_t prior = 0; prior < index; ++prior) {
      if (strcmp(fields[prior].key, fields[index].key) == 0) {
        return false;
      }
    }
    const int count = snprintf(destination + used, capacity - used, "|%s=%s",
                               fields[index].key, fields[index].value);
    if (count < 0 || static_cast<size_t>(count) >= capacity - used) {
      return false;
    }
    used += static_cast<size_t>(count);
  }
  const uint16_t crc =
      crc16_ccitt(reinterpret_cast<const uint8_t*>(destination), used);
  const int suffix = snprintf(destination + used, capacity - used, "*%04X\n", crc);
  if (suffix != 6 || used + static_cast<size_t>(suffix) > kMaxFrameBytes ||
      used + static_cast<size_t>(suffix) >= capacity) {
    return false;
  }
  *written = used + static_cast<size_t>(suffix);
  return true;
}

ParseError parse_frame(char* line, size_t length, ParsedFrame* output) {
  if (line == nullptr || output == nullptr || length > kMaxFrameBytes) {
    return ParseError::TOO_LONG;
  }
  if (length == 0 || line[length - 1] != '\n') {
    return ParseError::MALFORMED;
  }
  line[--length] = '\0';
  if (length > 0 && line[length - 1] == '\r') {
    line[--length] = '\0';
  }
  char* separator = strrchr(line, '*');
  if (separator == nullptr || strlen(separator + 1) != 4) {
    return ParseError::MALFORMED;
  }
  for (uint8_t index = 0; index < 4; ++index) {
    const unsigned char value = static_cast<unsigned char>(separator[1 + index]);
    if (!isxdigit(value) || islower(value)) {
      return ParseError::MALFORMED;
    }
  }
  char* crc_end = nullptr;
  const unsigned long parsed_crc = strtoul(separator + 1, &crc_end, 16);
  if (crc_end == nullptr || *crc_end != '\0' || parsed_crc > 0xFFFFUL) {
    return ParseError::MALFORMED;
  }
  const size_t payload_length = static_cast<size_t>(separator - line);
  const uint16_t expected =
      crc16_ccitt(reinterpret_cast<const uint8_t*>(line), payload_length);
  if (expected != static_cast<uint16_t>(parsed_crc)) {
    return ParseError::BAD_CRC;
  }
  *separator = '\0';
  char* save = nullptr;
  char* token = strtok_r(line, "|", &save);
  if (token == nullptr || strcmp(token, "KP1") != 0) {
    return token != nullptr && token[0] == 'K' && token[1] == 'P'
               ? ParseError::UNSUPPORTED_VERSION
               : ParseError::MALFORMED;
  }
  token = strtok_r(nullptr, "|", &save);
  if (token == nullptr || strlen(token) != 1 ||
      (token[0] != 'Q' && token[0] != 'R' && token[0] != 'E')) {
    return ParseError::MALFORMED;
  }
  output->kind = token[0];
  token = strtok_r(nullptr, "|", &save);
  if (token == nullptr || strlen(token) != 8) {
    return ParseError::MALFORMED;
  }
  for (uint8_t index = 0; index < 8; ++index) {
    if (!isxdigit(static_cast<unsigned char>(token[index])) ||
        islower(static_cast<unsigned char>(token[index]))) {
      return ParseError::MALFORMED;
    }
  }
  memcpy(output->request_id, token, 8);
  output->request_id[8] = '\0';
  token = strtok_r(nullptr, "|", &save);
  if (token == nullptr || !valid_identifier(token, 1, 16, true)) {
    return ParseError::MALFORMED;
  }
  strncpy(output->operation, token, 16);
  output->operation[16] = '\0';
  output->field_count = 0;
  while ((token = strtok_r(nullptr, "|", &save)) != nullptr) {
    if (output->field_count >= kMaxFields) {
      return ParseError::MALFORMED;
    }
    char* equals = strchr(token, '=');
    if (equals == nullptr || strchr(equals + 1, '=') != nullptr) {
      return ParseError::MALFORMED;
    }
    *equals = '\0';
    const char* value = equals + 1;
    if (!valid_identifier(token, 1, 16, false) || !valid_value(value)) {
      return ParseError::MALFORMED;
    }
    for (uint8_t index = 0; index < output->field_count; ++index) {
      if (strcmp(output->fields[index].key, token) == 0) {
        return ParseError::MALFORMED;
      }
    }
    Field& field = output->fields[output->field_count++];
    field.key = token;
    field.value = value;
  }
  return ParseError::NONE;
}

FrameParser::FrameParser() : buffer_{}, length_(0), discarding_(false) {}

bool FrameParser::push(char byte, ParsedFrame* output, ParseError* error) {
  *error = ParseError::NONE;
  if (discarding_) {
    if (byte == '\n') {
      discarding_ = false;
      *error = ParseError::TOO_LONG;
      return true;
    }
    return false;
  }
  if (length_ >= kMaxFrameBytes) {
    length_ = 0;
    discarding_ = true;
    if (byte == '\n') {
      discarding_ = false;
      *error = ParseError::TOO_LONG;
      return true;
    }
    return false;
  }
  buffer_[length_++] = byte;
  if (byte != '\n') {
    return false;
  }
  buffer_[length_] = '\0';
  *error = parse_frame(buffer_, length_, output);
  length_ = 0;
  return true;
}

}  // namespace kegpulse
