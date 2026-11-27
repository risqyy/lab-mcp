"""In-memory stand-in for an ISO-TECH IPS power supply at the transport level."""

from __future__ import annotations

import re


class FakeIPSTransport:
    """Simulates an IPS-405 (40 V / 5 A / 200 W) with a resistive load."""

    def __init__(self, voltage_limit: int = 40, current_limit: float = 5.0, power_limit: int = 200) -> None:
        self.commands: list[str] = []
        self.voltage = 0.0
        self.voltage_limit = voltage_limit
        self.current_limit = current_limit
        self.power_limit = power_limit
        self.output = False
        self.remote = True
        self.locked = False
        self.overheat = False
        self.load_ohms = 10.0
        self.stored: tuple | None = None
        self.closed = False

    def _output_values(self) -> tuple[float, float]:
        if not self.output:
            return 0.0, 0.0
        current = min(self.voltage / self.load_ohms, self.current_limit)
        return current * self.load_ohms, current

    def query(self, command: str) -> str:
        self.commands.append(command)
        v, a = self._output_values()
        if command == "L":
            flags = "".join(
                "1" if f else "0" for f in (self.output, self.overheat, False, True, self.remote, self.locked)
            )
            return (
                f"V{v:05.2f}A{a:05.3f}W{v * a:05.1f}"
                f"U{self.voltage_limit:02d}I{self.current_limit:04.2f}P{self.power_limit:03d}F{flags}"
            )
        raise AssertionError(f"unexpected query {command!r}")

    def write(self, command: str) -> None:
        self.commands.append(command)
        if not self.remote:
            return  # like the user manual: no set commands without remote mode
        if command in ("KOE", "KOD"):
            self.output = command == "KOE"
        elif command == "EEP":
            self.stored = (self.voltage, self.voltage_limit, self.current_limit, self.power_limit)
        elif m := re.fullmatch(r"SV (\d\d\.\d\d)", command):
            self.voltage = min(float(m[1]), self.voltage_limit)
        elif m := re.fullmatch(r"SU (\d\d)", command):
            self.voltage_limit = int(m[1])
        elif m := re.fullmatch(r"SI (\d+\.\d\d)", command):
            self.current_limit = float(m[1])
        elif m := re.fullmatch(r"SP (\d\d\d)", command):
            self.power_limit = int(m[1])
        else:
            raise AssertionError(f"unexpected command {command!r}")

    def close(self) -> None:
        self.closed = True
