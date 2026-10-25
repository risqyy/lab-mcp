"""SCPI driver for the GW Instek AFG-2105 (AFG-2000/2100 command set).

USB is a virtual COM port, not USB-TMC. Commands have no leading colon.
Do not use APPLy: it also switches the output on.

Tested on firmware V2.02: amplitude queries always return Vpp into 50 Ohm,
and offset queries always return volts into 50 Ohm, including MIN/MAX queries.
This does not depend on OUTPut:LOAD or VOLTage:UNIT. Writes, however, use the
selected load. The driver reports and accepts voltages for that selected load.
"""

from __future__ import annotations

import math
from typing import Literal, Protocol

import serial
from pydantic import BaseModel, ConfigDict, Field

Waveform = Literal["sine", "square", "ramp", "noise"]
Load = Literal["50_ohm", "high_z"]
BAUD_RATE = 115200  # USB CDC; the instrument ignores the baud rate.
TIMEOUT_S = 1.0
MAX_ERRORS = 16
FUNCTIONS = {"sine": "SIN", "square": "SQU", "ramp": "RAMP", "noise": "NOIS", "arbitrary": "USER"}
LOADS = {"50_ohm": "DEF", "high_z": "INF"}
MODES = {"am": "AM", "fm": "FM", "fsk": "FSKey", "sweep": "SWEep"}


class SCPITransport(Protocol):
    def write(self, message: str) -> object: ...
    def query(self, message: str) -> str: ...
    def close(self) -> None: ...
    @property
    def is_open(self) -> bool: ...


class AFGProtocolError(OSError):
    """The instrument sent an incomplete or unexpected reply."""


class AFGSerialTransport:
    """Line-based SCPI on a serial port. Replies end with CR LF."""

    def __init__(self, port: serial.Serial):
        self._port = port

    @classmethod
    def open(cls, device: str, timeout: float = TIMEOUT_S) -> AFGSerialTransport:
        return cls(serial.Serial(
            device, baudrate=BAUD_RATE, bytesize=8, parity="N", stopbits=1,
            timeout=timeout, write_timeout=timeout,
        ))

    @property
    def is_open(self) -> bool:
        return self._port.is_open

    def write(self, message: str) -> None:
        data = message.encode("ascii") + b"\n"
        if self._port.write(data) != len(data):
            raise AFGProtocolError(f"Incomplete write to {self._port.port}")

    def query(self, message: str) -> str:
        self._port.reset_input_buffer()
        self.write(message)
        data = self._port.read_until(b"\n", size=1024)
        if not data.endswith(b"\n"):
            raise TimeoutError(f"No complete reply to {message} from {self._port.port}")
        try:
            reply = data.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise AFGProtocolError(f"Non-ASCII reply to {message}") from exc
        if not reply:
            raise AFGProtocolError(f"Empty reply to {message}")
        return reply

    def close(self) -> None:
        self._port.close()


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class AFGModelLimits(_Model):
    max_frequency_hz: float
    max_ramp_frequency_hz: float = 1e6
    max_peak_voltage_50_ohm: float = 5.0


# Other AFG-2000/2100 models are not yet verified with this driver.
MODELS = {"AFG-2105": AFGModelLimits(max_frequency_hz=5e6)}


class AFGRange(_Model):
    minimum: float
    maximum: float


class AFGLimits(_Model):
    frequency_hz: AFGRange = Field(description="Limits of the stored frequency; frequency is not used for noise")
    amplitude_vpp: AFGRange = Field(description="Limits at the present offset, for the selected load")
    offset_v: AFGRange = Field(description="Limits at the present amplitude, for the selected load")
    duty_cycle_percent: AFGRange
    symmetry_percent: AFGRange


class AFGStatus(_Model):
    model: str
    waveform: Literal["sine", "square", "ramp", "noise", "arbitrary"]
    frequency_hz: float = Field(description="Stored frequency in Hz; not used for noise")
    amplitude_vpp: float = Field(description="Peak-to-peak voltage for the selected load, independent of display_unit")
    offset_v: float = Field(description="DC offset in V for the selected load")
    duty_cycle_percent: float = Field(description="Stored square-wave duty cycle in percent")
    symmetry_percent: float = Field(description="Stored ramp symmetry in percent; 50 gives a triangle")
    load: Load = Field(description="Assumed load, not a measurement. A high impedance input gets twice the 50 Ohm voltage.")
    display_unit: Literal["VPP", "VRMS", "DBM"]
    output_enabled: bool
    active_modes: list[str] = Field(description="Active modulation/sweep modes. Disable these on the front panel before setting a basic waveform.")
    limits: AFGLimits


class GWInstekAFG2000:
    def __init__(self, transport: SCPITransport, model: str):
        if model.upper() not in MODELS:
            raise ValueError(f"Unsupported GW Instek model {model!r}")
        self._t = transport
        self.model = model.upper()
        self.model_limits = MODELS[self.model]

    @property
    def is_open(self) -> bool:
        return self._t.is_open

    def close(self) -> None:
        self._t.close()

    def status(self) -> AFGStatus:
        waveform = self._choice("SOURce1:FUNCtion?", FUNCTIONS)
        load = self._choice("OUTPut:LOAD?", LOADS)
        factor = _load_factor(load)
        unit = self._choice("SOURce1:VOLTage:UNIT?", {u: u for u in ("VPP", "VRMS", "DBM")})
        frequency_limits = self._range("SOURce1:FREQuency")
        frequency_limits = AFGRange(
            minimum=max(0.1, frequency_limits.minimum),
            maximum=min(self._frequency_max(waveform), frequency_limits.maximum),
        )
        if frequency_limits.minimum > frequency_limits.maximum:
            raise AFGProtocolError("Frequency limits do not overlap the model limits")
        return AFGStatus(
            model=self.model, waveform=waveform, load=load, display_unit=unit,
            frequency_hz=self._number("SOURce1:FREQuency?"),
            amplitude_vpp=self._number("SOURce1:AMPlitude?") * factor,
            offset_v=self._number("SOURce1:DCOffset?") * factor,
            duty_cycle_percent=self._number("SOURce1:SQUare:DCYCle?"),
            symmetry_percent=self._number("SOURce1:RAMP:SYMMetry?"),
            output_enabled=self._boolean("OUTPut?"),
            active_modes=[name for name, command in MODES.items() if self._boolean(f"SOURce1:{command}:STATe?")],
            limits=AFGLimits(
                frequency_hz=frequency_limits,
                amplitude_vpp=self._range("SOURce1:AMPlitude", factor),
                offset_v=self._range("SOURce1:DCOffset", factor),
                duty_cycle_percent=self._range("SOURce1:SQUare:DCYCle"),
                symmetry_percent=self._range("SOURce1:RAMP:SYMMetry"),
            ),
        )

    def set(
        self,
        waveform: Waveform | None = None,
        frequency_hz: float | None = None,
        amplitude_vpp: float | None = None,
        offset_v: float | None = None,
        duty_cycle_percent: float | None = None,
        symmetry_percent: float | None = None,
        load: Load | None = None,
    ) -> AFGStatus:
        """Set a basic waveform without switching the output. Read back all settings.

        Voltages refer to the selected load. A load change is allowed only with the
        output off. Omitted voltages retain their physical value when the load changes.
        Changes use VPP on the display. There is no automatic rollback or command retry.
        """
        before = self.status()
        if all(v is None for v in (waveform, frequency_hz, amplitude_vpp, offset_v, duty_cycle_percent, symmetry_percent, load)):
            return before
        target_load = load if load is not None else before.load
        if target_load not in LOADS:
            raise ValueError(f"Unknown load {target_load!r}")
        if target_load != before.load and before.output_enabled:
            raise ValueError("Switch the output off before changing the load setting")
        ratio = _load_factor(target_load) / _load_factor(before.load)
        target = before.model_copy(update={
            "waveform": waveform if waveform is not None else before.waveform,
            "frequency_hz": frequency_hz if frequency_hz is not None else before.frequency_hz,
            "amplitude_vpp": amplitude_vpp if amplitude_vpp is not None else before.amplitude_vpp * ratio,
            "offset_v": offset_v if offset_v is not None else before.offset_v * ratio,
            "duty_cycle_percent": duty_cycle_percent if duty_cycle_percent is not None else before.duty_cycle_percent,
            "symmetry_percent": symmetry_percent if symmetry_percent is not None else before.symmetry_percent,
            "load": target_load,
            "display_unit": "VPP",
        })
        self._validate(target)
        if frequency_hz is not None and target.waveform == "noise":
            raise ValueError("Noise has no frequency setting")
        if duty_cycle_percent is not None and target.waveform != "square":
            raise ValueError("Duty cycle is only used for square waves")
        if symmetry_percent is not None and target.waveform != "ramp":
            raise ValueError("Symmetry is only used for ramp waves")

        # MIN/MAX describe the current pair of amplitude and offset. Recover the
        # combined voltage ceiling before any write, then validate the target pair.
        peak = min(
            self.model_limits.max_peak_voltage_50_ohm * _load_factor(target_load),
            (before.limits.amplitude_vpp.maximum / 2 + abs(before.offset_v)) * ratio,
        )
        _check_range("amplitude_vpp", target.amplitude_vpp, before.limits.amplitude_vpp.minimum * ratio,
                     2 * (peak - abs(target.offset_v)))
        offset_space = (before.amplitude_vpp * ratio - target.amplitude_vpp) / 2
        _check_range("offset_v", target.offset_v,
                     before.limits.offset_v.minimum * ratio - offset_space,
                     before.limits.offset_v.maximum * ratio + offset_space)
        if target.waveform == before.waveform and target.waveform != "noise":
            _check_range("frequency_hz", target.frequency_hz,
                         before.limits.frequency_hz.minimum, before.limits.frequency_hz.maximum)
        if symmetry_percent is not None:
            _check_range("symmetry_percent", symmetry_percent,
                         before.limits.symmetry_percent.minimum, before.limits.symmetry_percent.maximum)
        if target.waveform == "square" and target.frequency_hz == before.frequency_hz:
            _check_range("duty_cycle_percent", target.duty_cycle_percent,
                         before.limits.duty_cycle_percent.minimum, before.limits.duty_cycle_percent.maximum)
        self._check_errors("before changing settings")

        if target_load != before.load:
            self._write(f"OUTPut:LOAD {LOADS[target_load]}")
        if before.display_unit != "VPP":
            self._write("SOURce1:VOLTage:UNIT VPP")

        # Reduce amplitude first when necessary to make space for a larger offset.
        # If amplitude increases, change the offset first to make space for it.
        old_amplitude = before.amplitude_vpp * ratio
        if target.amplitude_vpp < old_amplitude:
            self._set_number("SOURce1:AMPlitude", target.amplitude_vpp, _load_factor(target_load), "VPP")
        if target.offset_v != before.offset_v * ratio:
            self._set_number("SOURce1:DCOffset", target.offset_v, _load_factor(target_load))
        if target.amplitude_vpp > old_amplitude:
            self._set_number("SOURce1:AMPlitude", target.amplitude_vpp, _load_factor(target_load), "VPP")

        # A lower frequency widens the duty-cycle range and can make a ramp valid.
        # A duty-cycle change precedes an increase of frequency, to avoid clamping.
        if target.frequency_hz < before.frequency_hz:
            self._set_number("SOURce1:FREQuency", target.frequency_hz)
        if target.duty_cycle_percent != before.duty_cycle_percent:
            self._set_number("SOURce1:SQUare:DCYCle", target.duty_cycle_percent)
        if target.waveform != before.waveform:
            self._write(f"SOURce1:FUNCtion {FUNCTIONS[target.waveform]}")
        if target.frequency_hz > before.frequency_hz:
            self._set_number("SOURce1:FREQuency", target.frequency_hz)
        if target.symmetry_percent != before.symmetry_percent:
            self._set_number("SOURce1:RAMP:SYMMetry", target.symmetry_percent)

        after = self.status()
        self._check_errors("after changing settings")
        self._check_readback(target, after)
        return after

    def set_output(self, enabled: bool) -> bool:
        """Switch the output and verify it. OFF never depends on the signal settings."""
        if enabled:
            status = self.status()
            self._validate(status)
            for name in ("amplitude_vpp", "offset_v"):
                limits = getattr(status.limits, name)
                _check_range(name, getattr(status, name), limits.minimum, limits.maximum)
            self._check_errors("before switching the output on")
        try:
            self._t.write("OUTPut ON" if enabled else "OUTPut OFF")
            actual = self._boolean("OUTPut?")
            if actual != enabled:
                raise AFGProtocolError("Output readback does not agree with the command")
            self._check_errors("after switching the output")
        except (OSError, RuntimeError):
            if enabled:
                # The output might already be on. Do not leave it on after a failed check.
                try:
                    self._t.write("OUTPut OFF")
                except OSError:
                    pass
            raise
        return actual

    def _validate(self, status: AFGStatus) -> None:
        if status.waveform not in ("sine", "square", "ramp", "noise"):
            raise ValueError("Select a supported basic waveform before setting or enabling the output")
        if status.active_modes:
            raise ValueError(f"Disable these modes on the front panel first: {', '.join(status.active_modes)}")
        if status.waveform != "noise":
            _check_range("frequency_hz", status.frequency_hz, 0.1, self._frequency_max(status.waveform))
        factor = _load_factor(status.load)
        peak = self.model_limits.max_peak_voltage_50_ohm * factor
        _check_range("offset_v", status.offset_v, -peak, peak)
        _check_range("amplitude_vpp", status.amplitude_vpp, 0.001 * factor, 2 * (peak - abs(status.offset_v)))
        if status.waveform == "square":
            # Use the narrower range at the boundary (the manual uses strict inequalities).
            low, high = (1, 99) if status.frequency_hz < 100000 else (20, 80) if status.frequency_hz < 5e6 else (40, 60)
            _check_range("duty_cycle_percent", status.duty_cycle_percent, low, high)
        _check_range("symmetry_percent", status.symmetry_percent, 0, 100)

    def _frequency_max(self, waveform: str) -> float:
        return self.model_limits.max_ramp_frequency_hz if waveform == "ramp" else self.model_limits.max_frequency_hz

    def _set_number(self, command: str, value: float, factor: float = 1.0, suffix: str = "") -> None:
        limits = self._range(command, factor)
        _check_range(command, value, limits.minimum, limits.maximum)
        self._write(f"{command} {value:.12g}{suffix}")

    def _write(self, command: str) -> None:
        self._t.write(command)
        self._check_errors(f"after {command}")

    def _check_errors(self, when: str) -> None:
        errors = []
        for _ in range(MAX_ERRORS):
            reply = self._t.query("SYST:ERR?").strip()
            if reply == "No Error" or reply in ("0", '+0,"No error"', '0,"No error"'):
                if errors:
                    raise RuntimeError(f"AFG errors {when}: {', '.join(errors)}. Read sig_status before continuing; settings may be partial.")
                return
            errors.append(reply)
        raise AFGProtocolError(f"AFG error queue did not end {when}: {errors!r}")

    def _number(self, command: str) -> float:
        reply = self._t.query(command).strip()
        try:
            value = float(reply)
        except ValueError:
            raise AFGProtocolError(f"Unexpected reply to {command}: {reply!r}") from None
        if not math.isfinite(value):
            raise AFGProtocolError(f"Non-finite reply to {command}: {reply!r}")
        return value

    def _range(self, command: str, factor: float = 1.0) -> AFGRange:
        low = self._number(f"{command}? MIN") * factor
        high = self._number(f"{command}? MAX") * factor
        if low > high:
            raise AFGProtocolError(f"Reversed limits for {command}: {low}..{high}")
        return AFGRange(minimum=low, maximum=high)

    def _choice(self, command: str, choices: dict[str, str]) -> str:
        reply = self._t.query(command).strip().upper()
        for name, code in choices.items():
            if reply == code:
                return name
        raise AFGProtocolError(f"Unexpected reply to {command}: {reply!r}")

    def _boolean(self, command: str) -> bool:
        return self._choice(command, {"true": "1", "false": "0"}) == "true"

    def _check_readback(self, expected: AFGStatus, actual: AFGStatus) -> None:
        if actual.output_enabled and not expected.output_enabled:
            self._t.write("OUTPut OFF")
        for name in ("waveform", "load", "display_unit", "output_enabled", "active_modes"):
            if getattr(expected, name) != getattr(actual, name):
                raise AFGProtocolError(f"{name} readback does not agree with the request; read sig_status")
        factor = _load_factor(actual.load)
        # Resolution of the instrument's replies, not measurement accuracy.
        tolerances = {
            "frequency_hz": 0.05,
            "amplitude_vpp": 0.0005 * factor,
            "offset_v": _reply_tolerance(expected.offset_v / factor, 3, 0.0005) * factor,
            "duty_cycle_percent": _reply_tolerance(expected.duty_cycle_percent, 3, 0.005),
            "symmetry_percent": _reply_tolerance(expected.symmetry_percent, 4, 0.0005),
        }
        for name, tolerance in tolerances.items():
            wanted, got = getattr(expected, name), getattr(actual, name)
            if not math.isclose(wanted, got, rel_tol=0, abs_tol=tolerance + 1e-12):
                raise AFGProtocolError(f"{name} readback {got} does not agree with requested {wanted}; settings may be partial")


def _load_factor(load: str) -> float:
    return 2.0 if load == "high_z" else 1.0


def _reply_tolerance(value: float, digits: int, minimum: float) -> float:
    """Half a digit of the scientific-notation reply, with the device resolution as a floor."""
    if value == 0:
        return minimum
    return max(minimum, 0.5 * 10 ** (math.floor(math.log10(abs(value))) - digits + 1))


def _check_range(name: str, value: float, minimum: float, maximum: float) -> None:
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} {value} is out of range {minimum}..{maximum}")
