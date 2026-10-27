"""SCPI driver for the SIGLENT SDS1104X-E digital oscilloscope.

The driver uses the commands of the SIGLENT Digital Oscilloscope Series Programming Guide
PG01-E02D (SDS1000X-E section). The replies have the format of the COMM_HEADER setting
(CHDR SHORT, LONG or OFF). The driver reads all three formats and does not change CHDR.

The driver needs only a resource with ``write``, ``query`` and ``read_raw`` (for example, a pyvisa resource).
"""

from __future__ import annotations

import array
import math
import re
import sys
import time
import zlib
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Channel = Literal["C1", "C2", "C3", "C4"]
Coupling = Literal["dc", "ac", "gnd"]
TriggerSource = Literal["C1", "C2", "C3", "C4", "EX", "EX5", "LINE"]
TriggerSlope = Literal["rising", "falling", "alternating"]
TriggerCoupling = Literal["dc", "ac", "hf_reject", "lf_reject"]
TriggerSweep = Literal["auto", "normal", "single"]
RunSweep = Literal["auto", "normal"]

CHANNELS: tuple[str, ...] = ("C1", "C2", "C3", "C4")
# Horizontal grid of the SDS1000X-E: 14 divisions. Vertical: 25 codes for each division.
HORIZONTAL_DIVISIONS = 14
CODES_PER_DIVISION = 25
# The screen has 8 vertical divisions (4 above and 4 below the center), thus codes -100 to 100.
# A signal above the screen gave codes of approximately 120, not 127 (tested). The driver counts
# codes outside the screen, because their voltage can be clipped.
SCREEN_CODE_LIMIT = 4 * CODES_PER_DIVISION
# Vertical scale at the input (probe 1X), programming guide VOLT_DIV.
VDIV_MIN_V, VDIV_MAX_V = 500e-6, 10.0
ATTENUATIONS = (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)
# Time base values of TIME_DIV. The SDS1104X-E data sheet gives 1 ns/div to 100 s/div.
_TDIV_TOKENS = (
    "1NS", "2NS", "5NS", "10NS", "20NS", "50NS", "100NS", "200NS", "500NS",
    "1US", "2US", "5US", "10US", "20US", "50US", "100US", "200US", "500US",
    "1MS", "2MS", "5MS", "10MS", "20MS", "50MS", "100MS", "200MS", "500MS",
    "1S", "2S", "5S", "10S", "20S", "50S", "100S",
)
_SI = {"NS": 1e-9, "US": 1e-6, "MS": 1e-3, "S": 1.0}
TIME_DIVS: dict[float, str] = {
    float(re.match(r"\d+", t)[0]) * _SI[re.sub(r"^\d+", "", t)]: t for t in _TDIV_TOKENS  # type: ignore[index]
}

_COUPLING_CODES = {"dc": "D1M", "ac": "A1M", "gnd": "GND"}
_COUPLING_NAMES = {"D1M": "dc", "A1M": "ac", "GND": "gnd", "D50": "dc_50_ohm", "A50": "ac_50_ohm"}
_SLOPE_CODES = {"rising": "POS", "falling": "NEG", "alternating": "WINDOW"}
_SLOPE_NAMES = {v: k for k, v in _SLOPE_CODES.items()}
_TRIG_COUPLING_CODES = {"dc": "DC", "ac": "AC", "hf_reject": "HFREJ", "lf_reject": "LFREJ"}
_TRIG_COUPLING_NAMES = {v: k for k, v in _TRIG_COUPLING_CODES.items()}
_SWEEP_CODES = {"auto": "AUTO", "normal": "NORM", "single": "SINGLE"}
_SWEEP_NAMES = {"AUTO": "auto", "NORM": "normal", "SINGLE": "single", "STOP": "stop"}
_SAST_NAMES = {"TRIG'D": "triggered", "READY": "ready", "AUTO": "auto", "STOP": "stopped", "ARM": "armed"}

# PARAMETER_VALUE parameters with one value for each source (programming guide, SDS1000X-E).
MEASUREMENTS: dict[str, str] = {
    "pkpk": "PKPK", "max": "MAX", "min": "MIN", "ampl": "AMPL", "top": "TOP", "base": "BASE",
    "cmean": "CMEAN", "mean": "MEAN", "stdev": "STDEV", "vstd": "VSTD", "rms": "RMS", "crms": "CRMS",
    "ovsn": "OVSN", "fpre": "FPRE", "ovsp": "OVSP", "rpre": "RPRE", "per": "PER", "freq": "FREQ",
    "pwid": "PWID", "nwid": "NWID", "rise": "RISE", "fall": "FALL", "wid": "WID", "duty": "DUTY",
    "nduty": "NDUTY",
}
Measurement = Literal[
    "pkpk", "max", "min", "ampl", "top", "base", "cmean", "mean", "stdev", "vstd", "rms", "crms",
    "ovsn", "fpre", "ovsp", "rpre", "per", "freq", "pwid", "nwid", "rise", "fall", "wid", "duty", "nduty",
]
# Unit of each parameter. The reply also has the unit, except with CHDR OFF.
_MEASUREMENT_UNITS = {
    **{p: "V" for p in ("pkpk", "max", "min", "ampl", "top", "base", "cmean", "mean", "stdev", "vstd", "rms", "crms")},
    **{p: "%" for p in ("ovsn", "fpre", "ovsp", "rpre", "duty", "nduty")},
    **{p: "s" for p in ("per", "pwid", "nwid", "rise", "fall", "wid")},
    "freq": "Hz",
}
_UNIT_NAMES = {"V": "V", "S": "s", "HZ": "Hz", "%": "%", "A": "A"}

_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[Ee][-+]?\d+)?")
MAX_WAVEFORM_POINTS = 100_000

# Math (programming guide DEFINE, MATH_VERT_DIV, MATH_VERT_POS).
MathOperation = Literal["add", "subtract", "multiply", "divide", "fft", "integral", "differential", "sqrt"]
_MATH_SYMBOLS = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}
_MATH_OPERATORS = {v: k for k, v in _MATH_SYMBOLS.items()}
ARITHMETIC_OPERATIONS = frozenset(_MATH_SYMBOLS)
_MATH_PREFIXES = {"fft": "FFT", "integral": "INTG", "differential": "DIFF", "sqrt": "SQRT"}
_MATH_FUNCTIONS = {v: k for k, v in _MATH_PREFIXES.items()}
_MATH_UNITS = {"add": "V", "subtract": "V", "multiply": "V^2", "divide": "V/V"}
_MATH_VDIV_TOKENS = (
    "500uV", "1mV", "2mV", "5mV", "10mV", "20mV", "50mV", "100mV", "200mV", "500mV",
    "1V", "2V", "5V", "10V", "20V", "50V", "100V",
)
_VOLT_SI = {"uV": 1e-6, "mV": 1e-3, "V": 1.0}
MATH_VDIVS: dict[float, str] = {
    float(re.match(r"\d+", t)[0]) * _VOLT_SI[re.sub(r"^\d+", "", t)]: t for t in _MATH_VDIV_TOKENS  # type: ignore[index]
}
# MATH_VERT_POS is in screen pixels: 50 for each division, from -255 to 255.
MATH_POSITION_PER_DIV = 50
MATH_POSITION_MAX = 255
# After a math change, MATH:WF? sends data with the previous scale for approximately 0.5 s,
# also when the acquisition is stopped (tested: 0.4 s too short, 0.6 s sufficient).
MATH_SETTLE_S = 1.0
# The screen shows changes with a delay (tested): a single change after approximately 1.2 s,
# the two cursor positions of one CRST command after approximately 2 s. Value messages
# (for example "1.00V" after a change of the scale) stay on the screen for approximately 3 s.
SCREEN_SETTLE_S = 2.5


class SCPIResource(Protocol):
    def write(self, message: str) -> object: ...
    def query(self, message: str) -> str: ...
    def read_raw(self, size: int | None = None) -> bytes: ...


class IncompleteTransferError(OSError):
    """The waveform data block is not complete. The connection is then in an unknown state."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChannelConfiguration(_Model):
    channel: Channel
    displayed: bool = Field(description="True if the trace is on. Measurements and waveforms need a displayed channel.")
    volts_per_div: float = Field(description="Vertical scale in V/div at the probe tip (includes the probe attenuation)")
    offset_v: float = Field(description="Vertical offset in V at the probe tip")
    coupling: str = Field(description="dc, ac or gnd (1 MOhm input)")
    probe_attenuation: float = Field(description="Probe attenuation factor that the oscilloscope uses, for example 10 for a 10X probe")
    bandwidth_limit: bool = Field(description="True if the 20 MHz bandwidth limit is on")


class TimebaseConfiguration(_Model):
    seconds_per_div: float
    trigger_delay_s: float = Field(description="Time from the trigger to the horizontal center of the screen")
    horizontal_divisions: int
    sample_rate_sa_s: float = Field(description="Sample rate in Sa/s")


class TriggerConfiguration(_Model):
    sweep: str = Field(description="auto, normal, single or stop")
    type: str = Field(description="Trigger type, for example EDGE. The tools set only EDGE.")
    source: str | None
    slope: str | None = Field(description="rising, falling or alternating. None if not an edge trigger on a channel or EX.")
    level_v: float | None = Field(description="Trigger level in V. None for the LINE source.")
    coupling: str | None


class ScopeStatus(_Model):
    acquisition: str = Field(description="triggered, ready, auto, armed or stopped (SAMPLE_STATUS)")
    timebase: TimebaseConfiguration
    trigger: TriggerConfiguration
    channels: list[ChannelConfiguration]


class MeasurementResult(_Model):
    channel: Channel
    parameter: Measurement
    value: float | None = Field(description="Measured value in unit. None if valid is false.")
    unit: str
    valid: bool = Field(description="False if the oscilloscope cannot calculate the value (reply ****), for example for no signal or too few periods")


class Waveform(_Model):
    channel: Channel
    sample_count: int = Field(description="Number of samples in voltages_v")
    memory_points: int = Field(description="Number of points in the acquisition memory (SAMPLE_NUM)")
    sparsing: int = Field(description="Every n-th memory point is in this result")
    first_sample_time_s: float = Field(description="Time of the first sample relative to the trigger point")
    sample_interval_s: float = Field(description="Time between two samples in this result. Time of sample i = first_sample_time_s + i * sample_interval_s")
    seconds_per_div: float
    trigger_delay_s: float
    sample_rate_sa_s: float = Field(description="Acquisition sample rate in Sa/s")
    volts_per_div: float
    offset_v: float
    probe_attenuation: float
    off_screen_samples: int = Field(description=(
        "Samples outside the vertical screen range (more than 4 div from the center). Their voltage can be clipped. "
        "If this is not 0, increase volts_per_div or change offset_v."
    ))
    voltages_v: list[float] = Field(description="Voltage of each sample: code * volts_per_div / 25 - offset_v")


class MathConfiguration(_Model):
    operation: str = Field(description="add, subtract, multiply, divide, fft, integral, differential or sqrt")
    equation: str = Field(description="Equation of the oscilloscope, for example C1+C2")
    source1: Channel
    source2: Channel | None
    volts_per_div: float | None = Field(description="Vertical scale of the math trace. Only for add, subtract, multiply and divide.")
    position_div: float | None = Field(description="Vertical position of the math trace in div above the screen center. Only for add, subtract, multiply and divide.")


class MathWaveform(_Model):
    math: MathConfiguration
    unit: str = Field(description="Unit of values: V for add and subtract, V^2 for multiply, V/V for divide")
    sample_count: int
    memory_points: int = Field(description="Number of points that the oscilloscope transferred")
    decimation: int = Field(description="Every n-th transferred point is in this result")
    first_sample_time_s: float = Field(description="Time of the first sample relative to the trigger point")
    sample_interval_s: float = Field(description="Time of sample i = first_sample_time_s + i * sample_interval_s")
    off_screen_samples: int = Field(description="Samples more than 4 div from the screen center. Their value can be clipped.")
    values: list[float] = Field(description="Value of each sample: code * volts_per_div / 25 - position_div * volts_per_div")


class TimeCursors(_Model):
    mode: str = Field(description="off, manual or track (CURSOR_MEASURE)")
    source: Channel
    x1_s: float | None = Field(description="Time of cursor X1 relative to the trigger point. None if the mode is not manual.")
    x2_s: float | None = Field(description="Time of cursor X2 relative to the trigger point. None if the mode is not manual.")
    delta_s: float | None = Field(description="Time between X1 and X2 (always positive)")
    inverse_delta_hz: float | None = Field(description="1 / delta_s")


class SiglentSDS1000XE:
    def __init__(self, resource: SCPIResource, model: str = "SDS1104X-E",
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self._res = resource
        self.model = model
        self._sleep = sleep
        self._clock = clock
        # After STOP, TRMD? returns STOP and the previous sweep is not known (tested, firmware
        # 8.3.6.1.37R17). The driver remembers the last continuous sweep that it read or set.
        self._continuous_sweep: str | None = None
        self._last_change: float | None = None

    @property
    def resource(self) -> SCPIResource:
        return self._res

    def _write(self, message: str, change: bool = True) -> None:
        self._res.write(message)
        if change:
            self._last_change = self._clock()

    def identify(self) -> str:
        return self._res.query("*IDN?").strip()

    # -- status --------------------------------------------------------

    def _sweep_code(self) -> str:
        sweep = _payload(self._res.query("TRMD?")).upper()
        if sweep not in _SWEEP_NAMES:
            raise ValueError(f"Unexpected reply to TRMD?: {sweep!r}")
        if sweep in ("AUTO", "NORM"):
            self._continuous_sweep = sweep
        return sweep

    def acquisition_state(self) -> str:
        # In some stop states (for example after a second TRMD SINGLE), the oscilloscope does not
        # answer SAST? (tested). TRMD? always answers. Thus send SAST? only if the sweep is not STOP.
        if self._sweep_code() == "STOP":
            return "stopped"
        reply = _payload(self._res.query("SAST?")).upper()
        try:
            return _SAST_NAMES[reply]
        except KeyError:
            raise ValueError(f"Unexpected reply to SAST?: {reply!r}") from None

    def status(self) -> ScopeStatus:
        return ScopeStatus(
            acquisition=self.acquisition_state(), timebase=self.timebase(),
            trigger=self.trigger(), channels=[self.channel(c) for c in CHANNELS],  # type: ignore[arg-type]
        )

    # -- channels ------------------------------------------------------

    def channel(self, channel: Channel) -> ChannelConfiguration:
        ch = _channel(channel)
        coupling = _payload(self._res.query(f"{ch}:CPL?")).upper()
        if coupling not in _COUPLING_NAMES:
            raise ValueError(f"Unexpected reply to {ch}:CPL?: {coupling!r}")
        return ChannelConfiguration(
            channel=ch,  # type: ignore[arg-type]
            displayed=_on_off(self._res.query(f"{ch}:TRA?"), f"{ch}:TRA?"),
            volts_per_div=_number(self._res.query(f"{ch}:VDIV?"), f"{ch}:VDIV?"),
            offset_v=_number(self._res.query(f"{ch}:OFST?"), f"{ch}:OFST?"),
            coupling=_COUPLING_NAMES[coupling],
            probe_attenuation=_number(self._res.query(f"{ch}:ATTN?"), f"{ch}:ATTN?"),
            bandwidth_limit=self._bandwidth_limits()[ch],
        )

    def configure_channel(
        self,
        channel: Channel,
        *,
        displayed: bool | None = None,
        volts_per_div: float | None = None,
        offset_v: float | None = None,
        coupling: Coupling | None = None,
        probe_attenuation: float | None = None,
        bandwidth_limit: bool | None = None,
    ) -> ChannelConfiguration:
        """Set the given values. The oscilloscope sets an offset outside its limit to the nearest limit."""
        ch = _channel(channel)
        if probe_attenuation is not None and probe_attenuation not in ATTENUATIONS:
            raise ValueError(f"probe_attenuation must be one of {list(ATTENUATIONS)}")
        if coupling is not None and coupling not in _COUPLING_CODES:
            raise ValueError(f"coupling must be one of {sorted(_COUPLING_CODES)}")
        if offset_v is not None and not math.isfinite(offset_v):
            raise ValueError("offset_v must be a finite number")
        if volts_per_div is not None:
            attenuation = probe_attenuation or _number(self._res.query(f"{ch}:ATTN?"), f"{ch}:ATTN?")
            low, high = VDIV_MIN_V * attenuation, VDIV_MAX_V * attenuation
            if not (math.isfinite(volts_per_div) and low * (1 - 1e-9) <= volts_per_div <= high * (1 + 1e-9)):
                raise ValueError(
                    f"volts_per_div must be from {low:g} V to {high:g} V with probe attenuation {attenuation:g}"
                )
        # The attenuation changes the scale, thus set it first.
        if probe_attenuation is not None:
            self._write(f"{ch}:ATTN {probe_attenuation:g}")
        if coupling is not None:
            self._write(f"{ch}:CPL {_COUPLING_CODES[coupling]}")
        if volts_per_div is not None:
            self._write(f"{ch}:VDIV {volts_per_div:.6E}V")
        if offset_v is not None:
            self._write(f"{ch}:OFST {offset_v:.6E}V")
        if bandwidth_limit is not None:
            self._write(f"BWL {ch},{'ON' if bandwidth_limit else 'OFF'}")
        if displayed is not None:
            self._write(f"{ch}:TRA {'ON' if displayed else 'OFF'}")
        return self.channel(ch)  # type: ignore[arg-type]

    def _bandwidth_limits(self) -> dict[str, bool]:
        reply = _payload(self._res.query("BWL?"))
        parts = [p.strip().upper() for p in reply.split(",")]
        if len(parts) % 2 or not parts[0]:
            raise ValueError(f"Unexpected reply to BWL?: {reply!r}")
        result = {}
        for ch, state in zip(parts[::2], parts[1::2]):
            if state not in ("ON", "OFF"):
                raise ValueError(f"Unexpected reply to BWL?: {reply!r}")
            result[ch] = state == "ON"
        if not set(CHANNELS) <= result.keys():
            raise ValueError(f"Unexpected reply to BWL?: {reply!r}")
        return result

    # -- timebase ------------------------------------------------------

    def timebase(self) -> TimebaseConfiguration:
        return TimebaseConfiguration(
            seconds_per_div=_number(self._res.query("TDIV?"), "TDIV?"),
            trigger_delay_s=_number(self._res.query("TRDL?"), "TRDL?"),
            horizontal_divisions=HORIZONTAL_DIVISIONS,
            sample_rate_sa_s=_number(self._res.query("SARA?"), "SARA?"),
        )

    def configure_timebase(
        self, seconds_per_div: float | None = None, trigger_delay_s: float | None = None
    ) -> TimebaseConfiguration:
        """Set the time base (a value of the 1-2-5 sequence) and the trigger delay.

        The oscilloscope sets a delay outside its limit to the nearest limit.
        """
        token = None
        if seconds_per_div is not None:
            token = _tdiv_token(seconds_per_div)
        if trigger_delay_s is not None and not math.isfinite(trigger_delay_s):
            raise ValueError("trigger_delay_s must be a finite number")
        if token is not None:
            self._write(f"TDIV {token}")
        if trigger_delay_s is not None:
            self._write(f"TRDL {trigger_delay_s:.6E}S")
        return self.timebase()

    # -- trigger -------------------------------------------------------

    def trigger(self) -> TriggerConfiguration:
        sweep = self._sweep_code()
        trse = _payload(self._res.query("TRSE?"))
        fields = [p.strip().upper() for p in trse.split(",")]
        trig_type = fields[0]
        source = None
        if "SR" in fields[1:-1]:
            source = fields[fields.index("SR", 1) + 1]
        if not trig_type or source is None:
            raise ValueError(f"Unexpected reply to TRSE?: {trse!r}")
        slope = level = coupling = None
        if source != "LINE":
            level = _number(self._res.query(f"{source}:TRLV?"), f"{source}:TRLV?")
            if trig_type == "EDGE":
                slope_code = _payload(self._res.query(f"{source}:TRSL?")).upper()
                slope = _SLOPE_NAMES.get(slope_code, slope_code.lower())
            coupling_code = _payload(self._res.query(f"{source}:TRCP?")).upper()
            coupling = _TRIG_COUPLING_NAMES.get(coupling_code, coupling_code.lower())
        return TriggerConfiguration(
            sweep=_SWEEP_NAMES[sweep], type=trig_type, source=source, slope=slope, level_v=level, coupling=coupling,
        )

    def configure_trigger(
        self,
        *,
        source: TriggerSource | None = None,
        slope: TriggerSlope | None = None,
        level_v: float | None = None,
        coupling: TriggerCoupling | None = None,
        sweep: TriggerSweep | None = None,
    ) -> TriggerConfiguration:
        """Set an edge trigger. Values that are not given stay the same.

        The oscilloscope sets a level outside its limit (4.5 div from the center, 3 div for EX) to the nearest limit.
        """
        if source is not None and source not in ("C1", "C2", "C3", "C4", "EX", "EX5", "LINE"):
            raise ValueError("source must be C1, C2, C3, C4, EX, EX5 or LINE")
        if slope is not None and slope not in _SLOPE_CODES:
            raise ValueError(f"slope must be one of {sorted(_SLOPE_CODES)}")
        if coupling is not None and coupling not in _TRIG_COUPLING_CODES:
            raise ValueError(f"coupling must be one of {sorted(_TRIG_COUPLING_CODES)}")
        if sweep is not None and sweep not in _SWEEP_CODES:
            raise ValueError(f"sweep must be one of {sorted(_SWEEP_CODES)}")
        if level_v is not None and not math.isfinite(level_v):
            raise ValueError("level_v must be a finite number")
        present = self.trigger()
        target = source or present.source
        if target == "LINE" and (level_v is not None or slope is not None or coupling is not None):
            raise ValueError("The LINE source has no level, slope or coupling setting")
        if source is not None or present.type != "EDGE":
            self._write(f"TRSE EDGE,SR,{target},HT,OFF")
        if slope is not None:
            self._write(f"{target}:TRSL {_SLOPE_CODES[slope]}")
        if coupling is not None:
            self._write(f"{target}:TRCP {_TRIG_COUPLING_CODES[coupling]}")
        if level_v is not None:
            self._write(f"{target}:TRLV {level_v:.6E}V")
        # A second TRMD SINGLE stops the acquisition (tested). Thus do not send the present sweep again.
        if sweep is not None and _SWEEP_CODES[sweep] != self._sweep_code():
            self._write(f"TRMD {_SWEEP_CODES[sweep]}")
        return self.trigger()

    # -- acquisition ---------------------------------------------------

    def run(self, sweep: RunSweep | None = None) -> str:
        """Start continuous acquisition with TRMD AUTO or TRMD NORM.

        The driver does not use ARM: on the SDS1104X-E, ARM starts a single acquisition (tested).
        Without sweep, the driver uses the last continuous sweep that it knows.
        """
        if sweep is not None and sweep not in ("auto", "normal"):
            raise ValueError("sweep must be auto or normal")
        present = self._sweep_code()
        if sweep is not None:
            code = _SWEEP_CODES[sweep]
        elif present in ("AUTO", "NORM"):
            code = present
        elif self._continuous_sweep is not None:
            code = self._continuous_sweep
        else:
            raise ValueError(
                f"The trigger sweep is {_SWEEP_NAMES[present]} and the previous sweep is not known. "
                "Select auto or normal to start."
            )
        if code != present:
            self._write(f"TRMD {code}")
            self._continuous_sweep = code
        return self.acquisition_state()

    def stop(self) -> str:
        self._sweep_code()  # remember the present continuous sweep for run()
        self._write("STOP")
        return self.acquisition_state()

    def single(self, timeout_s: float = 0.0, poll_s: float = 0.1) -> str:
        """Start one acquisition. Wait a maximum of timeout_s until the oscilloscope stops after the trigger.

        Return the acquisition state. If no trigger occurs in timeout_s, the state is ready.
        If a single acquisition waits for the trigger already, the driver does not start it again:
        a second TRMD SINGLE stops the acquisition (tested).
        """
        if not (0 <= timeout_s <= 60):
            raise ValueError("timeout_s must be from 0 to 60")
        if self._sweep_code() != "SINGLE":
            self._write("TRMD SINGLE")
        deadline = self._clock() + timeout_s
        state = self.acquisition_state()
        while timeout_s > 0 and state != "stopped" and self._clock() < deadline:
            self._sleep(poll_s)
            state = self.acquisition_state()
        return state

    # -- measurement ---------------------------------------------------

    def measure(self, channel: Channel, parameter: Measurement) -> MeasurementResult:
        ch = _channel(channel)
        try:
            code = MEASUREMENTS[parameter]
        except KeyError:
            raise ValueError(f"Unknown measurement {parameter!r}; valid measurements are {sorted(MEASUREMENTS)}") from None
        reply = _payload(self._res.query(f"{ch}:PAVA? {code}"))
        name, _, raw = reply.partition(",")
        if name.strip().upper() != code or not raw:
            raise ValueError(f"Unexpected reply to {ch}:PAVA? {code}: {reply!r}")
        raw = raw.strip()
        unit = _MEASUREMENT_UNITS[parameter]
        if raw.startswith("*"):
            return MeasurementResult(channel=ch, parameter=parameter, value=None, unit=unit, valid=False)  # type: ignore[arg-type]
        value = _number(raw, f"{ch}:PAVA? {code}")
        reply_unit = _NUMBER.sub("", raw, count=1).strip().upper()
        if reply_unit and _UNIT_NAMES.get(reply_unit, reply_unit) != unit:
            raise ValueError(f"Unexpected unit in reply to {ch}:PAVA? {code}: {raw!r}")
        return MeasurementResult(channel=ch, parameter=parameter, value=value, unit=unit, valid=True)  # type: ignore[arg-type]

    # -- waveform ------------------------------------------------------

    def waveform(self, channel: Channel, max_points: int = 1000) -> Waveform:
        """Read the waveform of a channel from the acquisition memory.

        If the memory has more than max_points points, the driver sets a sparsing (WFSU SP)
        so that the result has max_points points or fewer. The driver sets WFSU for each transfer.
        """
        ch = _channel(channel)
        if not (1 <= max_points <= MAX_WAVEFORM_POINTS):
            raise ValueError(f"max_points must be from 1 to {MAX_WAVEFORM_POINTS}")
        if not _on_off(self._res.query(f"{ch}:TRA?"), f"{ch}:TRA?"):
            raise ValueError(f"Channel {ch} is off. Switch the channel on and acquire a signal first.")
        vdiv = _number(self._res.query(f"{ch}:VDIV?"), f"{ch}:VDIV?")
        offset = _number(self._res.query(f"{ch}:OFST?"), f"{ch}:OFST?")
        attenuation = _number(self._res.query(f"{ch}:ATTN?"), f"{ch}:ATTN?")
        tb = self.timebase()
        memory_points = int(_number(self._res.query(f"SANU? {ch}"), f"SANU? {ch}"))
        if memory_points < 1:
            raise ValueError(f"The acquisition memory of {ch} has no data. Start an acquisition first.")
        if tb.sample_rate_sa_s <= 0:
            raise ValueError(f"Unexpected sample rate {tb.sample_rate_sa_s} Sa/s")
        sparsing = max(1, math.ceil(memory_points / max_points))
        count = math.ceil(memory_points / sparsing)
        self._write(f"WFSU SP,{sparsing},NP,{count},FP,0", change=False)
        codes = self._read_block(f"{ch}:WF? DAT2")
        if not codes:
            raise IncompleteTransferError(f"The waveform of {ch} has no data points")
        if len(codes) > count:
            raise ValueError(f"The waveform of {ch} has {len(codes)} points, more than the requested {count}")
        signed = [c - 256 if c > 127 else c for c in codes]
        scale = vdiv / CODES_PER_DIVISION
        return Waveform(
            channel=ch,  # type: ignore[arg-type]
            sample_count=len(signed), memory_points=memory_points, sparsing=sparsing,
            first_sample_time_s=-tb.trigger_delay_s - tb.seconds_per_div * HORIZONTAL_DIVISIONS / 2,
            sample_interval_s=sparsing / tb.sample_rate_sa_s,
            seconds_per_div=tb.seconds_per_div, trigger_delay_s=tb.trigger_delay_s,
            sample_rate_sa_s=tb.sample_rate_sa_s, volts_per_div=vdiv, offset_v=offset,
            probe_attenuation=attenuation,
            off_screen_samples=sum(abs(c) > SCREEN_CODE_LIMIT for c in signed),
            voltages_v=[c * scale - offset for c in signed],
        )

    # -- math ----------------------------------------------------------

    def math(self) -> MathConfiguration:
        reply = _payload(self._res.query("DEF?"))
        m = re.fullmatch(r"EQN,['\"](.*)['\"]", reply.strip(), re.IGNORECASE)
        if m is None:
            raise ValueError(f"Unexpected reply to DEF?: {reply!r}")
        equation = m[1].strip().upper()
        if a := re.fullmatch(r"(C[1-4])([-+*/])(C[1-4])", equation):
            operation, source1, source2 = _MATH_OPERATORS[a[2]], a[1], a[3]
        elif f := re.fullmatch(r"(FFT|INTG|DIFF|SQRT)(C[1-4])", equation):
            operation, source1, source2 = _MATH_FUNCTIONS[f[1]], f[2], None
        else:
            raise ValueError(f"Unexpected reply to DEF?: {reply!r}")
        vdiv = position = None
        if operation in ARITHMETIC_OPERATIONS:
            vdiv = _number(self._res.query("MTVD?"), "MTVD?")
            position = _number(self._res.query("MTVP?"), "MTVP?") / MATH_POSITION_PER_DIV
        return MathConfiguration(
            operation=operation, equation=equation, source1=source1, source2=source2,  # type: ignore[arg-type]
            volts_per_div=vdiv, position_div=position,
        )

    def configure_math(
        self,
        operation: MathOperation,
        source1: Channel,
        source2: Channel | None = None,
        volts_per_div: float | None = None,
        position_div: float | None = None,
    ) -> MathConfiguration:
        """Set the math operation. The SDS1104X-E has no remote command to switch the math trace on or off."""
        a = _channel(source1)
        if operation in ARITHMETIC_OPERATIONS:
            if source2 is None:
                raise ValueError(f"{operation} needs source2")
            b = _channel(source2)
            equation = f"{a}{_MATH_SYMBOLS[operation]}{b}"
        elif operation in _MATH_PREFIXES:
            if source2 is not None:
                raise ValueError(f"{operation} has only one source")
            if volts_per_div is not None or position_div is not None:
                raise ValueError(f"{operation} has no volts_per_div or position_div setting")
            equation = f"{_MATH_PREFIXES[operation]}{a}"
        else:
            raise ValueError(f"operation must be one of {sorted([*ARITHMETIC_OPERATIONS, *_MATH_PREFIXES])}")
        vdiv_token = None
        if volts_per_div is not None:
            vdiv_token = _math_vdiv_token(volts_per_div)
        position = None
        if position_div is not None:
            if not (math.isfinite(position_div) and abs(position_div) * MATH_POSITION_PER_DIV <= MATH_POSITION_MAX):
                raise ValueError(f"position_div must be from {-MATH_POSITION_MAX / 50:g} to {MATH_POSITION_MAX / 50:g}")
            position = round(position_div * MATH_POSITION_PER_DIV)
        self._write(f"DEF EQN,'{equation}'")
        if vdiv_token is not None:
            self._write(f"MTVD {vdiv_token}")
        if position is not None:
            self._write(f"MTVP {position}")
        self._sleep(MATH_SETTLE_S)
        return self.math()

    def math_waveform(self, max_points: int = 1000) -> MathWaveform:
        """Read the math waveform (add, subtract, multiply or divide) from the acquisition memory.

        The oscilloscope ignores WFSU for the math trace and always sends all points (tested).
        Thus the driver transfers all points and keeps every n-th point.
        """
        if not (1 <= max_points <= MAX_WAVEFORM_POINTS):
            raise ValueError(f"max_points must be from 1 to {MAX_WAVEFORM_POINTS}")
        cfg = self.math()
        if cfg.operation not in ARITHMETIC_OPERATIONS or cfg.volts_per_div is None or cfg.position_div is None:
            raise ValueError(
                f"The math waveform is available only for {sorted(ARITHMETIC_OPERATIONS)}, not for {cfg.operation}. "
                "Use scope_screenshot to see the trace."
            )
        tb = self.timebase()
        source_points = int(_number(self._res.query(f"SANU? {cfg.source1}"), f"SANU? {cfg.source1}"))
        if source_points < 1 or tb.sample_rate_sa_s <= 0:
            raise ValueError("The acquisition memory has no data. Start an acquisition first.")
        codes = self._read_block("MATH:WF? DAT2")
        if not codes:
            raise IncompleteTransferError("The math waveform has no data points")
        decimation = max(1, math.ceil(len(codes) / max_points))
        kept = codes[::decimation]
        signed = [c - 256 if c > 127 else c for c in kept]
        scale = cfg.volts_per_div / CODES_PER_DIVISION
        offset = cfg.position_div * cfg.volts_per_div
        # Programming guide: interpolation factor = points of the block / SANU of the source.
        interval = source_points / (len(codes) * tb.sample_rate_sa_s)
        return MathWaveform(
            math=cfg, unit=_MATH_UNITS[cfg.operation], sample_count=len(signed), memory_points=len(codes),
            decimation=decimation,
            first_sample_time_s=-tb.trigger_delay_s - tb.seconds_per_div * HORIZONTAL_DIVISIONS / 2,
            sample_interval_s=interval * decimation,
            off_screen_samples=sum(abs(c) > SCREEN_CODE_LIMIT for c in signed),
            values=[c * scale - offset for c in signed],
        )

    # -- cursors -------------------------------------------------------

    def time_cursors(self, source: Channel) -> TimeCursors:
        ch = _channel(source)
        mode = _payload(self._res.query("CRMS?")).upper()
        if mode not in ("OFF", "MANUAL", "TRACK", "ON"):
            raise ValueError(f"Unexpected reply to CRMS?: {mode!r}")
        mode_name = {"OFF": "off", "MANUAL": "manual", "ON": "track", "TRACK": "track"}[mode]
        if mode_name != "manual":
            return TimeCursors(mode=mode_name, source=ch, x1_s=None, x2_s=None, delta_s=None, inverse_delta_hz=None)  # type: ignore[arg-type]
        # The order of the times in CRVA? HREL does not agree with the programming guide (tested).
        # Thus read the positions with CRST? and only the differences with CRVA?.
        positions = _pairs(self._res.query(f"{ch}:CRST? TREF,TDIF"), f"{ch}:CRST? TREF,TDIF")
        hrel = [p.strip() for p in _payload(self._res.query(f"{ch}:CRVA? HREL")).split(",")]
        if len(hrel) != 5 or hrel[0].upper() != "HREL":
            raise ValueError(f"Unexpected reply to {ch}:CRVA? HREL: {hrel!r}")
        try:
            x1, x2 = positions["TREF"], positions["TDIF"]
        except KeyError:
            raise ValueError(f"Unexpected reply to {ch}:CRST? TREF,TDIF: {positions!r}") from None
        return TimeCursors(
            mode=mode_name, source=ch, x1_s=x1, x2_s=x2,  # type: ignore[arg-type]
            delta_s=_number(hrel[1], f"{ch}:CRVA? HREL"), inverse_delta_hz=_number(hrel[2], f"{ch}:CRVA? HREL"),
        )

    def set_time_cursors(self, source: Channel, x1_s: float, x2_s: float) -> TimeCursors:
        """Show the manual X cursors at x1_s and x2_s (relative to the trigger point) and read the difference."""
        ch = _channel(source)
        tb = self.timebase()
        center = -tb.trigger_delay_s
        half = tb.seconds_per_div * HORIZONTAL_DIVISIONS / 2
        for name, value in (("x1_s", x1_s), ("x2_s", x2_s)):
            if not (math.isfinite(value) and abs(value - center) <= half * (1 + 1e-9)):
                raise ValueError(f"{name} must be in the screen range from {center - half:g} s to {center + half:g} s")
        if _payload(self._res.query("CRMS?")).upper() != "MANUAL":
            self._write("CRMS MANUAL")
        if _payload(self._res.query("CRTY?")).upper() not in ("X", "X-Y"):
            self._write("CRTY X")
        self._write(f"{ch}:CRST TREF,{x1_s:.6E}S,TDIF,{x2_s:.6E}S")
        return self.time_cursors(ch)  # type: ignore[arg-type]

    def cursors_off(self) -> None:
        self._write("CRMS OFF")

    # -- screenshot ----------------------------------------------------

    def screenshot_png(self) -> bytes:
        """Read the screen (SCREEN_DUMP, a BMP file) and return it as a PNG file.

        The screen shows changes with a delay of up to approximately 2 s (tested). Thus the driver
        waits until SCREEN_SETTLE_S after its last change before it reads the screen.
        """
        if self._last_change is not None:
            remaining = self._last_change + SCREEN_SETTLE_S - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._write("SCDP", change=False)
        data = bytearray()
        size = None
        while size is None or len(data) < size:
            chunk = self._res.read_raw()
            if not chunk:
                raise IncompleteTransferError(f"Incomplete screen dump: no more data after {len(data)} bytes")
            data += chunk
            if size is None and len(data) >= 6:
                if data[:2] != b"BM":
                    raise IncompleteTransferError(f"The screen dump is not a BMP file: {bytes(data[:16])!r}")
                size = int.from_bytes(data[2:6], "little")
        # The oscilloscope sends one byte more than the BMP size (tested): a terminating LF.
        return bmp_to_png(bytes(data[:size]))

    def _read_block(self, query: str) -> bytes:
        """Send query and read the reply: [header,]#9<length><data>\\n\\n (programming guide WF?)."""
        self._write(query, change=False)
        buffer = bytearray()
        start = length = None
        while True:
            chunk = self._res.read_raw()
            if not chunk:
                raise IncompleteTransferError(f"Incomplete reply to {query}: no more data after {len(buffer)} bytes")
            buffer += chunk
            if start is None:
                hash_index = buffer.find(b"#")
                if hash_index < 0 or len(buffer) < hash_index + 2:
                    if len(buffer) > 64:
                        raise IncompleteTransferError(f"No data block in the reply to {query}: {bytes(buffer[:64])!r}")
                    continue
                digits = chr(buffer[hash_index + 1])
                if not digits.isdigit() or digits == "0":
                    raise IncompleteTransferError(f"Invalid block header in the reply to {query}: {bytes(buffer[:32])!r}")
                n = int(digits)
                if len(buffer) < hash_index + 2 + n:
                    continue
                length_text = bytes(buffer[hash_index + 2:hash_index + 2 + n])
                if not length_text.isdigit():
                    raise IncompleteTransferError(f"Invalid block length in the reply to {query}: {length_text!r}")
                start, length = hash_index + 2 + n, int(length_text)
            assert length is not None
            if len(buffer) >= start + length + 2:
                break
        tail = bytes(buffer[start + length:])
        if tail != b"\n\n":
            raise IncompleteTransferError(f"Unexpected end of the reply to {query}: {tail[:16]!r}")
        return bytes(buffer[start:start + length])


def _channel(channel: str) -> str:
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {list(CHANNELS)}")
    return channel


def _payload(reply: str) -> str:
    """Remove the command header: "C1:VDIV 5.00E-01V" -> "5.00E-01V". With CHDR OFF, there is no header."""
    reply = reply.strip()
    head, sep, rest = reply.partition(" ")
    if sep and re.fullmatch(r"[A-Za-z0-9_:]+", head):
        return rest.strip()
    return reply


def _number(reply: str, query: str) -> float:
    text = _payload(reply)
    m = _NUMBER.match(text)
    if m is None:
        raise ValueError(f"Unexpected reply to {query}: {reply.strip()!r}")
    value = float(m[0])
    if not math.isfinite(value):
        raise ValueError(f"Unexpected reply to {query}: {reply.strip()!r}")
    return value


def _on_off(reply: str, query: str) -> bool:
    text = _payload(reply).upper()
    if text not in ("ON", "OFF"):
        raise ValueError(f"Unexpected reply to {query}: {reply.strip()!r}")
    return text == "ON"


def _tdiv_token(seconds_per_div: float) -> str:
    if math.isfinite(seconds_per_div) and seconds_per_div > 0:
        for value, token in TIME_DIVS.items():
            if math.isclose(value, seconds_per_div, rel_tol=1e-6):
                return token
    raise ValueError(
        f"seconds_per_div must be a value of the 1-2-5 sequence from 1e-9 to 100 s, not {seconds_per_div!r}"
    )


def _math_vdiv_token(volts_per_div: float) -> str:
    if math.isfinite(volts_per_div) and volts_per_div > 0:
        for value, token in MATH_VDIVS.items():
            if math.isclose(value, volts_per_div, rel_tol=1e-6):
                return token
    raise ValueError(
        f"volts_per_div of the math trace must be a value of the 1-2-5 sequence from 0.0005 to 100, not {volts_per_div!r}"
    )


def _pairs(reply: str, query: str) -> dict[str, float]:
    """Read "C1:CRST TREF,-5.00E-04S,TDIF,5.00E-04S" as {"TREF": -5e-4, "TDIF": 5e-4}."""
    parts = [p.strip() for p in _payload(reply).split(",")]
    if len(parts) % 2:
        raise ValueError(f"Unexpected reply to {query}: {reply.strip()!r}")
    return {name.upper(): _number(value, query) for name, value in zip(parts[::2], parts[1::2])}


def bmp_to_png(bmp: bytes) -> bytes:
    """Convert an uncompressed BMP file (16 bit RGB565 with bit fields, or 24 bit) to a PNG file.

    The SDS1104X-E sends 800 x 480 pixels with 16 bit RGB565, top-down (tested).
    """
    if len(bmp) < 54 or bmp[:2] != b"BM":
        raise ValueError("The data is not a BMP file")
    offset = int.from_bytes(bmp[10:14], "little")
    width = int.from_bytes(bmp[18:22], "little", signed=True)
    height = int.from_bytes(bmp[22:26], "little", signed=True)
    bpp = int.from_bytes(bmp[28:30], "little")
    compression = int.from_bytes(bmp[30:34], "little")
    top_down = height < 0
    height = abs(height)
    if width <= 0 or height == 0:
        raise ValueError(f"Invalid BMP size {width} x {height}")
    if bpp == 16 and compression == 3:
        masks = tuple(int.from_bytes(bmp[54 + 4 * i:58 + 4 * i], "little") for i in range(3))
        if masks != (0xF800, 0x07E0, 0x001F):
            raise ValueError(f"Unsupported BMP bit fields {[hex(m) for m in masks]}")
        table = [
            bytes((((v >> 11) & 0x1F) * 255 // 31, ((v >> 5) & 0x3F) * 255 // 63, (v & 0x1F) * 255 // 31))
            for v in range(65536)
        ]
    elif not (bpp == 24 and compression == 0):
        raise ValueError(f"Unsupported BMP format: {bpp} bit, compression {compression}")
    stride = (width * bpp // 8 + 3) & ~3
    if len(bmp) < offset + stride * height:
        raise ValueError("The BMP file is not complete")
    rows = []
    for y in range(height):
        start = offset + stride * (y if top_down else height - 1 - y)
        if bpp == 16:
            pixels = array.array("H", bmp[start:start + width * 2])
            if sys.byteorder == "big":
                pixels.byteswap()  # BMP data is little-endian
            rows.append(b"\x00" + b"".join([table[v] for v in pixels]))
        else:
            bgr = bmp[start:start + width * 3]
            rgb = bytearray(bgr)
            rgb[0::3], rgb[2::3] = bgr[2::3], bgr[0::3]
            rows.append(b"\x00" + bytes(rgb))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b"")
    )
