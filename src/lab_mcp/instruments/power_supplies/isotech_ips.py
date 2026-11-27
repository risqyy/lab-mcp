"""Driver for ISO-TECH IPS series DC power supplies (IPS-405, IPS-603, IPS-2010).

The IPS series does not use SCPI. It uses a text protocol on an RS-232 port
(2400 baud, 8N1, no flow control). Each command ends with CR. The instrument sends
one reply line for a query. The reply line ends with CR LF. A set command gets no reply.

Use a straight DB9 cable (pin 2 to pin 2, pin 3 to pin 3). The wiring in the user manual
is not correct. The interface of the instrument gets its supply from pin 4 (DTR of the
computer). Thus the driver sets DTR. Do not send the asterisk that the user manual
shows in front of each command.

The protocol has no identification command and no serial number. Thus the user must
state the model.

Behavior of a real IPS-405 (no load):

- The reply line ends with CR CR LF. The reply to "L" takes up to 0.8 s.
- The voltage in the reply is the voltage setpoint while the output is off. Thus the
  driver can examine a new voltage setpoint only while the output is off.
- The instrument keeps P = U x I (P rounded down to 1 W). A change of the current limit
  or the voltage limit changes the power limit. "SP" sets the current limit to P / U.
  Thus a change of the power limit can increase the current limit.
- The instrument decreases a voltage setpoint above the voltage limit to the voltage limit.

The user manual states that the instrument accepts set commands only in remote mode
(REM key on the front panel). The driver reads the limits and the output state again
after each change. If the instrument did not accept the change, the driver raises an error.
"""

from __future__ import annotations

import re
import time
from typing import Protocol

import serial
from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveFloat

BAUD_RATE = 2400
# A real IPS-405 sends the reply to "L" after up to 0.8 s.
TIMEOUT_S = 2.0
# The instrument needs time to apply a set command (value from the isotech CLI).
SET_DELAY_S = 0.5

# Reply to "L": Vvv.vvAa.aaaWwww.wUuuIi.iiPpppFffffff. A lower-case U, I or P shows that
# the limit is in the setting mode on the front panel.
_STATUS_LINE = re.compile(
    r"V(?P<voltage>\d+\.\d+)A(?P<current>\d+\.\d+)W(?P<power>\d+\.\d+)"
    r"[Uu](?P<voltage_limit>\d+)[Ii](?P<current_limit>\d+\.\d+)[Pp](?P<power_limit>\d+)"
    r"F(?P<flags>[01]{6})"
)


class IPSTransport(Protocol):
    def query(self, command: str) -> str:
        """Send one command. Return the reply line without CR LF."""
        ...

    def write(self, command: str) -> None:
        """Send one command that gets no reply."""
        ...

    def close(self) -> None: ...


class IPSProtocolError(OSError):
    """The instrument sent a reply that does not agree with the protocol."""


class IPSSerialTransport:
    """IPS framing on an RS-232 port."""

    def __init__(self, port: serial.Serial, set_delay: float = SET_DELAY_S):
        self._port = port
        self._set_delay = set_delay

    @classmethod
    def open(cls, device: str, timeout: float = TIMEOUT_S) -> IPSSerialTransport:
        port = serial.Serial(device, baudrate=BAUD_RATE, bytesize=8, parity="N", stopbits=1, timeout=timeout)
        port.dtr = True  # supply for the interface of the instrument
        return cls(port)

    def query(self, command: str) -> str:
        self._port.reset_input_buffer()
        self._send(command)
        line = self._port.read_until(b"\n")
        if not line.endswith(b"\n"):
            raise TimeoutError(f"No reply to {command} from {self._port.port}")
        return line.decode("ascii", errors="replace").strip()

    def write(self, command: str) -> None:
        self._send(command)
        time.sleep(self._set_delay)

    def _send(self, command: str) -> None:
        self._port.write(command.encode("ascii") + b"\r")
        self._port.flush()

    def close(self) -> None:
        self._port.close()


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IPSLimits(_Model):
    max_voltage: PositiveFloat = Field(description="Maximum voltage of the model in V")
    max_current: PositiveFloat = Field(description="Maximum current of the model in A")
    max_power: PositiveFloat = Field(description="Maximum power of the model in W")


# Values from the technical specifications in the IPS series user manual.
MODELS: dict[str, IPSLimits] = {
    "IPS-405": IPSLimits(max_voltage=40, max_current=5, max_power=200),
    "IPS-603": IPSLimits(max_voltage=60, max_current=3.5, max_power=200),
    "IPS-2010": IPSLimits(max_voltage=20, max_current=10, max_power=200),
}


class IPSMeasurement(_Model):
    voltage: float = Field(description="Output voltage in V. While the output is off: the voltage setpoint.")
    current: float = Field(description="Output current in A")
    power: float = Field(description="Output power in W")
    output_enabled: bool = Field(description="State of the output relay")


class IPSReading(_Model):
    """All values of one reply to "L"."""

    voltage: float
    current: float
    power: float
    voltage_limit: float
    current_limit: float
    power_limit: float
    output_enabled: bool
    overheat: bool
    knob_fine: bool
    knob_unlocked: bool
    remote: bool
    locked: bool


class IPSStatus(_Model):
    model: str
    output_enabled: bool = Field(description="State of the output relay")
    remote: bool = Field(description="Remote mode (REM key). The user manual states that set commands need it.")
    front_panel_locked: bool = Field(description="LOCK key is on")
    overheat: bool
    voltage_setpoint: NonNegativeFloat | None = Field(
        description="Voltage setpoint in V. While the output is on, the last value that the server set "
        "(None: unknown), because the instrument then reports the output voltage."
    )
    voltage_limit: NonNegativeFloat = Field(description="Voltage limit (U) in V. The voltage setpoint must not be higher.")
    current_limit: NonNegativeFloat = Field(description="Current limit (I) in A")
    power_limit: NonNegativeFloat = Field(description="Power limit (P) in W. The instrument keeps P = U x I.")
    limits: IPSLimits


def parse_status_line(data: str) -> IPSReading:
    m = _STATUS_LINE.fullmatch(data)
    if m is None:
        raise IPSProtocolError(f"Unexpected reply to L: {data!r}")
    flags = m["flags"]
    return IPSReading(
        voltage=float(m["voltage"]),
        current=float(m["current"]),
        power=float(m["power"]),
        voltage_limit=float(m["voltage_limit"]),
        current_limit=float(m["current_limit"]),
        power_limit=float(m["power_limit"]),
        output_enabled=flags[0] == "1",
        overheat=flags[1] == "1",
        knob_fine=flags[2] == "1",
        knob_unlocked=flags[3] == "1",
        remote=flags[4] == "1",
        locked=flags[5] == "1",
    )


class IsoTechIPS:
    def __init__(self, transport: IPSTransport, model: str):
        model = model.upper()
        if model not in MODELS:
            raise ValueError(f"Unknown IPS model {model}; supported models are {', '.join(MODELS)}")
        self._t = transport
        self.model = model
        self.limits = MODELS[model]
        self._voltage_setpoint: float | None = None
        self.read()  # examine that the instrument answers

    def read(self) -> IPSReading:
        return parse_status_line(self._t.query("L"))

    # -- setpoints -------------------------------------------------------

    def set_voltage(self, volts: float) -> None:
        _check_range("voltage", volts, self.limits.max_voltage, "V")
        reading = self.read()
        if volts > reading.voltage_limit:
            raise ValueError(
                f"voltage {volts} V is above the voltage limit {reading.voltage_limit:g} V. "
                "Increase the voltage limit with ips_set_limits first."
            )
        command = f"SV {volts:05.2f}"
        self._t.write(command)
        self._voltage_setpoint = round(volts, 2)
        # While the output is off, the instrument reports the setpoint. While the output
        # is on, it reports the output voltage, which depends on the load.
        if not reading.output_enabled:
            self._expect(command, lambda r: r.output_enabled or abs(r.voltage - round(volts, 2)) < 0.005)

    def set_current_limit(self, amps: float) -> None:
        _check_range("current limit", amps, self.limits.max_current, "A")
        command = f"SI {amps:04.2f}"
        self._t.write(command)
        self._expect(command, lambda r: abs(r.current_limit - round(amps, 2)) < 0.005)

    def set_voltage_limit(self, volts: int) -> None:
        _check_integer("voltage limit", volts, self.limits.max_voltage, "V")
        command = f"SU {int(volts):02d}"
        self._t.write(command)
        self._expect(command, lambda r: r.voltage_limit == volts)

    def set_power_limit(self, watts: int) -> None:
        """Set the power limit. The instrument then sets the current limit to P / U. This can increase it."""
        _check_integer("power limit", watts, self.limits.max_power, "W")
        voltage_limit = self.read().voltage_limit
        amps = watts / voltage_limit if voltage_limit else (0 if watts == 0 else float("inf"))
        if amps > self.limits.max_current:
            raise ValueError(
                f"power limit {watts} W sets the current limit to {amps:.2f} A at the voltage limit "
                f"{voltage_limit:g} V. This is above {self.limits.max_current:g} A. "
                "Use a lower power limit, or increase the voltage limit first."
            )
        command = f"SP {int(watts):03d}"
        self._t.write(command)
        self._expect(command, lambda r: r.power_limit == watts)

    # -- output ----------------------------------------------------------

    def set_output(self, enabled: bool) -> None:
        command = "KOE" if enabled else "KOD"
        self._t.write(command)
        self._expect(command, lambda r: r.output_enabled == enabled)

    def store(self) -> None:
        """Save the present settings to the EEPROM of the instrument."""
        self._t.write("EEP")

    # -- measurement -----------------------------------------------------

    def measure(self) -> IPSMeasurement:
        r = self.read()
        return IPSMeasurement(voltage=r.voltage, current=r.current, power=r.power, output_enabled=r.output_enabled)

    def status(self) -> IPSStatus:
        r = self.read()
        if not r.output_enabled:
            self._voltage_setpoint = r.voltage
        return IPSStatus(
            model=self.model,
            output_enabled=r.output_enabled,
            remote=r.remote,
            front_panel_locked=r.locked,
            overheat=r.overheat,
            voltage_setpoint=self._voltage_setpoint,
            voltage_limit=r.voltage_limit,
            current_limit=r.current_limit,
            power_limit=r.power_limit,
            limits=self.limits,
        )

    def close(self) -> None:
        self._t.close()

    # -- helpers ---------------------------------------------------------

    def _expect(self, command: str, accepted) -> None:
        reading = self.read()
        if not accepted(reading):
            hint = "" if reading.remote else " The instrument is not in remote mode. Push the REM key on the front panel."
            raise RuntimeError(f"The instrument did not accept {command}.{hint}")


def _check_range(name: str, value: float, maximum: float, unit: str) -> None:
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} {value} {unit} out of range 0..{maximum:g} {unit}")


def _check_integer(name: str, value: float, maximum: float, unit: str) -> None:
    _check_range(name, value, maximum, unit)
    if value != int(value):
        raise ValueError(f"{name} {value} {unit} is not a whole number; the instrument accepts only steps of 1 {unit}")
