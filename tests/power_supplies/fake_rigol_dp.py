"""In-memory stand-in for a Rigol DP832 (DP800) or DP932A (DP900) for the SCPI subset the driver uses."""

from __future__ import annotations

import re


class FakeRigolDPResource:
    def __init__(self, model: str = "DP832") -> None:
        self.model = model
        self.dp900 = model.startswith("DP9")
        self.writes: list[str] = []
        ovp12, ovp3 = (35.2, 6.6) if self.dp900 else (33.0, 5.5)
        self.state = {
            ch: {
                "VOLT": 0.0, "CURR": 0.0, "OUT": False,
                "OVP": ovp12 if ch < 3 else ovp3, "OVP_STAT": False, "OVP_TRIP": False,
                "OCP": 3.3, "OCP_STAT": False, "OCP_TRIP": False,
            }
            for ch in (1, 2, 3)
        }
        self.tracking = False
        self.pair = "OFF"  # DP900 only: OFF, PARALLEL or SERIES
        self.load_ohms = 10.0

    def _bool(self, value: bool, yes_no: bool = False) -> str:
        if self.dp900:
            return "1" if value else "0"
        if yes_no:
            return "YES" if value else "NO"
        return "ON" if value else "OFF"

    def close(self) -> None:
        pass

    def write(self, msg: str) -> None:
        self.writes.append(msg)
        if m := re.fullmatch(r":SOUR(\d):(VOLT|CURR) ([\d.]+)", msg):
            self.state[int(m[1])][m[2]] = float(m[3])
        elif m := re.fullmatch(r":SOUR(\d):(VOLT|CURR):PROT ([\d.]+)", msg):
            self.state[int(m[1])]["OVP" if m[2] == "VOLT" else "OCP"] = float(m[3])
        elif m := re.fullmatch(r":SOUR(\d):(VOLT|CURR):PROT:STAT (ON|OFF)", msg):
            self.state[int(m[1])][("OVP" if m[2] == "VOLT" else "OCP") + "_STAT"] = m[3] == "ON"
        elif m := re.fullmatch(r":SOUR(\d):(VOLT|CURR):PROT:CLE", msg):
            # Like the real DP832 and the DP900 guide: this command also switches the output on.
            self.state[int(m[1])][("OVP" if m[2] == "VOLT" else "OCP") + "_TRIP"] = False
            self.state[int(m[1])]["OUT"] = True
        elif m := re.fullmatch(r":OUTPut:(OVP|OCP):CLEar CH(\d)", msg):
            self.state[int(m[2])][m[1] + "_TRIP"] = False
        elif re.fullmatch(r":OUTP:(OVP|OCP):CLE.*", msg):
            raise AssertionError(f"the DP832 firmware does not accept the short form {msg!r}")
        elif m := re.fullmatch(r":OUTP CH(\d),(ON|OFF)", msg):
            self.state[int(m[1])]["OUT"] = m[2] == "ON"
        else:
            raise AssertionError(f"unexpected write {msg!r}")

    def query(self, msg: str) -> str:
        if msg == "*IDN?":
            serial = "DP9A000000001" if self.dp900 else "DP8C000000000"
            return f"RIGOL TECHNOLOGIES,{self.model},{serial},00.01.16\n"
        if m := re.fullmatch(r":SOUR(\d):(VOLT|CURR)\?", msg):
            return f"{self.state[int(m[1])][m[2]]:.3f}"
        if m := re.fullmatch(r":SOUR(\d):(VOLT|CURR):PROT(:STAT|:TRIP)?\?", msg):
            key = ("OVP" if m[2] == "VOLT" else "OCP") + (m[3] or "").replace(":", "_")
            val = self.state[int(m[1])][key]
            return self._bool(val, yes_no=key.endswith("TRIP")) if isinstance(val, bool) else f"{val:.3f}"
        if m := re.fullmatch(r":OUTP\? CH(\d)", msg):
            return self._bool(self.state[int(m[1])]["OUT"])
        if re.fullmatch(r":OUTP:MODE\? CH(\d)", msg):
            return "CV"
        if msg == ":OUTP:TRACK? CH1" and not self.dp900:
            return self._bool(self.tracking)
        if msg == ":OUTP:TRACK?" and self.dp900:
            return self._bool(self.tracking)
        if msg == ":OUTP:PAIR?":
            if not self.dp900:
                raise TimeoutError("the DP800 does not reply to :OUTP:PAIR?")
            return self.pair
        if m := re.fullmatch(r":MEAS:ALL\? CH(\d)", msg):
            s = self.state[int(m[1])]
            v = s["VOLT"] if s["OUT"] else 0.0
            i = min(v / self.load_ohms, s["CURR"])
            return f"{v:.3f},{i:.3f},{v * i:.3f}"
        raise AssertionError(f"unexpected query {msg!r}")
