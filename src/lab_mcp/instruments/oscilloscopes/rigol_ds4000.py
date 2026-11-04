"""SCPI driver for the RIGOL DS4014 digital oscilloscope (DS4000 series).

The driver uses the commands of the RIGOL MSO4000/DS4000(E) Programming Guide (August 2016,
software 00.02.03). It returns the same models as the SIGLENT driver, so that the scope_* tools
operate both oscilloscopes in the same way.

The oscilloscope does not reply to an unknown query, and the connection then stays blocked
until a timeout (tested, firmware 00.02.02.SP5). Thus the driver sends only commands from the
programming guide.

The driver needs only a resource with ``write``, ``query`` and ``read_raw`` (for example, a pyvisa resource).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Protocol

from .siglent_sds1000xe import (
    MAX_WAVEFORM_POINTS,
    Channel,
    ChannelConfiguration,
    IncompleteTransferError,
    MathConfiguration,
    MathWaveform,
    Measurement,
    MeasurementResult,
    RunSweep,
    ScopeStatus,
    TimebaseConfiguration,
    TimeCursors,
    TriggerConfiguration,
    TriggerCoupling,
    TriggerSlope,
    TriggerSource,
    TriggerSweep,
    Waveform,
    bmp_to_png,
)

CHANNELS: tuple[str, ...] = ("C1", "C2", "C3", "C4")
HORIZONTAL_DIVISIONS = 14
# Smallest time base of each model in s/div (programming guide :TIMebase[:MAIN]:SCALe).
MIN_SECONDS_PER_DIV = {"DS4014": 5e-9}
MAX_SECONDS_PER_DIV = 1000.0
# Vertical scale at the input (probe 1X): 1 mV/div to 5 V/div at 1 MOhm, 1 mV/div to 1 V/div at 50 Ohm.
VDIV_MIN_V = 1e-3
VDIV_MAX_V = {"OMEG": 5.0, "FIFT": 1.0}
ATTENUATIONS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
# The screen data of the NORMal waveform mode has 1400 points (programming guide :WAVeform:POINts).
SCREEN_POINTS = 1400
# :WAVeform:DATA? codes: 0 is the screen bottom and 255 the screen top. Codes at the limits can be clipped.
CODE_MIN, CODE_MAX = 0, 255
# A query sends 9.9e37 when the measurement is not valid.
INVALID_VALUE = 9.9e37
# The screen shows changes with a delay. Wait this time after a change before a screenshot.
SCREEN_SETTLE_S = 1.0
# After :SINGle, :TRIGger:STATus? can send the previous STOP for up to 0.5 s, and the
# :WAVeform:PREamble? point count is 0 until the new acquisition is complete (tests on the DS4014).
# The driver does not accept STOP as the end of the acquisition before it sees the oscilloscope busy,
# or before this time.
SINGLE_STALE_S = 0.6
# Some acquisitions after :SINGle stop without data (point count 0 for more than 8 s).
# After this time, the driver sends :SINGle one more time.
EMPTY_RETRY_S = 1.0
# Maximum time that the driver waits for the new state after :RUN or :STOP.
STATE_WAIT_S = 1.2
# Time that waveform() waits for a completed acquisition (point count more than 0).
WAVEFORM_WAIT_S = 2.0

_CHANNEL_CODES = {c: f"CHAN{c[1]}" for c in CHANNELS}
_COUPLING_CODES = {"dc": "DC", "ac": "AC", "gnd": "GND"}
_COUPLING_NAMES = {"DC": "dc", "AC": "ac", "GND": "gnd"}
_SOURCE_CODES = {**{c: f"CHANnel{c[1]}" for c in CHANNELS}, "EX": "EXT", "EX5": "EXT5", "LINE": "ACLine"}
_SOURCE_NAMES = {**{f"CHAN{c[1]}": c for c in CHANNELS}, "EXT": "EX", "EXT5": "EX5", "ACL": "LINE"}
_SLOPE_CODES = {"rising": "POSitive", "falling": "NEGative", "alternating": "RFALl"}
_SLOPE_NAMES = {"POS": "rising", "NEG": "falling", "RFAL": "alternating"}
_TRIG_COUPLING_CODES = {"dc": "DC", "ac": "AC", "hf_reject": "HFReject", "lf_reject": "LFReject"}
_TRIG_COUPLING_NAMES = {"DC": "dc", "AC": "ac", "HFR": "hf_reject", "LFR": "lf_reject"}
_SWEEP_CODES = {"auto": "AUTO", "normal": "NORMal", "single": "SINGle"}
_SWEEP_NAMES = {"AUTO": "auto", "NORM": "normal", "SING": "single"}
_STATUS_NAMES = {"TD": "triggered", "WAIT": "armed", "RUN": "ready", "AUTO": "auto", "STOP": "stopped"}

# :MEASure:<item>? for each measurement name of the scope_measure tool.
MEASUREMENTS: dict[str, str] = {
    "pkpk": "VPP", "max": "VMAX", "min": "VMIN", "ampl": "VAMP", "top": "VTOP", "base": "VBASe",
    "mean": "VAVG", "rms": "VRMS", "crms": "PVRMs", "ovsp": "OVERshoot", "rpre": "PREShoot",
    "per": "PERiod", "freq": "FREQuency", "pwid": "PWIDth", "nwid": "NWIDth", "rise": "RTIMe",
    "fall": "FTIMe", "duty": "PDUTy", "nduty": "NDUTy",
}
_MEASUREMENT_UNITS = {
    **{p: "V" for p in ("pkpk", "max", "min", "ampl", "top", "base", "mean", "rms", "crms")},
    **{p: "%" for p in ("ovsp", "rpre", "duty", "nduty")},
    **{p: "s" for p in ("per", "pwid", "nwid", "rise", "fall")},
    "freq": "Hz",
}
# The oscilloscope sends these measurements as a ratio (0.5 for 50 %). The driver multiplies by 100.
_PERCENT_MEASUREMENTS = {"ovsp", "rpre", "duty", "nduty"}


class SCPIResource(Protocol):
    def write(self, message: str) -> object: ...
    def query(self, message: str) -> str: ...
    def read_raw(self, size: int | None = None) -> bytes: ...


class RigolDS4000:
    def __init__(self, resource: SCPIResource, model: str = "DS4014",
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        if model not in MIN_SECONDS_PER_DIV:
            raise ValueError(f"Unsupported model {model!r}; supported models are {sorted(MIN_SECONDS_PER_DIV)}")
        self._res = resource
        self.model = model
        self._sleep = sleep
        self._clock = clock
        # The driver remembers the last continuous sweep (auto or normal) that it read or set,
        # because the sweep is single after scope_single.
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
        sweep = self._res.query(":TRIGger:SWEep?").strip().upper()
        if sweep not in _SWEEP_NAMES:
            raise ValueError(f"Unexpected reply to :TRIGger:SWEep?: {sweep!r}")
        if sweep in ("AUTO", "NORM"):
            self._continuous_sweep = sweep
        return sweep

    def acquisition_state(self) -> str:
        reply = self._res.query(":TRIGger:STATus?").strip().upper()
        try:
            return _STATUS_NAMES[reply]
        except KeyError:
            raise ValueError(f"Unexpected reply to :TRIGger:STATus?: {reply!r}") from None

    def status(self) -> ScopeStatus:
        return ScopeStatus(
            acquisition=self.acquisition_state(), timebase=self.timebase(),
            trigger=self.trigger(), channels=[self.channel(c) for c in CHANNELS],  # type: ignore[arg-type]
        )

    # -- channels ------------------------------------------------------

    def channel(self, channel: Channel) -> ChannelConfiguration:
        n = _channel(channel)[1]
        coupling = self._res.query(f":CHANnel{n}:COUPling?").strip().upper()
        impedance = self._res.query(f":CHANnel{n}:IMPedance?").strip().upper()
        if coupling not in _COUPLING_NAMES or impedance not in VDIV_MAX_V:
            raise ValueError(f"Unexpected coupling {coupling!r} or impedance {impedance!r} of CH{n}")
        name = _COUPLING_NAMES[coupling]
        if impedance == "FIFT" and name != "gnd":
            name = f"{name}_50_ohm"
        bandwidth = self._res.query(f":CHANnel{n}:BWLimit?").strip().upper()
        if bandwidth not in ("20M", "100M", "OFF"):
            raise ValueError(f"Unexpected reply to :CHANnel{n}:BWLimit?: {bandwidth!r}")
        return ChannelConfiguration(
            channel=channel,
            displayed=_bool(self._res.query(f":CHANnel{n}:DISPlay?"), f":CHANnel{n}:DISPlay?"),
            volts_per_div=self._number(f":CHANnel{n}:SCALe?"),
            offset_v=self._number(f":CHANnel{n}:OFFSet?"),
            coupling=name,
            probe_attenuation=self._number(f":CHANnel{n}:PROBe?"),
            bandwidth_limit=bandwidth != "OFF",
        )

    def configure_channel(
        self,
        channel: Channel,
        *,
        displayed: bool | None = None,
        volts_per_div: float | None = None,
        offset_v: float | None = None,
        coupling: str | None = None,
        probe_attenuation: float | None = None,
        bandwidth_limit: bool | None = None,
    ) -> ChannelConfiguration:
        """Set the configuration of one channel. Values that are not given stay the same.

        The oscilloscope limits the offset to its range and reads back the scale of the 1-2-5 sequence.
        """
        n = _channel(channel)[1]
        if coupling is not None and coupling not in _COUPLING_CODES:
            raise ValueError(f"coupling must be one of {sorted(_COUPLING_CODES)}")
        if probe_attenuation is not None and not any(math.isclose(probe_attenuation, a) for a in ATTENUATIONS):
            raise ValueError(f"probe_attenuation must be one of {list(ATTENUATIONS)}")
        if offset_v is not None and not math.isfinite(offset_v):
            raise ValueError("offset_v must be a finite number")
        if volts_per_div is not None:
            attenuation = probe_attenuation if probe_attenuation is not None else self._number(f":CHANnel{n}:PROBe?")
            impedance = self._res.query(f":CHANnel{n}:IMPedance?").strip().upper()
            low, high = VDIV_MIN_V * attenuation, VDIV_MAX_V.get(impedance, 5.0) * attenuation
            if not (math.isfinite(volts_per_div) and low * (1 - 1e-9) <= volts_per_div <= high * (1 + 1e-9)):
                raise ValueError(
                    f"volts_per_div must be from {low:g} to {high:g} V/div for probe {attenuation:g}X, not {volts_per_div!r}"
                )
        # The probe ratio changes the scale range. Thus set it first.
        if probe_attenuation is not None:
            self._write(f":CHANnel{n}:PROBe {probe_attenuation:g}")
        if displayed is not None:
            self._write(f":CHANnel{n}:DISPlay {'ON' if displayed else 'OFF'}")
        if coupling is not None:
            self._write(f":CHANnel{n}:COUPling {_COUPLING_CODES[coupling]}")
        if bandwidth_limit is not None:
            self._write(f":CHANnel{n}:BWLimit {'20M' if bandwidth_limit else 'OFF'}")
        if volts_per_div is not None:
            self._write(f":CHANnel{n}:SCALe {volts_per_div:.6E}")
        if offset_v is not None:
            self._write(f":CHANnel{n}:OFFSet {offset_v:.6E}")
        return self.channel(channel)

    # -- timebase ------------------------------------------------------

    def timebase(self) -> TimebaseConfiguration:
        # :TIMebase:MAIN:OFFSet is the time of the screen center after the trigger (tested:
        # :WAVeform:XORigin? = offset - 7 div). trigger_delay_s has the sign of the SIGLENT
        # driver: the screen center is at -trigger_delay_s.
        return TimebaseConfiguration(
            seconds_per_div=self._number(":TIMebase:MAIN:SCALe?"),
            trigger_delay_s=-self._number(":TIMebase:MAIN:OFFSet?"),
            horizontal_divisions=HORIZONTAL_DIVISIONS,
            sample_rate_sa_s=self._number(":ACQuire:SRATe?"),
        )

    def configure_timebase(
        self, seconds_per_div: float | None = None, trigger_delay_s: float | None = None
    ) -> TimebaseConfiguration:
        """Set the time base (a value of the 1-2-5 sequence) and the trigger delay.

        The oscilloscope sets a delay outside its limit to the nearest limit.
        """
        if seconds_per_div is not None:
            _check_1_2_5(seconds_per_div, MIN_SECONDS_PER_DIV[self.model], MAX_SECONDS_PER_DIV)
        if trigger_delay_s is not None and not math.isfinite(trigger_delay_s):
            raise ValueError("trigger_delay_s must be a finite number")
        if seconds_per_div is not None:
            self._write(f":TIMebase:MAIN:SCALe {seconds_per_div:.6E}")
        if trigger_delay_s is not None:
            self._write(f":TIMebase:MAIN:OFFSet {-trigger_delay_s:.6E}")
        return self.timebase()

    # -- trigger -------------------------------------------------------

    def trigger(self) -> TriggerConfiguration:
        sweep = self._sweep_code()
        trig_type = self._res.query(":TRIGger:MODE?").strip().upper()
        source = slope = level = coupling = None
        if trig_type == "EDGE":
            code = self._res.query(":TRIGger:EDGe:SOURce?").strip().upper()
            if code not in _SOURCE_NAMES:
                raise ValueError(f"Unexpected reply to :TRIGger:EDGe:SOURce?: {code!r}")
            source = _SOURCE_NAMES[code]
            if source != "LINE":
                slope_code = self._res.query(":TRIGger:EDGe:SLOPe?").strip().upper()
                slope = _SLOPE_NAMES.get(slope_code, slope_code.lower())
                level = self._number(":TRIGger:EDGe:LEVel?")
                coupling_code = self._res.query(":TRIGger:COUPling?").strip().upper()
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

        The oscilloscope sets a level outside its limit (6 div from the center for channels,
        +/-0.8 V for EX, +/-4 V for EX5) to the nearest limit.
        """
        if source is not None and source not in _SOURCE_CODES:
            raise ValueError(f"source must be one of {list(_SOURCE_CODES)}")
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
        if present.type != "EDGE":
            self._write(":TRIGger:MODE EDGE")
        if source is not None:
            self._write(f":TRIGger:EDGe:SOURce {_SOURCE_CODES[source]}")
        if slope is not None:
            self._write(f":TRIGger:EDGe:SLOPe {_SLOPE_CODES[slope]}")
        if coupling is not None:
            self._write(f":TRIGger:COUPling {_TRIG_COUPLING_CODES[coupling]}")
        if level_v is not None:
            self._write(f":TRIGger:EDGe:LEVel {level_v:.6E}")
        if sweep is not None and _SWEEP_NAMES[self._sweep_code()] != sweep:
            self._write(f":TRIGger:SWEep {_SWEEP_CODES[sweep]}")
        return self.trigger()

    # -- acquisition ---------------------------------------------------

    def run(self, sweep: RunSweep | None = None) -> str:
        """Start continuous acquisition with the auto or normal sweep.

        Without sweep, the driver uses the present sweep, or the last continuous sweep that it knows.
        """
        if sweep is not None and sweep not in ("auto", "normal"):
            raise ValueError("sweep must be auto or normal")
        present = self._sweep_code()
        if sweep is not None:
            code = "AUTO" if sweep == "auto" else "NORM"
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
            self._write(f":TRIGger:SWEep {_SWEEP_CODES[_SWEEP_NAMES[code]]}")
            self._continuous_sweep = code
        self._write(":RUN")
        return self._wait_state(stopped=False, poll_s=0.1)

    def stop(self, poll_s: float = 0.1) -> str:
        self._sweep_code()  # remember the present continuous sweep for run()
        self._write(":STOP")
        return self._wait_state(stopped=True, poll_s=poll_s)

    def _wait_state(self, stopped: bool, poll_s: float) -> str:
        # :TRIGger:STATus? can send the previous state for up to 0.5 s after :RUN or :STOP.
        start = self._clock()
        state = self.acquisition_state()
        while (state == "stopped") != stopped and self._clock() - start < STATE_WAIT_S:
            self._sleep(poll_s)
            state = self.acquisition_state()
        return state

    def single(self, timeout_s: float = 0.0, poll_s: float = 0.1) -> str:
        """Start one acquisition. Wait a maximum of timeout_s until the oscilloscope stops after the trigger.

        Return the acquisition state. If no trigger occurs in timeout_s, the state is armed or ready.
        """
        if not (0 <= timeout_s <= 60):
            raise ValueError("timeout_s must be from 0 to 60")
        self._sweep_code()  # remember the present continuous sweep for run()
        # The point count of a displayed channel shows when the acquisition is complete.
        source = next((c for c in CHANNELS if self._res.query(f":CHANnel{c[1]}:DISPlay?").strip() in ("1", "ON")), None)
        if source is not None:
            self._write(f":WAVeform:SOURce CHANnel{source[1]}", change=False)
            self._write(":WAVeform:MODE NORMal", change=False)
        # :SINGle during a running acquisition can stop the oscilloscope without data (tests on the DS4014).
        if self.acquisition_state() != "stopped":
            self._write(":STOP", change=False)
            self._wait_state(stopped=True, poll_s=poll_s)
        self._write(":SINGle")
        start = self._clock()
        busy = False
        empty_since: float | None = None
        retried = False
        while True:
            state = self.acquisition_state()
            complete = source is None or self._preamble_points() > 0
            if state != "stopped" or not complete:
                busy = True
            elif busy or self._clock() - start >= SINGLE_STALE_S:
                return state
            if state == "stopped" and not complete:
                # Stopped without data: send :SINGle one more time.
                empty_since = self._clock() if empty_since is None else empty_since
                if not retried and self._clock() - empty_since >= EMPTY_RETRY_S:
                    self._write(":SINGle")
                    retried, empty_since = True, None
            else:
                empty_since = None
            if self._clock() - start >= timeout_s:
                # A STOP before the end of the acquisition is the previous state.
                return "armed" if state == "stopped" else state
            self._sleep(poll_s)

    # -- measurement ---------------------------------------------------

    def measure(self, channel: Channel, parameter: Measurement) -> MeasurementResult:
        n = _channel(channel)[1]
        try:
            item = MEASUREMENTS[parameter]
        except KeyError:
            raise ValueError(
                f"The {self.model} has no measurement {parameter!r}. Use one of {sorted(MEASUREMENTS)}."
            ) from None
        query = f":MEASure:{item}? CHANnel{n}"
        value = self._number(query, allow_invalid=True)
        unit = _MEASUREMENT_UNITS[parameter]
        if abs(value) >= INVALID_VALUE * 0.99:
            return MeasurementResult(channel=channel, parameter=parameter, value=None, unit=unit, valid=False)
        if parameter in _PERCENT_MEASUREMENTS:
            value *= 100
        return MeasurementResult(channel=channel, parameter=parameter, value=value, unit=unit, valid=True)

    # -- waveform ------------------------------------------------------

    def waveform(self, channel: Channel, max_points: int = 1000) -> Waveform:
        """Read the waveform of a channel as shown on the screen (:WAVeform:MODE NORMal, 1400 points).

        If max_points is less than 1400, the driver keeps every n-th point.
        """
        n = _channel(channel)[1]
        if not (1 <= max_points <= MAX_WAVEFORM_POINTS):
            raise ValueError(f"max_points must be from 1 to {MAX_WAVEFORM_POINTS}")
        config = self.channel(channel)
        if not config.displayed:
            raise ValueError(f"Channel {channel} is off. Switch the channel on and acquire a signal first.")
        tb = self.timebase()
        self._write(f":WAVeform:SOURce CHANnel{n}", change=False)
        self._write(":WAVeform:MODE NORMal", change=False)
        self._write(":WAVeform:FORMat BYTE", change=False)
        deadline = self._clock() + WAVEFORM_WAIT_S
        while True:
            fields = self._preamble()
            if int(float(fields[2])) > 0:
                break
            if self._clock() >= deadline:
                raise ValueError(
                    f"No completed acquisition of {channel}. The oscilloscope waits for a trigger. "
                    "Examine the trigger settings or use scope_run with the auto sweep."
                )
            self._sleep(0.1)
        try:
            x_increment, x_origin = float(fields[4]), float(fields[5])
            y_increment, y_origin, y_reference = float(fields[7]), float(fields[8]), float(fields[9])
        except ValueError:
            raise ValueError(f"Unexpected reply to :WAVeform:PREamble?: {','.join(fields)!r}") from None
        codes = self._read_block(":WAVeform:DATA?")
        if not codes:
            raise IncompleteTransferError(f"The waveform of {channel} has no data points")
        sparsing = max(1, math.ceil(len(codes) / max_points))
        kept = codes[::sparsing]
        return Waveform(
            channel=channel,
            sample_count=len(kept), memory_points=len(codes), sparsing=sparsing,
            first_sample_time_s=x_origin, sample_interval_s=x_increment * sparsing,
            seconds_per_div=tb.seconds_per_div, trigger_delay_s=tb.trigger_delay_s,
            sample_rate_sa_s=tb.sample_rate_sa_s, volts_per_div=config.volts_per_div, offset_v=config.offset_v,
            probe_attenuation=config.probe_attenuation,
            off_screen_samples=sum(c <= CODE_MIN or c >= CODE_MAX for c in kept),
            voltages_v=[(c - y_reference - y_origin) * y_increment for c in kept],
        )

    # -- math and cursors ----------------------------------------------

    def _not_supported(self, what: str) -> ValueError:
        return ValueError(f"{what} is not supported for the RIGOL {self.model} in this version. Use scope_screenshot.")

    def math(self) -> MathConfiguration:
        raise self._not_supported("Math")

    def configure_math(self, *args: object, **kwargs: object) -> MathConfiguration:
        raise self._not_supported("Math")

    def math_waveform(self, max_points: int = 1000) -> MathWaveform:
        raise self._not_supported("Math")

    def time_cursors(self, source: Channel) -> TimeCursors:
        raise self._not_supported("Cursors")

    def set_time_cursors(self, source: Channel, x1_s: float, x2_s: float) -> TimeCursors:
        raise self._not_supported("Cursors")

    def cursors_off(self) -> None:
        raise self._not_supported("Cursors")

    # -- screenshot ----------------------------------------------------

    def screenshot_png(self) -> bytes:
        """Read the screen (:DISPlay:DATA?, a 24 bit BMP file of 800 x 480 pixels) and return it as a PNG file."""
        if self._last_change is not None:
            remaining = self._last_change + SCREEN_SETTLE_S - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        bmp = self._read_block(":DISPlay:DATA?")
        if bmp[:2] != b"BM":
            raise IncompleteTransferError(f"The screen data is not a BMP file: {bmp[:16]!r}")
        return bmp_to_png(bmp)

    # -- helpers -------------------------------------------------------

    def _number(self, query: str, allow_invalid: bool = False) -> float:
        reply = self._res.query(query).strip()
        try:
            value = float(reply)
        except ValueError:
            raise ValueError(f"Unexpected reply to {query}: {reply!r}") from None
        if not math.isfinite(value) and not allow_invalid:
            raise ValueError(f"Unexpected reply to {query}: {reply!r}")
        return value

    def _preamble(self) -> list[str]:
        fields = self._res.query(":WAVeform:PREamble?").strip().split(",")
        try:
            float(fields[2])
        except (IndexError, ValueError):
            raise ValueError(f"Unexpected reply to :WAVeform:PREamble?: {','.join(fields)!r}") from None
        if len(fields) != 10:
            raise ValueError(f"Unexpected reply to :WAVeform:PREamble?: {','.join(fields)!r}")
        return fields

    def _preamble_points(self) -> int:
        return int(float(self._preamble()[2]))

    def _read_block(self, query: str) -> bytes:
        """Send query and read the reply: #9<length><data>, then an LF (programming guide :WAVeform:DATA?)."""
        self._write(query, change=False)
        buffer = bytearray()
        start = length = None
        while True:
            chunk = self._res.read_raw()
            if not chunk:
                raise IncompleteTransferError(f"Incomplete reply to {query}: no more data after {len(buffer)} bytes")
            buffer += chunk
            if start is None:
                if len(buffer) < 2:
                    continue
                if buffer[:1] != b"#" or not chr(buffer[1]).isdigit() or buffer[1:2] == b"0":
                    raise IncompleteTransferError(f"Invalid block header in the reply to {query}: {bytes(buffer[:16])!r}")
                digits = int(chr(buffer[1]))
                if len(buffer) < 2 + digits:
                    continue
                length_text = bytes(buffer[2:2 + digits])
                if not length_text.isdigit():
                    raise IncompleteTransferError(f"Invalid block length in the reply to {query}: {length_text!r}")
                start, length = 2 + digits, int(length_text)
            assert length is not None
            if len(buffer) >= start + length + 1:
                break
        tail = bytes(buffer[start + length:])
        if tail != b"\n":
            raise IncompleteTransferError(f"Unexpected end of the reply to {query}: {tail[:16]!r}")
        return bytes(buffer[start:start + length])


def _channel(channel: str) -> str:
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {list(CHANNELS)}")
    return channel


def _bool(reply: str, query: str) -> bool:
    text = reply.strip().upper()
    if text not in ("1", "0", "ON", "OFF"):
        raise ValueError(f"Unexpected reply to {query}: {reply.strip()!r}")
    return text in ("1", "ON")


def _check_1_2_5(seconds_per_div: float, minimum: float, maximum: float) -> None:
    if math.isfinite(seconds_per_div) and minimum * (1 - 1e-9) <= seconds_per_div <= maximum * (1 + 1e-9):
        exponent = math.floor(math.log10(seconds_per_div) + 1e-9)
        mantissa = seconds_per_div / 10**exponent
        if any(math.isclose(mantissa, m, rel_tol=1e-6) for m in (1, 2, 5, 10)):
            return
    raise ValueError(
        f"seconds_per_div must be a value of the 1-2-5 sequence from {minimum:g} to {maximum:g} s, not {seconds_per_div!r}"
    )
