"""MCP tools for oscilloscopes: SIGLENT SDS1104X-E (scope_*)."""

from __future__ import annotations

from typing import Annotated

from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.oscilloscopes.siglent_sds1000xe import (
    MAX_WAVEFORM_POINTS,
    Channel,
    ChannelConfiguration,
    Coupling,
    Measurement,
    MeasurementResult,
    RunSweep,
    ScopeStatus,
    TimebaseConfiguration,
    TriggerConfiguration,
    TriggerCoupling,
    TriggerSlope,
    TriggerSource,
    TriggerSweep,
    Waveform,
)
from lab_mcp.server import READ_ONLY, _guard, mcp, sds

ChannelParam = Annotated[Channel, Field(description="Analog channel C1 to C4")]
VisaAddress = Annotated[
    str, Field(pattern=r"^\S+::\S+$", description="VISA address, for example USB0::0xF4EC::0xEE38::SDSMMGKQ000000::INSTR")
]
Volts = Annotated[float, Field(allow_inf_nan=False)]


class AcquisitionState(BaseModel):
    acquisition: str = Field(description="triggered, ready, auto, armed or stopped")


@mcp.tool()
def scope_connect(serial: str | None = None, address: VisaAddress | None = None) -> DiscoveredInstrument:
    """Connect to a SIGLENT SDS1104X-E oscilloscope.

    The server connects automatically if there is only one oscilloscope. If there are more, use serial to select one.
    Use address only if discovery cannot find the instrument.
    """
    with _guard(sds):
        return sds.connect(serial=serial, address=address)


@mcp.tool(annotations=READ_ONLY)
def scope_identify() -> DiscoveredInstrument:
    """Show the connected oscilloscope: address, model, serial number and firmware."""
    with _guard(sds):
        return sds.info()


@mcp.tool(annotations=READ_ONLY)
def scope_status() -> ScopeStatus:
    """Read the acquisition state, the time base, the trigger and the configuration of all channels.

    This tool does not change settings.
    """
    with _guard(sds):
        return sds.device().status()


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def scope_configure_channel(
    channel: ChannelParam,
    displayed: Annotated[bool | None, Field(description="Switch the trace on or off")] = None,
    volts_per_div: Annotated[float | None, Field(gt=0, allow_inf_nan=False, description=(
        "Vertical scale in V/div at the probe tip. Range: 500 uV/div to 10 V/div, multiplied by the probe attenuation."
    ))] = None,
    offset_v: Annotated[Volts | None, Field(description=(
        "Vertical offset in V at the probe tip. The oscilloscope limits the value; read the result."
    ))] = None,
    coupling: Annotated[Coupling | None, Field(description="Input coupling with 1 MOhm: dc, ac or gnd")] = None,
    probe_attenuation: Annotated[float | None, Field(description=(
        "Probe attenuation factor: 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000 or 10000. "
        "It must agree with the probe. It changes only the scaling, not the input."
    ))] = None,
    bandwidth_limit: Annotated[bool | None, Field(description="20 MHz bandwidth limit")] = None,
) -> ChannelConfiguration:
    """Set the configuration of one channel. Values that you do not give stay the same."""
    with _guard(sds):
        return sds.device().configure_channel(
            channel, displayed=displayed, volts_per_div=volts_per_div, offset_v=offset_v,
            coupling=coupling, probe_attenuation=probe_attenuation, bandwidth_limit=bandwidth_limit,
        )


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def scope_configure_timebase(
    seconds_per_div: Annotated[float | None, Field(gt=0, allow_inf_nan=False, description=(
        "Horizontal scale in s/div. Use a value of the 1-2-5 sequence from 1e-9 to 100, for example 0.0005."
    ))] = None,
    trigger_delay_s: Annotated[float | None, Field(allow_inf_nan=False, description=(
        "Time from the trigger to the horizontal center of the screen in s. The oscilloscope limits the value."
    ))] = None,
) -> TimebaseConfiguration:
    """Set the time base and the trigger delay. The screen has 14 horizontal divisions."""
    with _guard(sds):
        return sds.device().configure_timebase(seconds_per_div, trigger_delay_s)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def scope_configure_trigger(
    source: Annotated[TriggerSource | None, Field(description="C1 to C4, EX (external), EX5 (external / 5) or LINE")] = None,
    slope: Annotated[TriggerSlope | None, Field(description="rising, falling or alternating edge")] = None,
    level_v: Annotated[Volts | None, Field(description=(
        "Trigger level in V. Limit: 4.5 div from the screen center for channels, 3 div for EX. "
        "The oscilloscope sets a value outside the limit to the limit; read the result."
    ))] = None,
    coupling: Annotated[TriggerCoupling | None, Field(description="dc, ac, hf_reject or lf_reject")] = None,
    sweep: Annotated[TriggerSweep | None, Field(description=(
        "auto: acquire also without a trigger. normal: acquire only after a trigger. single: one acquisition."
    ))] = None,
) -> TriggerConfiguration:
    """Set an edge trigger. Values that you do not give stay the same.

    If the present trigger type is not edge, this tool changes it to edge.
    """
    with _guard(sds):
        return sds.device().configure_trigger(
            source=source, slope=slope, level_v=level_v, coupling=coupling, sweep=sweep
        )


@mcp.tool()
def scope_run(
    sweep: Annotated[RunSweep | None, Field(description=(
        "auto or normal. Necessary if the present sweep is single or stop. Otherwise the sweep stays the same."
    ))] = None,
) -> AcquisitionState:
    """Start continuous acquisition (the same as the Run key)."""
    with _guard(sds):
        return AcquisitionState(acquisition=sds.device().run(sweep))


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def scope_stop() -> AcquisitionState:
    """Stop the acquisition. The last waveform stays in the memory."""
    with _guard(sds):
        return AcquisitionState(acquisition=sds.device().stop())


@mcp.tool()
def scope_single(
    timeout_s: Annotated[float, Field(ge=0, le=60, description=(
        "Time in s to wait for the trigger. 0 returns immediately. Result stopped: the acquisition is complete."
    ))] = 5.0,
) -> AcquisitionState:
    """Set the single sweep and start one acquisition. Then read the waveform or measurements."""
    with _guard(sds):
        return AcquisitionState(acquisition=sds.device().single(timeout_s))


@mcp.tool(annotations=READ_ONLY)
def scope_measure(
    channel: ChannelParam,
    parameters: Annotated[list[Measurement], Field(min_length=1, max_length=25, description=(
        "Measurements. Voltage (V): pkpk, max, min, ampl, top, base, mean, cmean (one cycle), stdev, vstd (one cycle), "
        "rms, crms (one cycle). Time (s): per, pwid, nwid, rise, fall, wid (burst width). Frequency (Hz): freq. "
        "Percent: duty, nduty, ovsn, ovsp (overshoot), fpre, rpre (preshoot)."
    ))],
) -> list[MeasurementResult]:
    """Read automatic measurements of a channel from the present acquisition.

    The values are in the units at the probe tip. If the oscilloscope cannot calculate a value, valid is false.
    This tool does not change settings.
    """
    with _guard(sds):
        dev = sds.device()
        return [dev.measure(channel, p) for p in dict.fromkeys(parameters)]


@mcp.tool(description=(
    "Read the waveform samples of a channel from the acquisition memory, with the time and voltage scale. "
    "If the memory has more points than max_points, the server transfers only every n-th point (sparsing); "
    "this sets the waveform transfer setup (WFSU) of the oscilloscope. Stop the acquisition or use scope_single "
    "first to read one stable acquisition. Samples with clipping are counted in clipped_samples."
))
def scope_waveform(
    channel: ChannelParam,
    max_points: Annotated[int, Field(ge=1, le=MAX_WAVEFORM_POINTS, description="Largest number of samples in the result")] = 1000,
) -> Waveform:
    with _guard(sds):
        return sds.device().waveform(channel, max_points)
