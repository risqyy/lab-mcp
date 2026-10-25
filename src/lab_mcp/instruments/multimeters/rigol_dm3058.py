"""SCPI driver for the Rigol DM3058 and DM3058E 5½ digit digital multimeters.

The driver uses the RIGOL command set (DM3058/DM3058E programming guide, January 2015).
The instrument also has an Agilent 34401A and a Fluke 45 command set (CMDSET). The driver
selects the RIGOL command set when it connects.

The driver needs only a resource with ``write`` and ``query`` (for example, a pyvisa resource).
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

MeasurementFunction = Literal[
    "dc_voltage", "ac_voltage", "dc_current", "ac_current", "resistance_2w", "resistance_4w",
    "frequency", "period", "continuity", "diode", "capacitance",
]
Speed = Literal["slow", "medium", "fast"]

# The instrument sends a very large value for an overload (SCPI convention: 9.9E+37).
OVERLOAD_THRESHOLD = 1e37
# Time for the first reading after a change of the function or the range.
SETTLE_TIME_S = 0.5


class SCPIResource(Protocol):
    def write(self, message: str) -> object: ...
    def query(self, message: str) -> str: ...


class _Spec:
    def __init__(
        self,
        scpi: str,
        unit: str,
        replies: tuple[str, ...],
        ranges: tuple[float, ...] = (),
        rate: bool = False,
    ) -> None:
        self.scpi = scpi  # for example "VOLTage:DC"
        self.unit = unit
        self.replies = replies  # replies to :FUNCtion? (the guide is not consistent)
        self.ranges = ranges  # full scale of each range index, in the unit of the function
        self.rate = rate


# Ranges from the programming guide, tables 3-4 to 3-9.
_AC_VOLTAGE_RANGES = (0.2, 2.0, 20.0, 200.0, 750.0)
FUNCTIONS: dict[str, _Spec] = {
    "dc_voltage": _Spec("VOLTage:DC", "V", ("DCV",), (0.2, 2.0, 20.0, 200.0, 1000.0), rate=True),
    "ac_voltage": _Spec("VOLTage:AC", "V", ("ACV",), _AC_VOLTAGE_RANGES, rate=True),
    "dc_current": _Spec("CURRent:DC", "A", ("DCI",), (200e-6, 2e-3, 20e-3, 200e-3, 2.0, 10.0), rate=True),
    "ac_current": _Spec("CURRent:AC", "A", ("ACI",), (20e-3, 200e-3, 2.0, 10.0), rate=True),
    "resistance_2w": _Spec(
        "RESistance", "Ohm", ("2WR", "RESISTANCE"), (200.0, 2e3, 20e3, 200e3, 1e6, 10e6, 100e6), rate=True
    ),
    "resistance_4w": _Spec(
        "FRESistance", "Ohm", ("4WR", "FRESISTANCE"), (200.0, 2e3, 20e3, 200e3, 1e6, 10e6, 100e6), rate=True
    ),
    # For frequency and period, the range is the AC voltage range of the input signal.
    "frequency": _Spec("FREQuency", "Hz", ("FREQ", "FREQUENCY")),
    "period": _Spec("PERiod", "s", ("PERI", "PERIOD")),
    "continuity": _Spec("CONTinuity", "Ohm", ("CONT", "CONTINUITY")),
    "diode": _Spec("DIODe", "V", ("DIODE",)),
    "capacitance": _Spec("CAPacitance", "F", ("CAP", "CAPACITANCE"), (2e-9, 20e-9, 200e-9, 2e-6, 200e-6, 10000e-6)),
}
_RATE_CODES = {"slow": "S", "medium": "M", "fast": "F"}
_RATE_NAMES = {"S": "slow", "M": "medium", "F": "fast"}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DMMReading(_Model):
    function: MeasurementFunction
    value: float | None = Field(description="Measured value in unit. None if the instrument shows an overload.")
    unit: str
    overload: bool = Field(description="True if the value is out of the range")
    range: float | Literal["auto"] = Field(description="Full scale of the range in unit, or auto")


class DMMSeries(_Model):
    function: MeasurementFunction
    unit: str
    count: int = Field(description="Number of readings")
    overloads: int = Field(description="Number of readings with an overload. They are not in the statistics.")
    minimum: float | None
    maximum: float | None
    mean: float | None
    stdev: float | None = Field(description="Sample standard deviation. None for fewer than 2 values.")
    values: list[float | None] = Field(description="All values in the sequence of the measurement")


class DMMConfiguration(_Model):
    function: MeasurementFunction
    unit: str
    range: float | Literal["auto"]
    speed: Speed | None = Field(description="Measurement rate. None for functions without a rate setting.")
    available_ranges: list[float] = Field(description="Full scale of each range in unit")


class RigolDM3058:
    def __init__(self, resource: SCPIResource, sleep: Callable[[float], None] = time.sleep):
        self._res = resource
        self._sleep = sleep
        # The instrument has no query for auto or manual range. The driver remembers it.
        self._auto_range = True
        if self._res.query("CMDSET?").strip().upper() != "RIGOL":
            self._res.write("CMDSET RIGOL")

    @property
    def resource(self) -> SCPIResource:
        return self._res

    def identify(self) -> str:
        return self._res.query("*IDN?").strip()

    # -- configuration -------------------------------------------------

    def get_function(self) -> MeasurementFunction:
        reply = self._res.query(":FUNCtion?").strip().upper()
        for name, spec in FUNCTIONS.items():
            if reply in spec.replies:
                return name  # type: ignore[return-value]
        raise ValueError(f"Unexpected reply to :FUNCtion?: {reply!r}")

    def configure(
        self,
        function: MeasurementFunction,
        expected_max: float | Literal["auto"] = "auto",
        speed: Speed | None = None,
    ) -> DMMConfiguration:
        """Set the function, the range and the speed.

        expected_max: the largest absolute value that you expect, in the unit of the function.
        The driver selects the smallest range that includes this value. "auto" selects autorange.
        """
        spec = _spec(function)
        if speed is not None and not spec.rate:
            raise ValueError(f"{function} has no speed setting")
        index = None
        if expected_max != "auto":
            if not spec.ranges:
                raise ValueError(f"{function} has no range setting. Use auto.")
            index = _range_index(function, spec, float(expected_max))

        changed = self.get_function() != function
        if changed:
            self._res.write(f":FUNCtion:{spec.scpi}")
        if spec.ranges:
            if index is None:
                if not self._auto_range or changed:
                    self._res.write(":MEASure AUTO")
                self._auto_range = True
            else:
                self._res.write(f":MEASure:{spec.scpi} {index}")
                self._auto_range = False  # setting a range selects manual range
        if speed is not None:
            self._res.write(f":RATE:{spec.scpi} {_RATE_CODES[speed]}")
        if changed or index is not None:
            self._sleep(SETTLE_TIME_S)
        return self.configuration()

    def configuration(self) -> DMMConfiguration:
        function = self.get_function()
        spec = FUNCTIONS[function]
        speed = None
        if spec.rate:
            speed = _RATE_NAMES.get(self._res.query(f":RATE:{spec.scpi}?").strip().upper()[:1])
        return DMMConfiguration(
            function=function, unit=spec.unit, range=self._current_range(function),
            speed=speed, available_ranges=list(spec.ranges),
        )

    # -- measurement ---------------------------------------------------

    def read(self) -> DMMReading:
        """Read one value with the present configuration."""
        function = self.get_function()
        spec = FUNCTIONS[function]
        value, overload = _parse_value(self._res.query(f":MEASure:{spec.scpi}?"))
        return DMMReading(
            function=function, value=value, unit=spec.unit, overload=overload,
            range=self._current_range(function),
        )

    def measure(
        self,
        function: MeasurementFunction,
        expected_max: float | Literal["auto"] = "auto",
        speed: Speed | None = None,
    ) -> DMMReading:
        self.configure(function, expected_max, speed)
        return self.read()

    def measure_series(self, count: int, interval_s: float) -> DMMSeries:
        """Read count values with the present configuration, with interval_s between the readings."""
        if count < 1:
            raise ValueError("count must be 1 or more")
        if interval_s < 0:
            raise ValueError("interval_s must be 0 or more")
        readings = []
        for i in range(count):
            if i:
                self._sleep(interval_s)
            readings.append(self.read())
        values = [r.value for r in readings if r.value is not None]
        return DMMSeries(
            function=readings[0].function,
            unit=readings[0].unit,
            count=count,
            overloads=sum(r.overload for r in readings),
            minimum=min(values) if values else None,
            maximum=max(values) if values else None,
            mean=statistics.fmean(values) if values else None,
            stdev=statistics.stdev(values) if len(values) > 1 else None,
            values=[r.value for r in readings],
        )

    # -- helpers -------------------------------------------------------

    def _current_range(self, function: str) -> float | Literal["auto"]:
        spec = FUNCTIONS[function]
        if not spec.ranges or self._auto_range:
            return "auto"
        index = int(float(self._res.query(f":MEASure:{spec.scpi}:RANGe?")))
        return spec.ranges[index]


def _spec(function: str) -> _Spec:
    try:
        return FUNCTIONS[function]
    except KeyError:
        raise ValueError(f"Unknown function {function!r}; valid functions are {sorted(FUNCTIONS)}") from None


def _range_index(function: str, spec: _Spec, expected_max: float) -> int:
    magnitude = abs(expected_max)
    for index, full_scale in enumerate(spec.ranges):
        if magnitude <= full_scale:
            return index
    raise ValueError(
        f"{expected_max} {spec.unit} is above the largest {function} range ({spec.ranges[-1]} {spec.unit})"
    )


def _parse_value(reply: str) -> tuple[float | None, bool]:
    try:
        value = float(reply.strip())
    except ValueError:
        return None, True  # for example "OVERLOAD"
    if not math.isfinite(value) or abs(value) >= OVERLOAD_THRESHOLD:
        return None, True
    return value, False
