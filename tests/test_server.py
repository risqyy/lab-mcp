"""Shared server behavior. Device-specific tools are tested in test_server_<type>.py."""

import asyncio
from types import SimpleNamespace

import pytest

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.multimeters import RigolDM3058
from lab_mcp.instruments.power_supplies import MansonHCS, RigolDP
from lab_mcp.instruments.signal_generators import GWInstekAFG2000

from fake_gwinstek_afg2000 import FakeAFGTransport
from fake_manson_hcs import FakeHCSTransport
from fake_rigol_dm3058 import FakeDM3058Resource
from fake_rigol_dp import FakeRigolDPResource


@pytest.fixture
def instruments(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    devices = SimpleNamespace(
        dp=FakeRigolDPResource(), hcs=FakeHCSTransport(),
        dmm=FakeDM3058Resource(), afg=FakeAFGTransport(),
    )
    for slot, device, info in (
        (server.rigol_dp, RigolDP(devices.dp, "DP832"), DiscoveredInstrument(
            address="USB0::0x1AB1::0x0E11::DP1::INSTR", backend="@py",
            manufacturer="RIGOL", model="DP832", serial="DP1", firmware="",
        )),
        (server.hcs, MansonHCS(devices.hcs), DiscoveredInstrument(
            address="COM7", backend="serial", manufacturer="MANSON",
            model="HCS-3404", serial="HCS1", firmware="",
        )),
        (server.dm3058, RigolDM3058(devices.dmm), DiscoveredInstrument(
            address="USB0::0x1AB1::0x09C4::DMM1::INSTR", backend="@py",
            manufacturer="RIGOL", model="DM3058E", serial="DMM1", firmware="",
        )),
        (server.afg, GWInstekAFG2000(devices.afg, "AFG-2105"), DiscoveredInstrument(
            address="COM8", backend="serial", manufacturer="GW INSTEK",
            model="AFG-2105", serial="AFG1", firmware="V2.02",
        )),
    ):
        slot.set(device, info)
    for state in devices.dp.state.values():
        state["OUT"] = True
    devices.hcs.output = devices.afg.output = True
    yield devices
    for slot in server.SLOTS:
        slot.close()


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_all_outputs_off_switches_sources_and_leaves_multimeter_unchanged(instruments):
    results = call("all_outputs_off").structured_content["result"]
    assert {r["instrument"]: (r["serial"], r["result"]) for r in results} == {
        "Rigol DP": ("DP1", "off"),
        "Manson HCS": ("HCS1", "off"),
        "GW Instek AFG": ("AFG1", "off"),
    }
    assert not any(state["OUT"] for state in instruments.dp.state.values())
    assert instruments.hcs.output is False
    assert instruments.afg.output is False
    assert instruments.dmm.writes == []


@pytest.mark.parametrize("error,reason", [
    (None, "not found"),
    (RuntimeError("command refused"), "command failed"),
    (TimeoutError("no reply"), "communication failed"),
])
def test_all_outputs_off_continues_after_failure(instruments, monkeypatch, error, reason):
    if error is None:
        server.rigol_dp.close()
    else:
        def fail(message):
            raise error

        monkeypatch.setattr(instruments.dp, "write", fail)

    results = {r["instrument"]: r["result"] for r in call("all_outputs_off").structured_content["result"]}
    assert results["Rigol DP"].startswith(reason)
    assert results["Manson HCS"] == results["GW Instek AFG"] == "off"
    assert instruments.hcs.output is False
    assert instruments.afg.output is False
    if isinstance(error, OSError):
        assert server.rigol_dp.connected_info is None


def test_discovery_merges_connected_devices_and_skips_open_serial_ports(instruments, monkeypatch):
    other = server.rigol_dp.connected_info.model_copy(update={"serial": "DP2", "address": "TCPIP0::192.0.2.10::INSTR"})
    calls = []

    def discover(**kwargs):
        calls.append(kwargs)
        return [server.afg.connected_info, other]

    monkeypatch.setattr(server.discovery, "discover", discover)
    results = call("discover_instruments", scan_lan=False).structured_content["result"]
    assert sorted(i["serial"] for i in results) == ["AFG1", "DMM1", "DP1", "DP2", "HCS1"]
    assert len(calls) == 1
    assert calls[0]["scan_lan"] is False
    assert set(calls[0]["skip_serial_ports"]) == {"COM7", "COM8"}


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
