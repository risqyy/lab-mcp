"""MCP tools for DC power supplies: Rigol DP800/DP900 (dp_*) and Manson HCS (hcs_*)."""

from __future__ import annotations

from typing import Annotated

from mcp_types import ToolAnnotations
from pydantic import Field

from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.power_supplies.manson_hcs import HCSMeasurement, HCSPreset, HCSStatus
from lab_mcp.instruments.power_supplies.rigol_dp import ChannelStatus, Measurement, RigolDP
from lab_mcp.server import READ_ONLY, _guard, hcs, mcp, rigol_dp

# No fixed maximum: the limits depend on the model and the channel (see dp_status).
# The driver checks them, for example DP832 CH3 max. 5 V, DP932A CH1 max. 32 V.
DpChannel = Annotated[int, Field(ge=1, le=3, description="Output channel 1 to 3")]
SetVolts = Annotated[float, Field(ge=0, description="Voltage setpoint in V (limit of the channel: see dp_status)")]
SetAmps = Annotated[float, Field(ge=0, description="Current limit in A (limit of the channel: see dp_status)")]
OvpVolts = Annotated[float, Field(ge=0, description="OVP level in V (limit of the channel: see dp_status)")]
OcpAmps = Annotated[float, Field(ge=0, description="OCP level in A (limit of the channel: see dp_status)")]
VisaAddress = Annotated[
    str, Field(pattern=r"^\S+::\S+$", description="VISA address, for example TCPIP0::192.0.2.10::INSTR")
]
# No fixed maximum: the limits of each HCS model come from the instrument (GMAX),
# and they can be higher than the datasheet (HCS-3404: 60.5 V / 11.0 A). The driver checks them.
HcsVolts = Annotated[float, Field(ge=0, description="Voltage in V (limit of the model: see hcs_status)")]
HcsAmps = Annotated[float, Field(ge=0, description="Current in A (limit of the model: see hcs_status)")]
SerialPort = Annotated[str, Field(min_length=1, description="Serial port, for example COM7 or /dev/ttyUSB0")]


def _channels(dev: RigolDP, channel: int | None) -> list[int]:
    return dev.channels if channel is None else [channel]


# -- Rigol DP800 / DP900 -------------------------------------------------


@mcp.tool()
def dp_connect(serial: str | None = None, address: VisaAddress | None = None) -> DiscoveredInstrument:
    """Connect to a Rigol DP power supply (DP800 or DP900 series).

    The server connects automatically if there is only one DP. If there are more, use serial to select one.
    Use address only if discovery cannot find the instrument.
    """
    with _guard(rigol_dp):
        return rigol_dp.connect(serial=serial, address=address)


@mcp.tool(annotations=READ_ONLY)
def dp_identify() -> DiscoveredInstrument:
    """Show the connected DP power supply: address, model, serial number and firmware."""
    with _guard(rigol_dp):
        return rigol_dp.info()


@mcp.tool(annotations=READ_ONLY)
def dp_status(channel: DpChannel | None = None) -> list[ChannelStatus]:
    """Read the output state, setpoints, protection settings, CV/CC mode, limits and coupling of the channels.

    Without channel, read all channels. The server does not change CH1 or CH2 while tracking is on or
    while CH1 and CH2 are connected in series or in parallel (pair_mode).
    """
    with _guard(rigol_dp):
        dev = rigol_dp.device()
        return [dev.status(ch) for ch in _channels(dev, channel)]


@mcp.tool(annotations=READ_ONLY)
def dp_measure(channel: DpChannel | None = None) -> list[Measurement]:
    """Measure the output voltage (V), current (A) and power (W). Without channel, measure all channels.

    The instrument updates the measurement approximately one time each second.
    After you change a setpoint or an output, wait 1 s before you measure.
    """
    with _guard(rigol_dp):
        dev = rigol_dp.device()
        return [dev.measure(ch) for ch in _channels(dev, channel)]


@mcp.tool()
def dp_set(
    channel: DpChannel, voltage: SetVolts | None = None, current: SetAmps | None = None
) -> ChannelStatus:
    """Set the voltage (V), the current limit (A) or both for one channel. The output state does not change."""
    with _guard(rigol_dp):
        dev = rigol_dp.device()
        if voltage is not None:
            dev.set_voltage(channel, voltage)
        if current is not None:
            dev.set_current(channel, current)
        return dev.status(channel)


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def dp_output(channel: DpChannel, enabled: bool) -> ChannelStatus:
    """Switch the output of one channel on or off."""
    with _guard(rigol_dp):
        dev = rigol_dp.device()
        dev.set_output(channel, enabled)
        return dev.status(channel)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def dp_all_off() -> list[ChannelStatus]:
    """Switch all outputs of the DP power supply off."""
    with _guard(rigol_dp):
        dev = rigol_dp.device()
        dev.all_outputs_off()
        return [dev.status(ch) for ch in dev.channels]


@mcp.tool()
def dp_set_protection(
    channel: DpChannel,
    ovp_voltage: OvpVolts | None = None,
    ocp_current: OcpAmps | None = None,
    enabled: bool = True,
) -> ChannelStatus:
    """Set the over-voltage protection (V), the over-current protection (A) or both for one channel."""
    with _guard(rigol_dp):
        dev = rigol_dp.device()
        if ovp_voltage is not None:
            dev.set_ovp(channel, ovp_voltage, enabled)
        if ocp_current is not None:
            dev.set_ocp(channel, ocp_current, enabled)
        return dev.status(channel)


@mcp.tool()
def dp_clear_protection(channel: DpChannel) -> ChannelStatus:
    """Reset a tripped OVP or OCP. The output stays off. After the reset, you can switch the output on again."""
    with _guard(rigol_dp):
        dev = rigol_dp.device()
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
