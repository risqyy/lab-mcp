import asyncio
from collections.abc import Iterator

import pytest
import pyvisa
from mcp.server.mcpserver.exceptions import ToolError

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.power_supplies import RigolDP

from fake_rigol_dp import FakeRigolDPResource

INFO = DiscoveredInstrument(
    address="USB0::0x1AB1::0x0E11::DP8C000000001::INSTR",
    backend="@py",
    manufacturer="RIGOL TECHNOLOGIES",
    model="DP832",
    serial="DP8C000000001",
    firmware="00.01.16",
)


@pytest.fixture(autouse=True)
def fake_device() -> Iterator[FakeRigolDPResource]:
    res = FakeRigolDPResource()
    server.rigol_dp.set(RigolDP(res, "DP832"), INFO)
    yield res
    server.rigol_dp.set(None)


@pytest.fixture
def fake_discovery(monkeypatch: pytest.MonkeyPatch):
    """Disconnect, and let discovery and pyvisa return fakes."""
    server.rigol_dp.set(None)
    found: list[DiscoveredInstrument] = []
    opened: list[FakeRigolDPResource] = []

    class FakeRM:
        def __init__(self, backend: str) -> None:
            pass

        def open_resource(self, address: str, **kwargs) -> FakeRigolDPResource:
            res = FakeRigolDPResource()
            opened.append(res)
            return res

    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: list(found))
    monkeypatch.setattr(server.pyvisa, "ResourceManager", FakeRM)
    return found, opened


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_tools_are_registered() -> None:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "discover_instruments", "dp_connect", "dp_identify", "dp_status", "dp_measure",
        "dp_set", "dp_output", "dp_all_off", "dp_set_protection", "dp_clear_protection",
    } <= names


def test_set_output_measure_roundtrip() -> None:
    result = call("dp_set", channel=1, voltage=10.0, current=2.0)
    assert result.structured_content["voltage_setpoint"] == 10.0

    result = call("dp_output", channel=1, enabled=True)
    assert result.structured_content["output_enabled"] is True

    result = call("dp_measure", channel=1)
    [m] = result.structured_content["result"]
    assert (m["voltage"], m["current"]) == (10.0, 1.0)


def test_status_all_channels() -> None:
    result = call("dp_status")
    assert [s["channel"] for s in result.structured_content["result"]] == [1, 2, 3]


def test_identify_returns_connection_info() -> None:
    assert call("dp_identify").structured_content["serial"] == "DP8C000000001"


def test_out_of_range_reaches_model_with_reason() -> None:
    with pytest.raises(ToolError, match="out of range") as exc_info:
        call("dp_set", channel=3, voltage=12.0)
    assert type(exc_info.value) is ToolError  # not UnexpectedToolError, so the message is shown


def test_auto_connects_on_first_call(fake_discovery) -> None:
    found, opened = fake_discovery
    found.append(INFO)
    call("dp_set", channel=2, voltage=3.3)
    assert opened[0].writes == [":SOUR2:VOLT 3.300"]


def test_more_than_one_instrument_needs_serial(fake_discovery) -> None:
    # The server must not select one of two power supplies by itself.
    found, opened = fake_discovery
    other = INFO.model_copy(update={"serial": "DP8C000000002", "address": "TCPIP0::192.0.2.10::INSTR"})
    found.extend([other, INFO])
    with pytest.raises(ToolError, match="More than one Rigol DP found: DP832 DP8C000000001 .*DP832 DP8C000000002"):
        call("dp_set", channel=1, voltage=1.0)
    assert opened == []
    assert call("dp_connect", serial="DP8C000000002").structured_content["address"] == other.address
    assert call("dp_identify").structured_content["serial"] == "DP8C000000002"


def test_no_instrument_found_message(fake_discovery) -> None:
    with pytest.raises(ToolError, match="No Rigol DP found"):
        call("dp_identify")


def test_reconnects_after_communication_error(fake_discovery, monkeypatch: pytest.MonkeyPatch) -> None:
    found, opened = fake_discovery
    found.append(INFO)
    call("dp_status", channel=1)

    def broken(msg: str) -> str:
        raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)

    monkeypatch.setattr(opened[0], "query", broken)
    with pytest.raises(ToolError, match="Communication with the instrument failed"):
        call("dp_status", channel=1)

    call("dp_status", channel=1)
    assert len(opened) == 2


def test_reconnects_when_session_was_closed(fake_discovery) -> None:
    # A closed ResourceManager closes the session (pyvisa: "Invalid session handle").
    # The server connects again before the command and sends the command one time.
    found, opened = fake_discovery
    found.append(INFO)
    call("dp_status", channel=1)
    opened[0].close()

    result = call("dp_set", channel=1, voltage=3.3)
    assert result.structured_content["voltage_setpoint"] == 3.3
    assert len(opened) == 2
    assert opened[1].writes == [":SOUR1:VOLT 3.300"]
    assert ":SOUR1:VOLT 3.300" not in opened[0].writes


@pytest.mark.parametrize(
    "arguments",
    [{"channel": 4, "enabled": True}, {"channel": 0, "enabled": True}, {"channel": "x", "enabled": True}],
)
def test_invalid_arguments_rejected_by_schema(arguments: dict, fake_device: FakeRigolDPResource) -> None:
    with pytest.raises(ToolError):
        call("dp_output", **arguments)
    assert fake_device.writes == []


def test_input_schema_exposes_limits() -> None:
    [tool] = [t for t in asyncio.run(server.mcp.list_tools()) if t.name == "dp_set"]
    props = tool.input_schema["properties"]
    assert props["channel"]["minimum"] == 1 and props["channel"]["maximum"] == 3
    # No fixed maximum: the limits depend on the model. The driver checks them.
    voltage = next(s for s in props["voltage"]["anyOf"] if s.get("type") == "number")
    assert voltage["minimum"] == 0 and "maximum" not in voltage


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], (("streamable-http",), {"host": "127.0.0.1", "port": 10765})),
        (["--host", "0.0.0.0", "--port", "9000"], (("streamable-http",), {"host": "0.0.0.0", "port": 9000})),
        (["--transport", "stdio"], ((), {})),
    ],
)
def test_main_transport_arguments(argv: list[str], expected, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    server.main(argv)
    assert calls == [expected]


def test_auto_connect_dp932a_uses_model_limits(fake_discovery, monkeypatch: pytest.MonkeyPatch) -> None:
    found, _ = fake_discovery
    found.append(INFO.model_copy(update={"model": "DP932A", "serial": "DP9A000000001"}))
    res = FakeRigolDPResource("DP932A")

    class RM:
        def __init__(self, backend: str) -> None:
            pass

        def open_resource(self, address: str, **kwargs) -> FakeRigolDPResource:
            return res

    monkeypatch.setattr(server.pyvisa, "ResourceManager", RM)
    st = call("dp_set", channel=1, voltage=32.0).structured_content
    assert (st["voltage_setpoint"], st["limits"]["max_voltage"], st["pair_mode"]) == (32.0, 32.0, "OFF")
    with pytest.raises(ToolError, match=r"out of range 0..32.0 V"):
        call("dp_set", channel=1, voltage=32.1)


def test_mixed_dp832_and_dp932a_need_serial(fake_discovery) -> None:
    found, _ = fake_discovery
    found.extend([INFO, INFO.model_copy(update={"model": "DP932A", "serial": "DP9A000000001"})])
    with pytest.raises(ToolError, match="More than one Rigol DP found: DP832 DP8C000000001 .*DP932A DP9A000000001"):
        call("dp_status")
