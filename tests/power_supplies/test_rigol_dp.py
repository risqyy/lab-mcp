import pytest
from pydantic import ValidationError

from lab_mcp.instruments.power_supplies import RigolDP

from fake_rigol_dp import FakeRigolDPResource


@pytest.fixture
def res() -> FakeRigolDPResource:
    return FakeRigolDPResource()


@pytest.fixture
def dev(res: FakeRigolDPResource) -> RigolDP:
    return RigolDP(res, "DP832")


def test_identify(dev: RigolDP) -> None:
    assert dev.identify().startswith("RIGOL TECHNOLOGIES,DP832")


def test_set_voltage_and_current(dev: RigolDP, res: FakeRigolDPResource) -> None:
    dev.set_voltage(1, 12.0)
    dev.set_current(1, 0.5)
    assert res.writes == [":SOUR1:VOLT 12.000", ":SOUR1:CURR 0.500"]
    assert dev.get_voltage_setpoint(1) == 12.0
    assert dev.get_current_setpoint(1) == 0.5


@pytest.mark.parametrize(
    ("channel", "volts"), [(1, 30.1), (2, -0.1), (3, 5.01)]
)
def test_voltage_out_of_range_is_rejected_without_writing(
    dev: RigolDP, res: FakeRigolDPResource, channel: int, volts: float
) -> None:
    with pytest.raises(ValueError, match="out of range"):
        dev.set_voltage(channel, volts)
    assert res.writes == []


def test_invalid_channel(dev: RigolDP) -> None:
    with pytest.raises(ValueError, match="Invalid channel 4"):
        dev.set_output(4, True)


def test_output_and_measure(dev: RigolDP) -> None:
    dev.set_voltage(2, 5.0)
    dev.set_current(2, 1.0)
    dev.set_output(2, True)
    assert dev.get_output(2) is True
    m = dev.measure(2)
    assert (m.voltage, m.current, m.power) == (5.0, 0.5, 2.5)


def test_all_outputs_off(dev: RigolDP) -> None:
    for ch in dev.channels:
        dev.set_output(ch, True)
    dev.all_outputs_off()
    assert not any(dev.get_output(ch) for ch in dev.channels)


def test_protection_and_status(dev: RigolDP, res: FakeRigolDPResource) -> None:
    dev.set_ovp(3, 5.2)
    dev.set_ocp(3, 1.5)
    res.state[3]["OCP_TRIP"] = True
    st = dev.status(3)
    assert (st.ovp_enabled, st.ovp_level, st.ocp_enabled, st.ocp_level) == (True, 5.2, True, 1.5)
    assert st.ocp_tripped is True
    dev.clear_protection(3)
    assert dev.status(3).ocp_tripped is False


def test_clear_protection_keeps_output_off(dev: RigolDP, res: FakeRigolDPResource) -> None:
    # A trip switches the output off. Clearing the trip must not switch it on again.
    res.state[1].update(OVP_TRIP=True, OCP_TRIP=True, OUT=False)
    dev.clear_protection(1)
    st = dev.status(1)
    assert (st.ovp_tripped, st.ocp_tripped, st.output_enabled) == (False, False, False)
    assert res.writes[-2:] == [":OUTPut:OVP:CLEar CH1", ":OUTPut:OCP:CLEar CH1"]


def test_ovp_above_channel_maximum_rejected(dev: RigolDP) -> None:
    with pytest.raises(ValueError):
        dev.set_ovp(3, 6.0)


def test_unexpected_mode_reply_fails_validation(dev: RigolDP, res: FakeRigolDPResource, monkeypatch) -> None:
    original = res.query
    monkeypatch.setattr(res, "query", lambda msg: "XX" if msg.startswith(":OUTP:MODE?") else original(msg))
    with pytest.raises(ValidationError):
        dev.status(1)


def test_models_are_frozen(dev: RigolDP) -> None:
    m = dev.measure(1)
    with pytest.raises(ValidationError):
        m.voltage = 1.0


# -- models and DP900 --------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "channel", "ok", "too_high"),
    [
        ("DP832", 1, 30.0, 30.1),
        ("DP832", 3, 5.0, 5.1),
        ("DP932A", 1, 32.0, 32.1),
        ("DP932A", 3, 6.0, 6.1),
        ("DP932U", 2, 32.0, 32.1),
        ("DP932E", 1, 30.0, 30.1),
    ],
)
def test_voltage_limits_for_each_model(model: str, channel: int, ok: float, too_high: float) -> None:
    res = FakeRigolDPResource(model)
    dev = RigolDP(res, model)
    dev.set_voltage(channel, ok)
    with pytest.raises(ValueError, match="out of range"):
        dev.set_voltage(channel, too_high)
    assert dev.get_voltage_setpoint(channel) == ok


def test_dp932a_ovp_limits() -> None:
    dev = RigolDP(FakeRigolDPResource("DP932A"), "DP932A")
    dev.set_ovp(1, 35.2)
    dev.set_ovp(3, 6.6)
    with pytest.raises(ValueError):
        dev.set_ovp(3, 6.7)


def test_unknown_model_is_refused() -> None:
    with pytest.raises(ValueError, match="Unsupported model 'DP811'"):
        RigolDP(FakeRigolDPResource(), "DP811")


def test_dp900_status_with_numeric_booleans() -> None:
    res = FakeRigolDPResource("DP932A")
    dev = RigolDP(res, "DP932A")
    dev.set_ovp(2, 10.0)
    dev.set_output(2, True)
    res.state[2]["OCP_TRIP"] = True
    st = dev.status(2)
    assert (st.output_enabled, st.ovp_enabled, st.ocp_tripped, st.tracking, st.pair_mode) == (True, True, True, False, "OFF")
    assert st.limits.max_voltage == 32.0


def test_dp800_does_not_query_pair_mode(dev: RigolDP, res: FakeRigolDPResource) -> None:
    # The DP800 does not reply to :OUTP:PAIR? (the fake raises TimeoutError).
    assert dev.status(1).pair_mode is None
    dev.set_voltage(1, 5.0)


@pytest.mark.parametrize("model", ["DP832", "DP932A"])
def test_tracking_blocks_changes_on_ch1_and_ch2(model: str) -> None:
    res = FakeRigolDPResource(model)
    dev = RigolDP(res, model)
    res.tracking = True
    for action in (lambda: dev.set_voltage(1, 5.0), lambda: dev.set_current(2, 1.0),
                   lambda: dev.set_output(1, True), lambda: dev.set_ovp(2, 10.0)):
        with pytest.raises(ValueError, match="tracking is on"):
            action()
    assert not any(w.startswith((":SOUR1", ":SOUR2", ":OUTP CH1,ON", ":OUTP CH2,ON")) for w in res.writes)
    dev.set_voltage(3, 3.3)  # CH3 is not coupled
    dev.set_output(1, False)  # switching off is always permitted
    assert dev.status(1).tracking is True and dev.status(3).tracking is False


@pytest.mark.parametrize("pair", ["SERIES", "PARALLEL"])
def test_dp900_pair_mode_blocks_changes_on_ch1_and_ch2(pair: str) -> None:
    res = FakeRigolDPResource("DP932A")
    dev = RigolDP(res, "DP932A")
    res.pair = pair
    with pytest.raises(ValueError, match=f"connected in {pair.lower()}"):
        dev.set_voltage(1, 5.0)
    with pytest.raises(ValueError, match=f"connected in {pair.lower()}"):
        dev.set_output(2, True)
    dev.all_outputs_off()
    assert dev.status(1).pair_mode == pair


def test_dp900_clear_protection_keeps_output_off() -> None:
    res = FakeRigolDPResource("DP932A")
    dev = RigolDP(res, "DP932A")
    res.state[3].update(OVP_TRIP=True, OUT=False)
    dev.clear_protection(3)
    st = dev.status(3)
    assert (st.ovp_tripped, st.output_enabled) == (False, False)
