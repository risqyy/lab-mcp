import asyncio
from collections.abc import Iterator

import pytest
import pyvisa
from mcp.server.mcpserver.exceptions import ToolError

from rigol_mcp import server
from rigol_mcp.discovery import DiscoveredInstrument
from rigol_mcp.instruments import DP832

from fake_dp832 import FakeDP832Resource

INFO = DiscoveredInstrument(
    address="USB0::0x1AB1::0x0E11::DP8C000000001::INSTR",
    backend="@py",
    manufacturer="RIGOL TECHNOLOGIES",
    model="DP832",
    serial="DP8C000000001",
    firmware="00.01.16",
)


@pytest.fixture(autouse=True)
def fake_device() -> Iterator[FakeDP832Resource]:
    res = FakeDP832Resource()
    server.connections.set_dp832(DP832(res), INFO)
    yield res
    server.connections.set_dp832(None)


@pytest.fixture
def fake_discovery(monkeypatch: pytest.MonkeyPatch):
    """Disconnect, and let discovery and pyvisa return fakes."""
    server.connections.set_dp832(None)
    found: list[DiscoveredInstrument] = []
    opened: list[FakeDP832Resource] = []

    class FakeRM:
        def __init__(self, backend: str) -> None:
            pass

        def open_resource(self, address: str, **kwargs) -> FakeDP832Resource:
            res = FakeDP832Resource()
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
        "discover_instruments", "dp832_connect", "dp832_identify", "dp832_status", "dp832_measure",
        "dp832_set", "dp832_output", "dp832_all_off", "dp832_set_protection", "dp832_clear_protection",
    } <= names


def test_set_output_measure_roundtrip() -> None:
    result = call("dp832_set", channel=1, voltage=10.0, current=2.0)
    assert result.structured_content["voltage_setpoint"] == 10.0

    result = call("dp832_output", channel=1, enabled=True)
    assert result.structured_content["output_enabled"] is True

    result = call("dp832_measure", channel=1)
    [m] = result.structured_content["result"]
    assert (m["voltage"], m["current"]) == (10.0, 1.0)


def test_status_all_channels() -> None:
    result = call("dp832_status")
    assert [s["channel"] for s in result.structured_content["result"]] == [1, 2, 3]


def test_identify_returns_connection_info() -> None:
    assert call("dp832_identify").structured_content["serial"] == "DP8C000000001"


def test_out_of_range_reaches_model_with_reason() -> None:
    with pytest.raises(ToolError, match="out of range") as exc_info:
        call("dp832_set", channel=3, voltage=12.0)
    assert type(exc_info.value) is ToolError  # not UnexpectedToolError, so the message is shown


def test_auto_connects_on_first_call(fake_discovery) -> None:
    found, opened = fake_discovery
    found.append(INFO)
    call("dp832_set", channel=2, voltage=3.3)
    assert opened[0].writes == [":SOUR2:VOLT 3.300"]


def test_auto_connect_picks_serial(fake_discovery) -> None:
    found, _ = fake_discovery
    other = INFO.model_copy(update={"serial": "DP8C000000002", "address": "TCPIP0::192.0.2.10::INSTR"})
    found.extend([other, INFO])
    assert call("dp832_identify").structured_content["serial"] == "DP8C000000001"
    assert call("dp832_connect", serial="DP8C000000002").structured_content["address"] == other.address


def test_no_instrument_found_message(fake_discovery) -> None:
    with pytest.raises(ToolError, match="No DP832 found"):
        call("dp832_identify")


def test_reconnects_after_communication_error(fake_discovery, monkeypatch: pytest.MonkeyPatch) -> None:
    found, opened = fake_discovery
    found.append(INFO)
    call("dp832_status", channel=1)

    def broken(msg: str) -> str:
        raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)

    monkeypatch.setattr(opened[0], "query", broken)
    with pytest.raises(ToolError, match="Communication with the instrument failed"):
        call("dp832_status", channel=1)

    call("dp832_status", channel=1)
    assert len(opened) == 2


@pytest.mark.parametrize(
    "arguments",
    [{"channel": 4, "enabled": True}, {"channel": 0, "enabled": True}, {"channel": "x", "enabled": True}],
)
def test_invalid_arguments_rejected_by_schema(arguments: dict, fake_device: FakeDP832Resource) -> None:
    with pytest.raises(ToolError):
        call("dp832_output", **arguments)
    assert fake_device.writes == []


def test_input_schema_exposes_limits() -> None:
    [tool] = [t for t in asyncio.run(server.mcp.list_tools()) if t.name == "dp832_set"]
    props = tool.input_schema["properties"]
    assert props["channel"]["minimum"] == 1 and props["channel"]["maximum"] == 3
    voltage = next(s for s in props["voltage"]["anyOf"] if s.get("type") == "number")
    assert voltage["maximum"] == 30


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
