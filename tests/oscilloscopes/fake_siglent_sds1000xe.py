"""In-memory stand-in for a SIGLENT SDS1104X-E for the SCPI subset the driver uses.

The replies use the formats of programming guide PG01-E02D with the default CHDR SHORT.
"""

from __future__ import annotations

import re

import pyvisa

CHANNELS = ("C1", "C2", "C3", "C4")


def make_bmp(width: int, height: int) -> bytes:
    """BMP file as the SDS1104X-E sends it: RGB565 with bit fields, top-down. Pixel (x, y) is red, green or blue."""
    colors = (0xF800, 0x07E0, 0x001F)
    stride = (width * 2 + 3) & ~3
    rows = b"".join(
        b"".join(colors[(x + y) % 3].to_bytes(2, "little") for x in range(width)).ljust(stride, b"\0")
        for y in range(height)
    )
    offset = 66
    header = (
        b"BM" + (offset + len(rows)).to_bytes(4, "little") + bytes(4) + offset.to_bytes(4, "little")
        + (40).to_bytes(4, "little") + width.to_bytes(4, "little") + (-height).to_bytes(4, "little", signed=True)
        + (1).to_bytes(2, "little") + (16).to_bytes(2, "little") + (3).to_bytes(4, "little")
        + len(rows).to_bytes(4, "little") + bytes(16)
        + (0xF800).to_bytes(4, "little") + (0x07E0).to_bytes(4, "little") + (0x001F).to_bytes(4, "little")
    )
    return header + rows


class FakeSDSResource:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.chdr = "SHORT"
        self.closed = False
        self.channels = {
            ch: {"TRA": ch == "C1", "VDIV": 1.0, "OFST": 0.0, "CPL": "D1M", "ATTN": 1.0, "BWL": False}
            for ch in CHANNELS
        }
        self.tdiv = 1e-6
        self.trdl = 0.0
        self.sara = 1e9
        self.trmd = "AUTO"
        self.trse = "EDGE,SR,C1,HT,OFF"
        self.trlv = {ch: 0.0 for ch in (*CHANNELS, "EX", "EX5")}
        self.trsl = {ch: "POS" for ch in (*CHANNELS, "EX", "EX5")}
        self.trcp = {ch: "DC" for ch in (*CHANNELS, "EX", "EX5")}
        self.sast: str | None = "Trig'd"
        # Number of TRMD? queries until a single acquisition triggers. None: no trigger.
        self.trigger_after_polls: int | None = None
        self.single_polls: int | None = None
        self.pava: dict[tuple[str, str], str] = {}
        self.memory: dict[str, bytes] = {"C1": bytes(range(0, 10))}
        self.wfsu = {"SP": 0, "NP": 0, "FP": 0}
        # Replace the reply of the next WF? query (list of read_raw chunks), for error tests.
        self.raw_reply: list[bytes] | None = None
        self.chunk_size = 7
        self._pending: list[bytes] = []
        self.offset_limit = 10.0
        self.math_equation = "C1+C2"
        self.mtvd = 1.0
        self.mtvp = 0
        # Math block of the real oscilloscope: all points, also with WFSU (tested).
        self.math_memory = bytes(range(0, 10))
        self.crms = "OFF"
        self.crty = "X"
        self.crst = {"TREF": -1e-6, "TDIF": 1e-6, "VREF": 0.0, "VDIF": 0.0}
        self.screen_bmp = make_bmp(4, 2)

    @property
    def session(self) -> int:
        # As pyvisa: a closed resource has no session.
        if self.closed:
            raise pyvisa.errors.InvalidSession()
        return 1

    def close(self) -> None:
        self.closed = True

    def _reply(self, header: str, value: str, unit: str = "") -> str:
        if self.chdr == "OFF":
            return value
        return f"{header} {value}{unit}"

    def write(self, msg: str) -> None:
        self.writes.append(msg)
        if m := re.fullmatch(r"(C[1-4]):ATTN (\S+)", msg):
            self.channels[m[1]]["ATTN"] = float(m[2])
        elif m := re.fullmatch(r"(C[1-4]):CPL (D1M|A1M|GND)", msg):
            self.channels[m[1]]["CPL"] = m[2]
        elif m := re.fullmatch(r"(C[1-4]):VDIV (\S+)V", msg):
            self.channels[m[1]]["VDIV"] = float(m[2])
        elif m := re.fullmatch(r"(C[1-4]):OFST (\S+)V", msg):
            # Like the real oscilloscope: a value outside the limit is set to the limit.
            self.channels[m[1]]["OFST"] = max(-self.offset_limit, min(self.offset_limit, float(m[2])))
        elif m := re.fullmatch(r"BWL (C[1-4]),(ON|OFF)", msg):
            self.channels[m[1]]["BWL"] = m[2] == "ON"
        elif m := re.fullmatch(r"(C[1-4]):TRA (ON|OFF)", msg):
            self.channels[m[1]]["TRA"] = m[2] == "ON"
        elif m := re.fullmatch(r"TDIV (\d+)(NS|US|MS|S)", msg):
            self.tdiv = int(m[1]) * {"NS": 1e-9, "US": 1e-6, "MS": 1e-3, "S": 1.0}[m[2]]
        elif m := re.fullmatch(r"TRDL (\S+)S", msg):
            self.trdl = float(m[1])
        elif m := re.fullmatch(r"TRSE EDGE,SR,(\S+),HT,OFF", msg):
            self.trse = f"EDGE,SR,{m[1]},HT,OFF"
        elif m := re.fullmatch(r"(\S+):TRSL (POS|NEG|WINDOW)", msg):
            self.trsl[m[1]] = m[2]
        elif m := re.fullmatch(r"(\S+):TRCP (AC|DC|HFREJ|LFREJ)", msg):
            self.trcp[m[1]] = m[2]
        elif m := re.fullmatch(r"(\S+):TRLV (\S+)V", msg):
            self.trlv[m[1]] = float(m[2])
        elif msg in ("TRMD SINGLE", "ARM"):
            # Like the real oscilloscope (firmware 8.3.6.1.37R17): ARM starts a single acquisition.
            # A second TRMD SINGLE or ARM stops it, and then SAST? does not answer.
            if self.trmd == "SINGLE":
                self.trmd, self.sast = "STOP", None
            else:
                self.trmd, self.sast = "SINGLE", "Ready"
                self.single_polls = self.trigger_after_polls
        elif m := re.fullmatch(r"TRMD (AUTO|NORM)", msg):
            self.trmd, self.sast = m[1], "Trig'd"
        elif msg == "STOP":
            self.trmd, self.sast = "STOP", "Stop"
        elif m := re.fullmatch(r"WFSU SP,(\d+),NP,(\d+),FP,(\d+)", msg):
            self.wfsu = {"SP": int(m[1]), "NP": int(m[2]), "FP": int(m[3])}
        elif m := re.fullmatch(r"(C[1-4]):WF\? DAT2", msg):
            self._pending = self.raw_reply if self.raw_reply is not None else self._chunks(self._block(m[1]))
            self.raw_reply = None
        elif msg == "MATH:WF? DAT2":
            header = b"" if self.chdr == "OFF" else b"MATH:WF DAT2,"
            block = header + b"#9" + f"{len(self.math_memory):09d}".encode() + self.math_memory + b"\n\n"
            self._pending = self.raw_reply if self.raw_reply is not None else self._chunks(block)
            self.raw_reply = None
        elif msg == "SCDP":
            # Like the real oscilloscope: the BMP file and one LF.
            self._pending = self.raw_reply if self.raw_reply is not None else self._chunks(self.screen_bmp + b"\n")
            self.raw_reply = None
        elif m := re.fullmatch(r"DEF EQN,'(\S+)'", msg):
            self.math_equation = m[1]
        elif m := re.fullmatch(r"MTVD (\d+)(uV|mV|V)", msg):
            self.mtvd = int(m[1]) * {"uV": 1e-6, "mV": 1e-3, "V": 1.0}[m[2]]
        elif m := re.fullmatch(r"MTVP (-?\d+)", msg):
            self.mtvp = int(m[1])
        elif m := re.fullmatch(r"CRMS (OFF|MANUAL|TRACK)", msg):
            self.crms = m[1]
        elif m := re.fullmatch(r"CRTY (X|Y|X-Y)", msg):
            self.crty = m[1]
        elif m := re.fullmatch(r"C[1-4]:CRST TREF,(\S+)S,TDIF,(\S+)S", msg):
            self.crst["TREF"], self.crst["TDIF"] = float(m[1]), float(m[2])
        else:
            raise AssertionError(f"unexpected write {msg!r}")

    def _block(self, ch: str) -> bytes:
        data = self.memory.get(ch, b"")
        sp = max(1, self.wfsu["SP"])
        data = data[self.wfsu["FP"]::sp]
        if self.wfsu["NP"]:
            data = data[:self.wfsu["NP"]]
        header = b"" if self.chdr == "OFF" else f"{ch}:WF DAT2,".encode()
        return header + b"#9" + f"{len(data):09d}".encode() + data + b"\n\n"

    def _chunks(self, block: bytes) -> list[bytes]:
        # As pyvisa with a termination character: a read ends after each 0x0A or after chunk_size bytes.
        chunks, current = [], bytearray()
        for b in block:
            current.append(b)
            if b == 0x0A or len(current) == self.chunk_size:
                chunks.append(bytes(current))
                current = bytearray()
        if current:
            chunks.append(bytes(current))
        return chunks

    def read_raw(self, size: int | None = None) -> bytes:
        if not self._pending:
            raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
        return self._pending.pop(0)

    def query(self, msg: str) -> str:
        if msg == "*IDN?":
            return "Siglent Technologies,SDS1104X-E,SDSMMGKQ800001,8.2.6.1.37R9"
        if m := re.fullmatch(r"(C[1-4]):(TRA|VDIV|OFST|CPL|ATTN)\?", msg):
            ch, key = m[1], m[2]
            value = self.channels[ch][key]
            if key == "TRA":
                return self._reply(f"{ch}:TRA", "ON" if value else "OFF")
            if key == "CPL":
                return self._reply(f"{ch}:CPL", value)
            if key == "ATTN":
                return self._reply(f"{ch}:ATTN", f"{value:g}")
            return self._reply(f"{ch}:{key}", f"{value:.2E}", "V")
        if msg == "BWL?":
            return self._reply("BWL", ",".join(f"{ch},{'ON' if self.channels[ch]['BWL'] else 'OFF'}" for ch in CHANNELS))
        if msg == "TDIV?":
            return self._reply("TDIV", f"{self.tdiv:.2E}", "S")
        if msg == "TRDL?":
            return self._reply("TRDL", f"{self.trdl:.2E}", "S")
        if msg == "SARA?":
            return self._reply("SARA", f"{self.sara:.2E}", "Sa/s")
        if m := re.fullmatch(r"SANU\? (C[1-4])", msg):
            return self._reply("SANU", f"{len(self.memory.get(m[1], b'')):.2E}", "pts")
        if msg == "TRMD?":
            if self.trmd == "SINGLE" and self.single_polls is not None:
                if self.single_polls == 0:
                    self.trmd, self.sast, self.single_polls = "STOP", "Stop", None  # triggered
                else:
                    self.single_polls -= 1
            return self._reply("TRMD", self.trmd)
        if msg == "TRSE?":
            return self._reply("TRSE", self.trse)
        if m := re.fullmatch(r"(\S+):TRLV\?", msg):
            return self._reply(f"{m[1]}:TRLV", f"{self.trlv[m[1]]:.2E}", "V")
        if m := re.fullmatch(r"(\S+):TRSL\?", msg):
            return self._reply(f"{m[1]}:TRSL", self.trsl[m[1]])
        if m := re.fullmatch(r"(\S+):TRCP\?", msg):
            return self._reply(f"{m[1]}:TRCP", self.trcp[m[1]])
        if msg == "SAST?":
            if self.sast is None:
                raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
            return self._reply("SAST", self.sast)
        if msg == "DEF?":
            return self._reply("DEF", f"EQN,'{self.math_equation}'")
        if msg == "MTVD?":
            return self._reply("MTVD", f"{self.mtvd:.2E}")  # the real oscilloscope sends no unit
        if msg == "MTVP?":
            return self._reply("MTVP", str(self.mtvp))
        if msg == "CRMS?":
            return self._reply("CRMS", self.crms)
        if msg == "CRTY?":
            return self._reply("CRTY", self.crty)
        if m := re.fullmatch(r"(C[1-4]):CRST\? TREF,TDIF", msg):
            return self._reply(f"{m[1]}:CRST", f"TREF,{self.crst['TREF']:.2E}S,TDIF,{self.crst['TDIF']:.2E}S")
        if m := re.fullmatch(r"(C[1-4]):CRVA\? HREL", msg):
            delta = abs(self.crst["TDIF"] - self.crst["TREF"])
            # Like the real oscilloscope: X2 before X1, not as in the programming guide.
            return self._reply(f"{m[1]}:CRVA", f"HREL,{delta:.3E}s,{1 / delta:.4E}Hz,"
                               f"{self.crst['TDIF']:.2E}s,{self.crst['TREF']:.2E}s")
        if m := re.fullmatch(r"(C[1-4]):PAVA\? (\w+)", msg):
            return self._reply(f"{m[1]}:PAVA", f"{m[2]},{self.pava.get((m[1], m[2]), '****')}")
        raise AssertionError(f"unexpected query {msg!r}")
