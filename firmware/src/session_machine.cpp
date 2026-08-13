#include "kegpulse/session_machine.hpp"

#include <limits.h>
#include <string.h>

namespace kegpulse {

namespace {

bool valid_session_id(const char* session_id) {
  if (session_id == nullptr || strlen(session_id) != 32) {
    return false;
  }
  for (uint8_t index = 0; index < 32; ++index) {
    const char value = session_id[index];
    if (!((value >= '0' && value <= '9') || (value >= 'a' && value <= 'f'))) {
      return false;
    }
  }
  return true;
}

}  // namespace

SessionMachine::SessionMachine(uint32_t arm_timeout_ms, uint32_t flow_gap_ms,
                               uint32_t settling_ms)
    : arm_timeout_ms_(arm_timeout_ms),
      flow_gap_ms_(flow_gap_ms),
      settling_ms_(settling_ms),
      state_(DeviceState::IDLE),
      lifetime_pulses_(0),
      next_sequence_(1),
      active_present_(false),
      active_{},
      results_{},
      result_count_(0),
      recovery_pulses_(0),
      fault_("none") {}

bool SessionMachine::due(uint32_t now, uint32_t deadline) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

bool SessionMachine::after(uint32_t now, uint32_t deadline) {
  return static_cast<int32_t>(now - deadline) > 0;
}

void SessionMachine::copy_session(char destination[33], const char* source) const {
  if (source == nullptr) {
    destination[0] = '\0';
    return;
  }
  strncpy(destination, source, 32);
  destination[32] = '\0';
}

MachineError SessionMachine::allocate_sequence(uint32_t* sequence) {
  if (next_sequence_ == UINT32_MAX) {
    fault_ = "sequence_exhausted";
    return MachineError::SATURATED;
  }
  *sequence = next_sequence_;
  ++next_sequence_;
  return MachineError::NONE;
}

MachineError SessionMachine::arm(const char* session_id, uint32_t sequence,
                                 uint32_t now_ms, uint32_t ttl_ms,
                                 bool* duplicate) {
  *duplicate = false;
  bool ignored = false;
  tick(now_ms, &ignored);
  if (!valid_session_id(session_id) || ttl_ms == 0 || ttl_ms >= 0x80000000UL) {
    return MachineError::RANGE;
  }
  if (active_present_ && active_.sequence == sequence &&
      strcmp(active_.session_id, session_id) == 0) {
    *duplicate = true;
    return MachineError::NONE;
  }
  for (uint8_t index = 0; index < result_count_; ++index) {
    if (results_[index].sequence == sequence &&
        strcmp(results_[index].session_id, session_id) == 0) {
      *duplicate = true;
      return MachineError::NONE;
    }
  }
  if (active_present_ || result_count_ >= kResultCapacity) {
    return MachineError::BUSY;
  }
  if (sequence != next_sequence_) {
    return MachineError::STALE;
  }
  uint32_t allocated = 0;
  const MachineError allocation = allocate_sequence(&allocated);
  if (allocation != MachineError::NONE) {
    return allocation;
  }
  active_present_ = true;
  active_ = {};
  active_.sequence = allocated;
  copy_session(active_.session_id, session_id);
  active_.attributed = true;
  active_.state = DeviceState::ARMED;
  active_.armed_ms = now_ms;
  active_.arm_deadline_ms = now_ms + ttl_ms;
  state_ = DeviceState::ARMED;
  return MachineError::NONE;
}

MachineError SessionMachine::cancel(const char* session_id, uint32_t sequence,
                                    uint32_t now_ms, bool* duplicate,
                                    bool* produced_result) {
  *duplicate = false;
  *produced_result = false;
  bool ignored = false;
  tick(now_ms, &ignored);
  for (uint8_t index = 0; index < result_count_; ++index) {
    if (results_[index].sequence == sequence &&
        strcmp(results_[index].session_id, session_id) == 0) {
      *duplicate = true;
      return MachineError::NONE;
    }
  }
  if (!active_present_ || active_.sequence != sequence ||
      strcmp(active_.session_id, session_id) != 0) {
    return MachineError::STALE;
  }
  if (active_.pulses == 0) {
    active_present_ = false;
    state_ = DeviceState::IDLE;
    return MachineError::NONE;
  }
  const MachineError result =
      finalize(DeviceState::INTERRUPTED, now_ms, "cancelled");
  *produced_result = result == MachineError::NONE;
  return result;
}

MachineError SessionMachine::add_pulses(uint32_t count, uint32_t captured_ms,
                                        bool* produced_result) {
  *produced_result = false;
  if (count == 0) {
    return MachineError::RANGE;
  }
  if (active_present_ && active_.state == DeviceState::ARMED &&
      after(captured_ms, active_.arm_deadline_ms)) {
    tick(captured_ms, produced_result);
  } else if (active_present_ && active_.state == DeviceState::SETTLING &&
             after(captured_ms, active_.settle_deadline_ms)) {
    tick(captured_ms, produced_result);
  }
  if (UINT64_MAX - lifetime_pulses_ < count) {
    lifetime_pulses_ = UINT64_MAX;
    fault_ = "lifetime_saturated";
    if (active_present_ && active_.pulses > 0) {
      finalize(DeviceState::INTERRUPTED, captured_ms, fault_);
      *produced_result = true;
    }
    return MachineError::SATURATED;
  }
  lifetime_pulses_ += count;
  if (!active_present_) {
    if (result_count_ >= kResultCapacity) {
      if (UINT64_MAX - recovery_pulses_ < count) {
        recovery_pulses_ = UINT64_MAX;
      } else {
        recovery_pulses_ += count;
      }
      fault_ = "result_store_full";
      return MachineError::BUSY;
    }
    uint32_t sequence = 0;
    const MachineError allocation = allocate_sequence(&sequence);
    if (allocation != MachineError::NONE) {
      recovery_pulses_ += count;
      return allocation;
    }
    active_present_ = true;
    active_ = {};
    active_.sequence = sequence;
    active_.attributed = false;
    active_.state = DeviceState::POURING;
    active_.pulses = count;
    active_.armed_ms = captured_ms;
    active_.started_ms = captured_ms;
    active_.last_pulse_ms = captured_ms;
    state_ = DeviceState::POURING;
    return MachineError::NONE;
  }
  if (active_.pulses > kMaxResultPulses ||
      kMaxResultPulses - active_.pulses < count) {
    active_.pulses = kMaxResultPulses;
    fault_ = "session_saturated";
    finalize(DeviceState::INTERRUPTED, captured_ms, fault_);
    *produced_result = true;
    return MachineError::SATURATED;
  }
  if (active_.state == DeviceState::ARMED) {
    active_.started_ms = captured_ms;
  }
  active_.pulses += count;
  active_.last_pulse_ms = captured_ms;
  active_.settle_deadline_ms = 0;
  active_.state = DeviceState::POURING;
  state_ = DeviceState::POURING;
  return MachineError::NONE;
}

MachineError SessionMachine::add_pulse_batch(uint32_t count,
                                             uint32_t first_captured_ms,
                                             uint32_t last_captured_ms,
                                             bool* produced_result) {
  *produced_result = false;
  if (count == 0) {
    return MachineError::RANGE;
  }

  // The first edge decides an arm/settle deadline. The bounded second call
  // applies the rest of the ISR batch at the last captured timestamp without
  // moving an edge that arrived at/before the deadline into a newer event.
  bool first_produced = false;
  const MachineError first = add_pulses(1, first_captured_ms, &first_produced);
  *produced_result = first_produced;
  if (count == 1) {
    return first;
  }

  bool remainder_produced = false;
  const MachineError remainder =
      add_pulses(count - 1U, last_captured_ms, &remainder_produced);
  *produced_result = *produced_result || remainder_produced;
  return first != MachineError::NONE ? first : remainder;
}

MachineError SessionMachine::tick(uint32_t now_ms, bool* produced_result) {
  *produced_result = false;
  if (!active_present_) {
    return MachineError::NONE;
  }
  if (active_.state == DeviceState::ARMED && due(now_ms, active_.arm_deadline_ms)) {
    const MachineError result =
        finalize(DeviceState::TIMED_OUT, active_.arm_deadline_ms, "none");
    *produced_result = result == MachineError::NONE;
    return result;
  }
  if (active_.state == DeviceState::POURING) {
    const uint32_t gap_at = active_.last_pulse_ms + flow_gap_ms_;
    if (due(now_ms, gap_at)) {
      active_.state = DeviceState::SETTLING;
      active_.settle_deadline_ms = gap_at + settling_ms_;
      state_ = DeviceState::SETTLING;
    }
  }
  if (active_.state == DeviceState::SETTLING &&
      due(now_ms, active_.settle_deadline_ms)) {
    const MachineError result =
        finalize(DeviceState::COMPLETE, active_.settle_deadline_ms, "none");
    *produced_result = result == MachineError::NONE;
    return result;
  }
  return MachineError::NONE;
}

MachineError SessionMachine::mark_counter_saturated(uint32_t now_ms,
                                                     bool* produced_result) {
  *produced_result = false;
  fault_ = "counter_saturated";
  if (!active_present_) {
    return MachineError::SATURATED;
  }
  const MachineError outcome =
      finalize(DeviceState::INTERRUPTED, now_ms, "counter_saturated");
  *produced_result = outcome == MachineError::NONE;
  return outcome == MachineError::NONE ? MachineError::SATURATED : outcome;
}

MachineError SessionMachine::finalize(DeviceState status, uint32_t ended_ms,
                                      const char* fault) {
  if (!active_present_) {
    return MachineError::INVALID_STATE;
  }
  if (result_count_ >= kResultCapacity) {
    fault_ = "result_store_full";
    return MachineError::BUSY;
  }
  Result& output = results_[result_count_++];
  output = {};
  output.sequence = active_.sequence;
  copy_session(output.session_id, active_.session_id);
  output.attributed = active_.attributed;
  output.status = status;
  output.pulses = active_.pulses;
  output.lifetime = lifetime_pulses_;
  output.started_ms = active_.state == DeviceState::ARMED ? active_.armed_ms
                                                          : active_.started_ms;
  output.ended_ms = ended_ms;
  strncpy(output.fault, fault, sizeof(output.fault) - 1);
  output.fault[sizeof(output.fault) - 1] = '\0';
  active_present_ = false;
  state_ = status;
  return MachineError::NONE;
}

MachineError SessionMachine::acknowledge(uint32_t sequence, bool* already) {
  *already = true;
  for (uint8_t index = 0; index < result_count_; ++index) {
    if (results_[index].sequence == sequence) {
      for (uint8_t move = index; move + 1 < result_count_; ++move) {
        results_[move] = results_[move + 1];
      }
      --result_count_;
      *already = false;
      if (!active_present_ && (state_ == DeviceState::COMPLETE ||
                               state_ == DeviceState::TIMED_OUT ||
                               state_ == DeviceState::INTERRUPTED)) {
        state_ = DeviceState::IDLE;
      }
      break;
    }
  }
  return MachineError::NONE;
}

Snapshot SessionMachine::snapshot(uint32_t now_ms) const {
  Snapshot output{};
  output.state = state_;
  output.lifetime_pulses = lifetime_pulses_;
  if (active_present_ && active_.state == DeviceState::ARMED &&
      !due(now_ms, active_.arm_deadline_ms)) {
    output.arm_remaining_ms = active_.arm_deadline_ms - now_ms;
  }
  output.next_sequence = next_sequence_;
  output.retained_results = result_count_;
  output.recovery_pulses = recovery_pulses_;
  output.fault = fault_;
  if (active_present_) {
    output.sequence = active_.sequence;
    memcpy(output.session_id, active_.session_id, sizeof(output.session_id));
    output.attributed = active_.attributed;
    output.session_pulses = active_.pulses;
  } else if (result_count_ > 0) {
    const Result& latest = results_[result_count_ - 1];
    output.sequence = latest.sequence;
    memcpy(output.session_id, latest.session_id, sizeof(output.session_id));
    output.attributed = latest.attributed;
    output.session_pulses = latest.pulses;
  }
  return output;
}

const Result* SessionMachine::result_at(uint8_t index) const {
  return index < result_count_ ? &results_[index] : nullptr;
}

const char* state_name(DeviceState state) {
  switch (state) {
    case DeviceState::IDLE:
      return "idle";
    case DeviceState::ARMED:
      return "armed";
    case DeviceState::POURING:
      return "pouring";
    case DeviceState::SETTLING:
      return "settling";
    case DeviceState::COMPLETE:
      return "complete";
    case DeviceState::TIMED_OUT:
      return "timed_out";
    case DeviceState::INTERRUPTED:
      return "interrupted";
  }
  return "unknown";
}

}  // namespace kegpulse
