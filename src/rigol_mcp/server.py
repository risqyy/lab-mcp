"""MCP server for Rigol instruments.

The server needs no configuration. At the first tool call, it finds the DP832
automatically on USB or on the LAN (see ``rigol_mcp.discovery``). If the connection
is lost, the server finds the instrument again at the next tool call.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import pyvisa
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from rigol_mcp import discovery
from rigol_mcp.discovery import DiscoveredInstrument
from rigol_mcp.instruments.dp832 import DP832, ChannelStatus, Measurement

DP832_MODELS = {"DP832", "DP832A"}
IO_TIMEOUT_MS = 5000

READ_ONLY = ToolAnnotations(read_only_hint=True)

mcp = MCPServer(
    "rigol-mcp",
    instructions=(
        "This server controls a Rigol DP832 DC power supply with three outputs. "
        "CH1 and CH2: 0-30 V, 0-3 A. CH3: 0-5 V, 0-3 A. "
        "The server finds the instrument automatically. "
        "Set the voltage and the current limit before you switch an output on. "
        "Read dp832_status before you switch an output on. "
        "If you are not sure, use dp832_all_off."
    ),
)


def _is_dp832(inst: DiscoveredInstrument) -> bool:
    return inst.is_rigol and inst.model.upper() in DP832_MODELS


class _Connections:
    """Shared instrument connection. All tools use it under one lock."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._dp832: DP832 | None = None
        self._dp832_info: DiscoveredInstrument | None = None
        self._dp832_resource = None

    def connect_dp832(self, serial: str | None = None, address: str | None = None) -> DiscoveredInstrument:
        """Connect to a DP832. Without arguments, use the first DP832 that discovery finds."""
        if address is not None:
            info = self._identify_address(address)
        else:
            found = sorted(discovery.discover(match=_is_dp832), key=lambda i: i.serial)
            if serial is not None:
                found = [i for i in found if i.serial == serial]
            if not found:
                raise RuntimeError(_not_found_message(serial))
            info = found[0]
        if not _is_dp832(info):
            raise RuntimeError(f"The instrument at {info.address} is a {info.manufacturer} {info.model}, not a DP832.")

        self.close_dp832()
        res = pyvisa.ResourceManager(info.backend).open_resource(
            info.address, read_termination="\n", write_termination="\n", timeout=IO_TIMEOUT_MS
        )
        self._dp832_resource = res
        self._dp832 = DP832(res)
        self._dp832_info = info
        return info

    def close_dp832(self) -> None:
        if self._dp832_resource is not None:
            try:
                self._dp832_resource.close()
            except (OSError, pyvisa.Error):
                pass
        self._dp832_resource = None
        self._dp832 = None
        self._dp832_info = None

    def dp832(self) -> DP832:
        if self._dp832 is None:
            self.connect_dp832()
        assert self._dp832 is not None
        return self._dp832

    def dp832_info(self) -> DiscoveredInstrument:
        self.dp832()
        assert self._dp832_info is not None
        return self._dp832_info

    def set_dp832(self, instrument: DP832 | None, info: DiscoveredInstrument | None = None) -> None:
        """Set the instrument directly. The tests use this."""
        self._dp832_resource = None
        self._dp832 = instrument
        self._dp832_info = info

    @staticmethod
    def _identify_address(address: str) -> DiscoveredInstrument:
        for backend in discovery.available_backends():
            info = discovery.identify(address, backend)
            if info is not None:
                return info
        raise RuntimeError(f"No instrument answers at {address}.")


def _not_found_message(serial: str | None) -> str:
    what = f"No DP832 with serial number {serial} found." if serial else "No DP832 found."
    return (
        f"{what} Make sure that the instrument is on. "
        "For USB, connect the cable and install a USB-TMC driver (NI-VISA, or WinUSB on Windows). "
        "For LAN, connect the instrument to the same network as this computer. "
        "Use discover_instruments to see all instruments that the server finds."
    )


connections = _Connections()


@contextmanager
def _guard() -> Iterator[None]:
    """Lock the instrument and send expected errors to the model as ToolError.

    MCPServer does not show the message of other exceptions to the model.
    If the instrument does not answer, the connection is closed. The next tool
    call then finds the instrument again.
    """
    with connections.lock:
        try:
            yield
        except (pyvisa.Error, OSError) as exc:
            connections.close_dp832()
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


def _channels(dev: DP832, channel: int | None) -> list[int]:
    return dev.channels if channel is None else [channel]


@mcp.tool(annotations=READ_ONLY)
def discover_instruments(scan_lan: bool = True) -> list[DiscoveredInstrument]:
    """Find all Rigol instruments on USB and on the LAN.

    If scan_lan is true and no instrument answers the VISA search, the server scans the local networks.
    """
    with _guard():
        return discovery.discover(scan_lan=scan_lan)


@mcp.tool()
def dp832_connect(serial: str | None = None, address: VisaAddress | None = None) -> DiscoveredInstrument:
    """Connect to a DP832. Usually you do not need this tool, because the server connects automatically.

    Use serial to select one DP832 if there are more. Use address only if discovery cannot find the instrument.
    """
    with _guard():
        return connections.connect_dp832(serial=serial, address=address)


@mcp.tool(annotations=READ_ONLY)
def dp832_identify() -> DiscoveredInstrument:
    """Show the connected DP832: address, model, serial number and firmware."""
    with _guard():
        return connections.dp832_info()


@mcp.tool(annotations=READ_ONLY)
def dp832_status(channel: DP832Channel | None = None) -> list[ChannelStatus]:
    """Read the output state, setpoints, protection settings and CV/CC mode. Without channel, read all channels."""
    with _guard():
        dev = connections.dp832()
        return [dev.status(ch) for ch in _channels(dev, channel)]


@mcp.tool(annotations=READ_ONLY)
def dp832_measure(channel: DP832Channel | None = None) -> list[Measurement]:
    """Measure the output voltage (V), current (A) and power (W). Without channel, measure all channels."""
    with _guard():
        dev = connections.dp832()
        return [dev.measure(ch) for ch in _channels(dev, channel)]


@mcp.tool()
def dp832_set(
    channel: DP832Channel, voltage: SetVolts | None = None, current: SetAmps | None = None
) -> ChannelStatus:
    """Set the voltage (V), the current limit (A) or both for one channel. The output state does not change."""
    with _guard():
        dev = connections.dp832()
        if voltage is not None:
            dev.set_voltage(channel, voltage)
        if current is not None:
            dev.set_current(channel, current)
        return dev.status(channel)


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def dp832_output(channel: DP832Channel, enabled: bool) -> ChannelStatus:
    """Switch the output of one channel on or off."""
    with _guard():
        dev = connections.dp832()
        dev.set_output(channel, enabled)
        return dev.status(channel)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def dp832_all_off() -> list[ChannelStatus]:
    """Switch all outputs off."""
    with _guard():
        dev = connections.dp832()
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
    with _guard():
        dev = connections.dp832()
        if ovp_voltage is not None:
            dev.set_ovp(channel, ovp_voltage, enabled)
        if ocp_current is not None:
            dev.set_ocp(channel, ocp_current, enabled)
        return dev.status(channel)


@mcp.tool()
def dp832_clear_protection(channel: DP832Channel) -> ChannelStatus:
    """Reset a tripped OVP or OCP. After the reset, you can switch the output on again."""
    with _guard():
        dev = connections.dp832()
        dev.clear_protection(channel)
        return dev.status(channel)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
