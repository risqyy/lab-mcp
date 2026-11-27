"""In-memory stand-in for a PeakTech 4365 multimeter at the transport level.

The replies are recorded from a real PeakTech 4365.
"""

from __future__ import annotations

from collections.abc import Iterable

REPLIES: dict[str, bytes] = {
    "temperature": b"TE 0022.6   C\r             \r   0072.6   F\r             \r",
    "signal_output": b"FR 00.010 kHz\r             \r   00.216   V\r             \r",
    "diode_open": b"DI   OL.   mV\r             \r     Open    \r             \r",
    "diode_bad": b"DI 0000.1  mV\r             \r     bAd     \r             \r",
    "resistance_overload": b"OH  .OL  kOhm\r    .OL      \r   0.0000    \r   0.0000    \r",
    "dc_mv": b"DC 009.16  mV\r   097.57    \r     O.L     \r   000.00    \r",
    "dc_v_negative": b"DC-0012.5   V\r   0012.4    \r   0012.4    \r   0012.3    \r",
    "ac_v": b"AC 007.30   V\r             \rFR 00.147 kHz\rdB-008.53 dBm\r",
    "ac_v_range_change": b"AC 000.32   V\r             \rFR 00000  kHz\rdB 00000  dBm\r",
    "ac_v_level_overload": b"AC 0000.0   V\r             \rFR 00.001 kHz\rdB-  O.L  dBm\r",
    "ac_ma": b"AC 0.1146  mA\r             \rFR 00.000 kHz\r             \r",
    "dc_a": b"DC 00.003   A\r   00.000    \r   00.000    \r   00.000    \r",
    "hfe": b"HF 0000.1    \r   0000.0    \r   0000.0    \r   0000.0    \r",
    "logic": b"LO    rdy    \r             \rFR 00.000 kHz\r  -00.001    \r",
    "capacitance_measuring": b"CA 00000   nF\r   00000     \r   00000     \r   00000     \r",
    "capacitance_overload": b"CA   O.L   uF\r     O.L     \r     O.L     \r   00000     \r",
    "frequency": b"FR 00.000 kHz\r             \rAC 06.572   V\rdB 018.58 dBm\r",
    "frequency_mhz": b"FR 01.234 MHz\r             \rAC 00.176   V\rdB-012.87 dBm\r",
}


def lines(reply: bytes) -> list[str]:
    text = reply.decode("ascii")
    return [text[i : i + 13] for i in range(0, 56, 14)]


class FakePTTransport:
    """Returns the recorded replies in sequence. The last reply repeats."""

    def __init__(self, replies: Iterable[str] = ("dc_mv",)) -> None:
        self.replies = list(replies)
        self.queries = 0
        self.closed = False

    def query(self) -> list[str]:
        name = self.replies[min(self.queries, len(self.replies) - 1)]
        self.queries += 1
        return lines(REPLIES[name])

    def close(self) -> None:
        self.closed = True
