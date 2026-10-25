"""MCP tools for digital multimeters: Rigol DM3058 and DM3058E (dmm_*)."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp_types import ToolAnnotations
from pydantic import Field

from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.multimeters.rigol_dm3058 import (
    DMMConfiguration,
    DMMReading,
    DMMSeries,
    MeasurementFunction,
    Speed,
)
from lab_mcp.server import READ_ONLY, _guard, dm3058, mcp

# The longest time that dmm_measure_series can take, so that a tool call does not block the server for long.
MAX_SERIES_DURATION_S = 300.0

Function = Annotated[
    MeasurementFunction,
    Field(
        description=(
            "Measurement function. Units: dc_voltage, ac_voltage, diode: V. dc_current, ac_current: A. "
            "resistance_2w, resistance_4w, continuity: Ohm. frequency: Hz. period: s. capacitance: F. "
            "For dc_current and ac_current, the test leads must be in the current input (mA or 10 A)."
        )
    ),
]
ExpectedMax = Annotated[
    float | Literal["auto"],
    Field(
        description=(
            "The largest absolute value that you expect, in the unit of the function. The server selects the "
            "smallest range that includes this value. Use auto for autorange."
        )
    ),
]
SpeedParam = Annotated[
    Speed | None,
    Field(description="Measurement rate: slow (2.5 readings/s), medium (20/s), fast (123/s). Not for all functions."),
]
VisaAddress = Annotated[
    str, Field(pattern=r"^\S+::\S+$", description="VISA address, for example USB0::0x1AB1::0x09C4::DM3R000000000::INSTR")
]

CURRENT_WARNING = (
    "CAUTION: before a current measurement, make sure that the test leads are in the current input and that "
    "the multimeter is connected in series with the load. If the leads are in the current input and you "
    "connect them across a voltage source, the fuse of the multimeter blows."
)


@mcp.tool()
def dmm_connect(serial: str | None = None, address: VisaAddress | None = None) -> DiscoveredInstrument:
    """Connect to a Rigol DM3058 or DM3058E multimeter.

    The server connects automatically if there is only one multimeter. If there are more, use serial to select one.
    Use address only if discovery cannot find the instrument.
    """
    with _guard(dm3058):
        return dm3058.connect(serial=serial, address=address)


@mcp.tool(annotations=READ_ONLY)
def dmm_identify() -> DiscoveredInstrument:
    """Show the connected multimeter: address, model, serial number and firmware."""
    with _guard(dm3058):
        return dm3058.info()


@mcp.tool(annotations=READ_ONLY)
def dmm_configuration() -> DMMConfiguration:
    """Read the present measurement function, range and speed, and the available ranges."""
    with _guard(dm3058):
        return dm3058.device().configuration()


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True), description=(
    "Set the measurement function, the range and the speed, but do not measure. "
    "Then use dmm_read or dmm_measure_series. " + CURRENT_WARNING
))
def dmm_configure(function: Function, expected_max: ExpectedMax = "auto", speed: SpeedParam = None) -> DMMConfiguration:
    with _guard(dm3058):
        return dm3058.device().configure(function, expected_max, speed)


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True), description=(
    "Set the measurement function and measure one value. If the value is out of the range, "
    "overload is true and value is null. " + CURRENT_WARNING
))
def dmm_measure(function: Function, expected_max: ExpectedMax = "auto", speed: SpeedParam = None) -> DMMReading:
    with _guard(dm3058):
        return dm3058.device().measure(function, expected_max, speed)


@mcp.tool(annotations=READ_ONLY)
def dmm_read() -> DMMReading:
    """Measure one value with the present function and range. The configuration does not change."""
    with _guard(dm3058):
        return dm3058.device().read()


@mcp.tool(annotations=READ_ONLY)
def dmm_measure_series(
    count: Annotated[int, Field(ge=1, le=1000, description="Number of readings")] = 10,
    interval_s: Annotated[float, Field(ge=0, le=60, description="Time between the readings in s")] = 0.5,
) -> DMMSeries:
    """Measure a series of values with the present function and range, and calculate the statistics.

    Use dmm_configure first. The series can take a maximum of 300 s. Each reading also needs time:
    approximately 0.5 s at the slow rate, 0.06 s at medium, 0.02 s at fast, 0.85 s for frequency and period.
    """
    with _guard(dm3058):
        dev = dm3058.device()
        duration = count * dev.estimated_reading_time() + (count - 1) * interval_s
        if duration > MAX_SERIES_DURATION_S:
            raise ValueError(
                f"The series takes approximately {duration:.0f} s. The maximum is {MAX_SERIES_DURATION_S:.0f} s. "
                "Use fewer readings, a shorter interval or a faster rate."
            )
        return dev.measure_series(count, interval_s)
