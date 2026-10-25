"""SCPI driver for the Rigol DP832 and DP832A DC power supply with three outputs.

The driver needs only a resource with ``write`` and ``query`` (for example, a pyvisa
resource). The driver works the same on USB and on the LAN. The tests use a simulated resource.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat


class SCPIResource(Protocol):
    def write(self, message: str) -> object: ...
    def query(self, message: str) -> str: ...


Channel = Annotated[int, Field(ge=1, description="Output channel number")]
Volts = Annotated[float, Field(ge=0, description="Volts")]
Amps = Annotated[float, Field(ge=0, description="Amperes")]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChannelLimits(_Model):
    max_voltage: PositiveFloat
    max_current: PositiveFloat
    max_ovp: PositiveFloat
    max_ocp: PositiveFloat


# Ratings from the DP800 series datasheet.
DP832_LIMITS: dict[int, ChannelLimits] = {
    1: ChannelLimits(max_voltage=30.0, max_current=3.0, max_ovp=33.0, max_ocp=3.3),
    2: ChannelLimits(max_voltage=30.0, max_current=3.0, max_ovp=33.0, max_ocp=3.3),
    3: ChannelLimits(max_voltage=5.0, max_current=3.0, max_ovp=5.5, max_ocp=3.3),
}


class Measurement(_Model):
    channel: Channel
    voltage: float = Field(description="Measured output voltage in V")
    current: float = Field(description="Measured output current in A")
    power: float = Field(description="Measured output power in W")


class ChannelStatus(_Model):
    channel: Channel
    output_enabled: bool
    voltage_setpoint: Volts
    current_setpoint: Amps
    ovp_enabled: bool
    ovp_level: Volts
    ovp_tripped: bool
    ocp_enabled: bool
    ocp_level: Amps
    ocp_tripped: bool
    mode: Literal["CV", "CC", "UR"] = Field(description="Constant voltage, constant current or unregulated")


class DP832:
    def __init__(self, resource: SCPIResource, limits: dict[int, ChannelLimits] = DP832_LIMITS):
        self._res = resource
        self.limits = limits

    @property
    def channels(self) -> list[int]:
        return sorted(self.limits)

    def identify(self) -> str:
        return self._res.query("*IDN?").strip()

    # -- setpoints -------------------------------------------------------

    def set_voltage(self, channel: int, volts: float) -> None:
        lim = self._limits(channel)
        _check_range("voltage", volts, lim.max_voltage, "V")
        self._res.write(f":SOUR{channel}:VOLT {volts:.3f}")

    def set_current(self, channel: int, amps: float) -> None:
        lim = self._limits(channel)
        _check_range("current", amps, lim.max_current, "A")
        self._res.write(f":SOUR{channel}:CURR {amps:.3f}")

    def get_voltage_setpoint(self, channel: int) -> float:
        self._limits(channel)
        return float(self._res.query(f":SOUR{channel}:VOLT?"))

    def get_current_setpoint(self, channel: int) -> float:
        self._limits(channel)
        return float(self._res.query(f":SOUR{channel}:CURR?"))

    # -- output ----------------------------------------------------------

    def set_output(self, channel: int, enabled: bool) -> None:
        self._limits(channel)
        self._res.write(f":OUTP CH{channel},{'ON' if enabled else 'OFF'}")

    def get_output(self, channel: int) -> bool:
        self._limits(channel)
        return _parse_bool(self._res.query(f":OUTP? CH{channel}"))

    def all_outputs_off(self) -> None:
        for ch in self.channels:
            self.set_output(ch, False)

    # -- measurement -----------------------------------------------------

    def measure(self, channel: int) -> Measurement:
        self._limits(channel)
        v, i, p = (float(x) for x in self._res.query(f":MEAS:ALL? CH{channel}").split(","))
        return Measurement(channel=channel, voltage=v, current=i, power=p)

    def get_mode(self, channel: int) -> str:
        self._limits(channel)
        return self._res.query(f":OUTP:MODE? CH{channel}").strip().upper()

    # -- protection ------------------------------------------------------

    def set_ovp(self, channel: int, volts: float, enabled: bool = True) -> None:
        lim = self._limits(channel)
        _check_range("OVP level", volts, lim.max_ovp, "V")
        self._res.write(f":SOUR{channel}:VOLT:PROT {volts:.3f}")
        self._res.write(f":SOUR{channel}:VOLT:PROT:STAT {'ON' if enabled else 'OFF'}")

    def set_ocp(self, channel: int, amps: float, enabled: bool = True) -> None:
        lim = self._limits(channel)
        _check_range("OCP level", amps, lim.max_ocp, "A")
        self._res.write(f":SOUR{channel}:CURR:PROT {amps:.3f}")
        self._res.write(f":SOUR{channel}:CURR:PROT:STAT {'ON' if enabled else 'OFF'}")

    def clear_protection(self, channel: int) -> None:
        """Clear a tripped OVP and OCP. The output stays off.

        Do not use [:SOURce<n>]:VOLTage|CURRent:PROTection:CLEar: it also switches the
        output on (tested on a DP832, firmware 00.01.14). The DP832 firmware does not
        accept the short form :OUTP:OVP:CLE, thus the long form is necessary.
        """
        self._limits(channel)
        self._res.write(f":OUTPut:OVP:CLEar CH{channel}")
        self._res.write(f":OUTPut:OCP:CLEar CH{channel}")

    def status(self, channel: int) -> ChannelStatus:
        self._limits(channel)
        q = self._res.query
        return ChannelStatus(
            channel=channel,
            output_enabled=self.get_output(channel),
            voltage_setpoint=self.get_voltage_setpoint(channel),
            current_setpoint=self.get_current_setpoint(channel),
            ovp_enabled=_parse_bool(q(f":SOUR{channel}:VOLT:PROT:STAT?")),
            ovp_level=float(q(f":SOUR{channel}:VOLT:PROT?")),
            ovp_tripped=_parse_bool(q(f":SOUR{channel}:VOLT:PROT:TRIP?")),
            ocp_enabled=_parse_bool(q(f":SOUR{channel}:CURR:PROT:STAT?")),
            ocp_level=float(q(f":SOUR{channel}:CURR:PROT?")),
            ocp_tripped=_parse_bool(q(f":SOUR{channel}:CURR:PROT:TRIP?")),
            mode=self.get_mode(channel),
        )

    # -- helpers ---------------------------------------------------------

    def _limits(self, channel: int) -> ChannelLimits:
        try:
            return self.limits[channel]
        except KeyError:
            raise ValueError(f"Invalid channel {channel}; valid channels are {self.channels}") from None


def _check_range(name: str, value: float, maximum: float, unit: str) -> None:
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} {value} {unit} out of range 0..{maximum} {unit}")


def _parse_bool(reply: str) -> bool:
    reply = reply.strip().upper()
    if reply in ("ON", "1", "YES"):
        return True
    if reply in ("OFF", "0", "NO"):
        return False
    raise ValueError(f"Unexpected boolean reply from instrument: {reply!r}")
