import asyncio
from collections.abc import Iterator

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.signal_generators import GWInstekAFG2000

from fake_gwinstek_afg2000 import FakeAFGTransport

AFG_INFO = DiscoveredInstrument(
    address="COM8", backend="serial", manufacturer="GW INSTEK",
    model="AFG-2105", serial="TEST0001", firmware="V2.02",
)


@pytest.fixture(autouse=True)
def fake_afg(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAFGTransport]:
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    transport = FakeAFGTransport()
    server.afg.set(GWInstekAFG2000(transport, "AFG-2105"), AFG_INFO)
    yield transport
    server.afg.close()


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_signal_tools_are_registered():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert {"sig_connect", "sig_identify", "sig_status", "sig_set", "sig_output"} <= tools.keys()
    assert tools["sig_status"].annotations.read_only_hint
    assert tools["sig_identify"].annotations.read_only_hint
    assert "twice" in tools["sig_status"].description
    assert "output state does not change" in tools["sig_set"].description


def test_identify_and_status(fake_afg):
    assert call("sig_identify").structured_content["serial"] == "TEST0001"
    status = call("sig_status").structured_content
    assert (status["load"], status["amplitude_vpp"], status["output_enabled"]) == ("high_z", 1, False)
    assert fake_afg.writes == []


def test_set_and_output_roundtrip():
    status = call("sig_set", waveform="square", frequency_hz=2000, amplitude_vpp=2,
                  offset_v=0.2, duty_cycle_percent=25).structured_content
    assert (status["waveform"], status["frequency_hz"], status["amplitude_vpp"], status["offset_v"],
            status["duty_cycle_percent"], status["output_enabled"]) == ("square", 2000, 2, 0.2, 25, False)
    assert call("sig_output", enabled=True).structured_content["output_enabled"] is True
    assert call("sig_output", enabled=False).structured_content["output_enabled"] is False
    status = call("sig_set", waveform="ramp", symmetry_percent=50, load="50_ohm").structured_content
    assert status["symmetry_percent"] == 50 and status["load"] == "50_ohm"


@pytest.mark.parametrize("arguments", [
    {"frequency_hz": -1}, {"amplitude_vpp": float("nan")}, {"offset_v": float("inf")},
    {"waveform": "pulse"}, {"symmetry_percent": 101}, {"load": "auto"},
])
def test_schema_rejects_invalid_settings(fake_afg, arguments):
    with pytest.raises(ToolError):
        call("sig_set", **arguments)
    assert fake_afg.writes == []


def test_driver_validation_reaches_client(fake_afg):
    with pytest.raises(ToolError, match="out of range"):
        call("sig_set", amplitude_vpp=10, offset_v=6)
    assert fake_afg.writes == []


def test_auto_connect_and_skip_open_ports(monkeypatch):
    server.afg.set(None)
    transport = FakeAFGTransport()
    calls = []

    def discover(**kwargs):
        calls.append(kwargs)
        if "match" in kwargs:
            assert kwargs["match"](AFG_INFO)
        return [AFG_INFO]

    monkeypatch.setattr(server.discovery, "discover", discover)
    monkeypatch.setattr(server.AFGSerialTransport, "open", classmethod(lambda cls, device: transport))
    assert call("sig_identify").structured_content["address"] == "COM8"
    assert calls[0]["skip_serial_ports"] == []
    assert server._open_serial_ports() == ["COM8"]
    call("discover_instruments", scan_lan=False)
    assert calls[-1]["skip_serial_ports"] == ["COM8"]


@pytest.mark.parametrize("selector", [{"serial": "TEST0001"}, {"port": "COM8"}])
def test_explicit_connect(monkeypatch, selector):
    server.afg.set(None)
    transport = FakeAFGTransport()
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [AFG_INFO])
    monkeypatch.setattr(server.afg, "_identify_address", lambda address: AFG_INFO)
    monkeypatch.setattr(server.AFGSerialTransport, "open", classmethod(lambda cls, device: transport))
    assert call("sig_connect", **selector).structured_content["model"] == "AFG-2105"


def test_multiple_generators_require_selection(monkeypatch):
    server.afg.set(None)
    second = AFG_INFO.model_copy(update={"address": "COM9", "serial": "TEST0002"})
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [AFG_INFO, second])
    with pytest.raises(ToolError, match="More than one.*TEST0001.*TEST0002"):
        call("sig_identify")


def test_wrong_instrument_is_not_connected(monkeypatch):
    wrong = AFG_INFO.model_copy(update={"manufacturer": "RIGOL", "model": "DP832"})
    monkeypatch.setattr(server.afg, "_identify_address", lambda address: wrong)
    with pytest.raises(ToolError, match="not a GW Instek AFG"):
        call("sig_connect", port="COM8")


def test_missing_generator_message():
    server.afg.set(None)
    with pytest.raises(ToolError, match="No GW Instek AFG found.*USB CDC"):
        call("sig_identify")


def test_communication_error_closes_slot_and_next_call_reconnects(monkeypatch):
    server.afg.set(None)
    transports = []

    def open_transport(cls, device):
        transport = FakeAFGTransport()
        transports.append(transport)
        return transport

    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [AFG_INFO])
    monkeypatch.setattr(server.AFGSerialTransport, "open", classmethod(open_transport))
    call("sig_identify")

    def broken(command):
        raise TimeoutError("no reply")

    monkeypatch.setattr(transports[0], "query", broken)
    with pytest.raises(ToolError, match="Communication with the instrument failed"):
        call("sig_status")
    assert not transports[0].is_open
    assert server.afg.connected_info is None
    call("sig_status")
    assert len(transports) == 2


def test_closed_transport_reconnects_before_command(monkeypatch, fake_afg):
    fake_afg.close()
    fresh = FakeAFGTransport()
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [AFG_INFO])
    monkeypatch.setattr(server.AFGSerialTransport, "open", classmethod(lambda cls, device: fresh))
    call("sig_set", amplitude_vpp=2)
    assert fake_afg.writes == []
    assert fresh.writes == ["SOURce1:AMPlitude 2VPP"]


def test_all_outputs_off_includes_generator(fake_afg):
    fake_afg.output = True
    results = call("all_outputs_off").structured_content["result"]
    assert results[-1] == {"instrument": "GW Instek AFG", "serial": "TEST0001", "result": "off"}
    assert not fake_afg.output


def test_all_outputs_off_reports_generator_communication_failure(monkeypatch, fake_afg):
    def broken(command):
        raise TimeoutError("output query failed")

    monkeypatch.setattr(fake_afg, "query", broken)
    results = call("all_outputs_off").structured_content["result"]
    assert results[-1]["result"].startswith("communication failed")
    assert fake_afg.writes == ["OUTPut OFF"]
    assert server.afg.connected_info is None


def test_all_outputs_off_does_not_call_device_errors_not_found(fake_afg):
    fake_afg.output = True
    fake_afg.errors.append("-222")
    results = call("all_outputs_off").structured_content["result"]
    assert results[-1]["result"].startswith("command failed")
    assert not fake_afg.output
