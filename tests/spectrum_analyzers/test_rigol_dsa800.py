import pytest

from lab_mcp.instruments.spectrum_analyzers import RigolDSA800
from lab_mcp.instruments.spectrum_analyzers.rigol_dsa800 import IncompleteTransferError

from fake_rigol_dsa800 import FakeDSA800Resource


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def res() -> FakeDSA800Resource:
    return FakeDSA800Resource()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def dev(res: FakeDSA800Resource, clock: Clock) -> RigolDSA800:
    return RigolDSA800(res, sleep=clock.sleep, clock=clock)


def test_unsupported_model(res: FakeDSA800Resource) -> None:
    with pytest.raises(ValueError, match="Unsupported model 'DSA815'"):
        RigolDSA800(res, "DSA815")


def test_status(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.detector, res.preamp = "QPEAK", True
    st = dev.status()
    assert (st.sweep, st.sweep_count) == ("continuous", 1)
    assert (st.frequency.center_hz, st.frequency.span_hz, st.frequency.start_hz, st.frequency.stop_hz) == (1.6e9, 3.2e9, 0, 3.2e9)
    assert (st.bandwidth.rbw_hz, st.bandwidth.rbw_auto, st.bandwidth.sweep_points, st.bandwidth.detector) == (1e6, True, 601, "quasi_peak")
    assert (st.amplitude.reference_level, st.amplitude.unit, st.amplitude.scale_type, st.amplitude.preamplifier) == (0.0, "dBm", "log", True)
    assert [(t.trace, t.mode) for t in st.traces] == [(1, "clear_write"), (2, "blank"), (3, "blank")]
    assert res.writes == []


def test_configure_center_and_span(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    f = dev.configure_frequency(center_hz=100e6, span_hz=10e6)
    assert res.writes == [":SENSe:FREQuency:CENTer 100000000", ":SENSe:FREQuency:SPAN 10000000"]
    assert (f.start_hz, f.stop_hz) == (95e6, 105e6)


def test_configure_start_and_stop_keeps_start_below_stop(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.center, res.span = 50e6, 20e6  # 40 MHz to 60 MHz
    f = dev.configure_frequency(start_hz=100e6, stop_hz=200e6)
    assert res.writes == [":SENSe:FREQuency:STOP 200000000", ":SENSe:FREQuency:STARt 100000000"]
    assert (f.start_hz, f.stop_hz) == (100e6, 200e6)


def test_zero_span_and_full_span(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    assert dev.configure_frequency(center_hz=100e6, span_hz=0).span_hz == 0
    assert dev.configure_frequency(full_span=True).span_hz == 3.2e9
    assert res.writes[-1] == ":SENSe:FREQuency:SPAN:FULL"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"center_hz": 4e9}, "center_hz must be from 0 to 3.2e"),
        ({"span_hz": 50}, "span_hz must be from 100"),
        ({"center_hz": 1e6, "span_hz": 10e6}, "outside 0 to"),
        ({"center_hz": 1e6, "start_hz": 0}, "not both pairs"),
        ({"start_hz": 1e6, "stop_hz": 1e6 + 50}, "at least 100 Hz above"),
        ({"full_span": True, "span_hz": 1e6}, "without other"),
        ({"stop_hz": float("nan")}, "stop_hz must be"),
    ],
)
def test_configure_frequency_refuses_invalid_values(dev: RigolDSA800, res: FakeDSA800Resource, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        dev.configure_frequency(**kwargs)
    assert res.writes == []


def test_configure_bandwidth(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    bw = dev.configure_bandwidth(rbw_hz=3000, vbw_hz=300, sweep_time_s=0.5, sweep_points=1001, detector="rms")
    assert res.writes == [
        ":SENSe:DETector:FUNCtion RMS", ":SENSe:BANDwidth:RESolution 3000", ":SENSe:BANDwidth:VIDeo 300",
        ":SENSe:SWEep:TIME 5.000000E-01", ":SENSe:SWEep:POINts 1001",
    ]
    assert (bw.rbw_hz, bw.rbw_auto, bw.vbw_hz, bw.sweep_time_s, bw.sweep_time_auto, bw.sweep_points, bw.detector) == (
        3000, False, 300, 0.5, False, 1001, "rms"
    )


def test_configure_bandwidth_auto(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    dev.configure_bandwidth(rbw_auto=True, vbw_auto=False, sweep_time_auto=True)
    assert res.writes == [
        ":SENSe:BANDwidth:RESolution:AUTO ON", ":SENSe:BANDwidth:VIDeo:AUTO OFF", ":SENSe:SWEep:TIME:AUTO ON",
    ]


def test_quasi_peak_rbw(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    bw = dev.configure_bandwidth(detector="quasi_peak", rbw_hz=9000)
    assert (bw.rbw_hz, bw.emi_filter) == (9000, True)
    with pytest.raises(ValueError, match="with the EMI filter"):
        dev.configure_bandwidth(rbw_hz=10000)
    with pytest.raises(ValueError, match="needs the EMI filter"):
        dev.configure_bandwidth(emi_filter=False)


def test_emi_filter_stays_on_after_the_detector_change(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.detector, res.emi = "QPEAK", True
    assert dev.configure_bandwidth(detector="positive_peak").emi_filter is True
    with pytest.raises(ValueError, match="with the EMI filter"):
        dev.configure_bandwidth(rbw_hz=10000)
    bw = dev.configure_bandwidth(emi_filter=False, rbw_hz=10000)
    assert (bw.emi_filter, bw.rbw_hz) == (False, 10000)
    assert res.writes[-2:] == [":SENSe:BANDwidth:EMIFilter:STATe OFF", ":SENSe:BANDwidth:RESolution 10000"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"rbw_hz": 2000}, "1-3-10 sequence"),
        ({"rbw_hz": 3e6}, "rbw_hz must be one of"),
        ({"rbw_hz": 9000}, "rbw_hz must be one of"),
        ({"vbw_hz": 5}, "vbw_hz must be one of"),
        ({"rbw_hz": 1000, "rbw_auto": True}, "not both"),
        ({"sweep_time_s": 1e-6}, "sweep_time_s must be from 2e-05"),
        ({"sweep_points": 100}, "sweep_points must be from 101 to 3001"),
        ({"detector": "peak"}, "detector must be one of"),
    ],
)
def test_configure_bandwidth_refuses_invalid_values(dev: RigolDSA800, res: FakeDSA800Resource, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        dev.configure_bandwidth(**kwargs)
    assert res.writes == []


def test_configure_amplitude(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    amp = dev.configure_amplitude(reference_level_dbm=-20, scale_db_per_div=5, attenuation_db=20, preamplifier=False)
    assert res.writes == [
        ":SENSe:POWer:RF:ATTenuation 20", ":SENSe:POWer:RF:GAIN:STATe OFF",
        ":DISPlay:WINdow:TRACe:Y:SCALe:RLEVel -2.000000E+01", ":DISPlay:WINdow:TRACe:Y:SCALe:PDIVision 5.000000E+00",
    ]
    assert (amp.reference_level, amp.scale_db_per_div, amp.attenuation_db, amp.attenuation_auto) == (-20, 5, 20, False)


@pytest.mark.parametrize(
    ("setup", "kwargs", "message"),
    [
        ({}, {"reference_level_dbm": 25}, "reference_level_dbm must be from -100 to 20"),
        ({"unit": "DBUV"}, {"reference_level_dbm": 0}, "The amplitude unit is dBuV"),
        ({"spacing": "LIN"}, {"scale_db_per_div": 5}, "The scale is linear"),
        ({}, {"scale_db_per_div": 0.05}, "scale_db_per_div must be from 0.1 to 20"),
        ({}, {"attenuation_db": 31}, "attenuation_db must be from 0 to 30"),
        ({}, {"attenuation_db": 10, "attenuation_auto": True}, "not both"),
    ],
)
def test_configure_amplitude_refuses_invalid_values(dev: RigolDSA800, res: FakeDSA800Resource, setup, kwargs, message) -> None:
    for key, value in setup.items():
        setattr(res, key, value)
    with pytest.raises(ValueError, match=message):
        dev.configure_amplitude(**kwargs)
    assert res.writes == []


def test_configure_trace(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    assert dev.configure_trace(2, "max_hold").mode == "max_hold"
    assert res.writes == [":TRACe2:MODE MAXHold"]
    with pytest.raises(ValueError, match="trace must be one of"):
        dev.configure_trace(4, "max_hold")


def test_run(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.continuous = False
    assert dev.run().sweep == "continuous"
    assert res.continuous


def test_single_waits_for_the_end_of_the_sweep(dev: RigolDSA800, res: FakeDSA800Resource, clock: Clock) -> None:
    res.single_polls = 3
    r = dev.single(timeout_s=5)
    assert (r.sweep, r.complete, r.sweeps_done, r.sweep_count) == ("single", True, 1, 1)
    assert res.writes == [":INITiate:CONTinuous OFF", ":INITiate:IMMediate"]
    assert clock.sleeps == [0.1, 0.1, 0.1]


def test_single_timeout(dev: RigolDSA800, res: FakeDSA800Resource, clock: Clock) -> None:
    res.single_polls = None
    r = dev.single(timeout_s=0.5)
    assert (r.complete, r.sweeps_done) == (False, 0)
    assert clock.now == pytest.approx(0.5, abs=0.11)


def test_trace(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.center, res.span = 100e6, 6e6  # 10 kHz between points
    t = dev.trace(1)
    assert (t.mode, t.unit, t.x_unit, t.trace_points, t.decimation) == ("clear_write", "dBm", "Hz", 601, 1)
    assert t.x[0] == 97e6 and t.x[-1] == pytest.approx(103e6)
    assert t.amplitudes[300] == -20.0 and t.x[300] == pytest.approx(100e6)
    assert res.writes == [":TRACe:DATA? TRACE1"]


def test_trace_decimation_keeps_the_peaks(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.center, res.span = 100e6, 6e6
    t = dev.trace(1, max_points=60)
    assert (len(t.amplitudes), t.decimation) == (55, 11)  # 601 points: ceil(601 / 60) = 11
    assert sorted(t.amplitudes)[-2:] == [-40.0, -20.0]
    assert t.x[t.amplitudes.index(-20.0)] == pytest.approx(100e6)


def test_trace_in_zero_span_has_a_time_axis(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.span, res.sweep_time = 0, 0.6
    t = dev.trace(1)
    assert t.x_unit == "s" and t.x[-1] == pytest.approx(0.6)


def test_trace_sets_the_ascii_format(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.format = "REAL,32"
    dev.trace(1)
    assert res.writes[0] == ":FORMat:TRACe:DATA ASCii"


def test_blank_trace_is_refused(dev: RigolDSA800) -> None:
    with pytest.raises(ValueError, match="Trace 2 is off"):
        dev.trace(2)


def test_marker_peak(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.center, res.span = 100e6, 6e6
    m = dev.marker_peak(1)
    assert res.writes == [":CALCulate:MARKer1:STATe ON", ":CALCulate:MARKer1:MAXimum:MAX"]
    assert (m.x, m.x_unit, m.amplitude, m.unit) == (100e6, "Hz", -20.0, "dBm")
    m = dev.marker_peak(1, "next")
    assert (m.x, m.amplitude) == (98e6, -40.0)
    assert res.writes[-1] == ":CALCulate:MARKer1:MAXimum:NEXT"


def test_marker_peak_sets_the_normal_type(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.markers[2].update(on=True, mode="DELT")
    dev.marker_peak(2)
    assert res.writes[0] == ":CALCulate:MARKer2:MODE POSition"


def test_set_marker(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.center, res.span = 100e6, 6e6
    m = dev.set_marker(3, 98e6)
    assert res.writes[-1] == ":CALCulate:MARKer3:X 98000000"
    assert (m.marker, m.x, m.amplitude) == (3, 98e6, -40.0)
    with pytest.raises(ValueError, match="x must be from 9.7e"):
        dev.set_marker(3, 200e6)


def test_markers_off(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    dev.marker_peak(1)
    dev.markers_off()
    assert res.writes[-1] == ":CALCulate:MARKer:AOFF"
    assert not res.markers[1]["on"]


def test_screenshot(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    assert dev.screenshot_png().startswith(b"\x89PNG\r\n\x1a\n")
    assert res.writes == [":PRIVate:SNAP? BMP"]


def test_screenshot_that_is_not_a_bmp(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.screen = b"GIF89a" + bytes(100)
    with pytest.raises(IncompleteTransferError, match="not a BMP file"):
        dev.screenshot_png()


def test_wrong_block_end(dev: RigolDSA800, res: FakeDSA800Resource, monkeypatch) -> None:
    monkeypatch.setattr(res, "_block", lambda data: b"#9" + f"{len(data):09d}".encode() + data + b"XX")
    with pytest.raises(IncompleteTransferError, match="Unexpected end"):
        dev.trace(1)


def test_unexpected_reply(dev: RigolDSA800, res: FakeDSA800Resource) -> None:
    res.unit = "XYZ"
    with pytest.raises(ValueError, match="Unexpected reply to :UNIT:POWer"):
        dev.amplitude()
