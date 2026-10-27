"""MCP server for laboratory instruments.

By default, the server uses Streamable HTTP at http://127.0.0.1:10765/mcp.
The server needs no configuration. At the first tool call for an instrument type,
it finds the instrument automatically (see ``lab_mcp.discovery``). If the connection
is lost, the server finds the instrument again at the next tool call.
"""

from __future__ import annotations

import argparse
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Generic, TypeVar

import pyvisa
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from lab_mcp import discovery
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.multimeters import DM3058_MODELS, RigolDM3058
from lab_mcp.instruments.oscilloscopes import SDS_MODELS, SiglentSDS1000XE
from lab_mcp.instruments.power_supplies import RIGOL_DP_MODELS, MansonHCS, RigolDP, SerialTransport
from lab_mcp.instruments.signal_generators import AFG_MODELS, AFGSerialTransport, GWInstekAFG2000

IO_TIMEOUT_MS = 5000
# A multimeter query waits for the reading. Autorange of a resistance took 2.4 s in a test.
DMM_IO_TIMEOUT_MS = 10000
# Each read of a waveform block must complete in this time.
SCOPE_IO_TIMEOUT_MS = 10000
# The server has no authentication. Thus it accepts only local connections by default.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10765

READ_ONLY = ToolAnnotations(read_only_hint=True)

mcp = MCPServer(
    "lab-mcp",
    instructions=(
        "This server controls laboratory instruments. The server finds the instruments automatically. "
        "Rigol DP800 and DP900 series (DP832, DP932A, ...): DC power supplies with three outputs, tools dp_*. "
        "Read the limits of each channel with dp_status. "
        "Manson HCS series: DC power supply with one output, tools hcs_*. "
        "Read the limits of the HCS model with hcs_status. "
        "Rigol DM3058 and DM3058E: digital multimeter, tools dmm_*. "
        "GW Instek AFG-2105: signal generator, tools sig_*. "
        "Read sig_status for the load setting and voltage limits before setting or enabling a signal. "
        "SIGLENT SDS1104X-E: oscilloscope with four channels, tools scope_*. "
        "Read scope_status before you change settings. "
        "Set the voltage and the current limit before you switch an output on. "
        "Read the status before you switch an output on. "
        "After a change, wait 1 s before you measure. "
        "If you are not sure, use all_outputs_off."
    ),
)

T = TypeVar("T")

# Message for each instrument type when discovery finds no instrument.
RIGOL_DP_HINT = (
    "For USB, connect the cable and install a USB-TMC driver (NI-VISA, or WinUSB on Windows). "
    "For LAN, connect the instrument to the same network as this computer. "
    "Close other programs that use the instrument."
)
DMM_HINT = (
    "Connect the USB cable and install a USB-TMC driver (NI-VISA, or WinUSB on Windows). "
    "The DM3058E has no LAN interface. Close other programs that use the instrument."
)
HCS_HINT = (
    "Set the MODE switch on the rear panel to Normal. Connect the USB cable. "
    "Make sure that Windows shows a COM port for the instrument (Silicon Labs CP210x driver). "
    "Close other programs that use the port."
)
AFG_HINT = (
    "Connect the USB cable. Make sure that the operating system shows a serial port for the "
    "GW Instek AFG (USB CDC, not USB-TMC). Close other programs that use the port."
)
SCOPE_HINT = (
    "Connect the USB cable and install a USB-TMC driver (NI-VISA, or WinUSB on Windows). "
    "For LAN, connect the instrument to the same network as this computer. "
    "Close other programs that use the instrument, for example EasyScopeX."
)


def _is_rigol_dp(inst: DiscoveredInstrument) -> bool:
    return inst.is_rigol and inst.model.upper() in RIGOL_DP_MODELS


def _is_dm3058(inst: DiscoveredInstrument) -> bool:
    return inst.is_rigol and inst.model.upper() in DM3058_MODELS


def _is_hcs(inst: DiscoveredInstrument) -> bool:
    return inst.is_manson and inst.model.upper().startswith("HCS")


def _is_afg(inst: DiscoveredInstrument) -> bool:
    return inst.is_gwinstek and inst.model.upper() in AFG_MODELS


def _is_sds(inst: DiscoveredInstrument) -> bool:
    return inst.is_siglent and inst.model.upper() in SDS_MODELS


class _Slot(Generic[T]):
    """Connection to one instrument of one type."""

    def __init__(
        self,
        name: str,
        match: Callable[[DiscoveredInstrument], bool],
        open_device: Callable[[DiscoveredInstrument], tuple[T, Callable[[], None]]],
        identify_address: Callable[[str], DiscoveredInstrument | None],
        hint: str,
        is_open: Callable[[T], bool] = lambda _device: True,
    ) -> None:
        self.name = name
        self._match = match
        self._open_device = open_device
        self._identify_address = identify_address
        self._hint = hint
        self._is_open = is_open
        self._device: T | None = None
        self._info: DiscoveredInstrument | None = None
        self._close: Callable[[], None] | None = None

    def connect(self, serial: str | None = None, address: str | None = None) -> DiscoveredInstrument:
        """Connect to the instrument.

        Without arguments, discovery must find exactly one instrument of this type. If it
        finds more, the serial number is necessary: the server does not select one, because
        a command to the wrong instrument can be dangerous.
        """
        if address is not None:
            info = self._identify_address(address)
            if info is None:
                raise RuntimeError(f"No instrument answers at {address}.")
        else:
            found = sorted(
                discovery.discover(match=self._match, skip_serial_ports=_open_serial_ports()),
                key=lambda i: i.serial,
            )
            if serial is not None:
                found = [i for i in found if i.serial == serial]
            if not found:
                what = f"No {self.name} with serial number {serial} found." if serial else f"No {self.name} found."
                raise RuntimeError(
                    f"{what} Make sure that the instrument is on. {self._hint} "
                    "Use discover_instruments to see all instruments that the server finds."
                )
            if len(found) > 1:
                listed = ", ".join(f"{i.model} {i.serial} ({i.address})" for i in found)
                raise RuntimeError(
                    f"More than one {self.name} found: {listed}. "
                    f"Select one with the serial parameter of the connect tool."
                )
            info = found[0]
        if not self._match(info):
            raise RuntimeError(f"The instrument at {info.address} is a {info.manufacturer} {info.model}, not a {self.name}.")

        self.close()
        self._device, self._close = self._open_device(info)
        self._info = info
        return info

    def close(self) -> None:
        if self._close is not None:
            try:
                self._close()
            except (OSError, pyvisa.Error):
                pass
        self._device = None
        self._info = None
        self._close = None

    @property
    def connected_info(self) -> DiscoveredInstrument | None:
        return self._info

    def device(self) -> T:
        # If something closed the connection (for example a VISA session), connect again
        # before the command. Then the server does not send the command two times.
        if self._device is not None and not self._is_open(self._device):
            self.close()
        if self._device is None:
            self.connect()
        assert self._device is not None
        return self._device

    def info(self) -> DiscoveredInstrument:
        self.device()
        assert self._info is not None
        return self._info

    def set(self, device: T | None, info: DiscoveredInstrument | None = None) -> None:
        """Set the instrument directly. The tests use this."""
        self._close = None
        self._device = device
        self._info = info


def _open_rigol_dp(info: DiscoveredInstrument) -> tuple[RigolDP, Callable[[], None]]:
    res = pyvisa.ResourceManager(info.backend).open_resource(
        info.address, read_termination="\n", write_termination="\n", timeout=IO_TIMEOUT_MS
    )
    try:
        dev = RigolDP(res, info.model)
    except Exception:
        res.close()
        raise
    return dev, res.close


def _visa_is_open(device: RigolDP | RigolDM3058 | SiglentSDS1000XE) -> bool:
    try:
        device.resource.session
    except pyvisa.errors.InvalidSession:
        return False
    return True


def _identify_visa_address(address: str) -> DiscoveredInstrument | None:
    for backend in discovery.available_backends():
        info = discovery.identify(address, backend)
        if info is not None:
            return info
    return None


def _open_dm3058(info: DiscoveredInstrument) -> tuple[RigolDM3058, Callable[[], None]]:
    res = pyvisa.ResourceManager(info.backend).open_resource(
        info.address, read_termination="\n", write_termination="\n", timeout=DMM_IO_TIMEOUT_MS
    )
    try:
        dev = RigolDM3058(res)
    except Exception:
        res.close()
        raise
    return dev, res.close


def _open_sds(info: DiscoveredInstrument) -> tuple[SiglentSDS1000XE, Callable[[], None]]:
    res = pyvisa.ResourceManager(info.backend).open_resource(
        info.address, read_termination="\n", write_termination="\n", timeout=SCOPE_IO_TIMEOUT_MS
    )
    return SiglentSDS1000XE(res, info.model.upper()), res.close


def _open_hcs(info: DiscoveredInstrument) -> tuple[MansonHCS, Callable[[], None]]:
    transport = SerialTransport.open(info.address)
    try:
        dev = MansonHCS(transport)
    except Exception:
        transport.close()
        raise
    return dev, dev.close


def _open_afg(info: DiscoveredInstrument) -> tuple[GWInstekAFG2000, Callable[[], None]]:
    transport = AFGSerialTransport.open(info.address)
    try:
        dev = GWInstekAFG2000(transport, info.model)
    except Exception:
        transport.close()
        raise
    return dev, dev.close


lock = threading.RLock()
rigol_dp = _Slot[RigolDP](
    "Rigol DP", _is_rigol_dp, _open_rigol_dp, _identify_visa_address, RIGOL_DP_HINT, _visa_is_open
)
hcs = _Slot[MansonHCS]("Manson HCS", _is_hcs, _open_hcs, discovery.identify_hcs, HCS_HINT)
dm3058 = _Slot[RigolDM3058](
    "Rigol DM3058", _is_dm3058, _open_dm3058, _identify_visa_address, DMM_HINT, _visa_is_open
)
afg = _Slot[GWInstekAFG2000](
    "GW Instek AFG", _is_afg, _open_afg, discovery.identify_afg, AFG_HINT, lambda d: d.is_open
)
sds = _Slot[SiglentSDS1000XE](
    "SIGLENT SDS", _is_sds, _open_sds, _identify_visa_address, SCOPE_HINT, _visa_is_open
)
SLOTS: list[_Slot] = [rigol_dp, hcs, dm3058, afg, sds]


def _open_serial_ports() -> list[str]:
    return [s.connected_info.address for s in SLOTS if s.connected_info and s.connected_info.backend == "serial"]


@contextmanager
def _guard(slot: _Slot | None = None) -> Iterator[None]:
    """Lock the instruments and send expected errors to the model as ToolError.

    MCPServer does not show the message of other exceptions to the model.
    If the instrument does not answer, the connection is closed. The next tool
    call then finds the instrument again.
    """
    with lock:
        try:
            yield
        except (pyvisa.Error, OSError) as exc:
            if slot is not None:
                slot.close()
            raise ToolError(f"Communication with the instrument failed: {exc}") from exc
        except (ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc



class OutputOffResult(BaseModel):
    instrument: str
    serial: str | None = None
    result: str = Field(description='"off", or the reason why the server could not switch the outputs off')


# -- all instruments -----------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def discover_instruments(scan_lan: bool = True) -> list[DiscoveredInstrument]:
    """Find all supported instruments on USB, on the LAN and on serial ports.

    If scan_lan is true and no instrument answers the VISA search, the server scans the local networks.
    The list includes the instruments that are connected now.
    """
    with _guard():
        connected = [s.connected_info for s in SLOTS if s.connected_info is not None]
        found = discovery.discover(scan_lan=scan_lan, skip_serial_ports=_open_serial_ports())
        known = {i.serial for i in connected}
        return connected + [i for i in found if i.serial not in known]


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def all_outputs_off() -> list[OutputOffResult]:
    """Switch off all outputs of all instruments that the server can find."""
    results = []
    with lock:
        for slot, switch_off in (
            (rigol_dp, lambda d: d.all_outputs_off()),
            (hcs, lambda d: d.set_output(False)),
            (afg, lambda d: d.set_output(False)),
        ):
            connected = False
            try:
                dev = slot.device()
                connected = True
                switch_off(dev)
                results.append(OutputOffResult(instrument=slot.name, serial=slot.info().serial, result="off"))
            except RuntimeError as exc:
                reason = "command failed" if connected else "not found"
                results.append(OutputOffResult(instrument=slot.name, result=f"{reason}: {exc}"))
            except (pyvisa.Error, OSError) as exc:
                slot.close()
                results.append(OutputOffResult(instrument=slot.name, result=f"communication failed: {exc}"))
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lab-mcp", description="MCP server for laboratory instruments.")
    parser.add_argument(
        "--transport", choices=["http", "stdio"], default="http",
        help="MCP transport (default: http, Streamable HTTP)",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"HTTP address (default: {DEFAULT_HOST}). Use 0.0.0.0 for access from the network.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default: {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run("streamable-http", host=args.host, port=args.port)


# The tool modules register their tools on mcp. They import mcp, the slots and _guard
# from this module, thus this import must be after these definitions.
from lab_mcp.tools import multimeters, oscilloscopes, power_supplies, signal_generators  # noqa: E402, F401


if __name__ == "__main__":
    # With "python -m lab_mcp.server", this module runs as __main__. The tool modules
    # registered their tools on the module lab_mcp.server, which is a different copy.
    # Thus run main() of that copy.
    from lab_mcp.server import main as _main

    _main()
