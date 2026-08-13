#pragma once

#include <stddef.h>
#include <stdint.h>

namespace kegpulse {

constexpr uint8_t kResultCapacity = 4;
constexpr uint64_t kMaxResultPulses = UINT64_C(0x7FFFFFFFFFFFFFFF);

enum class DeviceState : uint8_t {
  IDLE,
  ARMED,
  POURING,
  SETTLING,
  COMPLETE,
  TIMED_OUT,
  INTERRUPTED,
};

enum class MachineError : uint8_t {
  NONE,
  BUSY,
  STALE,
  INVALID_STATE,
  RANGE,
  SATURATED,
};

struct Result {
  uint32_t sequence;
  char session_id[33];
  bool attributed;
  DeviceState status;
  uint64_t pulses;
  uint64_t lifetime;
  uint32_t started_ms;
  uint32_t ended_ms;
  char fault[24];
};

struct Snapshot {
  DeviceState state;
  uint32_t sequence;
  char session_id[33];
  bool attributed;
  uint64_t session_pulses;
  uint64_t lifetime_pulses;
  uint32_t arm_remaining_ms;
  uint32_t next_sequence;
  uint8_t retained_results;
  uint64_t recovery_pulses;
  const char* fault;
};

class SessionMachine {
 public:
  SessionMachine(uint32_t arm_timeout_ms = 15000, uint32_t flow_gap_ms = 750,
                 uint32_t settling_ms = 1500);

  MachineError arm(const char* session_id, uint32_t sequence, uint32_t now_ms,
                   uint32_t ttl_ms, bool* duplicate);
  MachineError cancel(const char* session_id, uint32_t sequence,
                      uint32_t now_ms, bool* duplicate, bool* produced_result);
  MachineError add_pulses(uint32_t count, uint32_t captured_ms,
                          bool* produced_result);
  MachineError add_pulse_batch(uint32_t count, uint32_t first_captured_ms,
                               uint32_t last_captured_ms,
                               bool* produced_result);
  MachineError tick(uint32_t now_ms, bool* produced_result);
  MachineError mark_counter_saturated(uint32_t now_ms, bool* produced_result);
  MachineError acknowledge(uint32_t sequence, bool* already);
  Snapshot snapshot(uint32_t now_ms) const;
  const Result* result_at(uint8_t index) const;
  uint8_t result_count() const { return result_count_; }

 private:
  struct Active {
    uint32_t sequence;
    char session_id[33];
    bool attributed;
    DeviceState state;
    uint64_t pulses;
    uint32_t armed_ms;
    uint32_t started_ms;
    uint32_t last_pulse_ms;
    uint32_t arm_deadline_ms;
    uint32_t settle_deadline_ms;
  };

  static bool due(uint32_t now, uint32_t deadline);
  static bool after(uint32_t now, uint32_t deadline);
  MachineError allocate_sequence(uint32_t* sequence);
  MachineError finalize(DeviceState status, uint32_t ended_ms,
                        const char* fault);
  void copy_session(char destination[33], const char* source) const;

  uint32_t arm_timeout_ms_;
  uint32_t flow_gap_ms_;
  uint32_t settling_ms_;
  DeviceState state_;
  uint64_t lifetime_pulses_;
  uint32_t next_sequence_;
  bool active_present_;
  Active active_;
  Result results_[kResultCapacity];
  uint8_t result_count_;
  uint64_t recovery_pulses_;
  const char* fault_;
};

const char* state_name(DeviceState state);

}  // namespace kegpulse
