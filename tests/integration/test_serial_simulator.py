from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import pytest

from kegpulse.protocol import FrameParser, encode_frame
from kegpulse.serialio import DeviceManager, SimulatorTransport
from kegpulse.serialio.manager import ConnectionState


def wait_until(predicate: Callable[[], bool], timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def read_frames(transport: SimulatorTransport, expected: int = 1):
    parser = FrameParser()
    frames = []
    deadline = time.monotonic() + 1
    while len(frames) < expected and time.monotonic() < deadline:
        chunk = transport.read(256, 0.05)
        parsed, errors = parser.feed(chunk)
        assert not errors
        frames.extend(parsed)
    return frames


def test_simulator_uses_kp1_boundary_and_handles_partial_input() -> None:
    transport = SimulatorTransport(seed=11)
    transport.open()
    hello = encode_frame("Q", "00000001", "HELLO", {"min": 1, "max": 1})
    for byte in hello:
        transport.write(bytes([byte]))
    frames = read_frames(transport)
    assert frames[0].operation == "HELLO"
    assert frames[0].fields["device"] == "4B454750554C5345"
    transport.close()


def test_manager_handshake_attributed_pour_and_result_replay() -> None:
    transport = SimulatorTransport(seed=7)
    manager = DeviceManager(lambda: transport, status_interval=0.05)
    manager.start()
    try:
        wait_until(lambda: manager.connection_state == ConnectionState.CONNECTED)
        status = manager.request("STATUS")
        sid = uuid.uuid4().hex
        manager.request(
            "ARM",
            {
                "boot": status.fields["boot"],
                "seq": status.fields["next"],
                "sid": sid,
                "ttl": 15000,
            },
        )
        transport.inject_pulses(50)
        transport.finish_pour()

        results = []

        def received() -> bool:
            results.extend(event for event in manager.drain_events() if event.kind == "result")
            return bool(results)

        wait_until(received)
        result = results[0].frame
        assert result and result.fields["sid"] == sid
        assert result.fields["pulses"] == "50"
        manager.request("ACK", {"boot": result.fields["boot"], "seq": result.fields["seq"]})
        assert manager.request("STATUS").fields["retained"] == "0"
    finally:
        manager.stop()


def test_unattributed_disconnect_reconnect_and_reset() -> None:
    transport = SimulatorTransport()
    manager = DeviceManager(lambda: transport, status_interval=0.05)
    manager.start()
    try:
        wait_until(lambda: manager.connection_state == ConnectionState.CONNECTED)
        transport.inject_pulses(8)
        transport.finish_pour()
        found = []

        def got_unattributed() -> bool:
            for event in manager.drain_events():
                if event.kind == "result" and event.frame:
                    found.append(event.frame)
            return bool(found)

        wait_until(got_unattributed)
        assert found[0].fields["attr"] == "0"
        original_boot = manager.identity["boot"]
        transport.disconnect_device()
        wait_until(lambda: manager.connection_state == ConnectionState.RECONNECTING)
        transport.reconnect_device()
        wait_until(lambda: manager.connection_state == ConnectionState.CONNECTED)
        assert manager.identity["boot"] == original_boot
        transport.disconnect_device()
        wait_until(lambda: manager.connection_state == ConnectionState.RECONNECTING)
        transport.reset_device()
        transport.reconnect_device()
        wait_until(
            lambda: manager.connection_state == ConnectionState.CONNECTED
            and manager.identity.get("boot") != original_boot
        )
    finally:
        manager.stop()


def test_simulator_corrupt_duplicate_delay_and_script_controls() -> None:
    transport = SimulatorTransport(seed=4)
    transport.open()
    transport.configure_fault("duplicate_next")
    transport.write(encode_frame("Q", "00000001", "PING", {"nonce": "x"}))
    assert len(read_frames(transport, expected=2)) == 2

    transport.configure_fault("delay_next")
    transport.write(encode_frame("Q", "00000002", "PING", {"nonce": "y"}))
    assert transport.read(256, 0.01) == b""
    transport.flush_delayed(reverse=True)
    assert read_frames(transport)[0].fields["nonce"] == "y"

    transport.configure_fault("corrupt_next")
    transport.write(encode_frame("Q", "00000003", "PING", {"nonce": "z"}))
    parser = FrameParser()
    _, errors = parser.feed(transport.read(256, 0.05))
    assert errors

    transport.run_script(
        [{"action": "pulse", "count": 2}, {"action": "advance", "milliseconds": 3000}]
    )
    assert transport.device.lifetime_pulses == 2
    with pytest.raises(ValueError):
        transport.run_script([{"action": "unknown"}])
    transport.close()


def test_queue_overflow_automatically_replays_retained_measurement() -> None:
    transport = SimulatorTransport(seed=37)
    # Startup fills these four slots (connecting, connected, hello, status). A terminal
    # result therefore exercises the real overflow path rather than a private helper.
    manager = DeviceManager(lambda: transport, event_capacity=4, status_interval=10)
    manager.start()
    try:
        wait_until(lambda: manager.connection_state == ConnectionState.CONNECTED)
        transport.inject_pulses(17)
        transport.finish_pour()
        wait_until(lambda: manager.connection_state == ConnectionState.DEGRADED)
        assert manager.overflow_count >= 1

        manager.drain_events()
        replayed = []

        def recovered() -> bool:
            replayed.extend(
                event.frame
                for event in manager.drain_events()
                if event.kind == "result" and event.frame is not None
            )
            return manager.connection_state == ConnectionState.CONNECTED and bool(replayed)

        wait_until(recovered)
        assert replayed[0].fields["pulses"] == "17"
        assert "resynchronized" in manager.connection_detail
        manager.request(
            "ACK", {"boot": replayed[0].fields["boot"], "seq": replayed[0].fields["seq"]}
        )
        assert manager.request("STATUS").fields["retained"] == "0"
    finally:
        manager.stop()
