"""In-memory stand-in for a Rigol DM3058E for the SCPI subset the driver uses."""

from __future__ import annotations

import re

import pyvisa

# :FUNCtion keyword -> reply to :FUNCtion? (as in the programming guide)
FUNCTION_REPLIES = {
    "VOLTage:DC": "DCV", "VOLTage:AC": "ACV", "CURRent:DC": "DCI", "CURRent:AC": "ACI",
    "RESistance": "2WR", "FRESistance": "4WR", "FREQuency": "FREQ", "PERiod": "PERI",
    "CONTinuity": "CONT", "DIODe": "DIODE", "CAPacitance": "CAP",
}
DEFAULT_RANGES = {"VOLTage:DC": 2, "VOLTage:AC": 2, "CURRent:DC": 0, "CURRent:AC": 1,
                  "RESistance": 3, "FRESistance": 3, "CAPacitance": 2}


class FakeDM3058Resource:
    def __init__(self, cmdset: str = "RIGOL") -> None:
        self.cmdset = cmdset
        self.writes: list[str] = []
        self.function = "VOLTage:DC"
        self.auto = True
        self.ranges = dict(DEFAULT_RANGES)
        self.rates = {f: "M" for f in ("VOLTage:DC", "VOLTage:AC", "CURRent:DC", "CURRent:AC", "RESistance", "FRESistance")}
        self.values: dict[str, list[str]] = {f: ["1.234560e+00"] for f in FUNCTION_REPLIES}
        self.closed = False

    @property
    def session(self) -> int:
        # As pyvisa: a closed resource has no session.
        if self.closed:
            raise pyvisa.errors.InvalidSession()
        return 1

    def close(self) -> None:
        self.closed = True

    def write(self, msg: str) -> None:
        self.writes.append(msg)
        if m := re.fullmatch(r"CMDSET (RIGOL|AGILENT|FLUKE)", msg):
            self.cmdset = m[1]
        elif m := re.fullmatch(r":FUNCtion:(\S+)", msg):
            assert m[1] in FUNCTION_REPLIES, msg
            self.function = m[1]
        elif msg == ":MEASure AUTO":
            self.auto = True
        elif m := re.fullmatch(r":MEASure:(\S+) (\d)", msg):
            assert m[1] == self.function, f"range for {m[1]} while the function is {self.function}"
            self.ranges[m[1]] = int(m[2])
            self.auto = False
        elif m := re.fullmatch(r":RATE:(\S+) ([FMS])", msg):
            self.rates[m[1]] = m[2]
        else:
            raise AssertionError(f"unexpected write {msg!r}")

    def query(self, msg: str) -> str:
        if msg == "CMDSET?":
            return self.cmdset
        assert self.cmdset == "RIGOL", f"query {msg!r} with command set {self.cmdset}"
        if msg == "*IDN?":
            return "Rigol Technologies,DM3058E,DM3R000000001,01.01.00.01.11.00"
        if msg == ":FUNCtion?":
            return FUNCTION_REPLIES[self.function]
        if m := re.fullmatch(r":MEASure:(\S+):RANGe\?", msg):
            return str(self.ranges[m[1]])
        if m := re.fullmatch(r":MEASure:(\S+)\?", msg):
            assert m[1] == self.function, f"measure {m[1]} while the function is {self.function}"
            values = self.values[m[1]]
            return values.pop(0) if len(values) > 1 else values[0]
        if m := re.fullmatch(r":RATE:(\S+)\?", msg):
            return self.rates[m[1]]
        raise AssertionError(f"unexpected query {msg!r}")
