"""SCPI driver for Rigol DC power supplies with three outputs: DP800 series (DP832, DP832A)
and DP900 series (DP932A, DP932U, DP932E).

The driver needs only a resource with ``write`` and ``query`` (for example, a pyvisa
resource). The driver works the same on USB and on the LAN. The tests use a simulated resource.

Differences between the series that the driver handles:

- Boolean replies: the DP800 sends ON/OFF and YES/NO, the DP900 sends 1/0.
- Coupling of CH1 and CH2: both series have tracking (CH2 follows the changes of CH1).
  Only the DP900 can connect CH1 and CH2 internally in series or in parallel (:OUTP:PAIR).
  The DP800 does not reply to :OUTP:PAIR?.
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
PairMode = Literal["OFF", "PARALLEL", "SERIES"]

COUPLED_CHANNELS = (1, 2)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChannelLimits(_Model):
    max_voltage: PositiveFloat
    max_current: PositiveFloat
    max_ovp: PositiveFloat
    max_ocp: PositiveFloat


class ModelSpec(_Model):
    series: Literal["DP800", "DP900"]
    limits: dict[int, ChannelLimits]


def _limits(v12: float, v3: float, ovp12: float, ovp3: float) -> dict[int, ChannelLimits]:
    ch12 = ChannelLimits(max_voltage=v12, max_current=3.0, max_ovp=ovp12, max_ocp=3.3)
    return {1: ch12, 2: ch12, 3: ChannelLimits(max_voltage=v3, max_current=3.0, max_ovp=ovp3, max_ocp=3.3)}


# Setpoint limits: rated values from the datasheets. The instruments accept slightly
# more (DP900: 5 % more), but the server does not use this range.
# OVP and OCP limits: DP800 datasheet, DP900 programming guide (table 4.33).
MODELS: dict[str, ModelSpec] = {
    "DP832": ModelSpec(series="DP800", limits=_limits(30.0, 5.0, 33.0, 5.5)),
    "DP832A": ModelSpec(series="DP800", limits=_limits(30.0, 5.0, 33.0, 5.5)),
    "DP932A": ModelSpec(series="DP900", limits=_limits(32.0, 6.0, 35.2, 6.6)),
    "DP932U": ModelSpec(series="DP900", limits=_limits(32.0, 6.0, 35.2, 6.6)),
    "DP932E": ModelSpec(series="DP900", limits=_limits(30.0, 6.0, 33.0, 6.6)),
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
    limits: ChannelLimits = Field(description="Limits of this channel for the connected model")
    tracking: bool = Field(description="CH1 and CH2: CH2 follows the changes of CH1. Always false for CH3.")
    pair_mode: PairMode | None = Field(
        description="DP900 CH1 and CH2: internal series or parallel connection. None for CH3 and for the DP800."
    )


class RigolDP:
    def __init__(self, resource: SCPIResource, model: str):
        try:
            self.spec = MODELS[model.upper()]
        except KeyError:
            raise ValueError(f"Unsupported model {model!r}; supported models are {sorted(MODELS)}") from None
        self._res = resource
        self.model = model.upper()
        self.limits = self.spec.limits

    @property
    def channels(self) -> list[int]:
        return sorted(self.limits)

    def identify(self) -> str:
        return self._res.query("*IDN?").strip()

    # -- setpoints -------------------------------------------------------

    def set_voltage(self, channel: int, volts: float) -> None:
        lim = self._limits(channel)
        _check_range("voltage", volts, lim.max_voltage, "V")
        self._check_not_coupled(channel)
        self._res.write(f":SOUR{channel}:VOLT {volts:.3f}")

    def set_current(self, channel: int, amps: float) -> None:
        lim = self._limits(channel)
        _check_range("current", amps, lim.max_current, "A")
        self._check_not_coupled(channel)
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
        if enabled:  # switching off is always permitted
            self._check_not_coupled(channel)
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

    # -- coupling of CH1 and CH2 ----------------------------------------

    def get_tracking(self) -> bool:
        if self.spec.series == "DP800":
            return _parse_bool(self._res.query(":OUTP:TRACK? CH1"))
        return _parse_bool(self._res.query(":OUTP:TRACK?"))

    def get_pair_mode(self) -> PairMode | None:
        if self.spec.series == "DP800":
            return None  # the DP800 does not reply to :OUTP:PAIR?
        reply = self._res.query(":OUTP:PAIR?").strip().upper()
        modes = {"OFF": "OFF", "PAR": "PARALLEL", "PARALLEL": "PARALLEL", "SER": "SERIES", "SERIES": "SERIES"}
        if reply not in modes:
            raise ValueError(f"Unexpected reply to :OUTP:PAIR?: {reply!r}")
        return modes[reply]  # type: ignore[return-value]

    def _check_not_coupled(self, channel: int) -> None:
        """Refuse changes on CH1 and CH2 while they are coupled.

        With tracking, a change of CH1 also changes CH2. In series or parallel mode,
        the limits and the meaning of the setpoints are different. The server does not
        support these modes.
        """
        if channel not in COUPLED_CHANNELS:
            return
        if self.get_tracking():
            raise ValueError(
                f"CH{channel}: tracking is on, thus a change of CH1 also changes CH2. "
                "The server does not change CH1 or CH2 in this mode. Set tracking to off on the instrument."
            )
        pair = self.get_pair_mode()
        if pair not in (None, "OFF"):
            raise ValueError(
                f"CH{channel}: CH1 and CH2 are connected in {pair.lower()} (internal). "
                "The server does not support this mode. Set the connection mode to OFF on the instrument."
            )

    # -- protection ------------------------------------------------------

    def set_ovp(self, channel: int, volts: float, enabled: bool = True) -> None:
        lim = self._limits(channel)
        _check_range("OVP level", volts, lim.max_ovp, "V")
        self._check_not_coupled(channel)
        self._res.write(f":SOUR{channel}:VOLT:PROT {volts:.3f}")
        self._res.write(f":SOUR{channel}:VOLT:PROT:STAT {'ON' if enabled else 'OFF'}")

    def set_ocp(self, channel: int, amps: float, enabled: bool = True) -> None:
        lim = self._limits(channel)
        _check_range("OCP level", amps, lim.max_ocp, "A")
        self._check_not_coupled(channel)
        self._res.write(f":SOUR{channel}:CURR:PROT {amps:.3f}")
        self._res.write(f":SOUR{channel}:CURR:PROT:STAT {'ON' if enabled else 'OFF'}")

    def clear_protection(self, channel: int) -> None:
        """Clear a tripped OVP and OCP. The output stays off.

        Do not use [:SOURce<n>]:VOLTage|CURRent:PROTection:CLEar: it also switches the
        output on (tested on a DP832, firmware 00.01.14, and documented for the DP900).
        The DP832 firmware does not accept the short form :OUTP:OVP:CLE, thus the long
        form is necessary.
        """
        self._limits(channel)
        self._res.write(f":OUTPut:OVP:CLEar CH{channel}")
        self._res.write(f":OUTPut:OCP:CLEar CH{channel}")

    def status(self, channel: int) -> ChannelStatus:
        lim = self._limits(channel)
        q = self._res.query
        coupled = channel in COUPLED_CHANNELS
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
            limits=lim,
            tracking=self.get_tracking() if coupled else False,
            pair_mode=self.get_pair_mode() if coupled else None,
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
