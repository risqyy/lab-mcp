import math
from types import SimpleNamespace

import pytest

from lab_mcp.instruments.multimeters.peaktech_4365 import (
    PeakTech4365,
    PTProtocolError,
    PTSerialTransport,
    parse_line,
    parse_reply,
    split_reply,
)

from fake_peaktech_4365 import REPLIES, FakePTTransport, lines


def read(name: str):
    return parse_reply(lines(REPLIES[name]))


def test_split_reply() -> None:
    assert split_reply(REPLIES["dc_mv"]) == lines(REPLIES["dc_mv"])


@pytest.mark.parametrize("data", [b"", REPLIES["dc_mv"][:42], REPLIES["dc_mv"][1:] + b"\r", b"x" * 56])
def test_split_reply_rejects_incomplete_reply(data: bytes) -> None:
    with pytest.raises(PTProtocolError):
        split_reply(data)


def test_dc_millivolt_with_history() -> None:
    r = read("dc_mv")
    assert (r.function, r.value, r.unit, r.display, r.overload) == ("dc_voltage", 0.00916, "V", "009.16 mV", False)
    assert r.history == [0.09757, None, 0.0]  # the history uses the unit of line 1 (mV)


def test_lowest_resistance_range_in_ohm() -> None:
    r = read("resistance_ohm_overload")
    assert (r.function, r.unit, r.overload, r.display, r.history) == ("resistance", "Ohm", True, "O.L Ohm", [None, 0.0, 0.0])


def test_signal_output_with_overload_of_amplitude() -> None:
    r = read("signal_output_overload")
    assert (r.function, r.value) == ("signal_output", 10.0)
    amplitude = r.secondary["amplitude"]
    assert (amplitude.value, amplitude.unit, amplitude.overload) == (None, "V", True)


def test_negative_value() -> None:
    r = read("dc_v_negative")
    assert (r.value, r.display) == (-12.5, "-0012.5 V")


@pytest.mark.parametrize(("name", "function"), [
    ("resistance_overload", "resistance"), ("diode_open", "diode"), ("capacitance_overload", "capacitance"),
])
def test_overload(name: str, function: str) -> None:
    r = read(name)
    assert (r.function, r.value, r.overload) == (function, None, True)


def test_ac_voltage_with_frequency_and_level() -> None:
    r = read("ac_v")
    assert (r.function, r.value) == ("ac_voltage", 7.3)
    assert (r.secondary["frequency"].value, r.secondary["frequency"].unit) == (147.0, "Hz")
    assert (r.secondary["level"].value, r.secondary["level"].unit) == (-8.53, "dBm")
    assert r.history == []


def test_value_without_decimal_point_is_not_valid() -> None:
    r = read("ac_v_range_change")
    assert r.secondary["frequency"].value is None and not r.secondary["frequency"].overload
    c = read("capacitance_measuring")
    assert (c.function, c.value, c.overload, c.unit, c.display) == ("capacitance", None, False, "F", "00000 nF")


def test_negative_overload_of_level() -> None:
    level = read("ac_v_level_overload").secondary["level"]
    assert (level.value, level.overload, level.display) == (None, True, "-O.L dBm")


def test_temperature_in_celsius_and_fahrenheit() -> None:
    r = read("temperature")
    assert (r.function, r.value, r.unit) == ("temperature", 22.6, "°C")
    assert (r.secondary["temperature_f"].value, r.secondary["temperature_f"].unit) == (72.6, "°F")


def test_diode_status_text() -> None:
    r = read("diode_bad")
    assert (r.value, r.unit) == (0.0001, "V")
    assert r.secondary["status"].display == "bAd" and r.secondary["status"].value is None


@pytest.mark.parametrize(("name", "function", "value", "unit"), [
    ("ac_ma", "ac_current", 0.0001146, "A"),
    ("dc_a", "dc_current", 0.003, "A"),
    ("hfe", "hfe", 0.1, ""),
    ("frequency", "frequency", 0.0, "Hz"),
    ("frequency_mhz", "frequency", 1234000.0, "Hz"),
    ("signal_output", "signal_output", 10.0, "Hz"),
])
def test_functions(name: str, function: str, value: float, unit: str) -> None:
    r = read(name)
    assert (r.function, r.unit) == (function, unit)
    assert math.isclose(r.value, value)


def test_frequency_and_signal_output_secondary() -> None:
    assert read("frequency").secondary["ac_voltage"].value == 6.572
    assert read("signal_output").secondary["amplitude"].value == 0.216


def test_logic() -> None:
    r = read("logic")
    assert (r.function, r.value, r.display) == ("logic", None, "rdy")
    assert r.secondary["value"].value == -0.001


def test_unknown_unit_is_refused() -> None:
    with pytest.raises(PTProtocolError, match="Unknown unit"):
        parse_line("DC 001.00 XYZ")


def test_series_statistics() -> None:
    dev = PeakTech4365(FakePTTransport(["dc_mv", "dc_mv", "dc_v_negative"]))
    with pytest.raises(RuntimeError, match="function changed"):
        PeakTech4365(FakePTTransport(["dc_mv", "dc_mv", "dc_mv", "ac_v"])).read_series(3, 0)
    s = PeakTech4365(FakePTTransport(["dc_a", "dc_a"])).read_series(3, 0)
    assert (s.function, s.unit, s.count, s.mean, s.stdev) == ("dc_current", "A", 3, 0.003, 0.0)
    assert dev.read().function == "dc_voltage"


def test_series_counts_overload_and_invalid() -> None:
    # The constructor reads the first reply.
    dev = PeakTech4365(FakePTTransport(["dc_mv", "capacitance_measuring", "capacitance_measuring", "capacitance_overload"]))
    s = dev.read_series(3, 0)
    assert (s.overloads, s.invalid, s.mean) == (1, 2, None)


class _FakeSerial:
    def __init__(self, replies: list[bytes]) -> None:
        self.port = "/dev/ttyUSB0"
        self.replies = replies
        self.written: list[bytes] = []

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def read(self, size: int) -> bytes:
        assert size == 56
        return self.replies.pop(0)


def test_transport_retries_after_incomplete_reply() -> None:
    port = _FakeSerial([REPLIES["dc_mv"][:28], REPLIES["dc_mv"]])
    assert PTSerialTransport(port, settle_s=0).query() == lines(REPLIES["dc_mv"])
    assert port.written == [b"\r", b"\r"]


def test_transport_timeout() -> None:
    with pytest.raises(TimeoutError, match="No reply"):
        PTSerialTransport(_FakeSerial([b"", b""]), settle_s=0).query()


def test_transport_protocol_error_after_two_bad_replies() -> None:
    with pytest.raises(PTProtocolError):
        PTSerialTransport(_FakeSerial([b"x" * 10, b"y" * 56]), settle_s=0).query()


def test_transport_open_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = []

    def fake_serial(device, **kwargs):
        port = SimpleNamespace(port=device, dtr=False, rts=True, kwargs=kwargs)
        opened.append(port)
        return port

    monkeypatch.setattr("lab_mcp.instruments.multimeters.peaktech_4365.serial.Serial", fake_serial)
    monkeypatch.setattr("lab_mcp.instruments.multimeters.peaktech_4365.time.sleep", lambda s: None)
    PTSerialTransport.open("/dev/ttyUSB0")
    [port] = opened
    assert (port.dtr, port.rts) == (True, False)
    assert {k: port.kwargs[k] for k in ("baudrate", "bytesize", "parity", "stopbits")} == {
        "baudrate": 9600, "bytesize": 7, "parity": "N", "stopbits": 2,
    }
