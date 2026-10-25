"""MCP tools for signal generators: GW Instek AFG-2105 (sig_*)."""

from __future__ import annotations

from typing import Annotated

from mcp_types import ToolAnnotations
from pydantic import Field

from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.signal_generators.gwinstek_afg2000 import AFGStatus, Load, Waveform
from lab_mcp.server import READ_ONLY, _guard, afg, mcp

SerialPort = Annotated[str, Field(min_length=1, description="Serial port, for example COM8 or /dev/ttyACM0")]
Frequency = Annotated[float, Field(gt=0, allow_inf_nan=False, description="Frequency in Hz; limits depend on the waveform")]
Amplitude = Annotated[float, Field(gt=0, allow_inf_nan=False, description="Peak-to-peak voltage in V for the selected load")]
Offset = Annotated[float, Field(allow_inf_nan=False, description="DC offset in V for the selected load")]
DutyCycle = Annotated[float, Field(ge=1, le=99, allow_inf_nan=False, description="Square-wave duty cycle in percent; range decreases at high frequency")]
Symmetry = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False, description="Ramp symmetry in percent; 50 gives a triangle")]


@mcp.tool()
def sig_connect(serial: str | None = None, port: SerialPort | None = None) -> DiscoveredInstrument:
    """Connect to a GW Instek AFG-2105 by serial number or serial port.

    The server connects automatically if there is one generator. If there are more, use serial to select one.
    Use port only if discovery cannot find the instrument.
    """
    with _guard(afg):
        return afg.connect(serial=serial, address=port)


@mcp.tool(annotations=READ_ONLY)
def sig_identify() -> DiscoveredInstrument:
    """Show the connected generator: address, model, serial number and firmware."""
    with _guard(afg):
        return afg.info()


@mcp.tool(annotations=READ_ONLY)
def sig_status() -> AFGStatus:
    """Read the waveform, setpoints, load setting, output state, active modes and present limits.

    Voltages refer to the selected load. If the setting is 50_ohm but the actual load is high
    impedance, the voltage at the load is twice the reported value. This tool does not change settings.
    """
    with _guard(afg):
        return afg.device().status()


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def sig_set(
    waveform: Waveform | None = None,
    frequency_hz: Frequency | None = None,
    amplitude_vpp: Amplitude | None = None,
    offset_v: Offset | None = None,
    duty_cycle_percent: DutyCycle | None = None,
    symmetry_percent: Symmetry | None = None,
    load: Load | None = None,
) -> AFGStatus:
    """Set a basic waveform and read all settings back. The output state does not change.

    Amplitude is always Vpp, and the display unit becomes VPP. Voltages refer to the selected load.
    Change load only with the output off. If voltages are omitted during a load change, their
    physical values stay the same; their reported values change. Use symmetry_percent=50 for
    a triangle. Noise has no frequency setting. Disable modulation and sweep on the front panel.
    A failed command can leave partial settings; read sig_status before continuing.
    """
    with _guard(afg):
        return afg.device().set(waveform, frequency_hz, amplitude_vpp, offset_v, duty_cycle_percent, symmetry_percent, load)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def sig_output(enabled: bool) -> AFGStatus:
    """Switch the generator output on or off, then read the status.

    Before enabling, read sig_status and check the load setting, amplitude and offset.
    A high impedance input gets twice the voltage reported for a 50_ohm setting.
    The off command also works with arbitrary waveforms, modulation or sweep enabled.
    """
    with _guard(afg):
        dev = afg.device()
        dev.set_output(enabled)
        return dev.status()
