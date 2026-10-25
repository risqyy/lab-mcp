import ipaddress
import socket
from types import SimpleNamespace

import pytest

from lab_mcp import discovery
from lab_mcp.discovery import DiscoveredInstrument


def inst(address: str, serial: str, model: str = "DP832", manufacturer: str = "RIGOL TECHNOLOGIES"):
    return DiscoveredInstrument(
        address=address, backend="@py", manufacturer=manufacturer, model=model, serial=serial, firmware="1.0"
    )


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("USB0::0x1AB1::0x0E11::DP8C000000001::INSTR", True),
        ("USB0::6833::3601::DP8C000000001::INSTR", True),
        ("USB0::0x0957::0x1796::MY000000::INSTR", False),
        ("TCPIP0::192.0.2.10::INSTR", False),
    ],
)
def test_is_rigol_usb(address: str, expected: bool) -> None:
    assert discovery._is_rigol_usb(address) is expected


def test_host_of() -> None:
    assert discovery._host_of("TCPIP0::192.0.2.10::5555::SOCKET") == "192.0.2.10"
    assert discovery._host_of("USB0::0x1AB1::0x0E11::X::INSTR") is None


@pytest.fixture
def fake_visa(monkeypatch: pytest.MonkeyPatch):
    """Fake VISA layer: addresses per backend and *IDN? results per address."""
    state = SimpleNamespace(
        backends=["@py"], addresses={"@py": []}, idn={}, scanned=[], scan_called=False, serial=[]
    )
    monkeypatch.setattr(discovery, "available_backends", lambda: state.backends)
    monkeypatch.setattr(discovery, "_candidate_addresses", lambda backend: state.addresses.get(backend, []))
    monkeypatch.setattr(discovery, "identify", lambda address, backend: state.idn.get(address))

    def fake_scan(*_args, **_kwargs):
        state.scan_called = True
        return state.scanned

    monkeypatch.setattr(discovery, "scan_for_scpi_hosts", fake_scan)
    monkeypatch.setattr(discovery, "discover_serial", lambda skip_ports=(): state.serial)
    return state


def test_discover_merges_usb_and_lan_by_serial(fake_visa) -> None:
    usb, lan = "USB0::0x1AB1::0x0E11::A::INSTR", "TCPIP0::192.0.2.10::INSTR"
    fake_visa.addresses["@py"] = [usb, lan]
    fake_visa.idn = {usb: inst(usb, "A"), lan: inst(lan, "A")}
    assert [i.address for i in discovery.discover()] == [usb]
    assert fake_visa.scan_called is False


def test_discover_filters_non_rigol(fake_visa) -> None:
    lan = "TCPIP0::192.0.2.11::INSTR"
    fake_visa.addresses["@py"] = [lan]
    fake_visa.idn = {lan: inst(lan, "K", model="34465A", manufacturer="Keysight Technologies")}
    assert discovery.discover(scan_lan=False) == []


def test_discover_falls_back_to_lan_scan(fake_visa) -> None:
    fake_visa.scanned = ["192.0.2.20", "192.0.2.21"]
    addr = "TCPIP0::192.0.2.21::5555::SOCKET"
    fake_visa.idn = {addr: inst(addr, "B")}
    assert [i.serial for i in discovery.discover()] == ["B"]
    assert fake_visa.scan_called is True


def test_discover_without_scan(fake_visa) -> None:
    fake_visa.scanned = ["192.0.2.20"]
    assert discovery.discover(scan_lan=False) == []
    assert fake_visa.scan_called is False


def test_local_scan_networks_limits_to_slash_24(monkeypatch: pytest.MonkeyPatch) -> None:
    def addr(ip: str, mask: str):
        return SimpleNamespace(family=socket.AF_INET, address=ip, netmask=mask)

    monkeypatch.setattr(
        discovery.psutil,
        "net_if_addrs",
        lambda: {
            "eth0": [addr("10.1.2.3", "255.255.0.0")],
            "wifi": [addr("192.168.7.5", "255.255.255.0")],
            "lo": [addr("127.0.0.1", "255.0.0.0")],
            "apipa": [addr("169.254.1.1", "255.255.0.0")],
            "public": [addr("8.8.8.8", "255.255.255.0")],
        },
    )
    assert discovery.local_scan_networks() == [
        ipaddress.IPv4Network("10.1.2.0/24"),
        ipaddress.IPv4Network("192.168.7.0/24"),
    ]


def test_scan_finds_listening_port() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        hosts = discovery.scan_for_scpi_hosts([ipaddress.IPv4Network("127.0.0.0/30")], port=port)
    assert hosts == ["127.0.0.1"]


def test_available_backends_keeps_resource_manager_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # pyvisa shares one ResourceManager per backend. close() also closes the connections of the server.
    closed = []

    class FakeRM:
        def __init__(self, backend: str) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(discovery.pyvisa, "ResourceManager", FakeRM)
    assert discovery.available_backends() == ["@ivi", "@py"]
    assert closed == []
