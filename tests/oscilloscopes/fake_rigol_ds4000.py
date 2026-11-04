"""In-memory stand-in for a RIGOL DS4014 for the SCPI subset the driver uses.

Like the real oscilloscope, the fake does not reply to an unknown query (it raises TimeoutError).
"""

from __future__ import annotations

import re

import pyvisa


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


class FakeDS4000Resource:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.channels = {
            n: {"DISP": n == 1, "SCAL": 1.0, "OFFS": 0.0, "COUP": "DC", "PROB": 10.0, "BWLI": "OFF", "IMPE": "OMEG"}
            for n in (1, 2, 3, 4)
        }
        self.tscale, self.toffset, self.srate = 2e-4, 0.0, 1e8
        self.trig = {"MODE": "EDGE", "SOUR": "CHAN1", "SLOP": "POS", "LEV": 1.5, "COUP": "DC", "SWE": "AUTO"}
        self.status = "AUTO"
        self.single_polls: int | None = None
        # Like the oscilloscope: after :SINGle, STATus? can send the previous STOP (stale_polls times),
        # and the point count is 0 until the acquisition is complete.
        self.stale_polls = 0
        self._stale = 0
        self.acquiring = False
        # Number of :SINGle commands that stop without data, like the oscilloscope sometimes does.
        self.empty_singles = 0
        self.measurements: dict[str, float] = {"VPP": 3.0, "FREQuency": 1000.0, "PDUTy": 0.5, "OVERshoot": 0.021}  # ratios, like the oscilloscope
        self.codes = bytes([127] * 700 + [223] * 700)  # 0 V, then 3 div above the center
        self.screen = bmp_24()
        self.wav = {"SOUR": "CHANnel1", "MODE": "NORMal", "FORM": "BYTE"}
        self._pending: bytes | None = None
        self.chunk_size = 512
        self.session = 1

    def close(self) -> None:
        pass

    # -- write ---------------------------------------------------------

    def write(self, msg: str) -> None:
        self.writes.append(msg)
        if msg == ":WAVeform:DATA?":
            self._pending = self._block(b"" if self.acquiring else self.codes)
            return
        if msg == ":DISPlay:DATA?":
            self._pending = self._block(self.screen)
            return
        if m := re.fullmatch(r":CHANnel([1-4]):(DISPlay|COUPling|BWLimit|PROBe|SCALe|OFFSet|IMPedance) (\S+)", msg):
            ch, key, value = self.channels[int(m[1])], m[2][:4].upper(), m[3]
            if key == "DISP":
                ch["DISP"] = value in ("ON", "1")
            elif key in ("SCAL", "OFFS", "PROB"):
                ch[key] = float(value)
            else:
                ch[key] = value
        elif m := re.fullmatch(r":TIMebase:MAIN:(SCALe|OFFSet) (\S+)", msg):
            if m[1] == "SCALe":
                self.tscale = float(m[2])
            else:
                self.toffset = float(m[2])
        elif msg == ":TRIGger:MODE EDGE":
            self.trig["MODE"] = "EDGE"
        elif m := re.fullmatch(r":TRIGger:EDGe:SOURce (CHANnel[1-4]|EXT|EXT5|ACLine)", msg):
            self.trig["SOUR"] = {"EXT": "EXT", "EXT5": "EXT5", "ACLine": "ACL"}.get(m[1], f"CHAN{m[1][-1]}")
        elif m := re.fullmatch(r":TRIGger:EDGe:SLOPe (POSitive|NEGative|RFALl)", msg):
            self.trig["SLOP"] = {"POSitive": "POS", "NEGative": "NEG", "RFALl": "RFAL"}[m[1]]
        elif m := re.fullmatch(r":TRIGger:EDGe:LEVel (\S+)", msg):
            self.trig["LEV"] = float(m[1])
        elif m := re.fullmatch(r":TRIGger:COUPling (DC|AC|HFReject|LFReject)", msg):
            self.trig["COUP"] = {"HFReject": "HFR", "LFReject": "LFR"}.get(m[1], m[1])
        elif m := re.fullmatch(r":TRIGger:SWEep (AUTO|NORMal|SINGle)", msg):
            self.trig["SWE"] = {"NORMal": "NORM", "SINGle": "SING"}.get(m[1], m[1])
        elif msg == ":RUN":
            self.acquiring = False
            if self.trig["SWE"] == "SING":
                self.trig["SWE"] = "AUTO"  # like the oscilloscope: RUN in single mode selects auto
            self.status = "AUTO" if self.trig["SWE"] == "AUTO" else "WAIT"
        elif msg == ":STOP":
            self.status = "STOP"
        elif msg == ":SINGle":
            self.trig["SWE"] = "SING"
            self.status = "WAIT"
            self.acquiring = True
            self._stale = self.stale_polls
            if self.empty_singles > 0:
                self.empty_singles -= 1
                self.status = "STOP"
        elif m := re.fullmatch(r":WAVeform:(SOURce|MODE|FORMat) (\S+)", msg):
            self.wav[m[1][:4].upper()] = m[2]
        else:
            raise AssertionError(f"unexpected write {msg!r}")

    def _block(self, data: bytes) -> bytes:
        return b"#9" + f"{len(data):09d}".encode() + data + b"\n"

    def read_raw(self, size: int | None = None) -> bytes:
        if not self._pending:
            raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
        chunk, self._pending = self._pending[:self.chunk_size], self._pending[self.chunk_size:]
        return chunk

    # -- query ---------------------------------------------------------

    def query(self, msg: str) -> str:
        if msg == "*IDN?":
            return "RIGOL TECHNOLOGIES,DS4014,DS4A000000001,00.02.02.SP5"
        if m := re.fullmatch(r":CHANnel([1-4]):(DISPlay|COUPling|BWLimit|PROBe|SCALe|OFFSet|IMPedance)\?", msg):
            value = self.channels[int(m[1])][m[2][:4].upper()]
            if isinstance(value, bool):
                return "1" if value else "0"
            if isinstance(value, float):
                return f"{value:e}" if m[2] != "PROBe" else f"{value:g}"
            return str(value)
        if msg == ":TIMebase:MAIN:SCALe?":
            return f"{self.tscale:e}"
        if msg == ":TIMebase:MAIN:OFFSet?":
            return f"{self.toffset:e}"
        if msg == ":ACQuire:SRATe?":
            return f"{self.srate:e}"
        if msg == ":TRIGger:SWEep?":
            return self.trig["SWE"]
        if msg == ":TRIGger:MODE?":
            return self.trig["MODE"]
        if msg == ":TRIGger:EDGe:SOURce?":
            return self.trig["SOUR"]
        if msg == ":TRIGger:EDGe:SLOPe?":
            return self.trig["SLOP"]
        if msg == ":TRIGger:EDGe:LEVel?":
            return f"{self.trig['LEV']:e}"
        if msg == ":TRIGger:COUPling?":
            return self.trig["COUP"]
        if msg == ":TRIGger:STATus?":
            if self._stale > 0:
                self._stale -= 1
                return "STOP"
            if self.status == "WAIT" and self.single_polls is not None:
                if self.single_polls == 0:
                    self.status, self.single_polls = "STOP", None  # triggered and stopped
                    self.acquiring = False
                else:
                    self.single_polls -= 1
            return self.status
        if m := re.fullmatch(r":MEASure:(\w+)\? CHANnel([1-4])", msg):
            if m[1] not in {"VPP", "VMAX", "VMIN", "VAMP", "VTOP", "VBASe", "VAVG", "VRMS", "PVRMs", "OVERshoot",
                            "PREShoot", "PERiod", "FREQuency", "PWIDth", "NWIDth", "RTIMe", "FTIMe", "PDUTy", "NDUTy"}:
                raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
            value = self.measurements.get(m[1], 9.9e37)
            return "9.9e37" if value >= 9.9e37 else f"{value:e}"
        if msg == ":WAVeform:PREamble?":
            yinc = self.channels[int(self.wav["SOUR"][-1])]["SCAL"] / 32
            yorigin = round(self.channels[int(self.wav["SOUR"][-1])]["OFFS"] / yinc)
            xinc = self.tscale / 100
            xorigin = self.toffset - 7 * self.tscale
            points = 0 if self.acquiring else len(self.codes)
            return f"0,0,{points},1,{xinc:e},{xorigin:e},0,{yinc:e},{yorigin},127"
        raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
