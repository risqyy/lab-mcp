from types import SimpleNamespace

import pytest

from lab_mcp.instruments.power_supplies.isotech_ips import (
    IPSProtocolError,
    IPSSerialTransport,
    IsoTechIPS,
    parse_status_line,
)

from fake_isotech_ips import FakeIPSTransport


@pytest.fixture
def t() -> FakeIPSTransport:
    return FakeIPSTransport()


@pytest.fixture
def dev(t: FakeIPSTransport) -> IsoTechIPS:
    return IsoTechIPS(t, "IPS-405")


def test_parse_example_of_user_manual() -> None:
    r = parse_status_line("V20.00A2.500W050.0U40I5.00P200F101000")
    assert (r.voltage, r.current, r.power) == (20.0, 2.5, 50.0)
    assert (r.voltage_limit, r.current_limit, r.power_limit) == (40, 5.0, 200)
    assert r.output_enabled and not r.overheat and r.knob_fine and not r.remote and not r.locked


def test_parse_lower_case_limit_in_setting_mode() -> None:
    assert parse_status_line("V05.00A0.000W000.0u12I1.00P100F000110").voltage_limit == 12


@pytest.mark.parametrize("reply", ["", "V20.00", "V20.00A2.500W050.0U40I5.00P200F10100", "garbage"])
def test_parse_rejects_bad_reply(reply: str) -> None:
    with pytest.raises(IPSProtocolError):
        parse_status_line(reply)


def test_model_is_case_insensitive_and_checked(t: FakeIPSTransport) -> None:
    assert IsoTechIPS(t, "ips-2010").limits.max_current == 10
    with pytest.raises(ValueError, match="Unknown IPS model"):
        IsoTechIPS(t, "IPS-303")


def test_constructor_reads_instrument(t: FakeIPSTransport) -> None:
    IsoTechIPS(t, "IPS-405")
    assert t.commands == ["L"]


def test_set_voltage_and_current_limit_encoding(dev: IsoTechIPS, t: FakeIPSTransport) -> None:
    dev.set_current_limit(1.25)
    dev.set_voltage(5.5)
    assert [c for c in t.commands if c.startswith("S")] == ["SI 1.25", "SV 05.50"]
    st = dev.status()
    assert (st.voltage_setpoint, st.current_limit) == (5.5, 1.25)


def test_voltage_setpoint_is_read_while_output_is_off(t: FakeIPSTransport) -> None:
    t.voltage = 7.5
    assert IsoTechIPS(t, "IPS-405").status().voltage_setpoint == 7.5


def test_voltage_setpoint_is_unknown_while_output_is_on_before_first_set(t: FakeIPSTransport) -> None:
    t.output = True
    assert IsoTechIPS(t, "IPS-405").status().voltage_setpoint is None


def test_voltage_setpoint_is_examined_while_output_is_off(dev: IsoTechIPS, t: FakeIPSTransport) -> None:
    t.remote = False
    with pytest.raises(RuntimeError, match="did not accept SV 03.30.*REM key"):
        dev.set_voltage(3.3)


def test_voltage_setpoint_is_not_examined_while_output_is_on(dev: IsoTechIPS, t: FakeIPSTransport) -> None:
    dev.set_output(True)
    t.load_ohms = 1.0  # current limit active: the output voltage is lower than the setpoint
    dev.set_current_limit(1.0)
    dev.set_voltage(12.0)
    assert dev.status().voltage_setpoint == 12.0


@pytest.mark.parametrize(
    ("method", "value", "message"),
    [
        ("set_voltage", 40.01, "out of range"),
        ("set_voltage", -1, "out of range"),
        ("set_current_limit", 5.1, "out of range"),
        ("set_voltage_limit", 41, "out of range"),
        ("set_voltage_limit", 12.5, "not a whole number"),
        ("set_power_limit", 201, "out of range"),
    ],
)
def test_out_of_range_rejected_without_command(dev, t, method, value, message) -> None:
    before = list(t.commands)
    with pytest.raises(ValueError, match=message):
        getattr(dev, method)(value)
    assert [c for c in t.commands if c != "L"] == [c for c in before if c != "L"]


def test_power_limit_follows_voltage_and_current_limit(dev: IsoTechIPS) -> None:
    dev.set_voltage_limit(12)
    dev.set_current_limit(0.5)
    assert dev.status().power_limit == 6
    dev.set_voltage_limit(5)
    assert dev.status().power_limit == 2  # 2.5 W, rounded down


def test_power_limit_sets_current_limit(dev: IsoTechIPS) -> None:
    dev.set_voltage_limit(5)
    dev.set_current_limit(0.5)
    dev.set_power_limit(10)
    assert dev.status().current_limit == 2.0  # P / U: the current limit increases


def test_power_limit_above_max_current_is_refused(dev: IsoTechIPS, t: FakeIPSTransport) -> None:
    dev.set_voltage_limit(12)
    with pytest.raises(ValueError, match="current limit to 16.67 A"):
        dev.set_power_limit(200)
    assert not [c for c in t.commands if c.startswith("SP")]


def test_voltage_above_voltage_limit_is_refused(t: FakeIPSTransport) -> None:
    dev = IsoTechIPS(FakeIPSTransport(voltage_limit=12), "IPS-405")
    with pytest.raises(ValueError, match="above the voltage limit 12 V"):
        dev.set_voltage(12.5)


def test_limits_encoding(dev: IsoTechIPS, t: FakeIPSTransport) -> None:
    dev.set_voltage_limit(5)
    dev.set_power_limit(20)
    assert [c for c in t.commands if c.startswith("S")] == ["SU 05", "SP 020"]
    st = dev.status()
    assert (st.voltage_limit, st.power_limit, st.current_limit) == (5, 20, 4.0)


def test_output_is_read_back(dev: IsoTechIPS, t: FakeIPSTransport) -> None:
    dev.set_output(True)
    assert t.commands[-2:] == ["KOE", "L"] and dev.status().output_enabled is True
    dev.set_output(False)
    assert t.commands[-2:] == ["KOD", "L"] and t.output is False


def test_change_without_remote_mode_raises(dev: IsoTechIPS, t: FakeIPSTransport) -> None:
    t.remote = False
    with pytest.raises(RuntimeError, match="did not accept KOE.*REM key"):
        dev.set_output(True)
    with pytest.raises(RuntimeError, match="did not accept SI 1.00"):
        dev.set_current_limit(1.0)


def test_measure_with_load(dev: IsoTechIPS, t: FakeIPSTransport) -> None:
    dev.set_current_limit(0.5)
    dev.set_voltage(12.0)
    dev.set_output(True)
    m = dev.measure()
    assert (m.voltage, m.current, m.power) == (5.0, 0.5, 2.5)  # current limit active


def test_store(dev: IsoTechIPS, t: FakeIPSTransport) -> None:
    dev.set_voltage(3.3)
    dev.store()
    assert t.commands[-1] == "EEP" and t.stored is not None


class _FakeSerial:
    def __init__(self, reply: bytes) -> None:
        self.port = "/dev/ttyS0"
        self.reply = reply
        self.written: list[bytes] = []
        self.reset = 0

    def reset_input_buffer(self) -> None:
        self.reset += 1

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def read_until(self, terminator: bytes) -> bytes:
        assert terminator == b"\n"
        return self.reply


def test_serial_transport_framing() -> None:
    port = _FakeSerial(b"V20.00A2.500W050.0U40I5.00P200F101000\r\n")
    t = IPSSerialTransport(port, set_delay=0)
    assert t.query("L") == "V20.00A2.500W050.0U40I5.00P200F101000"
    t.write("SV 12.34")
    assert port.written == [b"L\r", b"SV 12.34\r"]
    assert port.reset == 1


def test_serial_transport_timeout() -> None:
    t = IPSSerialTransport(_FakeSerial(b""), set_delay=0)
    with pytest.raises(TimeoutError, match="No reply to L"):
        t.query("L")


def test_serial_transport_open_sets_dtr(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = []

    def fake_serial(device, **kwargs):
        port = SimpleNamespace(port=device, dtr=False, kwargs=kwargs)
        opened.append(port)
        return port

    monkeypatch.setattr("lab_mcp.instruments.power_supplies.isotech_ips.serial.Serial", fake_serial)
    IPSSerialTransport.open("/dev/ttyS0")
    [port] = opened
    assert port.dtr is True
    assert port.kwargs["baudrate"] == 2400
