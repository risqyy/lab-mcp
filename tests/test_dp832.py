import pytest
from pydantic import ValidationError

from rigol_mcp.instruments import DP832

from fake_dp832 import FakeDP832Resource


@pytest.fixture
def res() -> FakeDP832Resource:
    return FakeDP832Resource()


@pytest.fixture
def dev(res: FakeDP832Resource) -> DP832:
    return DP832(res)


def test_identify(dev: DP832) -> None:
    assert dev.identify().startswith("RIGOL TECHNOLOGIES,DP832")


def test_set_voltage_and_current(dev: DP832, res: FakeDP832Resource) -> None:
    dev.set_voltage(1, 12.0)
    dev.set_current(1, 0.5)
    assert res.writes == [":SOUR1:VOLT 12.000", ":SOUR1:CURR 0.500"]
    assert dev.get_voltage_setpoint(1) == 12.0
    assert dev.get_current_setpoint(1) == 0.5


@pytest.mark.parametrize(
    ("channel", "volts"), [(1, 30.1), (2, -0.1), (3, 5.01)]
)
def test_voltage_out_of_range_is_rejected_without_writing(
    dev: DP832, res: FakeDP832Resource, channel: int, volts: float
) -> None:
    with pytest.raises(ValueError, match="out of range"):
        dev.set_voltage(channel, volts)
    assert res.writes == []


def test_invalid_channel(dev: DP832) -> None:
    with pytest.raises(ValueError, match="Invalid channel 4"):
        dev.set_output(4, True)


def test_output_and_measure(dev: DP832) -> None:
    dev.set_voltage(2, 5.0)
    dev.set_current(2, 1.0)
    dev.set_output(2, True)
    assert dev.get_output(2) is True
    m = dev.measure(2)
    assert (m.voltage, m.current, m.power) == (5.0, 0.5, 2.5)


def test_all_outputs_off(dev: DP832) -> None:
    for ch in dev.channels:
        dev.set_output(ch, True)
    dev.all_outputs_off()
    assert not any(dev.get_output(ch) for ch in dev.channels)


def test_protection_and_status(dev: DP832, res: FakeDP832Resource) -> None:
    dev.set_ovp(3, 5.2)
    dev.set_ocp(3, 1.5)
    res.state[3]["OCP_TRIP"] = True
    st = dev.status(3)
    assert (st.ovp_enabled, st.ovp_level, st.ocp_enabled, st.ocp_level) == (True, 5.2, True, 1.5)
    assert st.ocp_tripped is True
    dev.clear_protection(3)
    assert dev.status(3).ocp_tripped is False


def test_ovp_above_channel_maximum_rejected(dev: DP832) -> None:
    with pytest.raises(ValueError):
        dev.set_ovp(3, 6.0)


def test_unexpected_mode_reply_fails_validation(dev: DP832, res: FakeDP832Resource, monkeypatch) -> None:
    original = res.query
    monkeypatch.setattr(res, "query", lambda msg: "XX" if msg.startswith(":OUTP:MODE?") else original(msg))
    with pytest.raises(ValidationError):
        dev.status(1)


def test_models_are_frozen(dev: DP832) -> None:
    m = dev.measure(1)
    with pytest.raises(ValidationError):
        m.voltage = 1.0
