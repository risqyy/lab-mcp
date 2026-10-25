"""AFG-2105 simulation, including the V2.02 voltage-query and clamping behavior."""

from __future__ import annotations

import re


class FakeAFGTransport:
    def __init__(self):
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.errors: list[str] = []
        self.is_open = True
        self.waveform = "SIN"
        self.frequency = 1000.0
        self.amplitude = 0.5  # Vpp into 50 Ohm, including when LOAD is INF
        self.offset = 0.0
        self.duty = 50.0
        self.symmetry = 50.0
        self.load = "INF"
        self.unit = "VPP"
        self.output = False
        self.modes = {"AM": False, "FM": False, "FSKey": False, "SWEep": False}
        self.query_overrides: dict[str, str] = {}
        self.ignore_writes: set[str] = set()

    def close(self):
        self.is_open = False

    def limits(self, command):
        if command == "FREQuency":
            return 0.1, 1e6 if self.waveform == "RAMP" else 5e6
        if command == "AMPlitude":
            return 0.001, 10 - 2 * abs(self.offset)
        if command == "DCOffset":
            bound = 5 - self.amplitude / 2
            return -bound, bound
        if command == "SQUare:DCYCle":
            if self.frequency >= 5e6:
                return 40, 60
            return (1, 99) if self.frequency < 100000 else (20, 80)
        if command == "RAMP:SYMMetry":
            return 0, 100
        raise AssertionError(command)

    def write(self, message):
        assert self.is_open
        assert not message.startswith(":") and "APPL" not in message.upper(), message
        self.writes.append(message)
        command, value = message.split(" ", 1)
        if command in self.ignore_writes:
            return
        if command == "OUTPut":
            self.output = value == "ON"
            return
        if command == "OUTPut:LOAD":
            self.load = value
            return
        assert command.startswith("SOURce1:")
        command = command.removeprefix("SOURce1:")
        if command == "VOLTage:UNIT":
            self.unit = value
            return
        if command == "FUNCtion":
            self.waveform = value
            if value == "RAMP" and self.frequency > 1e6:
                self.frequency = 1e6
            return
        numeric = float(value.removesuffix("VPP"))
        if command in ("AMPlitude", "DCOffset") and self.load == "INF":
            numeric /= 2
        low, high = self.limits(command)
        if not low <= numeric <= high:
            self.errors.append("-221" if command == "SQUare:DCYCle" else "-222")
            numeric = min(high, max(low, numeric))
        name = {"FREQuency": "frequency", "AMPlitude": "amplitude", "DCOffset": "offset",
                "SQUare:DCYCle": "duty", "RAMP:SYMMetry": "symmetry"}[command]
        setattr(self, name, numeric)
        if command == "FREQuency" and self.waveform == "SQU":
            low, high = self.limits("SQUare:DCYCle")
            if not low <= self.duty <= high:
                self.duty = min(high, max(low, self.duty))
                self.errors.append("-221")

    def query(self, message):
        assert self.is_open
        assert not message.startswith(":"), message
        self.queries.append(message)
        if message in self.query_overrides:
            return self.query_overrides[message]
        if message == "*IDN?":
            return "GW INSTEK,AFG-2105,SN:TEST0001,V2.02"
        if message == "OUTPut?":
            return str(int(self.output))
        if message == "OUTPut:LOAD?":
            return self.load
        if message == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else "No Error"
        assert message.startswith("SOURce1:"), message
        message = message.removeprefix("SOURce1:")
        if match := re.fullmatch(r"(.+)\? (MIN|MAX)", message):
            return str(self.limits(match[1])[match[2] == "MAX"])
        if message.endswith(":STATe?"):
            return str(int(self.modes[message.removesuffix(":STATe?")]))
        name = {"FUNCtion?": "waveform", "FREQuency?": "frequency", "AMPlitude?": "amplitude",
                "DCOffset?": "offset", "SQUare:DCYCle?": "duty", "RAMP:SYMMetry?": "symmetry",
                "VOLTage:UNIT?": "unit"}[message]
        return str(getattr(self, name))
