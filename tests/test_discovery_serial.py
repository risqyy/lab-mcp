from types import SimpleNamespace

import pytest

from lab_mcp import discovery
from lab_mcp.discovery import DiscoveredInstrument


def inst(address: str, serial: str, model: str = "DP832", manufacturer: str = "RIGOL TECHNOLOGIES"):
    backend = "serial" if manufacturer == "MANSON" else "@py"
    return DiscoveredInstrument(
        address=address, backend=backend, manufacturer=manufacturer, model=model, serial=serial, firmware=""
    )


@pytest.fixture
def fake_phases(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(addresses=[], idn={}, serial=[], scan_called=False)
    monkeypatch.setattr(discovery, "available_backends", lambda: ["@py"])
    monkeypatch.setattr(discovery, "_candidate_addresses", lambda backend: state.addresses)
    monkeypatch.setattr(discovery, "identify", lambda address, backend: state.idn.get(address))
    monkeypatch.setattr(discovery, "discover_serial", lambda skip_ports=(): state.serial)

    def fake_scan(*_args, **_kwargs):
        state.scan_called = True
        return []

    monkeypatch.setattr(discovery, "scan_for_scpi_hosts", fake_scan)
    return state


def test_discover_includes_serial_instruments(fake_phases) -> None:
    lan = "TCPIP0::192.0.2.10::INSTR"
    fake_phases.addresses = [lan]
    fake_phases.idn = {lan: inst(lan, "A")}
    fake_phases.serial = [inst("COM7", "HCS1", model="HCS-3404", manufacturer="MANSON")]
    assert [i.serial for i in discovery.discover()] == ["A", "HCS1"]


def test_serial_instrument_does_not_stop_lan_scan(fake_phases) -> None:
    fake_phases.serial = [inst("COM7", "HCS1", model="HCS-3404", manufacturer="MANSON")]
    discovery.discover()
    assert fake_phases.scan_called is True


def _port(device: str, vid: int | None, pid: int | None, serial: str | None = "SN1"):
    return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=serial)


def test_discover_serial_probes_only_known_bridges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery.list_ports,
        "comports",
        lambda: [
            _port("COM3", 0x0483, 0x374E),  # ST-Link
            _port("COM4", 0x0403, 0x6015),  # FTDI
            _port("COM5", None, None),  # Bluetooth
            _port("COM7", 0x10C4, 0xEA60, "0001"),  # CP210x
            _port("COM8", 0x10C4, 0xEA60, "0002"),  # CP210x, open already
        ],
    )
    probed = []

    def fake_identify(device, usb_serial=None):
        probed.append(device)
        return inst(device, usb_serial, model="HCS-3404", manufacturer="MANSON")

    monkeypatch.setattr(discovery, "identify_hcs", fake_identify)
    found = discovery.discover_serial(skip_ports=["com8"])
    assert probed == ["COM7"]
    assert [(i.address, i.serial) for i in found] == [("COM7", "0001")]


class _ProbeTransport:
    def __init__(self, reply):
        self.reply = reply
        self.closed = False

    def request(self, command: str):
        assert command == "GMOD"
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("reply", "expected_model"),
    [("HCS-3404", "HCS-3404"), ("SOMETHING", None), (TimeoutError("no reply"), None), (None, None)],
)
def test_identify_hcs(monkeypatch: pytest.MonkeyPatch, reply, expected_model) -> None:
    transport = _ProbeTransport(reply)
    monkeypatch.setattr(discovery.SerialTransport, "open", classmethod(lambda cls, device, timeout: transport))
    found = discovery.identify_hcs("COM7", usb_serial="0001")
    assert (found.model if found else None) == expected_model
    assert transport.closed is True
    if found:
        assert (found.manufacturer, found.backend, found.serial) == ("MANSON", "serial", "0001")


def test_identify_hcs_port_cannot_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(cls, device, timeout):
        raise OSError("access denied")

    monkeypatch.setattr(discovery.SerialTransport, "open", classmethod(fail))
    assert discovery.identify_hcs("COM7") is None
