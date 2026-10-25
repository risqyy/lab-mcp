import asyncio
from collections.abc import Iterator

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from rigol_mcp import server
from rigol_mcp.instruments import DP832

from fake_dp832 import FakeDP832Resource


@pytest.fixture(autouse=True)
def fake_device(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDP832Resource]:
    monkeypatch.delenv("RIGOL_DP832_RESOURCE", raising=False)
    res = FakeDP832Resource()
    server.connections.set_dp832(DP832(res))
    yield res
    server.connections.set_dp832(None)


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_tools_are_registered() -> None:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "list_visa_resources", "dp832_connect", "dp832_identify", "dp832_status", "dp832_measure",
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


def test_out_of_range_reaches_model_with_reason() -> None:
    with pytest.raises(ToolError, match="out of range") as exc_info:
        call("dp832_set", channel=3, voltage=12.0)
    assert type(exc_info.value) is ToolError  # not UnexpectedToolError, so the message is shown


def test_not_connected_message() -> None:
    server.connections.set_dp832(None)
    with pytest.raises(ToolError, match="dp832_connect"):
        call("dp832_identify")


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
