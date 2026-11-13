"""In-memory stand-in for a RIGOL DSA832E for the SCPI subset the driver uses.

Like the real analyzer, the fake does not reply to an unknown query, or to a marker X query
when the marker is off (it raises a VISA timeout).
"""

from __future__ import annotations

import re

import pyvisa

_TIMEOUT = pyvisa.constants.StatusCode.error_timeout


def bmp_24(width: int = 4, height: int = 2) -> bytes:
    stride = (width * 3 + 3) & ~3
    pixels = bytes(stride * height)
    size = 54 + len(pixels)
    header = (
        b"BM" + size.to_bytes(4, "little") + bytes(4) + (54).to_bytes(4, "little")
        + (40).to_bytes(4, "little") + width.to_bytes(4, "little", signed=True) + height.to_bytes(4, "little", signed=True)
        + (1).to_bytes(2, "little") + (24).to_bytes(2, "little") + bytes(4) + len(pixels).to_bytes(4, "little") + bytes(16)
    )
    return header + pixels


class FakeDSA800Resource:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.center, self.span = 1.6e9, 3.2e9
        self.rbw, self.rbw_auto, self.vbw, self.vbw_auto = 1e6, True, 1e6, True
        self.sweep_time, self.sweep_time_auto, self.points, self.count = 0.016, True, 601, 1
        self.detector, self.emi = "POS", False
        self.rlevel, self.pdiv, self.spacing, self.unit = 0.0, 10.0, "LOG", "DBM"
        self.atten, self.atten_auto, self.preamp = 10, True, False
        self.continuous = True
        self.trace_modes = {1: "WRIT", 2: "BLANK", 3: "BLANK"}
        self.format = "ASCII"
        self.markers = {n: {"on": False, "mode": "POS", "x": 0.0} for n in (1, 2, 3, 4)}
        # Trace values: noise at -80 dBm, a peak of -20 dBm at index 300 and of -40 dBm at index 100.
        self.values = [-80.0] * 601
        self.values[300], self.values[100] = -20.0, -40.0
        # Number of COUNt:CURRent? queries until the single sweep is complete. None: never.
        self.single_polls: int | None = 0
        self.current = 1
        self.screen = bmp_24()
        self._pending: bytes | None = None
        self.chunk_size = 1024
        self.session = 1

    def close(self) -> None:
        pass

    @property
    def start(self) -> float:
        return self.center - self.span / 2

    @property
    def stop(self) -> float:
        return self.center + self.span / 2

    def _index(self, x: float) -> int:
        if self.span == 0:
            return round(x / self.sweep_time * (self.points - 1))
        return round((x - self.start) / self.span * (self.points - 1))

    # -- write ---------------------------------------------------------

    def write(self, msg: str) -> None:
        self.writes.append(msg)
        if msg.startswith(":TRACe:DATA? TRACE"):
            text = ", ".join(f"{v:e}" for v in self.values)
            self._pending = self._block(text.encode())
            return
        if msg == ":PRIVate:SNAP? BMP":
            self._pending = self._block(self.screen)
            return
        if m := re.fullmatch(r":SENSe:FREQuency:(CENTer|SPAN|STARt|STOP) (\S+)", msg):
            value = float(m[2])
            if m[1] == "CENTer":
                self.center = value
            elif m[1] == "SPAN":
                self.span = value
            elif m[1] == "STARt":
                self.center, self.span = (value + self.stop) / 2, self.stop - value
            else:
                self.center, self.span = (self.start + value) / 2, value - self.start
        elif msg == ":SENSe:FREQuency:SPAN:FULL":
            self.center, self.span = 1.6e9, 3.2e9
        elif m := re.fullmatch(r":SENSe:BANDwidth:(RESolution|VIDeo)(:AUTO)? (\S+)", msg):
            key = "rbw" if m[1] == "RESolution" else "vbw"
            if m[2]:
                setattr(self, f"{key}_auto", m[3] == "ON")
            else:
                setattr(self, key, float(m[3]))
                setattr(self, f"{key}_auto", False)
        elif m := re.fullmatch(r":SENSe:SWEep:TIME(:AUTO)? (\S+)", msg):
            if m[1]:
                self.sweep_time_auto = m[2] == "ON"
            else:
                self.sweep_time, self.sweep_time_auto = float(m[2]), False
        elif m := re.fullmatch(r":SENSe:SWEep:POINts (\d+)", msg):
            self.points = int(m[1])
        elif m := re.fullmatch(r":SENSe:DETector:FUNCtion (\w+)", msg):
            self.detector = {"POSitive": "POS", "NEGative": "NEG", "NORMal": "NORM", "SAMPle": "SAMP",
                             "VAVerage": "VAV", "QPEak": "QPEAK"}.get(m[1], m[1])
            if self.detector == "QPEAK":
                self.emi = True  # like the analyzer: quasi-peak switches the EMI filter on
        elif m := re.fullmatch(r":SENSe:BANDwidth:EMIFilter:STATe (ON|OFF)", msg):
            if self.detector != "QPEAK":  # the analyzer ignores OFF with the quasi-peak detector
                self.emi = m[1] == "ON"
        elif m := re.fullmatch(r":SENSe:POWer:RF:ATTenuation(:AUTO)? (\S+)", msg):
            if m[1]:
                self.atten_auto = m[2] == "ON"
            else:
                self.atten, self.atten_auto = int(m[2]), False
        elif m := re.fullmatch(r":SENSe:POWer:RF:GAIN:STATe (ON|OFF)", msg):
            self.preamp = m[1] == "ON"
        elif m := re.fullmatch(r":DISPlay:WINdow:TRACe:Y:SCALe:(RLEVel|PDIVision) (\S+)", msg):
            setattr(self, "rlevel" if m[1] == "RLEVel" else "pdiv", float(m[2]))
        elif m := re.fullmatch(r":TRACe([1-3]):MODE (\w+)", msg):
            self.trace_modes[int(m[1])] = {"WRITe": "WRIT", "MAXHold": "MAXH", "MINHold": "MINH", "VIEW": "VIEW",
                                           "BLANk": "BLANK", "VIDeoavg": "VID", "POWeravg": "POW"}[m[2]]
        elif msg == ":FORMat:TRACe:DATA ASCii":
            self.format = "ASCII"
        elif m := re.fullmatch(r":INITiate:CONTinuous (ON|OFF)", msg):
            self.continuous = m[1] == "ON"
        elif msg == ":INITiate:IMMediate":
            self.current = 0
        elif m := re.fullmatch(r":CALCulate:MARKer([1-4]):STATe ON", msg):
            self.markers[int(m[1])].update(on=True, mode="POS")
        elif m := re.fullmatch(r":CALCulate:MARKer([1-4]):MODE POSition", msg):
            self.markers[int(m[1])]["mode"] = "POS"
        elif m := re.fullmatch(r":CALCulate:MARKer([1-4]):(MAXimum:MAX|MAXimum:NEXT|MAXimum:LEFT|MAXimum:RIGHt|MINimum)", msg):
            marker = self.markers[int(m[1])]
            ordered = sorted(range(len(self.values)), key=lambda i: self.values[i], reverse=True)
            index = {"MAXimum:MAX": ordered[0], "MAXimum:NEXT": ordered[1], "MINimum": ordered[-1]}.get(m[2], ordered[0])
            marker["x"] = self.start + index * self.span / (len(self.values) - 1)
        elif m := re.fullmatch(r":CALCulate:MARKer([1-4]):X (\S+)", msg):
            self.markers[int(m[1])]["x"] = float(m[2])
        elif msg == ":CALCulate:MARKer:AOFF":
            for marker in self.markers.values():
                marker["on"] = False
        else:
            raise AssertionError(f"unexpected write {msg!r}")

    def _block(self, data: bytes) -> bytes:
        return b"#9" + f"{len(data):09d}".encode() + data + b"\n"

    def read_raw(self, size: int | None = None) -> bytes:
        if not self._pending:
            raise pyvisa.errors.VisaIOError(_TIMEOUT)
        chunk, self._pending = self._pending[:self.chunk_size], self._pending[self.chunk_size:]
        return chunk

    # -- query ---------------------------------------------------------

    def query(self, msg: str) -> str:
        replies = {
            "*IDN?": "Rigol Technologies,DSA832E,DSA8H000000001,00.01.06.00.04",
            ":SENSe:FREQuency:CENTer?": f"{self.center:.0f}",
            ":SENSe:FREQuency:SPAN?": f"{self.span:.0f}",
            ":SENSe:FREQuency:STARt?": f"{self.start:.0f}",
            ":SENSe:FREQuency:STOP?": f"{self.stop:.0f}",
            ":SENSe:BANDwidth:RESolution?": f"{self.rbw:.0f}",
            ":SENSe:BANDwidth:RESolution:AUTO?": str(int(self.rbw_auto)),
            ":SENSe:BANDwidth:VIDeo?": f"{self.vbw:.0f}",
            ":SENSe:BANDwidth:VIDeo:AUTO?": str(int(self.vbw_auto)),
            ":SENSe:SWEep:TIME?": f"{self.sweep_time:E}",
            ":SENSe:SWEep:TIME:AUTO?": str(int(self.sweep_time_auto)),
            ":SENSe:SWEep:POINts?": str(self.points),
            ":SENSe:SWEep:COUNt?": str(self.count),
            ":SENSe:DETector:FUNCtion?": self.detector,
            ":SENSe:BANDwidth:EMIFilter:STATe?": str(int(self.emi)),
            ":DISPlay:WINdow:TRACe:Y:SCALe:RLEVel?": f"{self.rlevel:E}",
            ":DISPlay:WINdow:TRACe:Y:SCALe:PDIVision?": f"{self.pdiv:E}",
            ":DISPlay:WINdow:TRACe:Y:SCALe:SPACing?": self.spacing,
            ":UNIT:POWer?": self.unit,
            ":SENSe:POWer:RF:ATTenuation?": str(self.atten),
            ":SENSe:POWer:RF:ATTenuation:AUTO?": str(int(self.atten_auto)),
            ":SENSe:POWer:RF:GAIN:STATe?": str(int(self.preamp)),
            ":INITiate:CONTinuous?": str(int(self.continuous)),
            ":FORMat:TRACe:DATA?": self.format,
        }
        if msg in replies:
            return replies[msg]
        if m := re.fullmatch(r":TRACe([1-3]):MODE\?", msg):
            return self.trace_modes[int(m[1])]
        if msg == ":SENSe:SWEep:COUNt:CURRent?":
            if self.current < self.count and self.single_polls is not None:
                if self.single_polls == 0:
                    self.current = self.count
                else:
                    self.single_polls -= 1
            return str(self.current)
        if m := re.fullmatch(r":CALCulate:MARKer([1-4]):(STATe|MODE|X|Y)\?", msg):
            marker = self.markers[int(m[1])]
            if m[2] == "STATe":
                return str(int(marker["on"]))
            if m[2] == "MODE":
                return marker["mode"] if marker["on"] else "ERR"
            if not marker["on"]:
                if m[2] == "X":
                    raise pyvisa.errors.VisaIOError(_TIMEOUT)  # like the analyzer: no reply
                return "0.000000E+00"
            if m[2] == "X":
                return f"{marker['x']:.0f}" if self.span else f"{marker['x']:E}"
            index = min(max(self._index(marker["x"]), 0), len(self.values) - 1)
            return f"{self.values[index]:E}"
        raise pyvisa.errors.VisaIOError(_TIMEOUT)
