"""SCPI driver for the SIGLENT SDS1104X-E digital oscilloscope.

The driver uses the commands of the SIGLENT Digital Oscilloscope Series Programming Guide
PG01-E02D (SDS1000X-E section). The replies have the format of the COMM_HEADER setting
(CHDR SHORT, LONG or OFF). The driver reads all three formats and does not change CHDR.

The driver needs only a resource with ``write``, ``query`` and ``read_raw`` (for example, a pyvisa resource).
"""

from __future__ import annotations

import math
import re
import time
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

    @property
    def resource(self) -> SCPIResource:
        return self._res

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
            self._res.write(f"{ch}:ATTN {probe_attenuation:g}")
        if coupling is not None:
            self._res.write(f"{ch}:CPL {_COUPLING_CODES[coupling]}")
        if volts_per_div is not None:
            self._res.write(f"{ch}:VDIV {volts_per_div:.6E}V")
        if offset_v is not None:
            self._res.write(f"{ch}:OFST {offset_v:.6E}V")
        if bandwidth_limit is not None:
            self._res.write(f"BWL {ch},{'ON' if bandwidth_limit else 'OFF'}")
        if displayed is not None:
            self._res.write(f"{ch}:TRA {'ON' if displayed else 'OFF'}")
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
            self._res.write(f"TDIV {token}")
        if trigger_delay_s is not None:
            self._res.write(f"TRDL {trigger_delay_s:.6E}S")
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
            self._res.write(f"TRSE EDGE,SR,{target},HT,OFF")
        if slope is not None:
            self._res.write(f"{target}:TRSL {_SLOPE_CODES[slope]}")
        if coupling is not None:
            self._res.write(f"{target}:TRCP {_TRIG_COUPLING_CODES[coupling]}")
        if level_v is not None:
            self._res.write(f"{target}:TRLV {level_v:.6E}V")
        # A second TRMD SINGLE stops the acquisition (tested). Thus do not send the present sweep again.
        if sweep is not None and _SWEEP_CODES[sweep] != self._sweep_code():
            self._res.write(f"TRMD {_SWEEP_CODES[sweep]}")
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
            self._res.write(f"TRMD {code}")
            self._continuous_sweep = code
        return self.acquisition_state()

    def stop(self) -> str:
        self._sweep_code()  # remember the present continuous sweep for run()
        self._res.write("STOP")
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
            self._res.write("TRMD SINGLE")
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
        self._res.write(f"WFSU SP,{sparsing},NP,{count},FP,0")
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

    def _read_block(self, query: str) -> bytes:
        """Send query and read the reply: [header,]#9<length><data>\\n\\n (programming guide WF?)."""
        self._res.write(query)
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
