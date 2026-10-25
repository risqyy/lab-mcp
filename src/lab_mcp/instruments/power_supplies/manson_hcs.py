"""Driver for Manson HCS series DC power supplies (for example, HCS-3404).

The HCS series does not use SCPI. It uses a text protocol on a USB virtual COM port
(9600 baud, 8N1). Each command ends with CR. The instrument sends an optional data
line and then "OK", each line ends with CR.

The number format depends on the model. The length of the GMAX reply sets the format:
6 digits = 3 digits per value with 1 decimal place, 8 digits = 4 digits per value with
2 decimal places. GETD always uses 4 digits with 2 decimal places.

The protocol has no command that reads the output state. The driver remembers the
last state that it set. Before the first switch command, the state is unknown.
"""

from __future__ import annotations

from typing import Literal, Protocol

import serial
from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveFloat

BAUD_RATE = 9600
TIMEOUT_S = 1.0
PRESET_COUNT = 3


class HCSTransport(Protocol):
    def request(self, command: str) -> str | None:
        """Send one command. Return the data line, or None if the reply is only "OK"."""
        ...

    def close(self) -> None: ...


class HCSProtocolError(OSError):
    """The instrument sent a reply that does not agree with the protocol."""


class SerialTransport:
    """HCS framing on a serial port."""

    def __init__(self, port: serial.Serial):
        self._port = port

    @classmethod
    def open(cls, device: str, timeout: float = TIMEOUT_S) -> SerialTransport:
        return cls(serial.Serial(device, baudrate=BAUD_RATE, bytesize=8, parity="N", stopbits=1, timeout=timeout))

    def request(self, command: str) -> str | None:
        self._port.reset_input_buffer()
        self._port.write(command.encode("ascii") + b"\r")
        data = None
        for _ in range(2):
            line = self._port.read_until(b"\r")
            if not line.endswith(b"\r"):
                raise TimeoutError(f"No reply to {command} from {self._port.port}")
            text = line[:-1].decode("ascii", errors="replace").strip()
            if text == "OK":
                return data
            if data is not None:
                break
            data = text
        raise HCSProtocolError(f"Unexpected reply to {command}: {data!r}")

    def close(self) -> None:
        self._port.close()


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HCSLimits(_Model):
    max_voltage: PositiveFloat = Field(description="Maximum voltage of the model in V")
    max_current: PositiveFloat = Field(description="Maximum current of the model in A")
    digits: Literal[3, 4] = Field(description="Digits for each value in the protocol")
    decimals: Literal[1, 2] = Field(description="Decimal places for each value in the protocol")

    @property
    def voltage_step(self) -> float:
        return 10 ** -self.decimals


class HCSMeasurement(_Model):
    voltage: float = Field(description="Measured output voltage in V")
    current: float = Field(description="Measured output current in A")
    power: float = Field(description="Calculated output power in W")
    mode: Literal["CV", "CC"] = Field(description="Constant voltage or constant current")


class HCSPreset(_Model):
    index: int = Field(ge=0, lt=PRESET_COUNT)
    voltage: NonNegativeFloat
    current: NonNegativeFloat


class HCSStatus(_Model):
    model: str
    output_enabled: bool | None = Field(
        description="Last output state that the server set. None: unknown, because the protocol cannot read it."
    )
    voltage_setpoint: NonNegativeFloat
    current_setpoint: NonNegativeFloat
    upper_voltage_limit: NonNegativeFloat = Field(description="Upper voltage limit (SOVP) in V")
    upper_current_limit: NonNegativeFloat = Field(description="Upper current limit (SOCP) in A")
    limits: HCSLimits


class MansonHCS:
    def __init__(self, transport: HCSTransport):
        self._t = transport
        self.model = self._request_data("GMOD")
        self.limits = _parse_limits(self._request_data("GMAX"))
        self._output_enabled: bool | None = None

    # -- setpoints -------------------------------------------------------

    # The instrument does not reply to a setpoint above its upper limit (SOVP/SOCP).
    # Thus the driver examines the upper limits before it sends a setpoint.

    def set_voltage(self, volts: float) -> None:
        _check_range("voltage", volts, self.limits.max_voltage, "V")
        upper_v, _ = self.get_upper_limits()
        _check_upper("voltage", volts, upper_v, "V")
        self._t.request("VOLT" + self._encode(volts))

    def set_current(self, amps: float) -> None:
        _check_range("current", amps, self.limits.max_current, "A")
        _, upper_a = self.get_upper_limits()
        _check_upper("current", amps, upper_a, "A")
        self._t.request("CURR" + self._encode(amps))

    def get_setpoints(self) -> tuple[float, float]:
        voltage, current = self._decode(self._request_data("GETS"), count=2)
        return voltage, current

    def set_upper_limits(self, volts: float | None = None, amps: float | None = None) -> None:
        if volts is not None:
            _check_range("upper voltage limit", volts, self.limits.max_voltage, "V")
        if amps is not None:
            _check_range("upper current limit", amps, self.limits.max_current, "A")
        if volts is not None:
            self._t.request("SOVP" + self._encode(volts))
        if amps is not None:
            self._t.request("SOCP" + self._encode(amps))

    def get_upper_limits(self) -> tuple[float, float]:
        [volts] = self._decode(self._request_data("GOVP"), count=1)
        [amps] = self._decode(self._request_data("GOCP"), count=1)
        return volts, amps

    # -- output ----------------------------------------------------------

    def set_output(self, enabled: bool) -> None:
        # The protocol is inverted: SOUT0 switches the output on.
        self._t.request("SOUT0" if enabled else "SOUT1")
        self._output_enabled = enabled

    @property
    def output_enabled(self) -> bool | None:
        return self._output_enabled

    # -- measurement -----------------------------------------------------

    def measure(self) -> HCSMeasurement:
        data = self._request_data("GETD")
        if len(data) != 9 or not data.isdigit():
            raise HCSProtocolError(f"Unexpected GETD reply: {data!r}")
        voltage, current = int(data[0:4]) / 100, int(data[4:8]) / 100
        mode = {"0": "CV", "1": "CC"}.get(data[8])
        if mode is None:
            raise HCSProtocolError(f"Unknown mode digit in GETD reply: {data!r}")
        return HCSMeasurement(voltage=voltage, current=current, power=round(voltage * current, 4), mode=mode)

    # -- presets ---------------------------------------------------------

    def get_presets(self) -> list[HCSPreset]:
        values = self._decode(self._request_data("GETM"), count=2 * PRESET_COUNT)
        return [HCSPreset(index=i, voltage=values[2 * i], current=values[2 * i + 1]) for i in range(PRESET_COUNT)]

    def load_preset(self, index: int) -> None:
        """Load a preset into the setpoints.

        If a preset is above the upper limits, the instrument does not load it, but it
        keeps the command. It loads the preset later, when the upper limit increases.
        Thus the driver does not send RUNM for such a preset.
        """
        if not 0 <= index < PRESET_COUNT:
            raise ValueError(f"Invalid preset {index}; valid presets are 0 to {PRESET_COUNT - 1}")
        preset = self.get_presets()[index]
        upper_v, upper_a = self.get_upper_limits()
        _check_upper(f"preset {index} voltage", preset.voltage, upper_v, "V")
        _check_upper(f"preset {index} current", preset.current, upper_a, "A")
        self._t.request(f"RUNM{index}")
        if self.get_setpoints() != (preset.voltage, preset.current):
            raise HCSProtocolError(f"The instrument did not load preset {index}")

    def status(self) -> HCSStatus:
        voltage, current = self.get_setpoints()
        upper_v, upper_a = self.get_upper_limits()
        return HCSStatus(
            model=self.model,
            output_enabled=self._output_enabled,
            voltage_setpoint=voltage,
            current_setpoint=current,
            upper_voltage_limit=upper_v,
            upper_current_limit=upper_a,
            limits=self.limits,
        )

    def close(self) -> None:
        self._t.close()

    # -- helpers ---------------------------------------------------------

    def _request_data(self, command: str) -> str:
        data = self._t.request(command)
        if not data:
            raise HCSProtocolError(f"No data in reply to {command}")
        return data

    def _encode(self, value: float) -> str:
        scaled = round(value * 10**self.limits.decimals)
        return f"{scaled:0{self.limits.digits}d}"

    def _decode(self, data: str, count: int) -> list[float]:
        width = self.limits.digits
        if len(data) != width * count or not data.isdigit():
            raise HCSProtocolError(f"Expected {count} values with {width} digits, got {data!r}")
        scale = 10**self.limits.decimals
        return [int(data[i : i + width]) / scale for i in range(0, len(data), width)]


def _parse_limits(data: str) -> HCSLimits:
    if not data.isdigit() or len(data) not in (6, 8):
        raise HCSProtocolError(f"Unexpected GMAX reply: {data!r}")
    digits, decimals = (3, 1) if len(data) == 6 else (4, 2)
    scale = 10**decimals
    return HCSLimits(
        max_voltage=int(data[:digits]) / scale,
        max_current=int(data[digits:]) / scale,
        digits=digits,
        decimals=decimals,
    )


def _check_range(name: str, value: float, maximum: float, unit: str) -> None:
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} {value} {unit} out of range 0..{maximum} {unit}")


def _check_upper(name: str, value: float, upper: float, unit: str) -> None:
    if value > upper:
        raise ValueError(
            f"{name} {value} {unit} is above the upper limit {upper} {unit}. "
            "Increase the upper limit with hcs_set_upper_limits first."
        )
