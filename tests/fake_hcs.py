"""In-memory stand-in for a Manson HCS power supply at the transport level."""

from __future__ import annotations

import re


class FakeHCSTransport:
    """Simulates an HCS-3404 (60 V / 10 A, 3 digits with 1 decimal place)."""

    def __init__(self, model: str = "HCS-3404", gmax: str = "600100") -> None:
        self.model = model
        self.gmax = gmax
        self.digits = len(gmax) // 2
        self.scale = 10 if self.digits == 3 else 100
        self.commands: list[str] = []
        self.voltage = 0.0
        self.current = 0.0
        self.upper_voltage = int(gmax[: self.digits]) / self.scale
        self.upper_current = int(gmax[self.digits :]) / self.scale
        self.output = False
        self.presets = [(5.0, 1.0), (12.0, 2.0), (24.0, 3.0)]
        self.load_ohms = 10.0
        self.closed = False

    def _fmt(self, value: float) -> str:
        return f"{round(value * self.scale):0{self.digits}d}"

    def request(self, command: str) -> str | None:
        self.commands.append(command)
        if command == "GMOD":
            return self.model
        if command == "GMAX":
            return self.gmax
        if command == "GETS":
            return self._fmt(self.voltage) + self._fmt(self.current)
        if command == "GOVP":
            return self._fmt(self.upper_voltage)
        if command == "GOCP":
            return self._fmt(self.upper_current)
        if command == "GETM":
            return "".join(self._fmt(v) + self._fmt(i) for v, i in self.presets)
        if command == "GETD":
            v = self.voltage if self.output else 0.0
            i = min(v / self.load_ohms, self.current)
            cc = self.output and v / self.load_ohms > self.current
            if cc:
                v = i * self.load_ohms
            return f"{round(v * 100):04d}{round(i * 100):04d}{1 if cc else 0}"
        if m := re.fullmatch(r"(VOLT|CURR|SOVP|SOCP)(\d+)", command):
            if len(m[2]) != self.digits:
                raise AssertionError(f"wrong number of digits in {command!r}")
            value = int(m[2]) / self.scale
            attr = {"VOLT": "voltage", "CURR": "current", "SOVP": "upper_voltage", "SOCP": "upper_current"}[m[1]]
            setattr(self, attr, value)
            return None
        if m := re.fullmatch(r"SOUT([01])", command):
            self.output = m[1] == "0"  # inverted logic
            return None
        if m := re.fullmatch(r"RUNM([0-2])", command):
            self.voltage, self.current = self.presets[int(m[1])]
            return None
        raise AssertionError(f"unexpected command {command!r}")

    def close(self) -> None:
        self.closed = True
