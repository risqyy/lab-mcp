"""In-memory stand-in for a DP832 that understands the SCPI subset the driver uses."""

from __future__ import annotations

import re


class FakeDP832Resource:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.state = {
            ch: {
                "VOLT": 0.0, "CURR": 0.0, "OUT": False,
                "OVP": 33.0 if ch < 3 else 5.5, "OVP_STAT": False, "OVP_TRIP": False,
                "OCP": 3.3, "OCP_STAT": False, "OCP_TRIP": False,
            }
            for ch in (1, 2, 3)
        }
        self.load_ohms = 10.0

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
            # Like the real DP832: this command also switches the output on.
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
            return "RIGOL TECHNOLOGIES,DP832,DP8C000000000,00.01.16\n"
        if m := re.fullmatch(r":SOUR(\d):(VOLT|CURR)\?", msg):
            return f"{self.state[int(m[1])][m[2]]:.3f}"
        if m := re.fullmatch(r":SOUR(\d):(VOLT|CURR):PROT(:STAT|:TRIP)?\?", msg):
            key = ("OVP" if m[2] == "VOLT" else "OCP") + (m[3] or "").replace(":", "_")
            val = self.state[int(m[1])][key]
            return ("ON" if val else "OFF") if isinstance(val, bool) else f"{val:.3f}"
        if m := re.fullmatch(r":OUTP\? CH(\d)", msg):
            return "ON" if self.state[int(m[1])]["OUT"] else "OFF"
        if m := re.fullmatch(r":OUTP:MODE\? CH(\d)", msg):
            return "CV"
        if m := re.fullmatch(r":MEAS:ALL\? CH(\d)", msg):
            s = self.state[int(m[1])]
            v = s["VOLT"] if s["OUT"] else 0.0
            i = min(v / self.load_ohms, s["CURR"])
            return f"{v:.3f},{i:.3f},{v * i:.3f}"
        raise AssertionError(f"unexpected query {msg!r}")
