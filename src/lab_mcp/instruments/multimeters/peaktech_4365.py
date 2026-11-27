"""Driver for the PeakTech 4365 handheld digital multimeter (RS-232).

The multimeter sends the contents of its display. It cannot be controlled: the rotary
switch sets the function and the range. The protocol is from a test with a real
PeakTech 4365 and from an earlier read-out program for PeakTech multimeters:

- 9600 baud, 7 data bits, no parity, 2 stop bits. The opto-isolated interface gets its
  supply from the control lines. Thus the driver sets DTR on and RTS off. Immediately
  after this change, the multimeter sends one unwanted character. Thus the driver
  waits 0.3 s and then removes the received data.
- The computer sends CR. The multimeter sends 4 lines with 14 characters. Each line
  ends with CR. Characters 1 to 2: type (DC, AC, OH, DI, CA, FR, TE, HF, LO, dB) or
  spaces. Characters 3 to 9: value with sign, or a text (OL, rdy, Open, bAd).
  Characters 10 to 13: unit (mV, V, mA, A, Ohm, kOhm, MOhm, nF, uF, kHz, MHz, C, F, dBm).
- For DC voltage, DC current, resistance, capacitance and hFE, lines 2 to 4 contain the
  last 3 displayed values. The display moves them one line down each second.
- The multimeter sends the reply after 0.1 s (DC) to 1 s (AC, frequency, capacitance).
  After a turn of the rotary switch, a reply can be incomplete.
- An overload shows as OL with a decimal point (.OL, O.L, OL.). During a range change
  or a capacitance measurement, the value has no decimal point (for example 00000).
  The driver reports no value for it.
"""

from __future__ import annotations

import math
import re
import time
from decimal import Decimal
from statistics import mean, stdev
from typing import Literal, Protocol

import serial
from pydantic import BaseModel, ConfigDict, Field

BAUD_RATE = 9600
# The longest reply time in a test was 1.0 s (AC voltage).
TIMEOUT_S = 2.5
LINE_COUNT = 4
LINE_LENGTH = 14  # 13 characters and CR
REPLY_LENGTH = LINE_COUNT * LINE_LENGTH
# The multimeter updates the display approximately 2 times each second.
READING_TIME_S = 1.0
# Time for the interface after DTR/RTS change, and before a second attempt.
SETTLE_S = 0.3

Function = Literal[
    "dc_voltage", "ac_voltage", "dc_current", "ac_current", "resistance", "diode", "capacitance",
    "frequency", "temperature", "hfe", "logic", "signal_output", "unknown",
]

# Display unit -> (SI unit, factor).
_UNITS: dict[str, tuple[str, str]] = {
    "mV": ("V", "1e-3"), "V": ("V", "1"),
    "uA": ("A", "1e-6"), "mA": ("A", "1e-3"), "A": ("A", "1"),
    "Ohm": ("Ohm", "1"), "kOhm": ("Ohm", "1e3"), "MOhm": ("Ohm", "1e6"),
    "pF": ("F", "1e-12"), "nF": ("F", "1e-9"), "uF": ("F", "1e-6"), "mF": ("F", "1e-3"),
    "Hz": ("Hz", "1"), "kHz": ("Hz", "1e3"), "MHz": ("Hz", "1e6"),
    "C": ("°C", "1"), "F": ("°F", "1"),  # a capacitance always has a prefix (nF, uF)
    "dBm": ("dBm", "1"),
}
# Functions with the last 3 displayed values in lines 2 to 4.
_HISTORY_FUNCTIONS = {"dc_voltage", "dc_current", "resistance", "capacitance", "hfe"}
# Names of the values in lines 3 and 4, for each function.
_SECONDARY: dict[str, tuple[str | None, str | None]] = {
    "ac_voltage": ("frequency", "level"),
    "ac_current": ("frequency", None),
    "frequency": ("ac_voltage", "level"),
    "temperature": ("temperature_f", None),
    "diode": ("status", None),
    "logic": ("frequency", "value"),
    "signal_output": ("amplitude", None),
}


class PTTransport(Protocol):
    def query(self) -> list[str]:
        """Send CR. Return the 4 display lines without CR."""
        ...

    def close(self) -> None: ...


class PTProtocolError(OSError):
    """The multimeter sent a reply that does not agree with the protocol."""


class PTSerialTransport:
    """PeakTech framing on an RS-232 port."""

    def __init__(self, port: serial.Serial, settle_s: float = SETTLE_S):
        self._port = port
        self._settle_s = settle_s

    @classmethod
    def open(cls, device: str, timeout: float = TIMEOUT_S) -> PTSerialTransport:
        port = serial.Serial(device, baudrate=BAUD_RATE, bytesize=7, parity="N", stopbits=2, timeout=timeout)
        port.dtr = True  # supply for the interface of the multimeter
        port.rts = False
        time.sleep(SETTLE_S)  # the multimeter sends one unwanted character after the change
        return cls(port)

    def query(self) -> list[str]:
        # After a turn of the rotary switch, a reply can be incomplete. Thus try two times.
        error: OSError | None = None
        for attempt in range(2):
            if attempt:
                time.sleep(self._settle_s)  # let the rest of an incomplete reply arrive
            self._port.reset_input_buffer()
            self._port.write(b"\r")
            self._port.flush()
            data = self._port.read(REPLY_LENGTH)
            try:
                return split_reply(data)
            except PTProtocolError as exc:
                error = exc
        if error is not None and not data:
            raise TimeoutError(f"No reply from {self._port.port}")
        raise error  # type: ignore[misc]

    def close(self) -> None:
        self._port.close()


def split_reply(data: bytes) -> list[str]:
    if len(data) != REPLY_LENGTH or any(data[i] != 0x0D for i in range(LINE_LENGTH - 1, REPLY_LENGTH, LINE_LENGTH)):
        raise PTProtocolError(f"Incomplete or unexpected reply: {data!r}")
    text = data.decode("ascii", errors="replace")
    return [text[i : i + LINE_LENGTH - 1] for i in range(0, REPLY_LENGTH, LINE_LENGTH)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PTValue(_Model):
    value: float | None = Field(description="Value in unit. None for an overload, a text or an unstable display.")
    unit: str = Field(description="SI unit (V, A, Ohm, F, Hz, °C, °F, dBm), or empty")
    display: str = Field(description="Text on the display, for example '097.57 mV' or 'Open'")
    overload: bool = Field(description="True if the display shows OL")


class PTReading(_Model):
    function: Function = Field(description="Position of the rotary switch")
    value: float | None = Field(description="Main value in unit. None for an overload, a text or an unstable display.")
    unit: str
    display: str
    overload: bool
    secondary: dict[str, PTValue] = Field(
        description="Other values on the display, for example frequency and level (dBm) for AC voltage"
    )
    history: list[float | None] = Field(
        description="Last 3 displayed values in unit, newest first (DC voltage, DC current, resistance, "
        "capacitance, hFE). The display moves them each second."
    )
    lines: list[str] = Field(description="The 4 display lines as received")


class PTSeries(_Model):
    function: Function
    unit: str
    count: int = Field(description="Number of readings")
    overloads: int = Field(description="Number of readings with an overload. They are not in the statistics.")
    invalid: int = Field(description="Number of readings without a value (text or unstable display)")
    minimum: float | None
    maximum: float | None
    mean: float | None
    stdev: float | None = Field(description="Sample standard deviation. None for fewer than 2 values.")
    values: list[float | None] = Field(description="All values in the sequence of the measurement")


def parse_line(line: str, default_unit_text: str = "") -> tuple[str, PTValue]:
    """Return the type code and the value of one display line.

    default_unit_text: display unit (for example mV) for a line without a unit.
    """
    if len(line) != LINE_LENGTH - 1:
        raise PTProtocolError(f"Unexpected display line: {line!r}")
    code = line[0:2].strip()
    raw = line[2:9]
    unit_text = line[9:13].strip() or default_unit_text
    negative = raw.startswith("-")
    text = raw.lstrip("-").strip()
    if unit_text and unit_text not in _UNITS:
        raise PTProtocolError(f"Unknown unit in display line: {line!r}")
    unit, factor = _UNITS.get(unit_text, ("", "1"))
    display = " ".join(p for p in (("-" if negative else "") + text, unit_text) if p)
    overload = text.replace(".", "") == "OL"
    value: float | None = None
    if not overload and re.fullmatch(r"\d+\.\d*|\d*\.\d+", text):
        number = Decimal(text) * Decimal(factor)
        value = float(-number if negative else number)
    return code, PTValue(value=value, unit=unit, display=display, overload=overload)


def parse_reply(lines: list[str]) -> PTReading:
    if len(lines) != LINE_COUNT:
        raise PTProtocolError(f"Expected {LINE_COUNT} display lines, got {len(lines)}")
    code, main = parse_line(lines[0])
    main_unit_text = lines[0][9:13].strip()
    function = _function(code, main.unit, lines[2])
    history: list[float | None] = []
    secondary: dict[str, PTValue] = {}
    if function in _HISTORY_FUNCTIONS:
        history = [parse_line(line, main_unit_text)[1].value for line in lines[1:]]
    else:
        for name, line in zip(_SECONDARY.get(function, (None, None)), lines[2:]):
            if name is not None and line.strip():
                secondary[name] = parse_line(line)[1]
    return PTReading(
        function=function, value=main.value, unit=main.unit, display=main.display, overload=main.overload,
        secondary=secondary, history=history, lines=list(lines),
    )


def _function(code: str, unit: str, line3: str) -> Function:
    if code in ("DC", "AC"):
        if unit == "V":
            return "dc_voltage" if code == "DC" else "ac_voltage"
        if unit == "A":
            return "dc_current" if code == "DC" else "ac_current"
        return "unknown"
    if code == "FR":
        # Frequency: line 3 is "AC ... V". S/O: line 3 has a voltage without a type.
        return "frequency" if line3[0:2] == "AC" else "signal_output"
    return {
        "OH": "resistance", "DI": "diode", "CA": "capacitance", "TE": "temperature", "HF": "hfe", "LO": "logic",
    }.get(code, "unknown")


class PeakTech4365:
    model = "4365"

    def __init__(self, transport: PTTransport):
        self._t = transport
        self.read()  # examine that the multimeter answers

    def read(self) -> PTReading:
        return parse_reply(self._t.query())

    def read_series(self, count: int, interval_s: float) -> PTSeries:
        readings: list[PTReading] = []
        for i in range(count):
            if i:
                time.sleep(interval_s)
            reading = self.read()
            if readings and (reading.function, reading.unit) != (readings[0].function, readings[0].unit):
                raise RuntimeError(
                    f"The function changed during the series ({readings[0].function} to {reading.function}). "
                    "Do not turn the rotary switch during a series."
                )
            readings.append(reading)
        values = [r.value for r in readings]
        numbers = [v for v in values if v is not None and math.isfinite(v)]
        overloads = sum(r.overload for r in readings)
        return PTSeries(
            function=readings[0].function,
            unit=readings[0].unit,
            count=count,
            overloads=overloads,
            invalid=sum(1 for r in readings if r.value is None and not r.overload),
            minimum=min(numbers) if numbers else None,
            maximum=max(numbers) if numbers else None,
            mean=mean(numbers) if numbers else None,
            stdev=stdev(numbers) if len(numbers) > 1 else None,
            values=values,
        )

    def close(self) -> None:
        self._t.close()
