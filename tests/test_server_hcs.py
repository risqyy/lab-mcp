import asyncio
from collections.abc import Iterator

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments import DP832, MansonHCS

from fake_dp832 import FakeDP832Resource
from fake_hcs import FakeHCSTransport

DP832_INFO = DiscoveredInstrument(
    address="USB0::0x1AB1::0x0E11::DP8C000000001::INSTR", backend="@py",
    manufacturer="RIGOL TECHNOLOGIES", model="DP832", serial="DP8C000000001", firmware="00.01.16",
)
HCS_INFO = DiscoveredInstrument(
    address="COM7", backend="serial", manufacturer="MANSON", model="HCS-3404", serial="0001", firmware=""
)


@pytest.fixture(autouse=True)
def fake_hcs() -> Iterator[FakeHCSTransport]:
    t = FakeHCSTransport()
    server.hcs.set(MansonHCS(t), HCS_INFO)
    yield t
    server.hcs.set(None)


@pytest.fixture
def fake_dp832() -> Iterator[FakeDP832Resource]:
    res = FakeDP832Resource()
    server.dp832.set(DP832(res), DP832_INFO)
    yield res
    server.dp832.set(None)


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_hcs_tools_are_registered() -> None:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "all_outputs_off", "hcs_connect", "hcs_identify", "hcs_status", "hcs_measure", "hcs_set",
        "hcs_output", "hcs_set_upper_limits", "hcs_presets", "hcs_load_preset",
    } <= names


def test_hcs_set_output_measure(fake_hcs: FakeHCSTransport) -> None:
    st = call("hcs_set", voltage=12.0, current=2.0).structured_content
    assert (st["voltage_setpoint"], st["current_setpoint"], st["output_enabled"]) == (12.0, 2.0, None)
    st = call("hcs_output", enabled=True).structured_content
    assert st["output_enabled"] is True
    assert "SOUT0" in fake_hcs.commands
    m = call("hcs_measure").structured_content
    assert (m["voltage"], m["current"], m["mode"]) == (12.0, 1.2, "CV")


def test_hcs_limit_of_model_reaches_model() -> None:
    # The schema allows up to 40 A (HCS family). The HCS-3404 allows 10 A.
    with pytest.raises(ToolError, match=r"out of range 0..10.0 A"):
        call("hcs_set", current=15.0)


def test_hcs_schema_rejects_values_above_family_limit(fake_hcs: FakeHCSTransport) -> None:
    before = list(fake_hcs.commands)
    with pytest.raises(ToolError):
        call("hcs_set", voltage=61.0)
    assert fake_hcs.commands == before


def test_hcs_presets_and_upper_limits() -> None:
    presets = call("hcs_presets").structured_content["result"]
    assert [p["voltage"] for p in presets] == [5.0, 12.0, 24.0]
    assert call("hcs_load_preset", index=2).structured_content["voltage_setpoint"] == 24.0
    st = call("hcs_set_upper_limits", voltage=30.0).structured_content
    assert st["upper_voltage_limit"] == 30.0


def test_hcs_auto_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    server.hcs.set(None)
    t = FakeHCSTransport()
    calls = []

    def fake_discover(**kwargs):
        calls.append(kwargs)
        return [HCS_INFO]

    monkeypatch.setattr(server.discovery, "discover", fake_discover)
    monkeypatch.setattr(server.SerialTransport, "open", classmethod(lambda cls, device: t))
    assert call("hcs_identify").structured_content["address"] == "COM7"
    assert calls[0]["skip_serial_ports"] == []
    assert server._open_serial_ports() == ["COM7"]


def test_hcs_reconnects_after_communication_error(monkeypatch: pytest.MonkeyPatch, fake_hcs) -> None:
    def broken(command: str):
        raise TimeoutError("No reply to GETD from COM7")

    monkeypatch.setattr(fake_hcs, "request", broken)
    with pytest.raises(ToolError, match="Communication with the instrument failed"):
        call("hcs_measure")
    assert server.hcs.connected_info is None


def test_all_outputs_off_switches_every_instrument(fake_dp832: FakeDP832Resource, fake_hcs) -> None:
    call("dp832_output", channel=1, enabled=True)
    call("hcs_output", enabled=True)
    results = call("all_outputs_off").structured_content["result"]
    assert [r["result"] for r in results] == ["off", "off"]
    assert not any(s["OUT"] for s in fake_dp832.state.values())
    assert fake_hcs.output is False


def test_all_outputs_off_reports_missing_instrument(fake_dp832, monkeypatch: pytest.MonkeyPatch) -> None:
    server.hcs.set(None)
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    results = call("all_outputs_off").structured_content["result"]
    assert results[0]["result"] == "off"
    assert results[1]["instrument"] == "Manson HCS"
    assert results[1]["result"].startswith("not found")


def test_discover_instruments_lists_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [DP832_INFO])
    serials = [i["serial"] for i in call("discover_instruments").structured_content["result"]]
    assert serials == ["0001", "DP8C000000001"]
