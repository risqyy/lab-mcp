from types import SimpleNamespace

import pytest

from lab_mcp import discovery
from lab_mcp.discovery import DiscoveredInstrument


def inst(address: str, serial: str, model: str = "DP832", manufacturer: str = "RIGOL TECHNOLOGIES"):
    backend = "serial" if manufacturer in ("MANSON", "ISO-TECH") else "@py"
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


def test_discovery_uses_each_protocol_only_on_its_usb_id(monkeypatch):
    monkeypatch.setattr(discovery.list_ports, "comports", lambda: [
        _port("COM7", 0x10C4, 0xEA60), _port("COM8", 0x2184, 0x001C),
        _port("COM9", 0x2184, 0x001C), _port("COM10", 0x2184, 0x9999),
        _port("COM11", 0x0403, 0x6015),
    ])
    probed = []

    def hcs(device, usb_serial=None):
        probed.append((device, "GMOD"))
        return inst(device, "HCS1", model="HCS-3404", manufacturer="MANSON")

    def afg(device):
        probed.append((device, "*IDN?"))
        return inst(device, "AFG1", model="AFG-2105", manufacturer="GW INSTEK")

    monkeypatch.setattr(discovery, "identify_hcs", hcs)
    monkeypatch.setattr(discovery, "identify_afg", afg)
    assert len(discovery.discover_serial(skip_ports=["com9"])) == 2
    assert probed == [("COM7", "GMOD"), ("COM8", "*IDN?")]


@pytest.mark.parametrize("reply,expected", [
    ("GW INSTEK,AFG-2105,SN:TEST0001,V2.02", "TEST0001"),
    ("gw instek,afg-2105,TEST0002,V2.02", "TEST0002"),
    ("GW INSTEK,AFG-2105,SN:,V2.02", None),
    ("GW INSTEK,AFG-2125,SN:TEST0001,V2.02", None),
    ("OTHER,AFG-2105,SN:TEST0001,V2.02", None),
    ("broken reply", None), (TimeoutError("no reply"), None),
])
def test_identify_afg_closes_probe_and_normalizes_serial(monkeypatch, reply, expected):
    class Probe:
        closed = False

        def query(self, command):
            assert command == "*IDN?"
            if isinstance(reply, Exception):
                raise reply
            return reply

        def close(self):
            self.closed = True

    probe = Probe()
    monkeypatch.setattr(discovery.AFGSerialTransport, "open", classmethod(lambda cls, device, timeout: probe))
    info = discovery.identify_afg("COM8")
    assert probe.closed
    assert (info.serial if info else None) == expected
    if info:
        assert info.backend == "serial" and info.is_gwinstek and info.is_supported


def test_identify_afg_port_cannot_open(monkeypatch):
    def fail(cls, device, timeout):
        raise OSError("access denied")

    monkeypatch.setattr(discovery.AFGSerialTransport, "open", classmethod(fail))
    assert discovery.identify_afg("COM8") is None


@pytest.mark.parametrize("reply,found", [
    ("V20.00A2.500W050.0U40I5.00P200F101000", True),
    ("HCS-3404", False),
    (TimeoutError("no reply"), False),
])
def test_identify_ips_closes_probe(monkeypatch, reply, found):
    class Probe:
        closed = False

        def query(self, command):
            assert command == "L"
            if isinstance(reply, Exception):
                raise reply
            return reply

        def close(self):
            self.closed = True

    probe = Probe()
    monkeypatch.setattr(discovery.IPSSerialTransport, "open", classmethod(lambda cls, device, timeout: probe))
    info = discovery.identify_ips("/dev/ttyS0", "ips-405")
    assert probe.closed
    assert (info is not None) == found
    if info:
        assert (info.manufacturer, info.model, info.serial, info.backend) == (
            "ISO-TECH", "IPS-405", "/dev/ttyS0", "serial",
        )
        assert info.is_isotech and info.is_supported


def test_identify_ips_unknown_model_sends_nothing(monkeypatch):
    def fail(cls, device, timeout):
        raise AssertionError("port must not be opened")

    monkeypatch.setattr(discovery.IPSSerialTransport, "open", classmethod(fail))
    assert discovery.identify_ips("/dev/ttyS0", "IPS-303") is None


def test_identify_ips_port_cannot_open(monkeypatch):
    def fail(cls, device, timeout):
        raise OSError("access denied")

    monkeypatch.setattr(discovery.IPSSerialTransport, "open", classmethod(fail))
    assert discovery.identify_ips("/dev/ttyS0", "IPS-405") is None


def test_discover_probes_only_configured_ips_ports(fake_phases, monkeypatch):
    probed = []

    def identify(device, model):
        probed.append((device, model))
        return inst(device, device, model=model, manufacturer="ISO-TECH")

    monkeypatch.setattr(discovery, "identify_ips", identify)
    assert discovery.discover() == []
    assert probed == []
    found = discovery.discover(
        ips_ports={"/dev/ttyS0": "IPS-405", "/dev/ttyS1": "IPS-603"}, skip_serial_ports=["/dev/ttys1"]
    )
    assert probed == [("/dev/ttyS0", "IPS-405")]
    assert [(i.address, i.model) for i in found] == [("/dev/ttyS0", "IPS-405")]
