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
    "diode_good": b"DI 0617.5  mV\r             \r     good    \r             \r",  # 1N4004
    "diode_short": b"DI 0000.0  mV\r             \r     Shrt    \r             \r",
    "diode_number": b"DI 0000.0  mV\r             \r   0000.0    \r             \r",
    "resistance_shunt": b"OH 000.10 Ohm\r   000.10    \r   000.10    \r   000.10    \r",  # 0.1 Ohm shunt
    "dc_v_10v": b"DC 09.988   V\r   09.988    \r   09.987    \r   09.988    \r",
    "dc_a_2a": b"DC 01.998   A\r   01.998    \r   01.999    \r   01.998    \r",
    "resistance_overload": b"OH  .OL  kOhm\r    .OL      \r   0.0000    \r   0.0000    \r",
    "resistance_ohm_overload": b"OH   O.L  Ohm\r     O.L     \r   000.00    \r   000.00    \r",
    "signal_output_overload": b"FR 00.010 kHz\r             \r     .OL    V\r             \r",
    "dc_mv": b"DC 009.16  mV\r   097.57    \r     O.L     \r   000.00    \r",
    "dc_v_negative": b"DC-0012.5   V\r   0012.4    \r   0012.4    \r   0012.3    \r",
    "ac_v": b"AC 007.30   V\r             \rFR 00.147 kHz\rdB-008.53 dBm\r",
    "ac_v_range_change": b"AC 000.32   V\r             \rFR 00000  kHz\rdB 00000  dBm\r",
    "ac_v_level_overload": b"AC 0000.0   V\r             \rFR 00.001 kHz\rdB-  O.L  dBm\r",
    "ac_ma": b"AC 0.1146  mA\r             \rFR 00.000 kHz\r             \r",
    "dc_a": b"DC 00.003   A\r   00.000    \r   00.000    \r   00.000    \r",
    "hfe": b"HF 0000.1    \r   0000.0    \r   0000.0    \r   0000.0    \r",
    "logic": b"LO    rdy    \r             \rFR 00.000 kHz\r  -00.001    \r",
    "logic_square": b"LO    rdy    \r             \rFR 01.200 kHz\r   02.463    \r",  # 5 V square wave
    "capacitance_100nf": b"CA 100.81  nF\r   100.81    \r   100.80    \r   100.85    \r",
    "capacitance_10uf": b"CA 09.178  uF\r   09.178    \r   09.176    \r   09.178    \r",
    "resistance_10k": b"OH 09.954kOhm\r   09.953    \r   09.954    \r   09.953    \r",
    "resistance_1m": b"OH 01.008MOhm\r   01.008    \r   01.008    \r   01.009    \r",
    "hfe_bc547": b"HF 0255.9    \r   0255.9    \r   0255.8    \r   0255.8    \r",
    "dc_ma": b"DC 0.4725  mA\r   0.4725    \r   0.4725    \r   0.4725    \r",
    "ac_ma_square": b"AC 0.2558  mA\r             \rFR 00.150 kHz\r             \r",
    "frequency_square": b"FR 01.201 kHz\r             \rAC 02.579   V\rdB 010.45 dBm\r",
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
