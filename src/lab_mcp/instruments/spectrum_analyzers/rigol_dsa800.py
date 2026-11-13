"""SCPI driver for the RIGOL DSA832E spectrum analyzer (DSA800 series).

The driver uses the commands of the RIGOL DSA800 Programming Guide (August 2016). The guide
uses the DSA875 as the reference model. The DSA832E ranges below are from tests on the
instrument (firmware 00.01.06.00.04).

The analyzer does not reply to an unknown query, and the connection then stays blocked until a
timeout. It also does not reply to :CALCulate:MARKer<n>:X? when the marker is off. Thus the
driver sends only commands from the programming guide, and reads a marker only when it is on.

The analyzer changes an invalid value to the nearest valid value without an error
(for example RBW 2 kHz to 3 kHz). Thus the driver checks all values before it sends them.

The driver needs only a resource with ``write``, ``query`` and ``read_raw`` (for example, a pyvisa resource).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from lab_mcp.instruments.oscilloscopes.siglent_sds1000xe import bmp_to_png

# Largest frequency of each model in Hz. The analyzer sets the center frequency to a maximum of
# 3.2 GHz - 50 Hz, and to a minimum of 50 Hz.
MAX_FREQUENCY_HZ = {"DSA832E": 3.2e9}
# The span is 0 Hz (zero span) or 100 Hz to the largest frequency.
MIN_SPAN_HZ = 100.0
# RBW and VBW in the 1-3-10 sequence. With the EMI filter, the RBW is 200 Hz, 9 kHz or 120 kHz.
# The quasi-peak detector switches the EMI filter on, and the analyzer then ignores EMIFilter:STATe OFF.
# Another detector does not switch the EMI filter off (tested).
RBW_VALUES_HZ = tuple(m * 10.0**e for e in range(1, 7) for m in (1, 3) if m * 10.0**e <= 1e6)
EMI_RBW_VALUES_HZ = (200.0, 9e3, 120e3)
VBW_VALUES_HZ = tuple(m * 10.0**e for e in range(0, 7) for m in (1, 3) if m * 10.0**e <= 3e6)
# The minimum sweep time also depends on the span. The analyzer then uses the nearest possible value.
SWEEP_TIME_RANGE_S = (20e-6, 7500.0)
SWEEP_POINTS_RANGE = (101, 3001)
REFERENCE_LEVEL_RANGE_DBM = (-100.0, 20.0)
SCALE_RANGE_DB = (0.1, 20.0)
ATTENUATION_RANGE_DB = (0, 30)
TRACES = (1, 2, 3)
MARKERS = (1, 2, 3, 4)
SINGLE_POLL_S = 0.1

Detector = Literal["positive_peak", "negative_peak", "normal", "rms", "sample", "voltage_average", "quasi_peak"]
TraceMode = Literal["clear_write", "max_hold", "min_hold", "view", "blank", "video_average", "power_average"]
PeakSearch = Literal["max", "next", "left", "right", "min"]

_DETECTOR_CODES = {
    "positive_peak": "POSitive", "negative_peak": "NEGative", "normal": "NORMal", "rms": "RMS",
    "sample": "SAMPle", "voltage_average": "VAVerage", "quasi_peak": "QPEak",
}
_DETECTOR_NAMES = {
    "POS": "positive_peak", "NEG": "negative_peak", "NORM": "normal", "RMS": "rms",
    "SAMP": "sample", "VAV": "voltage_average", "QPEAK": "quasi_peak", "QPE": "quasi_peak",
}
_TRACE_MODE_CODES = {
    "clear_write": "WRITe", "max_hold": "MAXHold", "min_hold": "MINHold", "view": "VIEW",
    "blank": "BLANk", "video_average": "VIDeoavg", "power_average": "POWeravg",
}
_TRACE_MODE_NAMES = {
    "WRIT": "clear_write", "MAXH": "max_hold", "MINH": "min_hold", "VIEW": "view",
    "BLANK": "blank", "BLAN": "blank", "VID": "video_average", "POW": "power_average",
}
_PEAK_COMMANDS = {
    "max": "MAXimum:MAX", "next": "MAXimum:NEXT", "left": "MAXimum:LEFT", "right": "MAXimum:RIGHt", "min": "MINimum",
}
_UNIT_NAMES = {"DBM": "dBm", "DBMV": "dBmV", "DBUV": "dBuV", "V": "V", "W": "W"}


class SCPIResource(Protocol):
    def write(self, message: str) -> object: ...
    def query(self, message: str) -> str: ...
    def read_raw(self, size: int | None = None) -> bytes: ...


class IncompleteTransferError(OSError):
    """The data block is not complete. The connection is then in an unknown state."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FrequencyConfiguration(_Model):
    center_hz: float
    span_hz: float = Field(description="0 is zero span: the X axis is time, and the analyzer measures only at center_hz")
    start_hz: float
    stop_hz: float


class BandwidthConfiguration(_Model):
    rbw_hz: float = Field(description="Resolution bandwidth")
    rbw_auto: bool = Field(description="True if the analyzer sets the RBW from the span")
    emi_filter: bool = Field(description=(
        "True: EMI filter (-6 dB bandwidth, RBW 200 Hz, 9 kHz or 120 kHz). False: Gauss filter (-3 dB bandwidth)."
    ))
    vbw_hz: float = Field(description="Video bandwidth")
    vbw_auto: bool
    sweep_time_s: float
    sweep_time_auto: bool
    sweep_points: int = Field(description="Number of points of each trace")
    detector: str = Field(description="positive_peak, negative_peak, normal, rms, sample, voltage_average or quasi_peak")


class AmplitudeConfiguration(_Model):
    reference_level: float = Field(description="Reference level (top line of the screen) in unit")
    unit: str = Field(description="Amplitude unit: dBm, dBmV, dBuV, V or W")
    scale_type: str = Field(description="log or linear")
    scale_db_per_div: float = Field(description="Vertical scale in dB/div (only for the log scale)")
    attenuation_db: float = Field(description="Input attenuation")
    attenuation_auto: bool
    preamplifier: bool = Field(description="True if the preamplifier is on")


class TraceConfiguration(_Model):
    trace: int
    mode: str = Field(description="clear_write, max_hold, min_hold, view, blank, video_average or power_average")


class AnalyzerStatus(_Model):
    sweep: str = Field(description="continuous or single")
    sweep_count: int = Field(description="Number of sweeps of one single sweep")
    frequency: FrequencyConfiguration
    bandwidth: BandwidthConfiguration
    amplitude: AmplitudeConfiguration
    traces: list[TraceConfiguration]


class SweepResult(_Model):
    sweep: str = Field(description="continuous or single")
    complete: bool = Field(description="True if the single sweep is complete. Always false for the continuous sweep.")
    sweeps_done: int = Field(description="Number of complete sweeps of the single sweep")
    sweep_count: int = Field(description="Number of sweeps of one single sweep")


class TraceData(_Model):
    trace: int
    mode: str = Field(description="Trace mode, for example clear_write or max_hold")
    unit: str = Field(description="Unit of amplitudes")
    x_unit: str = Field(description="Hz, or s in zero span")
    trace_points: int = Field(description="Number of points of the trace in the analyzer")
    decimation: int = Field(description="Each value in this result is the largest of this number of trace points")
    x: list[float] = Field(description="Frequency (or time in zero span) of each value")
    amplitudes: list[float] = Field(description="Amplitude of each value in unit")


class MarkerResult(_Model):
    marker: int
    x: float = Field(description="Frequency of the marker in Hz (time in s in zero span)")
    x_unit: str
    amplitude: float = Field(description="Amplitude at the marker in unit")
    unit: str


class RigolDSA800:
    def __init__(self, resource: SCPIResource, model: str = "DSA832E",
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        if model not in MAX_FREQUENCY_HZ:
            raise ValueError(f"Unsupported model {model!r}; supported models are {sorted(MAX_FREQUENCY_HZ)}")
        self._res = resource
        self.model = model
        self.max_frequency_hz = MAX_FREQUENCY_HZ[model]
        self._sleep = sleep
        self._clock = clock

    @property
    def resource(self) -> SCPIResource:
        return self._res

    def identify(self) -> str:
        return self._res.query("*IDN?").strip()

    # -- status --------------------------------------------------------

    def frequency(self) -> FrequencyConfiguration:
        return FrequencyConfiguration(
            center_hz=self._number(":SENSe:FREQuency:CENTer?"), span_hz=self._number(":SENSe:FREQuency:SPAN?"),
            start_hz=self._number(":SENSe:FREQuency:STARt?"), stop_hz=self._number(":SENSe:FREQuency:STOP?"),
        )

    def bandwidth(self) -> BandwidthConfiguration:
        return BandwidthConfiguration(
            rbw_hz=self._number(":SENSe:BANDwidth:RESolution?"), rbw_auto=self._bool(":SENSe:BANDwidth:RESolution:AUTO?"),
            emi_filter=self._bool(":SENSe:BANDwidth:EMIFilter:STATe?"),
            vbw_hz=self._number(":SENSe:BANDwidth:VIDeo?"), vbw_auto=self._bool(":SENSe:BANDwidth:VIDeo:AUTO?"),
            sweep_time_s=self._number(":SENSe:SWEep:TIME?"), sweep_time_auto=self._bool(":SENSe:SWEep:TIME:AUTO?"),
            sweep_points=int(self._number(":SENSe:SWEep:POINts?")), detector=self._detector(),
        )

    def amplitude(self) -> AmplitudeConfiguration:
        return AmplitudeConfiguration(
            reference_level=self._number(":DISPlay:WINdow:TRACe:Y:SCALe:RLEVel?"), unit=self._unit(),
            scale_type=self._scale_type(), scale_db_per_div=self._number(":DISPlay:WINdow:TRACe:Y:SCALe:PDIVision?"),
            attenuation_db=self._number(":SENSe:POWer:RF:ATTenuation?"),
            attenuation_auto=self._bool(":SENSe:POWer:RF:ATTenuation:AUTO?"),
            preamplifier=self._bool(":SENSe:POWer:RF:GAIN:STATe?"),
        )

    def trace_configuration(self, trace: int) -> TraceConfiguration:
        _check_choice("trace", trace, TRACES)
        reply = self._res.query(f":TRACe{trace}:MODE?").strip().upper()
        if reply not in _TRACE_MODE_NAMES:
            raise ValueError(f"Unexpected reply to :TRACe{trace}:MODE?: {reply!r}")
        return TraceConfiguration(trace=trace, mode=_TRACE_MODE_NAMES[reply])

    def status(self) -> AnalyzerStatus:
        return AnalyzerStatus(
            sweep="continuous" if self._bool(":INITiate:CONTinuous?") else "single",
            sweep_count=int(self._number(":SENSe:SWEep:COUNt?")),
            frequency=self.frequency(), bandwidth=self.bandwidth(), amplitude=self.amplitude(),
            traces=[self.trace_configuration(t) for t in TRACES],
        )

    # -- settings ------------------------------------------------------

    def configure_frequency(self, center_hz: float | None = None, span_hz: float | None = None,
                            start_hz: float | None = None, stop_hz: float | None = None,
                            full_span: bool = False) -> FrequencyConfiguration:
        """Set center and span, or start and stop, or the full span. Other values stay the same."""
        if full_span and any(v is not None for v in (center_hz, span_hz, start_hz, stop_hz)):
            raise ValueError("Use full_span without other frequency values")
        if (center_hz is not None or span_hz is not None) and (start_hz is not None or stop_hz is not None):
            raise ValueError("Use center_hz and span_hz, or start_hz and stop_hz, not both pairs")
        f_max = self.max_frequency_hz
        for name, value in (("center_hz", center_hz), ("start_hz", start_hz), ("stop_hz", stop_hz)):
            if value is not None:
                _check_range(name, value, 0.0, f_max, "Hz")
        if span_hz is not None and span_hz != 0:
            _check_range("span_hz", span_hz, MIN_SPAN_HZ, f_max, "Hz", note="Use 0 for zero span.")
        if full_span:
            self._res.write(":SENSe:FREQuency:SPAN:FULL")
            return self.frequency()
        if center_hz is not None or span_hz is not None:
            present = self.frequency()
            center = present.center_hz if center_hz is None else center_hz
            span = present.span_hz if span_hz is None else span_hz
            if center - span / 2 < -1e-6 or center + span / 2 > f_max + 1e-6:
                raise ValueError(
                    f"center_hz {center:g} and span_hz {span:g} give a range outside 0 to {f_max:g} Hz"
                )
            if center_hz is not None:
                self._res.write(f":SENSe:FREQuency:CENTer {_fmt(center_hz)}")
            if span_hz is not None:
                self._res.write(f":SENSe:FREQuency:SPAN {_fmt(span_hz)}")
        if start_hz is not None or stop_hz is not None:
            present = self.frequency()
            start = present.start_hz if start_hz is None else start_hz
            stop = present.stop_hz if stop_hz is None else stop_hz
            if stop - start < MIN_SPAN_HZ:
                raise ValueError(f"stop_hz must be at least {MIN_SPAN_HZ:g} Hz above start_hz")
            # Send the value first that keeps start below stop.
            if start_hz is not None and start_hz >= present.stop_hz:
                order = (("STOP", stop_hz), ("STARt", start_hz))
            else:
                order = (("STARt", start_hz), ("STOP", stop_hz))
            for key, value in order:
                if value is not None:
                    self._res.write(f":SENSe:FREQuency:{key} {_fmt(value)}")
        return self.frequency()

    def configure_bandwidth(self, rbw_hz: float | None = None, rbw_auto: bool | None = None,
                            vbw_hz: float | None = None, vbw_auto: bool | None = None,
                            sweep_time_s: float | None = None, sweep_time_auto: bool | None = None,
                            sweep_points: int | None = None, detector: Detector | None = None,
                            emi_filter: bool | None = None) -> BandwidthConfiguration:
        """Set the bandwidths, the sweep time, the sweep points, the detector and the EMI filter. Other values stay the same.

        A value sets the manual mode. auto true sets the auto mode.
        """
        for name, value, auto in (("rbw", rbw_hz, rbw_auto), ("vbw", vbw_hz, vbw_auto),
                                  ("sweep_time", sweep_time_s, sweep_time_auto)):
            if value is not None and auto:
                raise ValueError(f"Use {name} value or {name}_auto true, not both")
        if detector is not None and detector not in _DETECTOR_CODES:
            raise ValueError(f"detector must be one of {sorted(_DETECTOR_CODES)}")
        if rbw_hz is not None or emi_filter is False:
            quasi_peak = (detector or self._detector()) == "quasi_peak"
            if emi_filter is False and quasi_peak:
                raise ValueError("The quasi_peak detector needs the EMI filter. Select another detector first.")
            emi = quasi_peak or (
                emi_filter if emi_filter is not None else self._bool(":SENSe:BANDwidth:EMIFilter:STATe?")
            )
            allowed = EMI_RBW_VALUES_HZ if emi else RBW_VALUES_HZ
            if rbw_hz is not None and not any(math.isclose(rbw_hz, v, rel_tol=1e-9) for v in allowed):
                raise ValueError(
                    f"rbw_hz must be one of {[f'{v:g}' for v in allowed]}"
                    + (" with the EMI filter. emi_filter false selects the 1-3-10 sequence." if emi
                       else " (1-3-10 sequence)")
                )
        if vbw_hz is not None and not any(math.isclose(vbw_hz, v, rel_tol=1e-9) for v in VBW_VALUES_HZ):
            raise ValueError(f"vbw_hz must be one of {[f'{v:g}' for v in VBW_VALUES_HZ]} (1-3-10 sequence)")
        if sweep_time_s is not None:
            _check_range("sweep_time_s", sweep_time_s, *SWEEP_TIME_RANGE_S, "s",
                         note="The minimum also depends on the span.")
        if sweep_points is not None:
            _check_range("sweep_points", sweep_points, *SWEEP_POINTS_RANGE, "")

        if detector is not None:
            self._res.write(f":SENSe:DETector:FUNCtion {_DETECTOR_CODES[detector]}")
        if emi_filter is not None:
            self._res.write(f":SENSe:BANDwidth:EMIFilter:STATe {_on_off(emi_filter)}")
        if rbw_auto is not None and rbw_hz is None:
            self._res.write(f":SENSe:BANDwidth:RESolution:AUTO {_on_off(rbw_auto)}")
        if rbw_hz is not None:
            self._res.write(f":SENSe:BANDwidth:RESolution {_fmt(rbw_hz)}")
        if vbw_auto is not None and vbw_hz is None:
            self._res.write(f":SENSe:BANDwidth:VIDeo:AUTO {_on_off(vbw_auto)}")
        if vbw_hz is not None:
            self._res.write(f":SENSe:BANDwidth:VIDeo {_fmt(vbw_hz)}")
        if sweep_time_auto is not None and sweep_time_s is None:
            self._res.write(f":SENSe:SWEep:TIME:AUTO {_on_off(sweep_time_auto)}")
        if sweep_time_s is not None:
            self._res.write(f":SENSe:SWEep:TIME {sweep_time_s:.6E}")
        if sweep_points is not None:
            self._res.write(f":SENSe:SWEep:POINts {int(sweep_points)}")
        return self.bandwidth()

    def configure_amplitude(self, reference_level_dbm: float | None = None, scale_db_per_div: float | None = None,
                            attenuation_db: int | None = None, attenuation_auto: bool | None = None,
                            preamplifier: bool | None = None) -> AmplitudeConfiguration:
        """Set the reference level, the scale, the input attenuation and the preamplifier. Other values stay the same."""
        if attenuation_db is not None and attenuation_auto:
            raise ValueError("Use attenuation_db or attenuation_auto true, not both")
        if reference_level_dbm is not None:
            _check_range("reference_level_dbm", reference_level_dbm, *REFERENCE_LEVEL_RANGE_DBM, "dBm")
            unit = self._unit()
            if unit != "dBm":
                raise ValueError(f"The amplitude unit is {unit}. Set the unit dBm on the analyzer (AMPT, Units) first.")
        if scale_db_per_div is not None:
            _check_range("scale_db_per_div", scale_db_per_div, *SCALE_RANGE_DB, "dB")
            if self._scale_type() != "log":
                raise ValueError("The scale is linear. Set the log scale on the analyzer (AMPT, Scale Type) first.")
        if attenuation_db is not None:
            if attenuation_db != int(attenuation_db):
                raise ValueError("attenuation_db must be an integer")
            _check_range("attenuation_db", attenuation_db, *ATTENUATION_RANGE_DB, "dB")

        if attenuation_auto is not None and attenuation_db is None:
            self._res.write(f":SENSe:POWer:RF:ATTenuation:AUTO {_on_off(attenuation_auto)}")
        if attenuation_db is not None:
            self._res.write(f":SENSe:POWer:RF:ATTenuation {int(attenuation_db)}")
        if preamplifier is not None:
            self._res.write(f":SENSe:POWer:RF:GAIN:STATe {_on_off(preamplifier)}")
        if reference_level_dbm is not None:
            self._res.write(f":DISPlay:WINdow:TRACe:Y:SCALe:RLEVel {reference_level_dbm:.6E}")
        if scale_db_per_div is not None:
            self._res.write(f":DISPlay:WINdow:TRACe:Y:SCALe:PDIVision {scale_db_per_div:.6E}")
        return self.amplitude()

    def configure_trace(self, trace: int, mode: TraceMode) -> TraceConfiguration:
        _check_choice("trace", trace, TRACES)
        if mode not in _TRACE_MODE_CODES:
            raise ValueError(f"mode must be one of {sorted(_TRACE_MODE_CODES)}")
        self._res.write(f":TRACe{trace}:MODE {_TRACE_MODE_CODES[mode]}")
        return self.trace_configuration(trace)

    # -- sweep ---------------------------------------------------------

    def run(self) -> SweepResult:
        """Start the continuous sweep."""
        self._res.write(":INITiate:CONTinuous ON")
        return SweepResult(sweep="continuous", complete=False, sweeps_done=0,
                           sweep_count=int(self._number(":SENSe:SWEep:COUNt?")))

    def single(self, timeout_s: float = 10.0) -> SweepResult:
        """Start a single sweep. Wait a maximum of timeout_s until the analyzer completes it.

        The analyzer sets :SENSe:SWEep:COUNt:CURRent? to 0 at the start, and to the sweep count
        at the end of the single sweep (tested).
        """
        if not (0 <= timeout_s <= 3600):
            raise ValueError("timeout_s must be from 0 to 3600")
        count = int(self._number(":SENSe:SWEep:COUNt?"))
        self._res.write(":INITiate:CONTinuous OFF")
        self._res.write(":INITiate:IMMediate")
        deadline = self._clock() + timeout_s
        while True:
            done = int(self._number(":SENSe:SWEep:COUNt:CURRent?"))
            if done >= count or self._clock() >= deadline:
                return SweepResult(sweep="single", complete=done >= count, sweeps_done=done, sweep_count=count)
            self._sleep(SINGLE_POLL_S)

    # -- trace data ----------------------------------------------------

    def trace(self, trace: int = 1, max_points: int = SWEEP_POINTS_RANGE[1]) -> TraceData:
        """Read the amplitudes of a trace, with the frequency (or time in zero span) of each value.

        If the trace has more points than max_points, each value is the largest of n neighbor points.
        Then the result keeps the peaks.
        """
        config = self.trace_configuration(trace)
        if config.mode == "blank":
            raise ValueError(f"Trace {trace} is off (blank). Set a trace mode first, for example clear_write.")
        if not (1 <= max_points <= SWEEP_POINTS_RANGE[1]):
            raise ValueError(f"max_points must be from 1 to {SWEEP_POINTS_RANGE[1]}")
        if self._res.query(":FORMat:TRACe:DATA?").strip().upper() != "ASCII":
            self._res.write(":FORMat:TRACe:DATA ASCii")
        freq = self.frequency()
        unit = self._unit()
        text = self._read_block(f":TRACe:DATA? TRACE{trace}").decode("ascii", errors="replace")
        try:
            values = [float(v) for v in text.split(",") if v.strip()]
        except ValueError:
            raise ValueError(f"Unexpected trace data: {text[:60]!r}") from None
        if not values:
            raise IncompleteTransferError(f"Trace {trace} has no data points")
        if freq.span_hz == 0:
            x_unit, first, last = "s", 0.0, self._number(":SENSe:SWEep:TIME?")
        else:
            x_unit, first, last = "Hz", freq.start_hz, freq.stop_hz
        n = len(values)
        step = (last - first) / (n - 1) if n > 1 else 0.0
        decimation = max(1, math.ceil(n / max_points))
        x, amplitudes = [], []
        for i in range(0, n, decimation):
            group = values[i:i + decimation]
            k = i + group.index(max(group))
            x.append(first + k * step)
            amplitudes.append(values[k])
        return TraceData(trace=trace, mode=config.mode, unit=unit, x_unit=x_unit, trace_points=n,
                         decimation=decimation, x=x, amplitudes=amplitudes)

    # -- markers -------------------------------------------------------

    def marker_peak(self, marker: int = 1, search: PeakSearch = "max") -> MarkerResult:
        """Switch the marker on (normal type) and put it on a peak of its trace."""
        _check_choice("marker", marker, MARKERS)
        if search not in _PEAK_COMMANDS:
            raise ValueError(f"search must be one of {sorted(_PEAK_COMMANDS)}")
        self._marker_on(marker)
        self._res.write(f":CALCulate:MARKer{marker}:{_PEAK_COMMANDS[search]}")
        return self._marker(marker)

    def set_marker(self, marker: int, x: float) -> MarkerResult:
        """Switch the marker on (normal type) and put it at a frequency (or time in zero span)."""
        _check_choice("marker", marker, MARKERS)
        freq = self.frequency()
        if freq.span_hz == 0:
            _check_range("x", x, 0.0, self._number(":SENSe:SWEep:TIME?"), "s", note="Zero span: x is a time.")
        else:
            _check_range("x", x, freq.start_hz, freq.stop_hz, "Hz", note="Use a frequency on the screen.")
        self._marker_on(marker)
        self._res.write(f":CALCulate:MARKer{marker}:X {_fmt(x) if freq.span_hz else f'{x:.6E}'}")
        return self._marker(marker)

    def markers_off(self) -> None:
        self._res.write(":CALCulate:MARKer:AOFF")

    def _marker_on(self, marker: int) -> None:
        if not self._bool(f":CALCulate:MARKer{marker}:STATe?"):
            # The analyzer sets a marker that it switches on to the normal type.
            self._res.write(f":CALCulate:MARKer{marker}:STATe ON")
        elif self._res.query(f":CALCulate:MARKer{marker}:MODE?").strip().upper() != "POS":
            self._res.write(f":CALCulate:MARKer{marker}:MODE POSition")

    def _marker(self, marker: int) -> MarkerResult:
        zero_span = self._number(":SENSe:FREQuency:SPAN?") == 0
        return MarkerResult(
            marker=marker, x=self._number(f":CALCulate:MARKer{marker}:X?"), x_unit="s" if zero_span else "Hz",
            amplitude=self._number(f":CALCulate:MARKer{marker}:Y?"), unit=self._unit(),
        )

    # -- screenshot ----------------------------------------------------

    def screenshot_png(self) -> bytes:
        """Read the screen (a 24 bit BMP file of 800 x 480 pixels) and return it as a PNG file.

        :PRIVate:SNAP? BMP is not in the programming guide. The analyzer sends the block after approximately 7 s (tested).
        """
        bmp = self._read_block(":PRIVate:SNAP? BMP")
        if bmp[:2] != b"BM":
            raise IncompleteTransferError(f"The screen data is not a BMP file: {bmp[:16]!r}")
        return bmp_to_png(bmp)

    # -- helpers -------------------------------------------------------

    def _number(self, query: str) -> float:
        reply = self._res.query(query).strip()
        try:
            value = float(reply)
        except ValueError:
            raise ValueError(f"Unexpected reply to {query}: {reply!r}") from None
        if not math.isfinite(value):
            raise ValueError(f"Unexpected reply to {query}: {reply!r}")
        return value

    def _bool(self, query: str) -> bool:
        reply = self._res.query(query).strip().upper()
        if reply in ("1", "ON"):
            return True
        if reply in ("0", "OFF"):
            return False
        raise ValueError(f"Unexpected reply to {query}: {reply!r}")

    def _detector(self) -> str:
        reply = self._res.query(":SENSe:DETector:FUNCtion?").strip().upper()
        if reply not in _DETECTOR_NAMES:
            raise ValueError(f"Unexpected reply to :SENSe:DETector:FUNCtion?: {reply!r}")
        return _DETECTOR_NAMES[reply]

    def _unit(self) -> str:
        reply = self._res.query(":UNIT:POWer?").strip().upper()
        if reply not in _UNIT_NAMES:
            raise ValueError(f"Unexpected reply to :UNIT:POWer?: {reply!r}")
        return _UNIT_NAMES[reply]

    def _scale_type(self) -> str:
        reply = self._res.query(":DISPlay:WINdow:TRACe:Y:SCALe:SPACing?").strip().upper()
        if reply not in ("LOG", "LIN"):
            raise ValueError(f"Unexpected reply to :DISPlay:WINdow:TRACe:Y:SCALe:SPACing?: {reply!r}")
        return "log" if reply == "LOG" else "linear"

    def _read_block(self, query: str) -> bytes:
        """Send query and read the reply: #9<length><data>, then an LF (programming guide :TRACe[:DATA])."""
        self._res.write(query)
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


def _fmt(hz: float) -> str:
    """Frequency for a command. The analyzer uses integer Hz values."""
    return str(int(round(hz)))


def _on_off(value: bool) -> str:
    return "ON" if value else "OFF"


def _check_range(name: str, value: float, low: float, high: float, unit: str, note: str = "") -> None:
    if not math.isfinite(value) or not (low <= value <= high):
        message = f"{name} must be from {low:g} to {high:g}" + (f" {unit}" if unit else "")
        raise ValueError(message + (f". {note}" if note else ""))


def _check_choice(name: str, value: int, allowed: tuple[int, ...]) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {list(allowed)}")
