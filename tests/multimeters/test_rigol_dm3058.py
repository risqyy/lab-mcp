import pytest

from lab_mcp.instruments.multimeters import RigolDM3058

from fake_rigol_dm3058 import FakeDM3058Resource


@pytest.fixture
def res() -> FakeDM3058Resource:
    return FakeDM3058Resource()


@pytest.fixture
def dev(res: FakeDM3058Resource) -> RigolDM3058:
    return RigolDM3058(res, sleep=lambda s: None)


def test_selects_rigol_command_set() -> None:
    res = FakeDM3058Resource(cmdset="AGILENT")
    RigolDM3058(res, sleep=lambda s: None)
    assert res.writes == ["CMDSET RIGOL"]


def test_keeps_rigol_command_set(res: FakeDM3058Resource, dev: RigolDM3058) -> None:
    assert res.writes == []


def test_measure_dc_voltage_autorange(dev: RigolDM3058, res: FakeDM3058Resource) -> None:
    res.values["VOLTage:DC"] = ["8.492853e-05"]
    r = dev.measure("dc_voltage")
    assert (r.function, r.value, r.unit, r.overload, r.range) == ("dc_voltage", 8.492853e-05, "V", False, "auto")
    assert ":FUNCtion:VOLTage:DC" not in res.writes  # already the present function


@pytest.mark.parametrize(
    ("function", "expected_max", "scpi", "index", "full_scale"),
    [
        ("dc_voltage", 12.0, "VOLTage:DC", 2, 20.0),
        ("dc_voltage", 0.2, "VOLTage:DC", 0, 0.2),
        ("dc_voltage", -150.0, "VOLTage:DC", 3, 200.0),
        ("ac_voltage", 230.0, "VOLTage:AC", 4, 750.0),
        ("dc_current", 0.05, "CURRent:DC", 3, 0.2),
        ("ac_current", 5.0, "CURRent:AC", 3, 10.0),
        ("resistance_2w", 4700.0, "RESistance", 2, 20e3),
        ("resistance_4w", 0.5, "FRESistance", 0, 200.0),
        ("capacitance", 1e-6, "CAPacitance", 3, 2e-6),
    ],
)
def test_range_selection(dev: RigolDM3058, res: FakeDM3058Resource, function, expected_max, scpi, index, full_scale) -> None:
    r = dev.measure(function, expected_max)
    assert f":MEASure:{scpi} {index}" in res.writes
    assert r.range == full_scale


def test_value_above_largest_range_is_refused(dev: RigolDM3058, res: FakeDM3058Resource) -> None:
    with pytest.raises(ValueError, match="above the largest dc_voltage range"):
        dev.measure("dc_voltage", 1500.0)
    with pytest.raises(ValueError, match="above the largest ac_voltage range"):
        dev.measure("ac_voltage", 1000.0)  # AC max. is 750 V
    assert res.writes == []


def test_range_for_function_without_ranges_is_refused(dev: RigolDM3058) -> None:
    with pytest.raises(ValueError, match="no range setting"):
        dev.measure("frequency", 1000.0)


def test_auto_after_manual_range(dev: RigolDM3058, res: FakeDM3058Resource) -> None:
    dev.configure("dc_voltage", 12.0)
    assert dev.configuration().range == 20.0
    dev.configure("dc_voltage", "auto")
    assert res.writes[-1] == ":MEASure AUTO"
    assert dev.configuration().range == "auto"


def test_function_change_selects_autorange(dev: RigolDM3058, res: FakeDM3058Resource) -> None:
    dev.configure("dc_voltage", 12.0)
    dev.configure("resistance_2w")
    assert res.writes[-2:] == [":FUNCtion:RESistance", ":MEASure AUTO"]


def test_speed(dev: RigolDM3058, res: FakeDM3058Resource) -> None:
    cfg = dev.configure("ac_voltage", speed="slow")
    assert ":RATE:VOLTage:AC S" in res.writes
    assert (cfg.function, cfg.speed) == ("ac_voltage", "slow")
    with pytest.raises(ValueError, match="no speed setting"):
        dev.configure("frequency", speed="fast")


@pytest.mark.parametrize("reply", ["9.900000e+37", "-9.9E+37", "OVERLOAD", "nan"])
def test_overload(dev: RigolDM3058, res: FakeDM3058Resource, reply: str) -> None:
    res.values["VOLTage:DC"] = [reply]
    r = dev.measure("dc_voltage")
    assert (r.value, r.overload) == (None, True)


@pytest.mark.parametrize(
    ("function", "scpi", "unit"),
    [("frequency", "FREQuency", "Hz"), ("period", "PERiod", "s"), ("continuity", "CONTinuity", "Ohm"),
     ("diode", "DIODe", "V"), ("resistance_4w", "FRESistance", "Ohm")],
)
def test_other_functions(dev: RigolDM3058, res: FakeDM3058Resource, function, scpi, unit) -> None:
    r = dev.measure(function)
    assert res.writes[0] == f":FUNCtion:{scpi}"
    assert (r.function, r.unit, r.range) == (function, unit, "auto")


def test_function_reply_variants(dev: RigolDM3058, res: FakeDM3058Resource, monkeypatch) -> None:
    # The programming guide shows RESISTANCE in one place and 2WR in another.
    original = res.query
    monkeypatch.setattr(res, "query", lambda m: "RESISTANCE" if m == ":FUNCtion?" else original(m))
    assert dev.get_function() == "resistance_2w"


def test_measure_series(dev: RigolDM3058, res: FakeDM3058Resource) -> None:
    sleeps = []
    dev._sleep = sleeps.append
    res.values["VOLTage:DC"] = ["1.0", "2.0", "9.9e37", "3.0"]
    s = dev.measure_series(4, 0.25)
    assert (s.count, s.overloads, s.minimum, s.maximum, s.mean) == (4, 1, 1.0, 3.0, 2.0)
    assert s.stdev == pytest.approx(1.0)
    assert s.values == [1.0, 2.0, None, 3.0]
    assert sleeps == [0.25, 0.25, 0.25]


def test_measure_series_one_value(dev: RigolDM3058) -> None:
    s = dev.measure_series(1, 0)
    assert (s.count, s.stdev) == (1, None)


def test_unknown_function(dev: RigolDM3058) -> None:
    with pytest.raises(ValueError, match="Unknown function"):
        dev.measure("temperature")  # type: ignore[arg-type]
