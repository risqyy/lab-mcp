import pytest

from lab_mcp.instruments.manson_hcs import HCSProtocolError, MansonHCS, SerialTransport

from fake_hcs import FakeHCSTransport


@pytest.fixture
def t() -> FakeHCSTransport:
    return FakeHCSTransport()


@pytest.fixture
def dev(t: FakeHCSTransport) -> MansonHCS:
    return MansonHCS(t)


def test_probe_reads_model_and_limits(dev: MansonHCS) -> None:
    assert dev.model == "HCS-3404"
    assert (dev.limits.max_voltage, dev.limits.max_current) == (60.0, 10.0)
    assert (dev.limits.digits, dev.limits.decimals) == (3, 1)


def test_four_digit_format() -> None:
    t = FakeHCSTransport(model="HCS-3402", gmax="32402000")  # 32.40 V / 20.00 A
    dev = MansonHCS(t)
    assert (dev.limits.max_voltage, dev.limits.max_current, dev.limits.digits) == (32.4, 20.0, 4)
    dev.set_voltage(12.34)
    assert t.commands[-1] == "VOLT1234"


def test_set_voltage_and_current_encoding(dev: MansonHCS, t: FakeHCSTransport) -> None:
    dev.set_voltage(12.3)
    dev.set_current(2.5)
    assert t.commands[-2:] == ["VOLT123", "CURR025"]
    assert dev.get_setpoints() == (12.3, 2.5)


@pytest.mark.parametrize(("volts", "amps"), [(60.1, None), (-1, None), (None, 10.1)])
def test_out_of_range_rejected_without_command(dev: MansonHCS, t: FakeHCSTransport, volts, amps) -> None:
    before = list(t.commands)
    with pytest.raises(ValueError, match="out of range"):
        if volts is not None:
            dev.set_voltage(volts)
        else:
            dev.set_current(amps)
    assert t.commands == before


def test_output_logic_is_inverted(dev: MansonHCS, t: FakeHCSTransport) -> None:
    assert dev.output_enabled is None
    dev.set_output(True)
    assert t.commands[-1] == "SOUT0" and t.output is True
    dev.set_output(False)
    assert t.commands[-1] == "SOUT1" and t.output is False
    assert dev.output_enabled is False


def test_measure_cv_and_cc(dev: MansonHCS, t: FakeHCSTransport) -> None:
    dev.set_voltage(12.0)
    dev.set_current(5.0)
    dev.set_output(True)
    m = dev.measure()
    assert (m.voltage, m.current, m.mode) == (12.0, 1.2, "CV")
    assert m.power == pytest.approx(14.4)
    dev.set_current(0.5)
    m = dev.measure()
    assert (m.current, m.mode) == (0.5, "CC")


def test_upper_limits(dev: MansonHCS, t: FakeHCSTransport) -> None:
    dev.set_upper_limits(volts=30.0, amps=4.0)
    assert t.commands[-2:] == ["SOVP300", "SOCP040"]
    assert dev.get_upper_limits() == (30.0, 4.0)


def test_upper_limit_out_of_range_sends_nothing(dev: MansonHCS, t: FakeHCSTransport) -> None:
    before = list(t.commands)
    with pytest.raises(ValueError):
        dev.set_upper_limits(volts=30.0, amps=11.0)
    assert t.commands == before


def test_presets(dev: MansonHCS) -> None:
    presets = dev.get_presets()
    assert [(p.voltage, p.current) for p in presets] == [(5.0, 1.0), (12.0, 2.0), (24.0, 3.0)]
    dev.load_preset(1)
    assert dev.get_setpoints() == (12.0, 2.0)
    with pytest.raises(ValueError):
        dev.load_preset(3)


def test_status(dev: MansonHCS) -> None:
    dev.set_voltage(5.0)
    st = dev.status()
    assert (st.model, st.voltage_setpoint, st.output_enabled, st.upper_voltage_limit) == ("HCS-3404", 5.0, None, 60.0)


@pytest.mark.parametrize("gmax_reply", ["60010", "6001000x", None])
def test_bad_gmax_rejected(gmax_reply: str | None) -> None:
    class BadGmax(FakeHCSTransport):
        def request(self, command: str):
            return gmax_reply if command == "GMAX" else super().request(command)

    with pytest.raises(HCSProtocolError):
        MansonHCS(BadGmax())


class FakeSerial:
    """Byte-level serial port with scripted replies."""

    port = "COM99"

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.written = b""

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        self.written += data

    def read_until(self, terminator: bytes) -> bytes:
        return self.replies.pop(0) if self.replies else b""

    def close(self) -> None:
        pass


def test_serial_framing_with_data() -> None:
    port = FakeSerial([b"HCS-3404\r", b"OK\r"])
    assert SerialTransport(port).request("GMOD") == "HCS-3404"
    assert port.written == b"GMOD\r"


def test_serial_framing_ok_only() -> None:
    assert SerialTransport(FakeSerial([b"OK\r"])).request("SOUT1") is None


def test_serial_timeout() -> None:
    with pytest.raises(TimeoutError):
        SerialTransport(FakeSerial([b""])).request("GMOD")


def test_serial_unexpected_reply() -> None:
    with pytest.raises(HCSProtocolError):
        SerialTransport(FakeSerial([b"FOO\r", b"BAR\r"])).request("GMOD")
