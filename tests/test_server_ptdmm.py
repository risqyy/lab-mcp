import asyncio
from collections.abc import Iterator

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.multimeters import PeakTech4365

from fake_peaktech_4365 import FakePTTransport

PT_INFO = DiscoveredInstrument(
    address="/dev/ttyUSB0", backend="serial", manufacturer="PEAKTECH", model="4365", serial="/dev/ttyUSB0",
    firmware="",
)


@pytest.fixture(autouse=True)
def fake_pt(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePTTransport]:
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    server.ptdmm_ports.clear()
    t = FakePTTransport(["ac_v"])
    server.ptdmm.set(PeakTech4365(t), PT_INFO)
    yield t
    server.ptdmm.set(None)
    server.ptdmm_ports.clear()


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_ptdmm_tools_are_registered() -> None:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"ptdmm_connect", "ptdmm_identify", "ptdmm_read", "ptdmm_read_series"} <= names


def test_ptdmm_read() -> None:
    r = call("ptdmm_read").structured_content
    assert (r["function"], r["value"], r["unit"]) == ("ac_voltage", 7.3, "V")
    assert r["secondary"]["frequency"]["value"] == 147.0


def test_ptdmm_read_series(fake_pt: FakePTTransport) -> None:
    s = call("ptdmm_read_series", count=3, interval_s=0).structured_content
    assert (s["count"], s["mean"], s["unit"]) == (3, 7.3, "V")


def test_ptdmm_series_duration_is_limited() -> None:
    with pytest.raises(ToolError, match="maximum is 300 s"):
        call("ptdmm_read_series", count=200, interval_s=2)


def test_ptdmm_is_not_found_without_configuration() -> None:
    server.ptdmm.set(None)
    with pytest.raises(ToolError, match="No PeakTech 4365 found.*ptdmm_connect"):
        call("ptdmm_read")


def test_ptdmm_connect_with_port(monkeypatch: pytest.MonkeyPatch) -> None:
    server.ptdmm.set(None)
    probed = []

    def identify(device):
        probed.append(device)
        return PT_INFO

    monkeypatch.setattr(server.discovery, "identify_ptdmm", identify)
    monkeypatch.setattr(server.PTSerialTransport, "open", classmethod(lambda cls, device: FakePTTransport()))
    info = call("ptdmm_connect", port="/dev/ttyUSB0").structured_content
    assert (info["manufacturer"], info["model"], info["address"]) == ("PEAKTECH", "4365", "/dev/ttyUSB0")
    assert probed == ["/dev/ttyUSB0"]
    assert server.ptdmm_ports == ["/dev/ttyUSB0"]


def test_ptdmm_connect_failure_keeps_previous_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    server.ptdmm_ports[:] = ["/dev/ttyUSB0"]
    monkeypatch.setattr(server.discovery, "identify_ptdmm", lambda device: None)
    with pytest.raises(ToolError, match="No instrument answers at /dev/ttyUSB9"):
        call("ptdmm_connect", port="/dev/ttyUSB9")
    assert server.ptdmm_ports == ["/dev/ttyUSB0"]


def test_ptdmm_auto_connect_uses_configured_port(monkeypatch: pytest.MonkeyPatch) -> None:
    server.ptdmm.set(None)
    server.ptdmm_ports[:] = ["/dev/ttyUSB0"]
    calls = []

    def discover(**kwargs):
        calls.append(kwargs)
        return [PT_INFO]

    monkeypatch.setattr(server.discovery, "discover", discover)
    monkeypatch.setattr(server.PTSerialTransport, "open", classmethod(lambda cls, device: FakePTTransport()))
    assert call("ptdmm_identify").structured_content["model"] == "4365"
    assert calls[0]["ptdmm_ports"] == ["/dev/ttyUSB0"]


def test_ptdmm_reconnects_after_communication_error(monkeypatch: pytest.MonkeyPatch, fake_pt) -> None:
    def broken():
        raise TimeoutError("No reply from /dev/ttyUSB0")

    monkeypatch.setattr(fake_pt, "query", broken)
    with pytest.raises(ToolError, match="Communication with the instrument failed"):
        call("ptdmm_read")
    assert server.ptdmm.connected_info is None


def test_main_sets_ptdmm_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.mcp, "run", lambda *args, **kwargs: None)
    server.main(["--ptdmm-port", "/dev/ttyUSB0"])
    assert server.ptdmm_ports == ["/dev/ttyUSB0"]
