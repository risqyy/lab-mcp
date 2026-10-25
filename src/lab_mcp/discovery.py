"""Discovery of laboratory instruments without configuration.

Discovery has three phases:

1. VISA search on all usable backends (an installed IVI VISA, for example NI-VISA,
   first, then pyvisa-py): USB-TMC devices with the Rigol vendor ID, and LAN
   instruments that answer the VXI-11 broadcast. Each candidate gets ``*IDN?``.
2. If phase 1 finds no instrument: a scan of the local /24 networks for TCP port 5555
   (Rigol raw SCPI). This is for networks that block the VXI-11 broadcast.
3. Serial ports of known USB-serial bridges (Manson HCS). Each candidate gets ``GMOD``.
   Other serial ports are not used, because a probe sends data to the port.

Discovery returns only instruments that answer.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import warnings
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

import psutil
import pyvisa
from pydantic import BaseModel, ConfigDict
from serial.tools import list_ports

from lab_mcp.instruments.manson_hcs import SerialTransport

log = logging.getLogger(__name__)

RIGOL_USB_VENDOR_IDS = ("0X1AB1", "6833")  # hex and decimal forms in VISA addresses, upper case
RIGOL_SCPI_PORT = 5555
IDN_TIMEOUT_MS = 2000
SCAN_CONNECT_TIMEOUT_S = 0.3
SCAN_WORKERS = 128
# USB-serial bridges in Manson HCS power supplies, as (vendor ID, product ID).
HCS_USB_IDS = {(0x10C4, 0xEA60)}  # Silicon Labs CP210x
SERIAL_PROBE_TIMEOUT_S = 0.5
SUPPORTED_MANUFACTURERS = ("RIGOL", "MANSON")


class DiscoveredInstrument(BaseModel):
    model_config = ConfigDict(frozen=True)

    address: str
    backend: str
    manufacturer: str
    model: str
    serial: str
    firmware: str

    @property
    def is_rigol(self) -> bool:
        return self.manufacturer.upper().startswith("RIGOL")

    @property
    def is_manson(self) -> bool:
        return self.manufacturer.upper().startswith("MANSON")

    @property
    def is_supported(self) -> bool:
        return self.manufacturer.upper().startswith(SUPPORTED_MANUFACTURERS)


def available_backends() -> list[str]:
    """VISA backends that can actually be opened, preferred first."""
    backends = []
    for backend in ("@ivi", "@py"):
        try:
            pyvisa.ResourceManager(backend).close()
        except (OSError, ValueError, pyvisa.Error) as exc:
            log.debug("VISA backend %s unavailable: %s", backend, exc)
        else:
            backends.append(backend)
    return backends


def discover(
    *,
    scan_lan: bool = True,
    match: Callable[[DiscoveredInstrument], bool] = lambda inst: inst.is_supported,
    skip_serial_ports: Iterable[str] = (),
) -> list[DiscoveredInstrument]:
    """Find instruments with VISA, with a LAN scan if VISA finds none, and on serial ports.

    skip_serial_ports: ports that are open already. A second open of a serial port fails.
    """
    found: dict[str, DiscoveredInstrument] = {}  # keyed by serial number to merge USB/LAN duplicates
    seen_hosts: set[str] = set()

    for backend in available_backends():
        for address in _candidate_addresses(backend):
            host = _host_of(address)
            if host:
                seen_hosts.add(host)
            inst = identify(address, backend)
            if inst is not None and match(inst):
                found.setdefault(inst.serial, inst)

    if scan_lan and not found:
        hosts = [h for h in scan_for_scpi_hosts() if h not in seen_hosts]
        for host in hosts:
            inst = identify(f"TCPIP0::{host}::{RIGOL_SCPI_PORT}::SOCKET", "@py")
            if inst is not None and match(inst):
                found.setdefault(inst.serial, inst)

    for inst in discover_serial(skip_ports=skip_serial_ports):
        if match(inst):
            found.setdefault(inst.serial, inst)

    return list(found.values())


def discover_serial(skip_ports: Iterable[str] = ()) -> list[DiscoveredInstrument]:
    """Find Manson HCS power supplies on the serial ports of known USB-serial bridges."""
    skip = {p.upper() for p in skip_ports}
    found = []
    for port in list_ports.comports():
        if (port.vid, port.pid) not in HCS_USB_IDS or port.device.upper() in skip:
            continue
        inst = identify_hcs(port.device, usb_serial=port.serial_number)
        if inst is not None:
            found.append(inst)
    return found


def identify_hcs(device: str, usb_serial: str | None = None) -> DiscoveredInstrument | None:
    """Send GMOD to a serial port. Return the instrument, or None if no HCS answers."""
    try:
        transport = SerialTransport.open(device, timeout=SERIAL_PROBE_TIMEOUT_S)
    except OSError as exc:
        log.debug("Cannot open %s: %s", device, exc)
        return None
    try:
        model = transport.request("GMOD")
    except OSError as exc:  # includes TimeoutError and HCSProtocolError
        log.debug("No HCS reply on %s: %s", device, exc)
        return None
    finally:
        transport.close()
    if not model or not model.upper().startswith("HCS"):
        log.debug("Device on %s is not an HCS: %r", device, model)
        return None
    return DiscoveredInstrument(
        address=device, backend="serial", manufacturer="MANSON",
        model=model, serial=usb_serial or device, firmware="",
    )


def identify(address: str, backend: str) -> DiscoveredInstrument | None:
    """Open ``address`` and parse its ``*IDN?`` reply; None if it does not answer."""
    try:
        rm = pyvisa.ResourceManager(backend)
        res = rm.open_resource(
            address, read_termination="\n", write_termination="\n", timeout=IDN_TIMEOUT_MS
        )
        try:
            reply = res.query("*IDN?").strip()
        finally:
            res.close()
    except (OSError, ValueError, pyvisa.Error) as exc:
        log.debug("No *IDN? reply from %s via %s: %s", address, backend, exc)
        return None
    parts = [p.strip() for p in reply.split(",")]
    if len(parts) != 4:
        log.debug("Unparseable *IDN? reply from %s: %r", address, reply)
        return None
    manufacturer, model, serial, firmware = parts
    return DiscoveredInstrument(
        address=address, backend=backend, manufacturer=manufacturer,
        model=model, serial=serial, firmware=firmware,
    )


def _candidate_addresses(backend: str) -> list[str]:
    try:
        rm = pyvisa.ResourceManager(backend)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # pyvisa-py warns about every missing optional transport
            resources = rm.list_resources("?*")
    except (OSError, ValueError, pyvisa.Error) as exc:
        log.debug("Listing resources on %s failed: %s", backend, exc)
        return []
    # Serial ports and GPIB are skipped: sending *IDN? to unknown serial devices is not safe.
    return [r for r in resources if _is_rigol_usb(r) or r.upper().startswith("TCPIP")]


def _is_rigol_usb(address: str) -> bool:
    parts = address.upper().split("::")
    return parts[0].startswith("USB") and len(parts) > 1 and parts[1] in RIGOL_USB_VENDOR_IDS


def _host_of(address: str) -> str | None:
    m = re.match(r"TCPIP\d*::([^:]+)::", address, re.IGNORECASE)
    return m[1] if m else None


def local_scan_networks() -> list[ipaddress.IPv4Network]:
    """Private IPv4 networks of the local interfaces, each narrowed to the /24 around our address."""
    networks: set[ipaddress.IPv4Network] = set()
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            iface = ipaddress.IPv4Interface(f"{a.address}/{a.netmask}")
            if not iface.ip.is_private or iface.ip.is_loopback or iface.ip.is_link_local:
                continue
            net = iface.network
            if net.prefixlen < 24:
                net = ipaddress.IPv4Interface(f"{a.address}/24").network
            networks.add(net)
    return sorted(networks)


def scan_for_scpi_hosts(
    networks: Iterable[ipaddress.IPv4Network] | None = None, port: int = RIGOL_SCPI_PORT
) -> list[str]:
    """Hosts in ``networks`` (default: local /24s) accepting TCP connections on ``port``."""
    hosts = [str(h) for net in (networks or local_scan_networks()) for h in net.hosts()]
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        open_flags = list(pool.map(lambda h: _port_open(h, port), hosts))
    return [h for h, is_open in zip(hosts, open_flags) if is_open]


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=SCAN_CONNECT_TIMEOUT_S):
            return True
    except OSError:
        return False
