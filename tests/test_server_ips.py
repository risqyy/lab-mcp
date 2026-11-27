import asyncio
from collections.abc import Iterator

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.power_supplies import IsoTechIPS

from fake_isotech_ips import FakeIPSTransport

IPS_INFO = DiscoveredInstrument(
    address="/dev/ttyS0", backend="serial", manufacturer="ISO-TECH", model="IPS-405", serial="/dev/ttyS0",
    firmware="",
)


@pytest.fixture(autouse=True)
def fake_ips(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIPSTransport]:
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    server.ips_ports.clear()
    t = FakeIPSTransport()
    server.ips.set(IsoTechIPS(t, "IPS-405"), IPS_INFO)
    yield t
    server.ips.set(None)
    server.ips_ports.clear()


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_ips_tools_are_registered() -> None:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "ips_connect", "ips_identify", "ips_status", "ips_measure", "ips_set",
        "ips_set_limits", "ips_output", "ips_store",
    } <= names


def test_ips_set_output_measure(fake_ips: FakeIPSTransport) -> None:
    st = call("ips_set", voltage=12.0, current=2.0).structured_content
    assert (st["voltage_setpoint"], st["current_limit"], st["output_enabled"]) == (12.0, 2.0, False)
    assert call("ips_output", enabled=True).structured_content["output_enabled"] is True
    m = call("ips_measure").structured_content
    assert (m["voltage"], m["current"], m["power"]) == (12.0, 1.2, 14.4)
    assert call("ips_output", enabled=False).structured_content["output_enabled"] is False
    assert fake_ips.output is False


def test_ips_limit_of_model_reaches_model(fake_ips: FakeIPSTransport) -> None:
    with pytest.raises(ToolError, match=r"out of range 0..5 A"):
        call("ips_set", current=6.0)
    with pytest.raises(ToolError, match=r"out of range 0..40 V"):
        call("ips_set", voltage=41.0)
    assert not [c for c in fake_ips.commands if c.startswith("S")]


def test_ips_set_limits(fake_ips: FakeIPSTransport) -> None:
    st = call("ips_set_limits", voltage=15, power=100).structured_content
    assert (st["voltage_limit"], st["power_limit"]) == (15, 100)
    with pytest.raises(ToolError, match="above the voltage limit 15 V"):
        call("ips_set", voltage=20.0)


def test_ips_schema_rejects_fraction_for_limits(fake_ips: FakeIPSTransport) -> None:
    with pytest.raises(ToolError):
        call("ips_set_limits", voltage=12.5)
    assert not [c for c in fake_ips.commands if c.startswith("SU")]


def test_ips_without_remote_mode_reports_hint(fake_ips: FakeIPSTransport) -> None:
    fake_ips.remote = False
    with pytest.raises(ToolError, match="REM key"):
        call("ips_output", enabled=True)


def test_ips_store(fake_ips: FakeIPSTransport) -> None:
    call("ips_store")
    assert fake_ips.commands[-2:] == ["EEP", "L"]


def test_ips_is_not_found_without_configuration() -> None:
    server.ips.set(None)
    with pytest.raises(ToolError, match="No ISO-TECH IPS found.*ips_connect"):
        call("ips_status")


def test_ips_connect_with_port_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    server.ips.set(None)
    t = FakeIPSTransport()
    probed = []

    def identify(device, model):
        probed.append((device, model))
        return IPS_INFO

    monkeypatch.setattr(server.discovery, "identify_ips", identify)
    monkeypatch.setattr(server.IPSSerialTransport, "open", classmethod(lambda cls, device: t))
    info = call("ips_connect", port="/dev/ttyS0", model="IPS-405").structured_content
    assert (info["address"], info["model"], info["manufacturer"]) == ("/dev/ttyS0", "IPS-405", "ISO-TECH")
    assert probed == [("/dev/ttyS0", "IPS-405")]
    assert server.ips_ports == {"/dev/ttyS0": "IPS-405"}


def test_ips_connect_failure_keeps_previous_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    server.ips_ports["/dev/ttyS0"] = "IPS-405"
    monkeypatch.setattr(server.discovery, "identify_ips", lambda device, model: None)
    with pytest.raises(ToolError, match="No instrument answers at /dev/ttyS1"):
        call("ips_connect", port="/dev/ttyS1", model="IPS-603")
    assert server.ips_ports == {"/dev/ttyS0": "IPS-405"}


@pytest.mark.parametrize("arguments", [{"port": "/dev/ttyS0"}, {"model": "IPS-405"}])
def test_ips_connect_needs_port_and_model(arguments) -> None:
    with pytest.raises(ToolError, match="together"):
        call("ips_connect", **arguments)


def test_ips_auto_connect_uses_configured_port(monkeypatch: pytest.MonkeyPatch) -> None:
    server.ips.set(None)
    server.ips_ports["/dev/ttyS0"] = "IPS-405"
    calls = []

    def discover(**kwargs):
        calls.append(kwargs)
        return [IPS_INFO]

    t = FakeIPSTransport()
    monkeypatch.setattr(server.discovery, "discover", discover)
    monkeypatch.setattr(server.IPSSerialTransport, "open", classmethod(lambda cls, device: t))
    assert call("ips_identify").structured_content["model"] == "IPS-405"
    assert calls[0]["ips_ports"] == {"/dev/ttyS0": "IPS-405"}


def test_ips_reconnects_after_communication_error(monkeypatch: pytest.MonkeyPatch, fake_ips) -> None:
    def broken(command: str):
        raise TimeoutError("No reply to L from /dev/ttyS0")

    monkeypatch.setattr(fake_ips, "query", broken)
    with pytest.raises(ToolError, match="Communication with the instrument failed"):
        call("ips_measure")
    assert server.ips.connected_info is None


def test_main_sets_ips_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.mcp, "run", lambda *args, **kwargs: None)
    server.main(["--ips-port", "/dev/ttyS0", "--ips-model", "ips-405"])
    assert server.ips_ports == {"/dev/ttyS0": "IPS-405"}


@pytest.mark.parametrize("argv", [["--ips-port", "/dev/ttyS0"], ["--ips-model", "IPS-405"]])
def test_main_needs_ips_port_and_model_together(argv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.mcp, "run", lambda *args, **kwargs: None)
    with pytest.raises(SystemExit):
        server.main(argv)
