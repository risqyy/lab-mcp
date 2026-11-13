"""MCP tools for spectrum analyzers: RIGOL DSA832E (sa_*)."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Image
from mcp_types import ToolAnnotations
from pydantic import Field

from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.spectrum_analyzers.rigol_dsa800 import (
    SWEEP_POINTS_RANGE,
    AmplitudeConfiguration,
    AnalyzerStatus,
    BandwidthConfiguration,
    Detector,
    FrequencyConfiguration,
    MarkerResult,
    PeakSearch,
    SweepResult,
    TraceConfiguration,
    TraceData,
    TraceMode,
)
from lab_mcp.server import READ_ONLY, _guard, mcp, spectrum_analyzer

VisaAddress = Annotated[
    str, Field(pattern=r"^\S+::\S+$", description="VISA address, for example USB0::0x1AB1::0x0960::DSA8H000000000::INSTR")
]
Hertz = Annotated[float, Field(ge=0, allow_inf_nan=False)]
TraceParam = Annotated[int, Field(ge=1, le=3, description="Trace 1, 2 or 3")]
MarkerParam = Annotated[int, Field(ge=1, le=4, description="Marker 1 to 4")]


@mcp.tool()
def sa_connect(serial: str | None = None, address: VisaAddress | None = None) -> DiscoveredInstrument:
    """Connect to a spectrum analyzer: RIGOL DSA832E.

    The server connects automatically if there is only one spectrum analyzer. If there are more, use serial to select one.
    Use address only if discovery cannot find the instrument.
    """
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.connect(serial=serial, address=address)


@mcp.tool(annotations=READ_ONLY)
def sa_identify() -> DiscoveredInstrument:
    """Show the connected spectrum analyzer: address, model, serial number and firmware."""
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.info()


@mcp.tool(annotations=READ_ONLY)
def sa_status() -> AnalyzerStatus:
    """Read the sweep mode, the frequency range, the bandwidths, the amplitude settings and the trace modes.

    This tool does not change settings.
    """
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().status()


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def sa_configure_frequency(
    center_hz: Annotated[Hertz | None, Field(description="Center frequency in Hz. DSA832E: 0 to 3.2e9.")] = None,
    span_hz: Annotated[Hertz | None, Field(description=(
        "Span in Hz: 100 to 3.2e9, or 0 for zero span (the X axis is then time at the center frequency)."
    ))] = None,
    start_hz: Annotated[Hertz | None, Field(description="Start frequency in Hz. Do not use with center_hz or span_hz.")] = None,
    stop_hz: Annotated[Hertz | None, Field(description="Stop frequency in Hz. Do not use with center_hz or span_hz.")] = None,
    full_span: Annotated[bool, Field(description="Set the full frequency range. Do not use with other values.")] = False,
) -> FrequencyConfiguration:
    """Set the frequency range: center and span, or start and stop. Values that you do not give stay the same.

    The analyzer can change the values slightly (for example, the center frequency is 50 Hz minimum). Read the result.
    """
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().configure_frequency(
            center_hz=center_hz, span_hz=span_hz, start_hz=start_hz, stop_hz=stop_hz, full_span=full_span
        )


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def sa_configure_bandwidth(
    rbw_hz: Annotated[float | None, Field(gt=0, allow_inf_nan=False, description=(
        "Resolution bandwidth in Hz: 10 to 1e6 in the 1-3-10 sequence (10, 30, 100, ...). "
        "With the EMI filter (also with the quasi_peak detector): 200, 9000 or 120000. A value sets the manual mode."
    ))] = None,
    rbw_auto: Annotated[bool | None, Field(description="true: the analyzer sets the RBW from the span")] = None,
    vbw_hz: Annotated[float | None, Field(gt=0, allow_inf_nan=False, description=(
        "Video bandwidth in Hz: 1 to 3e6 in the 1-3-10 sequence. A value sets the manual mode."
    ))] = None,
    vbw_auto: Annotated[bool | None, Field(description="true: the analyzer sets the VBW from the RBW")] = None,
    sweep_time_s: Annotated[float | None, Field(gt=0, allow_inf_nan=False, description=(
        "Sweep time in s: 2e-5 to 7500. The minimum also depends on the span; the analyzer then uses the nearest possible value. "
        "A value sets the manual mode."
    ))] = None,
    sweep_time_auto: Annotated[bool | None, Field(description="true: the analyzer sets the sweep time")] = None,
    sweep_points: Annotated[int | None, Field(ge=SWEEP_POINTS_RANGE[0], le=SWEEP_POINTS_RANGE[1], description=(
        "Number of points of each trace: 101 to 3001"
    ))] = None,
    detector: Annotated[Detector | None, Field(description=(
        "positive_peak, negative_peak, normal, rms, sample, voltage_average or quasi_peak. "
        "quasi_peak switches the EMI filter on."
    ))] = None,
    emi_filter: Annotated[bool | None, Field(description=(
        "true: EMI filter (-6 dB bandwidth, RBW 200 Hz, 9 kHz or 120 kHz). false: Gauss filter (-3 dB, 1-3-10 sequence). "
        "Another detector does not switch the EMI filter off. The quasi_peak detector needs the EMI filter."
    ))] = None,
) -> BandwidthConfiguration:
    """Set the bandwidths, the sweep time, the sweep points, the detector and the EMI filter.

    Values that you do not give stay the same.
    """
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().configure_bandwidth(
            rbw_hz=rbw_hz, rbw_auto=rbw_auto, vbw_hz=vbw_hz, vbw_auto=vbw_auto, sweep_time_s=sweep_time_s,
            sweep_time_auto=sweep_time_auto, sweep_points=sweep_points, detector=detector, emi_filter=emi_filter,
        )


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def sa_configure_amplitude(
    reference_level_dbm: Annotated[float | None, Field(allow_inf_nan=False, description=(
        "Reference level (top line of the screen) in dBm: -100 to 20. "
        "The maximum also depends on the attenuation; the analyzer then uses a lower value. Read the result."
    ))] = None,
    scale_db_per_div: Annotated[float | None, Field(allow_inf_nan=False, description="Vertical scale in dB/div: 0.1 to 20")] = None,
    attenuation_db: Annotated[int | None, Field(ge=0, le=30, description=(
        "Input attenuation in dB: 0 to 30. A value sets the manual mode. "
        "A low attenuation decreases the protection of the input mixer."
    ))] = None,
    attenuation_auto: Annotated[bool | None, Field(description="true: the analyzer sets the attenuation from the reference level")] = None,
    preamplifier: Annotated[bool | None, Field(description=(
        "Switch the preamplifier on or off. Use the preamplifier only for low signals."
    ))] = None,
) -> AmplitudeConfiguration:
    """Set the reference level, the scale, the input attenuation and the preamplifier. Values that you do not give stay the same.

    Do not apply more than the maximum input level of the analyzer to the RF input. The settings do not protect the input.
    """
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().configure_amplitude(
            reference_level_dbm=reference_level_dbm, scale_db_per_div=scale_db_per_div,
            attenuation_db=attenuation_db, attenuation_auto=attenuation_auto, preamplifier=preamplifier,
        )


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def sa_configure_trace(
    trace: TraceParam,
    mode: Annotated[TraceMode, Field(description=(
        "clear_write (each sweep), max_hold, min_hold, view (keep the trace), blank (off), "
        "video_average or power_average"
    ))],
) -> TraceConfiguration:
    """Set the mode of one trace."""
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().configure_trace(trace, mode)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def sa_run() -> SweepResult:
    """Start the continuous sweep."""
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().run()


@mcp.tool()
def sa_single(
    timeout_s: Annotated[float, Field(ge=0, le=3600, description=(
        "Time in s to wait for the end of the sweep. Use more than the sweep time multiplied by the sweep count."
    ))] = 10.0,
) -> SweepResult:
    """Stop the continuous sweep and do one single sweep. Wait until the single sweep is complete.

    Then read the trace or the markers. complete false: the sweep did not end in the timeout.
    """
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().single(timeout_s)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def sa_trace(
    trace: TraceParam = 1,
    max_points: Annotated[int, Field(ge=1, le=SWEEP_POINTS_RANGE[1], description=(
        "Largest number of values in the result. If the trace has more points, each value is the largest "
        "of n neighbor points, so the peaks stay in the result."
    ))] = SWEEP_POINTS_RANGE[1],
) -> TraceData:
    """Read the amplitudes of a trace with the frequency of each value (time in zero span).

    In the continuous sweep, the data can be from two sweeps. Use sa_single first to read one complete sweep.
    This tool changes only the data format of the transfer (ASCII). It does not change measurement settings.
    """
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().trace(trace, max_points)


@mcp.tool()
def sa_marker_peak(
    marker: MarkerParam = 1,
    search: Annotated[PeakSearch, Field(description=(
        "max: highest peak. next: next lower peak. left or right: next peak on that side. min: lowest point."
    ))] = "max",
) -> MarkerResult:
    """Switch the marker on and put it on a peak of the trace. Read the frequency and the amplitude of the marker."""
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().marker_peak(marker, search)


@mcp.tool()
def sa_set_marker(
    marker: MarkerParam,
    x: Annotated[float, Field(allow_inf_nan=False, description=(
        "Frequency in Hz in the present frequency range (time in s in zero span)"
    ))],
) -> MarkerResult:
    """Switch the marker on and put it at a frequency. Read the amplitude at the marker."""
    with _guard(spectrum_analyzer):
        return spectrum_analyzer.device().set_marker(marker, x)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
def sa_markers_off() -> None:
    """Switch off all markers."""
    with _guard(spectrum_analyzer):
        spectrum_analyzer.device().markers_off()


@mcp.tool(annotations=READ_ONLY)
def sa_screenshot() -> Image:
    """Read the screen of the spectrum analyzer as a PNG image (800 x 480 pixels). The transfer takes approximately 7 s.

    This tool does not change settings.
    """
    with _guard(spectrum_analyzer):
        return Image(data=spectrum_analyzer.device().screenshot_png(), format="png")
