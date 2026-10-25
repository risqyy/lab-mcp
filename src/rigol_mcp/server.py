"""MCP server exposing Rigol instruments as tools.

Configuration (environment variables):
    RIGOL_DP832_RESOURCE  VISA address of the DP832, e.g. ``TCPIP::192.168.1.50::INSTR``.
                          Optional; can also be passed to ``dp832_connect``.
    RIGOL_VISA_BACKEND    pyvisa backend, default ``@py`` (pyvisa-py). Use ``""`` for NI-VISA.
    RIGOL_VISA_TIMEOUT_MS I/O timeout in milliseconds, default 5000.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import pyvisa
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from rigol_mcp.instruments.dp832 import DP832, ChannelStatus, Measurement

READ_ONLY = ToolAnnotations(read_only_hint=True)

mcp = MCPServer(
    "rigol-mcp",
    instructions=(
        "Controls a Rigol DP832 triple-output DC power supply (CH1/CH2: 0-30 V, 0-3 A; CH3: 0-5 V, 0-3 A). "
        "Always set voltage and current limits before enabling an output, and check dp832_status "
        "before switching outputs on. Use dp832_all_off in case of doubt."
    ),
)


class _Connections:
    """Lazily opened, lock-protected instrument handles shared by all tools."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._rm: pyvisa.ResourceManager | None = None
        self._dp832: DP832 | None = None
        self._dp832_resource = None

    @property
    def rm(self) -> pyvisa.ResourceManager:
        if self._rm is None:
            self._rm = pyvisa.ResourceManager(os.environ.get("RIGOL_VISA_BACKEND", "@py"))
        return self._rm

    def connect_dp832(self, address: str) -> DP832:
        self.close_dp832()
        res = self.rm.open_resource(
            address,
            read_termination="\n",
            write_termination="\n",
            timeout=int(os.environ.get("RIGOL_VISA_TIMEOUT_MS", "5000")),
        )
        self._dp832_resource = res
        self._dp832 = DP832(res)
        return self._dp832

    def close_dp832(self) -> None:
        if self._dp832_resource is not None:
            self._dp832_resource.close()
        self._dp832_resource = None
        self._dp832 = None

    def dp832(self) -> DP832:
        if self._dp832 is None:
            address = os.environ.get("RIGOL_DP832_RESOURCE")
            if not address:
                raise RuntimeError(
                    "DP832 not connected. Call dp832_connect with a VISA address "
                    "(see list_visa_resources) or set RIGOL_DP832_RESOURCE."
                )
            self.connect_dp832(address)
        assert self._dp832 is not None
        return self._dp832

    def set_dp832(self, instrument: DP832 | None) -> None:
        """Inject an instrument directly (used by tests)."""
        self._dp832_resource = None
        self._dp832 = instrument


connections = _Connections()


@contextmanager
def _guard() -> Iterator[None]:
    """Serialize instrument access and surface anticipated failures to the model as ToolError.

    MCPServer hides the message of any other exception, so out-of-range values or a
    missing connection would otherwise reach the model only as "Error executing tool".
    """
    with connections.lock:
        try:
            yield
        except (ValueError, RuntimeError, pyvisa.Error) as exc:
            raise ToolError(str(exc)) from exc


# Envelope over all DP832 channels for the tool schemas; the per-channel limits
# (e.g. CH3 max 5 V) are enforced by the driver.
DP832Channel = Annotated[int, Field(ge=1, le=3, description="Output channel 1-3")]
SetVolts = Annotated[float, Field(ge=0, le=30, description="Voltage setpoint in V (CH3: max 5 V)")]
SetAmps = Annotated[float, Field(ge=0, le=3, description="Current limit in A")]
OvpVolts = Annotated[float, Field(ge=0, le=33, description="OVP level in V (CH3: max 5.5 V)")]
OcpAmps = Annotated[float, Field(ge=0, le=3.3, description="OCP level in A")]
VisaAddress = Annotated[
    str, Field(pattern=r"^\S+::\S+$", description="VISA address, e.g. TCPIP::192.168.1.50::INSTR")
]


def _channels(dev: DP832, channel: int | None) -> list[int]:
    return dev.channels if channel is None else [channel]


@mcp.tool(annotations=READ_ONLY)
def list_visa_resources(query: Annotated[str, Field(min_length=1)] = "?*::INSTR") -> list[str]:
    """List VISA instrument addresses visible to the configured backend (LAN discovery may be limited)."""
    with _guard():
        return list(connections.rm.list_resources(query))


@mcp.tool()
def dp832_connect(address: VisaAddress) -> str:
    """Connect to a DP832 at the given VISA address (e.g. TCPIP::192.168.1.50::INSTR). Returns *IDN?."""
    with _guard():
        return connections.connect_dp832(address).identify()


@mcp.tool(annotations=READ_ONLY)
def dp832_identify() -> str:
    """Return the instrument identification string (*IDN?)."""
    with _guard():
        return connections.dp832().identify()


@mcp.tool(annotations=READ_ONLY)
def dp832_status(channel: DP832Channel | None = None) -> list[ChannelStatus]:
    """Read output state, setpoints, protection settings and CV/CC mode. Omit channel for all channels."""
    with _guard():
        dev = connections.dp832()
        return [dev.status(ch) for ch in _channels(dev, channel)]


@mcp.tool(annotations=READ_ONLY)
def dp832_measure(channel: DP832Channel | None = None) -> list[Measurement]:
    """Measure actual output voltage (V), current (A) and power (W). Omit channel for all channels."""
    with _guard():
        dev = connections.dp832()
        return [dev.measure(ch) for ch in _channels(dev, channel)]


@mcp.tool()
def dp832_set(
    channel: DP832Channel, voltage: SetVolts | None = None, current: SetAmps | None = None
) -> ChannelStatus:
    """Set voltage (V) and/or current limit (A) of a channel. Does not change the output state."""
    with _guard():
        dev = connections.dp832()
        if voltage is not None:
            dev.set_voltage(channel, voltage)
        if current is not None:
            dev.set_current(channel, current)
        return dev.status(channel)


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def dp832_output(channel: DP832Channel, enabled: bool) -> ChannelStatus:
    """Switch a channel output on or off."""
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
    """Configure over-voltage (V) and/or over-current (A) protection of a channel."""
    with _guard():
        dev = connections.dp832()
        if ovp_voltage is not None:
            dev.set_ovp(channel, ovp_voltage, enabled)
        if ocp_current is not None:
            dev.set_ocp(channel, ocp_current, enabled)
        return dev.status(channel)


@mcp.tool()
def dp832_clear_protection(channel: DP832Channel) -> ChannelStatus:
    """Clear a tripped OVP/OCP condition so the output can be re-enabled."""
    with _guard():
        dev = connections.dp832()
        dev.clear_protection(channel)
        return dev.status(channel)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
