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
from typing import Annotated, Generic, TypeVar

import pyvisa
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from lab_mcp import discovery
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.dp832 import DP832, ChannelStatus, Measurement
from lab_mcp.instruments.manson_hcs import HCSMeasurement, HCSPreset, HCSStatus, MansonHCS, SerialTransport

DP832_MODELS = {"DP832", "DP832A"}
IO_TIMEOUT_MS = 5000
# The server has no authentication. Thus it accepts only local connections by default.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10765

READ_ONLY = ToolAnnotations(read_only_hint=True)

mcp = MCPServer(
    "lab-mcp",
    instructions=(
        "This server controls laboratory instruments. The server finds the instruments automatically. "
        "Rigol DP832: DC power supply with three outputs, tools dp832_*. "
        "CH1 and CH2: 0-30 V, 0-3 A. CH3: 0-5 V, 0-3 A. "
        "Manson HCS series: DC power supply with one output, tools hcs_*. "
        "Read the limits of the HCS model with hcs_status. "
        "Set the voltage and the current limit before you switch an output on. "
        "Read the status before you switch an output on. "
        "After a change, wait 1 s before you measure. "
        "If you are not sure, use all_outputs_off."
    ),
)

T = TypeVar("T")

# Message for each instrument type when discovery finds no instrument.
DP832_HINT = (
    "For USB, connect the cable and install a USB-TMC driver (NI-VISA, or WinUSB on Windows). "
    "For LAN, connect the instrument to the same network as this computer."
)
HCS_HINT = (
    "Set the MODE switch on the rear panel to Normal. Connect the USB cable. "
    "Make sure that Windows shows a COM port for the instrument (Silicon Labs CP210x driver)."
)


def _is_dp832(inst: DiscoveredInstrument) -> bool:
    return inst.is_rigol and inst.model.upper() in DP832_MODELS


def _is_hcs(inst: DiscoveredInstrument) -> bool:
    return inst.is_manson and inst.model.upper().startswith("HCS")


class _Slot(Generic[T]):
    """Connection to one instrument of one type."""

    def __init__(
        self,
        name: str,
        match: Callable[[DiscoveredInstrument], bool],
        open_device: Callable[[DiscoveredInstrument], tuple[T, Callable[[], None]]],
        identify_address: Callable[[str], DiscoveredInstrument | None],
        hint: str,
    ) -> None:
        self.name = name
        self._match = match
        self._open_device = open_device
        self._identify_address = identify_address
        self._hint = hint
        self._device: T | None = None
        self._info: DiscoveredInstrument | None = None
        self._close: Callable[[], None] | None = None

    def connect(self, serial: str | None = None, address: str | None = None) -> DiscoveredInstrument:
        """Connect to the instrument. Without arguments, use the first instrument that discovery finds."""
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


def _open_dp832(info: DiscoveredInstrument) -> tuple[DP832, Callable[[], None]]:
    res = pyvisa.ResourceManager(info.backend).open_resource(
        info.address, read_termination="\n", write_termination="\n", timeout=IO_TIMEOUT_MS
    )
    return DP832(res), res.close


def _identify_visa_address(address: str) -> DiscoveredInstrument | None:
    for backend in discovery.available_backends():
        info = discovery.identify(address, backend)
        if info is not None:
            return info
    return None


def _open_hcs(info: DiscoveredInstrument) -> tuple[MansonHCS, Callable[[], None]]:
    transport = SerialTransport.open(info.address)
    try:
        dev = MansonHCS(transport)
    except Exception:
        transport.close()
        raise
    return dev, dev.close


lock = threading.RLock()
dp832 = _Slot[DP832]("DP832", _is_dp832, _open_dp832, _identify_visa_address, DP832_HINT)
hcs = _Slot[MansonHCS]("Manson HCS", _is_hcs, _open_hcs, discovery.identify_hcs, HCS_HINT)
SLOTS: list[_Slot] = [dp832, hcs]


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


# Limits for the tool schemas. They include all DP832 channels. The driver
# also checks the limits of each channel (for example, CH3 max. 5 V).
DP832Channel = Annotated[int, Field(ge=1, le=3, description="Output channel 1 to 3")]
SetVolts = Annotated[float, Field(ge=0, le=30, description="Voltage setpoint in V (CH3: max. 5 V)")]
SetAmps = Annotated[float, Field(ge=0, le=3, description="Current limit in A")]
OvpVolts = Annotated[float, Field(ge=0, le=33, description="OVP level in V (CH3: max. 5.5 V)")]
OcpAmps = Annotated[float, Field(ge=0, le=3.3, description="OCP level in A")]
VisaAddress = Annotated[
    str, Field(pattern=r"^\S+::\S+$", description="VISA address, for example TCPIP0::192.0.2.10::INSTR")
]
# No fixed maximum: the limits of each HCS model come from the instrument (GMAX),
# and they can be higher than the datasheet (HCS-3404: 60.5 V / 11.0 A). The driver checks them.
HcsVolts = Annotated[float, Field(ge=0, description="Voltage in V (limit of the model: see hcs_status)")]
HcsAmps = Annotated[float, Field(ge=0, description="Current in A (limit of the model: see hcs_status)")]
SerialPort = Annotated[str, Field(min_length=1, description="Serial port, for example COM7 or /dev/ttyUSB0")]


def _channels(dev: DP832, channel: int | None) -> list[int]:
    return dev.channels if channel is None else [channel]


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
        for slot, switch_off in ((dp832, lambda d: d.all_outputs_off()), (hcs, lambda d: d.set_output(False))):
            try:
                dev = slot.device()
                switch_off(dev)
                results.append(OutputOffResult(instrument=slot.name, serial=slot.info().serial, result="off"))
            except RuntimeError as exc:  # no instrument of this type found
                results.append(OutputOffResult(instrument=slot.name, result=f"not found: {exc}"))
            except (pyvisa.Error, OSError) as exc:
                slot.close()
                results.append(OutputOffResult(instrument=slot.name, result=f"communication failed: {exc}"))
    return results


# -- Rigol DP832 ---------------------------------------------------------


@mcp.tool()
def dp832_connect(serial: str | None = None, address: VisaAddress | None = None) -> DiscoveredInstrument:
    """Connect to a DP832. Usually you do not need this tool, because the server connects automatically.

    Use serial to select one DP832 if there are more. Use address only if discovery cannot find the instrument.
    """
    with _guard(dp832):
        return dp832.connect(serial=serial, address=address)


@mcp.tool(annotations=READ_ONLY)
def dp832_identify() -> DiscoveredInstrument:
    """Show the connected DP832: address, model, serial number and firmware."""
    with _guard(dp832):
        return dp832.info()


@mcp.tool(annotations=READ_ONLY)
def dp832_status(channel: DP832Channel | None = None) -> list[ChannelStatus]:
    """Read the output state, setpoints, protection settings and CV/CC mode. Without channel, read all channels."""
    with _guard(dp832):
        dev = dp832.device()
        return [dev.status(ch) for ch in _channels(dev, channel)]


@mcp.tool(annotations=READ_ONLY)
def dp832_measure(channel: DP832Channel | None = None) -> list[Measurement]:
    """Measure the output voltage (V), current (A) and power (W). Without channel, measure all channels.

    The instrument updates the measurement approximately one time each second.
    After you change a setpoint or an output, wait 1 s before you measure.
    """
    with _guard(dp832):
        dev = dp832.device()
        return [dev.measure(ch) for ch in _channels(dev, channel)]


@mcp.tool()
def dp832_set(
    channel: DP832Channel, voltage: SetVolts | None = None, current: SetAmps | None = None
) -> ChannelStatus:
    """Set the voltage (V), the current limit (A) or both for one channel. The output state does not change."""
    with _guard(dp832):
        dev = dp832.device()
        if voltage is not None:
            dev.set_voltage(channel, voltage)
        if current is not None:
            dev.set_current(channel, current)
        return dev.status(channel)


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def dp832_output(channel: DP832Channel, enabled: bool) -> ChannelStatus:
    """Switch the output of one channel on or off."""
    with _guard(dp832):
        dev = dp832.device()
        dev.set_output(channel, enabled)
        return dev.status(channel)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def dp832_all_off() -> list[ChannelStatus]:
    """Switch all outputs of the DP832 off."""
    with _guard(dp832):
        dev = dp832.device()
        dev.all_outputs_off()
        return [dev.status(ch) for ch in dev.channels]


@mcp.tool()
def dp832_set_protection(
    channel: DP832Channel,
    ovp_voltage: OvpVolts | None = None,
    ocp_current: OcpAmps | None = None,
    enabled: bool = True,
) -> ChannelStatus:
    """Set the over-voltage protection (V), the over-current protection (A) or both for one channel."""
    with _guard(dp832):
        dev = dp832.device()
        if ovp_voltage is not None:
            dev.set_ovp(channel, ovp_voltage, enabled)
        if ocp_current is not None:
            dev.set_ocp(channel, ocp_current, enabled)
        return dev.status(channel)


@mcp.tool()
def dp832_clear_protection(channel: DP832Channel) -> ChannelStatus:
    """Reset a tripped OVP or OCP. After the reset, you can switch the output on again."""
    with _guard(dp832):
        dev = dp832.device()
        dev.clear_protection(channel)
        return dev.status(channel)


# -- Manson HCS ----------------------------------------------------------


@mcp.tool()
def hcs_connect(serial: str | None = None, port: SerialPort | None = None) -> DiscoveredInstrument:
    """Connect to a Manson HCS. Usually you do not need this tool, because the server connects automatically.

    Use serial (USB serial number) to select one HCS if there are more.
    Use port only if discovery cannot find the instrument.
    """
    with _guard(hcs):
        return hcs.connect(serial=serial, address=port)


@mcp.tool(annotations=READ_ONLY)
def hcs_identify() -> DiscoveredInstrument:
    """Show the connected Manson HCS: port, model and USB serial number."""
    with _guard(hcs):
        return hcs.info()


@mcp.tool(annotations=READ_ONLY)
def hcs_status() -> HCSStatus:
    """Read the setpoints, the upper limits and the limits of the model.

    The instrument cannot report the output state. output_enabled is the last state that the server set.
    """
    with _guard(hcs):
        return hcs.device().status()


@mcp.tool(annotations=READ_ONLY)
def hcs_measure() -> HCSMeasurement:
    """Measure the output voltage (V) and current (A), and read the CV/CC mode.

    After you change a setpoint or the output, wait 1 s before you measure.
    The instrument cannot decrease the output voltage without a load. Without a load, the voltage
    decreases slowly after you decrease the setpoint or switch the output off.
    """
    with _guard(hcs):
        return hcs.device().measure()


@mcp.tool()
def hcs_set(voltage: HcsVolts | None = None, current: HcsAmps | None = None) -> HCSStatus:
    """Set the voltage (V), the current limit (A) or both. The output state does not change.

    The values must not be above the upper limits (see hcs_status).
    """
    with _guard(hcs):
        dev = hcs.device()
        if voltage is not None:
            dev.set_voltage(voltage)
        if current is not None:
            dev.set_current(current)
        return dev.status()


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def hcs_output(enabled: bool) -> HCSStatus:
    """Switch the output on or off."""
    with _guard(hcs):
        dev = hcs.device()
        dev.set_output(enabled)
        return dev.status()


@mcp.tool()
def hcs_set_upper_limits(voltage: HcsVolts | None = None, current: HcsAmps | None = None) -> HCSStatus:
    """Set the upper voltage limit (V), the upper current limit (A) or both.

    The instrument does not accept setpoints above these limits, also not from the front panel.
    """
    with _guard(hcs):
        dev = hcs.device()
        dev.set_upper_limits(volts=voltage, amps=current)
        return dev.status()


@mcp.tool(annotations=READ_ONLY)
def hcs_presets() -> list[HCSPreset]:
    """Read the three preset memories (voltage and current)."""
    with _guard(hcs):
        return hcs.device().get_presets()


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def hcs_load_preset(index: Annotated[int, Field(ge=0, le=2, description="Preset 0 to 2")]) -> HCSStatus:
    """Load one preset memory into the setpoints. If the output is on, the output changes immediately.

    The preset must not be above the upper limits. The server refuses such a preset,
    because the instrument keeps it and loads it later, when the upper limit increases.
    """
    with _guard(hcs):
        dev = hcs.device()
        dev.load_preset(index)
        return dev.status()


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


if __name__ == "__main__":
    main()
