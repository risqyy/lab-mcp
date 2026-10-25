import asyncio
from collections.abc import Iterator

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.multimeters import RigolDM3058

from fake_rigol_dm3058 import FakeDM3058Resource

DMM_INFO = DiscoveredInstrument(
    address="USB0::0x1AB1::0x09C4::DM3R000000001::INSTR", backend="@py",
    manufacturer="Rigol Technologies", model="DM3058E", serial="DM3R000000001", firmware="01.01.00.01.11.00",
)


@pytest.fixture(autouse=True)
def fake_dmm(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDM3058Resource]:
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    res = FakeDM3058Resource()
    server.dm3058.set(RigolDM3058(res, sleep=lambda s: None), DMM_INFO)
    yield res
    server.dm3058.set(None)


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_dmm_tools_are_registered() -> None:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "dmm_connect", "dmm_identify", "dmm_configuration", "dmm_configure",
        "dmm_measure", "dmm_read", "dmm_measure_series",
    } <= names


def test_dmm_measure(fake_dmm: FakeDM3058Resource) -> None:
    fake_dmm.values["VOLTage:DC"] = ["1.200345e+01"]
    r = call("dmm_measure", function="dc_voltage", expected_max=15).structured_content
    assert (r["value"], r["unit"], r["range"], r["overload"]) == (12.00345, "V", 20.0, False)


def test_dmm_measure_auto_is_default(fake_dmm: FakeDM3058Resource) -> None:
    r = call("dmm_measure", function="resistance_2w").structured_content
    assert (r["function"], r["range"], r["auto_range"]) == ("resistance_2w", 200e3, True)


def test_dmm_overload_reaches_model(fake_dmm: FakeDM3058Resource) -> None:
    fake_dmm.values["VOLTage:DC"] = ["9.9E+37"]
    r = call("dmm_read").structured_content
    assert (r["value"], r["overload"]) == (None, True)


def test_dmm_range_error_reaches_model() -> None:
    with pytest.raises(ToolError, match="above the largest dc_voltage range"):
        call("dmm_measure", function="dc_voltage", expected_max=2000)


def test_dmm_unknown_function_rejected_by_schema(fake_dmm: FakeDM3058Resource) -> None:
    with pytest.raises(ToolError):
        call("dmm_measure", function="temperature")
    assert fake_dmm.writes == []


def test_dmm_configure_and_series(fake_dmm: FakeDM3058Resource) -> None:
    cfg = call("dmm_configure", function="ac_voltage", expected_max=230, speed="fast").structured_content
    assert (cfg["function"], cfg["range"], cfg["speed"]) == ("ac_voltage", 750.0, "fast")
    fake_dmm.values["VOLTage:AC"] = ["229.5", "230.5"]
    s = call("dmm_measure_series", count=2, interval_s=0).structured_content
    assert (s["count"], s["mean"], s["unit"]) == (2, 230.0, "V")


def test_dmm_series_duration_limit() -> None:
    with pytest.raises(ToolError, match="The maximum is 300 s"):
        call("dmm_measure_series", count=1000, interval_s=1)


def test_dmm_series_duration_includes_reading_time() -> None:
    # 700 readings at the slow rate take approximately 350 s, also without an interval.
    call("dmm_configure", function="dc_voltage", speed="slow")
    with pytest.raises(ToolError, match="approximately 350 s"):
        call("dmm_measure_series", count=700, interval_s=0)
    call("dmm_configure", function="dc_voltage", speed="fast")
    assert call("dmm_measure_series", count=700, interval_s=0).structured_content["count"] == 700


def test_current_warning_in_description() -> None:
    [tool] = [t for t in asyncio.run(server.mcp.list_tools()) if t.name == "dmm_measure"]
    assert "fuse" in tool.description


def test_dmm_auto_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    server.dm3058.set(None)
    res = FakeDM3058Resource(cmdset="FLUKE")
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [DMM_INFO])

    class RM:
        def __init__(self, backend: str) -> None:
            pass

        def open_resource(self, address: str, **kwargs) -> FakeDM3058Resource:
            return res

    monkeypatch.setattr(server.pyvisa, "ResourceManager", RM)
    assert call("dmm_identify").structured_content["model"] == "DM3058E"
    assert res.writes == ["CMDSET RIGOL"]


def test_dmm_not_found_message() -> None:
    server.dm3058.set(None)
    with pytest.raises(ToolError, match="No Rigol DM3058 found.*no LAN interface"):
        call("dmm_read")


def test_all_outputs_off_ignores_multimeter(fake_dmm: FakeDM3058Resource) -> None:
    results = call("all_outputs_off").structured_content["result"]
    assert [r["instrument"] for r in results] == ["Rigol DP", "Manson HCS"]
    assert fake_dmm.writes == []
