import pytest

from lab_mcp.instruments.oscilloscopes import IncompleteTransferError, RigolDS4000

from fake_rigol_ds4000 import FakeDS4000Resource


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
def res() -> FakeDS4000Resource:
    return FakeDS4000Resource()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def dev(res: FakeDS4000Resource, clock: Clock) -> RigolDS4000:
    return RigolDS4000(res, sleep=clock.sleep, clock=clock)


def test_unsupported_model(res: FakeDS4000Resource) -> None:
    with pytest.raises(ValueError, match="Unsupported model 'DS1054Z'"):
        RigolDS4000(res, "DS1054Z")


def test_status(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.channels[2]["IMPE"] = "FIFT"
    res.channels[2]["COUP"] = "AC"
    res.channels[3]["BWLI"] = "20M"
    st = dev.status()
    assert st.acquisition == "auto"
    assert (st.timebase.seconds_per_div, st.timebase.horizontal_divisions, st.timebase.sample_rate_sa_s) == (2e-4, 14, 1e8)
    assert (st.trigger.sweep, st.trigger.type, st.trigger.source, st.trigger.slope, st.trigger.level_v, st.trigger.coupling) == (
        "auto", "EDGE", "C1", "rising", 1.5, "dc"
    )
    c1, c2, c3, _ = st.channels
    assert (c1.channel, c1.displayed, c1.volts_per_div, c1.probe_attenuation, c1.coupling) == ("C1", True, 1.0, 10.0, "dc")
    assert c2.coupling == "ac_50_ohm"
    assert c3.bandwidth_limit is True and c1.bandwidth_limit is False


@pytest.mark.parametrize(("reply", "state"), [("TD", "triggered"), ("WAIT", "armed"), ("RUN", "ready"), ("AUTO", "auto"), ("STOP", "stopped")])
def test_acquisition_states(dev: RigolDS4000, res: FakeDS4000Resource, reply: str, state: str) -> None:
    res.status = reply
    assert dev.acquisition_state() == state


def test_trigger_delay_has_the_sign_of_the_siglent_driver(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    # :TIMebase:MAIN:OFFSet is the time of the screen center after the trigger.
    res.toffset = 4.2e-8
    assert dev.timebase().trigger_delay_s == pytest.approx(-4.2e-8)
    dev.configure_timebase(trigger_delay_s=1e-6)
    assert res.writes[-1] == ":TIMebase:MAIN:OFFSet -1.000000E-06"


def test_configure_timebase(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    tb = dev.configure_timebase(seconds_per_div=5e-7)
    assert res.writes == [":TIMebase:MAIN:SCALe 5.000000E-07"]
    assert tb.seconds_per_div == 5e-7


@pytest.mark.parametrize("value", [2e-9, 3e-6, 2000.0, 0.0, float("nan")])
def test_configure_timebase_refuses_invalid_values(dev: RigolDS4000, res: FakeDS4000Resource, value: float) -> None:
    with pytest.raises(ValueError, match="1-2-5 sequence from 5e-09 to 1000"):
        dev.configure_timebase(seconds_per_div=value)
    assert res.writes == []


def test_configure_channel(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    cfg = dev.configure_channel("C2", displayed=True, volts_per_div=0.5, offset_v=-1.0, coupling="ac",
                                probe_attenuation=1, bandwidth_limit=True)
    assert res.writes == [
        ":CHANnel2:PROBe 1", ":CHANnel2:DISPlay ON", ":CHANnel2:COUPling AC", ":CHANnel2:BWLimit 20M",
        ":CHANnel2:SCALe 5.000000E-01", ":CHANnel2:OFFSet -1.000000E+00",
    ]
    assert (cfg.displayed, cfg.volts_per_div, cfg.offset_v, cfg.coupling, cfg.probe_attenuation, cfg.bandwidth_limit) == (
        True, 0.5, -1.0, "ac", 1.0, True
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"volts_per_div": 60.0}, "from 0.01 to 50 V/div for probe 10X"),  # 10X probe: 5 V/div * 10
        ({"volts_per_div": 0.005}, "from 0.01 to 50 V/div"),
        ({"probe_attenuation": 3}, "probe_attenuation must be one of"),
        ({"coupling": "dc_50_ohm"}, "coupling must be one of"),
        ({"offset_v": float("inf")}, "finite"),
    ],
)
def test_configure_channel_refuses_invalid_values(dev: RigolDS4000, res: FakeDS4000Resource, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        dev.configure_channel("C1", **kwargs)
    assert res.writes == []


def test_volts_per_div_limit_at_50_ohm(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.channels[1].update(IMPE="FIFT", PROB=1.0)
    with pytest.raises(ValueError, match="from 0.001 to 1 V/div for probe 1X"):
        dev.configure_channel("C1", volts_per_div=2.0)


def test_configure_trigger(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.trig["MODE"] = "PULS"
    tr = dev.configure_trigger(source="C3", slope="falling", level_v=0.25, coupling="lf_reject", sweep="normal")
    assert res.writes == [
        ":TRIGger:MODE EDGE", ":TRIGger:EDGe:SOURce CHANnel3", ":TRIGger:EDGe:SLOPe NEGative",
        ":TRIGger:COUPling LFReject", ":TRIGger:EDGe:LEVel 2.500000E-01", ":TRIGger:SWEep NORMal",
    ]
    assert (tr.type, tr.source, tr.slope, tr.level_v, tr.coupling, tr.sweep) == ("EDGE", "C3", "falling", 0.25, "lf_reject", "normal")


def test_configure_trigger_sends_only_changes(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    dev.configure_trigger(sweep="auto")
    assert res.writes == []


def test_line_trigger_has_no_level(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    tr = dev.configure_trigger(source="LINE")
    assert (tr.source, tr.level_v, tr.slope, tr.coupling) == ("LINE", None, None, None)
    with pytest.raises(ValueError, match="LINE source has no level"):
        dev.configure_trigger(level_v=1.0)


def test_external_trigger_sources(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    assert dev.configure_trigger(source="EX5").source == "EX5"
    assert res.writes[-1] == ":TRIGger:EDGe:SOURce EXT5"


def test_run_stop(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    assert dev.stop() == "stopped"
    assert dev.run() == "auto"
    assert res.writes == [":STOP", ":RUN"]
    assert dev.run("normal") == "armed"
    assert res.writes[-2:] == [":TRIGger:SWEep NORMal", ":RUN"]


def test_stop_waits_for_the_stop_status(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    polls = iter(["AUTO", "AUTO", "STOP"])

    def query(msg: str, original=res.query) -> str:
        if msg == ":TRIGger:STATus?":
            return next(polls)
        return original(msg)

    res.query = query  # type: ignore[method-assign]
    assert dev.stop() == "stopped"
    assert clock.sleeps == [0.1, 0.1]


def test_run_waits_until_the_status_is_not_stop(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    polls = iter(["STOP", "STOP", "TD"])

    def query(msg: str, original=res.query) -> str:
        if msg == ":TRIGger:STATus?":
            return next(polls)
        return original(msg)

    res.query = query  # type: ignore[method-assign]
    assert dev.run() == "triggered"
    assert clock.sleeps == [0.1, 0.1]


def test_run_after_single_uses_the_last_continuous_sweep(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.trig["SWE"] = "NORM"
    dev.single(0)
    assert dev.run() == "armed"
    assert res.writes[-2:] == [":TRIGger:SWEep NORMal", ":RUN"]


def test_run_without_known_sweep(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.trig["SWE"] = "SING"
    with pytest.raises(ValueError, match="previous sweep is not known"):
        dev.run()


def test_single_waits_for_the_trigger(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    res.single_polls = 3
    assert dev.single(timeout_s=5) == "stopped"
    assert ":SINGle" in res.writes
    assert clock.sleeps == [0.1, 0.1, 0.1]


def test_single_ignores_the_previous_stop(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    # The oscilloscope can send the previous STOP directly after :SINGle.
    res.stale_polls, res.single_polls = 2, 2
    assert dev.single(timeout_s=5) == "stopped"
    assert res.acquiring is False
    assert res.writes[:4] == [":WAVeform:SOURce CHANnel1", ":WAVeform:MODE NORMal", ":STOP", ":SINGle"]


def test_single_from_stop_does_not_send_stop(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.status, res.single_polls = "STOP", 0
    assert dev.single(timeout_s=5) == "stopped"
    assert ":STOP" not in res.writes


def test_single_repeats_an_acquisition_without_data(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    res.empty_singles, res.single_polls = 1, 2
    assert dev.single(timeout_s=5) == "stopped"
    assert res.writes.count(":SINGle") == 2
    assert res.acquiring is False


def test_single_repeats_only_once(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    res.empty_singles = 5
    assert dev.single(timeout_s=3) == "armed"
    assert res.writes.count(":SINGle") == 2


def test_single_accepts_stop_after_the_stale_time(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    # A fast acquisition: the driver does not see the oscilloscope busy.
    res.status = "STOP"
    monkey_single = res.write

    def write(msg: str) -> None:
        monkey_single(msg)
        if msg == ":SINGle":
            res.status, res.acquiring = "STOP", False

    res.write = write  # type: ignore[method-assign]
    assert dev.single(timeout_s=5) == "stopped"
    assert clock.now == pytest.approx(0.6, abs=0.11)


def test_single_with_timeout_0_does_not_report_the_previous_stop(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.stale_polls = 5
    assert dev.single(0) == "armed"


def test_single_timeout(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    res.single_polls = 1000
    assert dev.single(timeout_s=0.5) == "armed"
    assert clock.now == pytest.approx(0.5, abs=0.11)


def test_measure(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    r = dev.measure("C1", "pkpk")
    assert (r.value, r.unit, r.valid) == (3.0, "V", True)
    assert res.writes == []
    r = dev.measure("C1", "rise")  # not in the fake: 9.9e37
    assert (r.value, r.valid, r.unit) == (None, False, "s")
    assert dev.measure("C1", "duty").unit == "%"


def test_ratio_measurements_in_percent(dev: RigolDS4000) -> None:
    # The oscilloscope sends 0.5 for a duty cycle of 50 %.
    assert dev.measure("C1", "duty").value == pytest.approx(50.0)
    assert dev.measure("C1", "ovsp").value == pytest.approx(2.1)


@pytest.mark.parametrize("parameter", ["cmean", "stdev", "vstd", "ovsn", "fpre", "wid"])
def test_measure_without_ds4000_command(dev: RigolDS4000, parameter: str) -> None:
    # The oscilloscope does not reply to an unknown query. Thus the driver must refuse it before it sends it.
    with pytest.raises(ValueError, match=f"has no measurement '{parameter}'"):
        dev.measure("C1", parameter)  # type: ignore[arg-type]


def test_waveform(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.toffset = 1e-4
    w = dev.waveform("C1", max_points=1400)
    assert res.writes[:3] == [":WAVeform:SOURce CHANnel1", ":WAVeform:MODE NORMal", ":WAVeform:FORMat BYTE"]
    assert (w.sample_count, w.memory_points, w.sparsing) == (1400, 1400, 1)
    assert w.first_sample_time_s == pytest.approx(1e-4 - 7 * 2e-4)
    assert w.sample_interval_s == pytest.approx(2e-6)
    assert w.voltages_v[0] == pytest.approx(0.0)
    assert w.voltages_v[-1] == pytest.approx(3.0)  # 96 codes * 1 V / 32
    assert w.off_screen_samples == 0


def test_waveform_with_offset_and_sparsing(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.channels[1]["OFFS"] = 1.0  # YORigin = 32
    res.codes = bytes([127 + 32] * 1400)  # the trace at the screen center plus offset: 0 V
    w = dev.waveform("C1", max_points=100)
    assert (w.sample_count, w.sparsing) == (100, 14)
    assert w.sample_interval_s == pytest.approx(14 * 2e-6)
    assert w.voltages_v == pytest.approx([0.0] * 100)


def test_waveform_waits_for_a_completed_acquisition(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    res.acquiring = True
    with pytest.raises(ValueError, match="No completed acquisition of C1"):
        dev.waveform("C1")
    assert clock.now == pytest.approx(2.0, abs=0.11)
    assert ":WAVeform:DATA?" not in res.writes


def test_waveform_counts_clipped_samples(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.codes = bytes([0, 255, 128, 255])
    assert dev.waveform("C1").off_screen_samples == 3


def test_waveform_of_a_channel_that_is_off(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    with pytest.raises(ValueError, match="Channel C2 is off"):
        dev.waveform("C2")


def test_incomplete_block(dev: RigolDS4000, res: FakeDS4000Resource, monkeypatch) -> None:
    monkeypatch.setattr(res, "_block", lambda data: b"#9000001400" + data[:10])
    with pytest.raises(Exception):
        dev.waveform("C1")


def test_wrong_block_end(dev: RigolDS4000, res: FakeDS4000Resource, monkeypatch) -> None:
    monkeypatch.setattr(res, "_block", lambda data: b"#9" + f"{len(data):09d}".encode() + data + b"XX")
    with pytest.raises(IncompleteTransferError, match="Unexpected end"):
        dev.waveform("C1")


def test_screenshot(dev: RigolDS4000, res: FakeDS4000Resource, clock: Clock) -> None:
    dev.configure_timebase(seconds_per_div=1e-3)  # a change: the driver waits before the screenshot
    png = dev.screenshot_png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert res.writes[-1] == ":DISPlay:DATA?"
    assert clock.sleeps == [pytest.approx(1.0)]


def test_screenshot_that_is_not_a_bmp(dev: RigolDS4000, res: FakeDS4000Resource) -> None:
    res.screen = b"GIF89a" + bytes(100)
    with pytest.raises(IncompleteTransferError, match="not a BMP file"):
        dev.screenshot_png()


@pytest.mark.parametrize(
    "call",
    [
        lambda d: d.math(), lambda d: d.configure_math("add", "C1", "C2"), lambda d: d.math_waveform(),
        lambda d: d.time_cursors("C1"), lambda d: d.set_time_cursors("C1", 0, 1e-3), lambda d: d.cursors_off(),
    ],
)
def test_math_and_cursors_are_not_supported(dev: RigolDS4000, res: FakeDS4000Resource, call) -> None:
    with pytest.raises(ValueError, match="not supported for the RIGOL DS4014"):
        call(dev)
    assert res.writes == []
