import pytest

from lab_mcp.instruments.oscilloscopes import IncompleteTransferError, SiglentSDS1000XE

from fake_siglent_sds1000xe import FakeSDSResource


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def res() -> FakeSDSResource:
    return FakeSDSResource()


@pytest.fixture
def dev(res: FakeSDSResource) -> SiglentSDS1000XE:
    clock = Clock()
    return SiglentSDS1000XE(res, sleep=clock.sleep, clock=clock)


def test_status_reads_without_writes(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    status = dev.status()
    assert status.acquisition == "triggered"
    assert (status.timebase.seconds_per_div, status.timebase.horizontal_divisions) == (1e-6, 14)
    assert (status.trigger.type, status.trigger.source, status.trigger.slope, status.trigger.sweep) == (
        "EDGE", "C1", "rising", "auto",
    )
    assert [c.displayed for c in status.channels] == [True, False, False, False]
    assert res.writes == []


@pytest.mark.parametrize("chdr", ["SHORT", "OFF"])
def test_reads_replies_with_and_without_header(res: FakeSDSResource, dev: SiglentSDS1000XE, chdr: str) -> None:
    res.chdr = chdr
    res.channels["C2"].update(VDIV=0.05, OFST=-0.2, ATTN=10.0, CPL="A1M", BWL=True)
    cfg = dev.channel("C2")
    assert (cfg.volts_per_div, cfg.offset_v, cfg.probe_attenuation, cfg.coupling, cfg.bandwidth_limit) == (
        0.05, -0.2, 10.0, "ac", True,
    )


def test_configure_channel(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    cfg = dev.configure_channel(
        "C2", displayed=True, probe_attenuation=10, volts_per_div=2.0, offset_v=-1.5,
        coupling="dc", bandwidth_limit=True,
    )
    assert res.writes[0] == "C2:ATTN 10"  # before the scale, because the attenuation changes the scale
    assert (cfg.displayed, cfg.volts_per_div, cfg.offset_v, cfg.bandwidth_limit) == (True, 2.0, -1.5, True)


def test_configure_channel_reports_limited_offset(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.offset_limit = 2.0
    assert dev.configure_channel("C1", offset_v=50).offset_v == 2.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"volts_per_div": 20.0}, "from 0.0005 V to 10 V"),
        ({"volts_per_div": 1e-4}, "from 0.0005 V to 10 V"),
        ({"probe_attenuation": 3}, "probe_attenuation must be one of"),
        ({"coupling": "d50"}, "coupling must be one of"),
        ({"offset_v": float("nan")}, "finite"),
    ],
)
def test_configure_channel_refuses_invalid_values(dev: SiglentSDS1000XE, res: FakeSDSResource, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        dev.configure_channel("C1", **kwargs)
    assert res.writes == []


def test_scale_limit_includes_probe_attenuation(dev: SiglentSDS1000XE) -> None:
    assert dev.configure_channel("C1", probe_attenuation=10, volts_per_div=100).volts_per_div == 100


def test_unknown_channel_is_refused(dev: SiglentSDS1000XE) -> None:
    with pytest.raises(ValueError, match="channel must be one of"):
        dev.channel("C5")  # type: ignore[arg-type]


@pytest.mark.parametrize(("seconds", "token"), [(1e-9, "1NS"), (5e-4, "500US"), (0.02, "20MS"), (100, "100S")])
def test_configure_timebase(dev: SiglentSDS1000XE, res: FakeSDSResource, seconds: float, token: str) -> None:
    tb = dev.configure_timebase(seconds, trigger_delay_s=-2e-6)
    assert res.writes == [f"TDIV {token}", "TRDL -2.000000E-06S"]
    assert tb.seconds_per_div == pytest.approx(seconds)
    assert tb.trigger_delay_s == -2e-6


@pytest.mark.parametrize("seconds", [3e-6, 200, 0.5e-9, 0])
def test_timebase_outside_sequence_is_refused(dev: SiglentSDS1000XE, res: FakeSDSResource, seconds: float) -> None:
    with pytest.raises(ValueError, match="1-2-5 sequence"):
        dev.configure_timebase(seconds)
    assert res.writes == []


def test_configure_edge_trigger(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    trig = dev.configure_trigger(source="C2", slope="falling", level_v=0.25, coupling="ac", sweep="normal")
    assert res.writes == ["TRSE EDGE,SR,C2,HT,OFF", "C2:TRSL NEG", "C2:TRCP AC", "C2:TRLV 2.500000E-01V", "TRMD NORM"]
    assert (trig.source, trig.slope, trig.level_v, trig.coupling, trig.sweep) == ("C2", "falling", 0.25, "ac", "normal")


def test_trigger_level_only_keeps_source(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    dev.configure_trigger(level_v=1.0)
    assert res.writes == ["C1:TRLV 1.000000E+00V"]


def test_non_edge_trigger_is_read_and_changed_to_edge(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.trse = "GLIT,SR,C3,HT,PS,HV,2.00E-08S"
    assert (dev.trigger().type, dev.trigger().source, dev.trigger().slope) == ("GLIT", "C3", None)
    dev.configure_trigger(sweep="auto")
    assert res.writes[0] == "TRSE EDGE,SR,C3,HT,OFF"


def test_line_trigger_has_no_level(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    trig = dev.configure_trigger(source="LINE")
    assert (trig.source, trig.level_v, trig.slope) == ("LINE", None, None)
    with pytest.raises(ValueError, match="LINE source has no level"):
        dev.configure_trigger(level_v=1.0)


def test_unexpected_trigger_reply(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.trse = "EDGE"
    with pytest.raises(ValueError, match="Unexpected reply to TRSE"):
        dev.trigger()


def test_run_after_stop_uses_previous_sweep(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.trmd = "NORM"
    assert dev.stop() == "stopped"
    assert res.trmd == "STOP"  # as the real oscilloscope: the sweep is not known after STOP
    assert dev.run() == "triggered"
    assert res.writes == ["STOP", "TRMD NORM"]
    assert "ARM" not in res.writes  # ARM starts a single acquisition on the real oscilloscope


def test_run_keeps_running_sweep(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    assert dev.run() == "triggered"
    assert res.writes == []


def test_run_without_known_sweep_needs_sweep(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.trmd = "STOP"
    with pytest.raises(ValueError, match="previous sweep is not known"):
        dev.run()
    assert dev.run("normal") == "triggered"
    assert res.writes == ["TRMD NORM"]


def test_single_without_trigger_returns_ready(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    assert dev.single(timeout_s=1) == "ready"
    assert res.writes == ["TRMD SINGLE"]


def test_single_waits_for_trigger(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.trigger_after_polls = 3
    assert dev.single(timeout_s=5) == "stopped"


def test_single_is_not_sent_two_times(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    # A second TRMD SINGLE stops the real oscilloscope, and then SAST? does not answer.
    dev.single(timeout_s=0)
    assert dev.single(timeout_s=0) == "ready"
    assert res.writes == ["TRMD SINGLE"]
    dev.configure_trigger(sweep="single")
    assert res.writes == ["TRMD SINGLE"]


def test_state_in_stop_does_not_use_sast(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.trmd, res.sast = "STOP", None  # SAST? would time out
    assert dev.acquisition_state() == "stopped"
    assert dev.status().acquisition == "stopped"


def test_measure_with_unit(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.pava[("C1", "FREQ")] = "2.50E+07Hz"
    res.pava[("C1", "RISE")] = "3.6E-9S"
    res.pava[("C1", "OVSN")] = "1.96%"
    assert dev.measure("C1", "freq").model_dump() == {
        "channel": "C1", "parameter": "freq", "value": 2.5e7, "unit": "Hz", "valid": True,
    }
    assert (dev.measure("C1", "rise").value, dev.measure("C1", "rise").unit) == (3.6e-9, "s")
    assert dev.measure("C1", "ovsn").unit == "%"
    assert res.writes == []


def test_invalid_measurement(dev: SiglentSDS1000XE) -> None:
    r = dev.measure("C1", "duty")
    assert (r.value, r.valid, r.unit) == (None, False, "%")


def test_measurement_with_wrong_unit_is_refused(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.pava[("C1", "PKPK")] = "1.00E+00S"
    with pytest.raises(ValueError, match="Unexpected unit"):
        dev.measure("C1", "pkpk")


def test_waveform_decoding_agrees_with_programming_guide(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    # Programming guide WF? example: VDIV 0.5 V, OFST -0.5 V, code 0x02 -> 0.54 V;
    # code 0xFC -> -4; TDIV 5 ns, SARA 1 GSa/s -> first point -35 ns, interval 1 ns.
    res.channels["C1"].update(VDIV=0.5, OFST=-0.5)
    res.tdiv, res.sara = 5e-9, 1e9
    res.memory["C1"] = bytes([0x02, 0xFC, 0x0A, 0x7F, 0x80])
    wf = dev.waveform("C1")
    assert wf.voltages_v == pytest.approx([0.54, 0.42, 0.7, 3.04, -2.06])
    assert (wf.first_sample_time_s, wf.sample_interval_s) == (pytest.approx(-35e-9), pytest.approx(1e-9))
    assert (wf.sample_count, wf.memory_points, wf.sparsing, wf.off_screen_samples) == (5, 5, 1, 2)
    assert res.writes == ["WFSU SP,1,NP,5,FP,0", "C1:WF? DAT2"]


def test_waveform_time_includes_trigger_delay(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.tdiv, res.trdl = 1e-6, 2e-6
    assert dev.waveform("C1").first_sample_time_s == pytest.approx(-9e-6)


def test_waveform_without_header(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.chdr = "OFF"
    assert dev.waveform("C1").sample_count == 10


def test_waveform_sparsing(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.memory["C1"] = bytes(i % 100 for i in range(14_000))
    res.sara = 500e6
    wf = dev.waveform("C1", max_points=1000)
    assert (wf.sparsing, wf.sample_count) == (14, 1000)
    assert wf.sample_interval_s == pytest.approx(14 / 500e6)
    assert "WFSU SP,14,NP,1000,FP,0" in res.writes


def test_waveform_of_channel_that_is_off(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    with pytest.raises(ValueError, match="Channel C2 is off"):
        dev.waveform("C2")
    assert res.writes == []


def test_waveform_with_empty_memory(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    res.memory["C1"] = b""
    with pytest.raises(ValueError, match="has no data"):
        dev.waveform("C1")


@pytest.mark.parametrize("max_points", [0, 100_001])
def test_waveform_point_limit(dev: SiglentSDS1000XE, max_points: int) -> None:
    with pytest.raises(ValueError, match="max_points"):
        dev.waveform("C1", max_points)


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ([b"C1:WF DAT2,#9000000010\x01\x02"], "no more data"),  # transfer ends early (timeout)
        ([b"C1:WF DAT2,#9000000002\x01\x02XY"], "Unexpected end"),
        ([b"C1:WF DAT2,#A000000002\x01\x02\n\n"], "Invalid block header"),
        ([b"C1:WF DAT2,#90000000x2\x01\x02\n\n"], "Invalid block length"),
        ([b"C1:WF DAT2,#9000000000\n\n"], "no data points"),
        ([b"x" * 80], "No data block"),
    ],
)
def test_incomplete_waveform_transfer(dev: SiglentSDS1000XE, res: FakeSDSResource, reply, message) -> None:
    res.raw_reply = list(reply)
    original = res.read_raw

    def read_raw(size=None):
        return original(size) if res._pending else b""

    res.read_raw = read_raw  # type: ignore[method-assign]
    with pytest.raises(IncompleteTransferError, match=message):
        dev.waveform("C1")


def test_waveform_timeout_is_visa_error(dev: SiglentSDS1000XE, res: FakeSDSResource) -> None:
    import pyvisa

    res.raw_reply = [b"C1:WF DAT2,#9000000010\x01"]
    with pytest.raises(pyvisa.errors.VisaIOError):
        dev.waveform("C1")
