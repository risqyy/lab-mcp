import math

import pytest

from lab_mcp.instruments.signal_generators.gwinstek_afg2000 import (
    AFGProtocolError, AFGSerialTransport, GWInstekAFG2000,
)

from fake_gwinstek_afg2000 import FakeAFGTransport


@pytest.fixture
def t():
    return FakeAFGTransport()


@pytest.fixture
def dev(t):
    return GWInstekAFG2000(t, "AFG-2105")


@pytest.mark.parametrize("load,factor", [("DEF", 1), ("INF", 2)])
@pytest.mark.parametrize("unit", ["VPP", "VRMS", "DBM"])
def test_status_normalizes_voltage_queries_without_writes(dev, t, load, factor, unit):
    t.load, t.unit, t.offset = load, unit, 0.25
    status = dev.status()
    assert status.amplitude_vpp == 0.5 * factor
    assert status.offset_v == 0.25 * factor
    assert status.limits.amplitude_vpp.maximum == 9.5 * factor
    assert status.limits.offset_v.maximum == 4.75 * factor
    assert status.display_unit == unit
    assert t.writes == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("waveform", ["sine", "square", "ramp", "noise"])
def test_set_waveform_and_voltages_preserves_output(dev, t, enabled, waveform):
    t.output = enabled
    status = dev.set(waveform=waveform, amplitude_vpp=2, offset_v=0.5)
    assert (status.waveform, status.amplitude_vpp, status.offset_v) == (waveform, 2, 0.5)
    assert status.output_enabled is enabled
    assert t.amplitude == 1 and t.offset == 0.25
    assert not any(c.startswith(("OUTPut ", "APPL", ":")) for c in t.writes)


@pytest.mark.parametrize("arguments", [
    {"frequency_hz": 5e6 + 1}, {"frequency_hz": 0}, {"frequency_hz": math.nan},
    {"amplitude_vpp": 0.001}, {"amplitude_vpp": math.inf}, {"amplitude_vpp": 21},
    {"amplitude_vpp": 10, "offset_v": 6}, {"offset_v": -11}, {"offset_v": math.nan},
    {"waveform": "ramp", "frequency_hz": 1e6 + 1}, {"waveform": "pulse"},
    {"waveform": "square", "frequency_hz": 1e6, "duty_cycle_percent": 99},
    {"waveform": "square", "frequency_hz": 5e6, "duty_cycle_percent": 80},
    {"waveform": "ramp", "symmetry_percent": 101}, {"load": "unknown"},
    {"waveform": "noise", "frequency_hz": 1000}, {"duty_cycle_percent": 40},
    {"symmetry_percent": 50},
])
def test_invalid_target_sends_no_settings(dev, t, arguments):
    with pytest.raises(ValueError):
        dev.set(**arguments)
    assert t.writes == []


def test_current_device_limits_are_used_before_writing(dev, t):
    t.query_overrides["SOURce1:AMPlitude? MAX"] = "0.6"
    with pytest.raises(ValueError, match="out of range"):
        dev.set(amplitude_vpp=2)
    assert t.writes == []


def test_device_offset_limit_checked_before_amplitude_change(dev, t):
    t.query_overrides["SOURce1:DCOffset? MAX"] = "0.1"
    with pytest.raises(ValueError, match="offset_v"):
        dev.set(amplitude_vpp=0.5, offset_v=1)
    assert t.writes == []


def test_device_duty_limit_checked_before_other_changes(dev, t):
    t.waveform = "SQU"
    t.query_overrides["SOURce1:SQUare:DCYCle? MAX"] = "60"
    with pytest.raises(ValueError, match="duty_cycle"):
        dev.set(amplitude_vpp=2, duty_cycle_percent=75)
    assert t.writes == []


def test_amplitude_decrease_makes_room_for_offset(dev, t):
    t.amplitude = 9
    status = dev.set(amplitude_vpp=2, offset_v=8)
    assert (status.amplitude_vpp, status.offset_v) == (2, 8)
    assert t.writes[:2] == ["SOURce1:AMPlitude 2VPP", "SOURce1:DCOffset 8"]


def test_offset_decrease_makes_room_for_amplitude(dev, t):
    t.amplitude, t.offset = 1, 4
    status = dev.set(amplitude_vpp=18, offset_v=0)
    assert (status.amplitude_vpp, status.offset_v) == (18, 0)
    assert t.writes[:2] == ["SOURce1:DCOffset 0", "SOURce1:AMPlitude 18VPP"]


def test_lower_frequency_before_switching_to_ramp(dev, t):
    t.frequency = 5e6
    status = dev.set(waveform="ramp", frequency_hz=1e6, symmetry_percent=50)
    assert status.frequency_hz == 1e6
    assert t.writes == ["SOURce1:FREQuency 1000000", "SOURce1:FUNCtion RAMP"]


def test_switch_from_ramp_before_increasing_frequency(dev, t):
    t.waveform, t.frequency = "RAMP", 1e6
    status = dev.set(waveform="sine", frequency_hz=5e6)
    assert status.frequency_hz == 5e6
    assert t.writes == ["SOURce1:FUNCtion SIN", "SOURce1:FREQuency 5000000"]


def test_duty_before_frequency_increase_and_after_decrease(dev, t):
    t.waveform, t.duty = "SQU", 99
    dev.set(frequency_hz=1e6, duty_cycle_percent=50)
    assert t.writes == ["SOURce1:SQUare:DCYCle 50", "SOURce1:FREQuency 1000000"]
    t.writes.clear()
    dev.set(frequency_hz=1000, duty_cycle_percent=99)
    assert t.writes == ["SOURce1:FREQuency 1000", "SOURce1:SQUare:DCYCle 99"]


def test_implicit_clamping_is_rejected(dev, t):
    t.waveform, t.frequency = "SIN", 5e6
    with pytest.raises(ValueError, match="frequency_hz"):
        dev.set(waveform="ramp")
    t.waveform, t.frequency, t.duty = "SQU", 1000, 99
    with pytest.raises(ValueError, match="duty_cycle"):
        dev.set(frequency_hz=1e6)
    assert t.writes == []


def test_load_change_preserves_physical_voltage(dev, t):
    t.offset = 0.25
    status = dev.set(load="50_ohm")
    assert (status.amplitude_vpp, status.offset_v, t.amplitude, t.offset) == (0.5, 0.25, 0.5, 0.25)
    status = dev.set(load="high_z", amplitude_vpp=4, offset_v=1)
    assert (status.amplitude_vpp, status.offset_v, t.amplitude, t.offset) == (4, 1, 2, 0.5)


def test_load_change_requires_output_off(dev, t):
    t.output = True
    with pytest.raises(ValueError, match="output off"):
        dev.set(load="50_ohm")
    assert t.writes == []


def test_set_selects_vpp_but_status_does_not(dev, t):
    t.unit = "VRMS"
    assert dev.status().display_unit == "VRMS"
    status = dev.set(amplitude_vpp=2)
    assert status.display_unit == "VPP"
    assert t.writes[0] == "SOURce1:VOLTage:UNIT VPP"


def test_no_arguments_is_read_only(dev, t):
    t.unit = "VRMS"
    assert dev.set().display_unit == "VRMS"
    assert t.writes == []


@pytest.mark.parametrize("mode", ["AM", "FM", "FSKey", "SWEep"])
def test_active_modes_block_changes_and_enabling_but_not_off(dev, t, mode):
    t.modes[mode] = True
    with pytest.raises(ValueError, match="Disable"):
        dev.set(amplitude_vpp=2)
    with pytest.raises(ValueError, match="Disable"):
        dev.set_output(True)
    assert t.writes == []
    assert dev.set_output(False) is False
    assert t.writes == ["OUTPut OFF"]


def test_output_off_does_not_depend_on_status(dev, t):
    t.output, t.waveform = True, "USER"
    t.query_overrides["SOURce1:FUNCtion?"] = "BROKEN"
    assert dev.set_output(False) is False
    assert t.queries == ["OUTPut?", "SYST:ERR?"]


def test_output_roundtrip(dev, t):
    assert dev.set_output(True) is True
    assert dev.set_output(False) is False


def test_failed_enable_attempts_to_switch_off(dev, t):
    t.ignore_writes.add("OUTPut")
    with pytest.raises(AFGProtocolError, match="Output readback"):
        dev.set_output(True)
    assert t.writes[-2:] == ["OUTPut ON", "OUTPut OFF"]


def test_readback_detects_ignored_write(dev, t):
    t.ignore_writes.add("SOURce1:AMPlitude")
    with pytest.raises(AFGProtocolError, match="amplitude_vpp readback"):
        dev.set(amplitude_vpp=2)


def test_small_ignored_offset_is_not_hidden_by_readback_tolerance(dev, t):
    t.ignore_writes.add("SOURce1:DCOffset")
    with pytest.raises(AFGProtocolError, match="offset_v readback"):
        dev.set(offset_v=0.005)


def test_readback_allows_rounding_in_scientific_notation(dev, t, monkeypatch):
    query = t.query

    def rounded(message):
        reply = query(message)
        if message in ("SOURce1:DCOffset?", "SOURce1:SQUare:DCYCle?"):
            return f"{float(reply):.2E}"
        if message == "SOURce1:RAMP:SYMMetry?":
            return f"{float(reply):.3E}"
        return reply

    monkeypatch.setattr(t, "query", rounded)
    status = dev.set(waveform="square", duty_cycle_percent=33.33, offset_v=2.3456)
    assert status.offset_v == 2.34 and status.duty_cycle_percent == 33.3
    assert dev.set(waveform="ramp", symmetry_percent=33.333).symmetry_percent == 33.33


def test_preexisting_errors_are_reported_before_settings(dev, t):
    t.errors = ["-102", "-222"]
    with pytest.raises(RuntimeError, match="before changing settings.*-102, -222"):
        dev.set(amplitude_vpp=2)
    assert t.writes == []


def test_device_error_after_write_stops_remaining_commands(dev, t, monkeypatch):
    write = t.write

    def fail(message):
        write(message)
        t.errors.append("-222")

    monkeypatch.setattr(t, "write", fail)
    with pytest.raises(RuntimeError, match="after SOURce1:AMPlitude.*-222"):
        dev.set(amplitude_vpp=2, waveform="square")
    assert t.writes == ["SOURce1:AMPlitude 2VPP"]


def test_ignored_display_unit_change_is_reported(dev, t):
    t.unit = "VRMS"
    t.ignore_writes.add("SOURce1:VOLTage:UNIT")
    with pytest.raises(AFGProtocolError, match="display_unit readback"):
        dev.set(amplitude_vpp=2)


def test_unexpected_output_enable_is_switched_off(dev, t, monkeypatch):
    write = t.write

    def unexpected_enable(message):
        write(message)
        if message.startswith("SOURce1:AMPlitude "):
            t.output = True

    monkeypatch.setattr(t, "write", unexpected_enable)
    with pytest.raises(AFGProtocolError, match="output_enabled readback"):
        dev.set(amplitude_vpp=2)
    assert not t.output
    assert t.writes[-1] == "OUTPut OFF"


def test_output_on_refuses_arbitrary_waveform(dev, t):
    t.waveform = "USER"
    with pytest.raises(ValueError, match="basic waveform"):
        dev.set_output(True)
    assert t.writes == []


def test_output_off_is_sent_even_with_old_errors(dev, t):
    t.output = True
    t.errors.append("-222")
    with pytest.raises(RuntimeError, match="-222"):
        dev.set_output(False)
    assert not t.output
    assert t.writes == ["OUTPut OFF"]


def test_error_queue_is_bounded(dev, t):
    t.query_overrides["SYST:ERR?"] = "-222"
    with pytest.raises(AFGProtocolError, match="did not end"):
        dev.set(amplitude_vpp=2)
    assert t.queries.count("SYST:ERR?") == 16
    assert t.writes == []


@pytest.mark.parametrize("query,reply", [
    ("SOURce1:AMPlitude?", "NaN"), ("SOURce1:FREQuency?", "bad"),
    ("OUTPut?", "2"), ("OUTPut:LOAD?", "unknown"), ("SOURce1:FUNCtion?", "PULSE"),
    ("SOURce1:AMPlitude? MIN", "999"),
    ("SOURce1:FREQuency? MIN", "10000000"),
])
def test_malformed_status_is_rejected(dev, t, query, reply):
    t.query_overrides[query] = reply
    with pytest.raises(AFGProtocolError):
        dev.status()


def test_unknown_model_rejected(t):
    with pytest.raises(ValueError, match="Unsupported"):
        GWInstekAFG2000(t, "AFG-2125")


class FakeSerial:
    port = "COM99"
    is_open = True

    def __init__(self, reply=b"1\r\n"):
        self.reply = reply
        self.written = b""

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written += data
        return len(data)

    def read_until(self, terminator, size):
        assert terminator == b"\n" and size == 1024
        return self.reply

    def close(self):
        self.is_open = False


def test_serial_framing_and_close():
    port = FakeSerial()
    transport = AFGSerialTransport(port)
    assert transport.query("OUTPut?") == "1"
    assert port.written == b"OUTPut?\n"
    transport.close()
    assert not transport.is_open


@pytest.mark.parametrize("reply,error", [(b"", TimeoutError), (b"1\r", TimeoutError),
                                        (b"\xff\n", AFGProtocolError), (b"\r\n", AFGProtocolError)])
def test_serial_bad_reply(reply, error):
    with pytest.raises(error):
        AFGSerialTransport(FakeSerial(reply)).query("OUTPut?")


def test_serial_partial_write():
    port = FakeSerial()
    port.write = lambda data: len(data) - 1
    with pytest.raises(AFGProtocolError, match="Incomplete write"):
        AFGSerialTransport(port).write("OUTPut OFF")


def test_serial_open_settings(monkeypatch):
    from lab_mcp.instruments.signal_generators import gwinstek_afg2000

    opened = []

    def open_port(device, **kwargs):
        opened.append((device, kwargs))
        return FakeSerial()

    monkeypatch.setattr(gwinstek_afg2000.serial, "Serial", open_port)
    AFGSerialTransport.open("COM8", timeout=0.5).close()
    assert opened == [("COM8", {"baudrate": 115200, "bytesize": 8, "parity": "N", "stopbits": 1,
                                "timeout": 0.5, "write_timeout": 0.5})]
