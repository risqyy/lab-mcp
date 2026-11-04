import asyncio
import base64
from collections.abc import Iterator

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.oscilloscopes import RigolDS4000

from fake_rigol_ds4000 import FakeDS4000Resource

DS4_INFO = DiscoveredInstrument(
    address="USB0::0x1AB1::0x04B1::DS4A000000001::INSTR", backend="@py",
    manufacturer="RIGOL TECHNOLOGIES", model="DS4014", serial="DS4A000000001", firmware="00.02.02.SP5",
)
SDS_INFO = DiscoveredInstrument(
    address="USB0::0xF4EC::0xEE38::SDSMMGKQ800001::INSTR", backend="@py",
    manufacturer="Siglent Technologies", model="SDS1104X-E", serial="SDSMMGKQ800001", firmware="8.2.6.1.37R9",
)


@pytest.fixture
def fake_ds4(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDS4000Resource]:
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    res = FakeDS4000Resource()
    server.scope.set(RigolDS4000(res, sleep=lambda s: None), DS4_INFO)
    yield res
    server.scope.set(None)


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_scope_slot_accepts_both_models() -> None:
    assert server._is_scope(DS4_INFO) and server._is_scope(SDS_INFO)
    assert not server._is_scope(DS4_INFO.model_copy(update={"model": "DS1054Z"}))
    assert not server._is_scope(DS4_INFO.model_copy(update={"manufacturer": "Siglent Technologies"}))


def test_scope_status(fake_ds4: FakeDS4000Resource) -> None:
    st = call("scope_status").structured_content
    assert st["acquisition"] == "auto"
    assert [c["channel"] for c in st["channels"]] == ["C1", "C2", "C3", "C4"]


def test_scope_configure_measure_and_waveform(fake_ds4: FakeDS4000Resource) -> None:
    cfg = call("scope_configure_channel", channel="C1", volts_per_div=1.0).structured_content
    assert cfg["volts_per_div"] == 1.0
    [r] = call("scope_measure", channel="C1", parameters=["pkpk"]).structured_content["result"]
    assert (r["value"], r["valid"]) == (3.0, True)
    w = call("scope_waveform", channel="C1", max_points=700).structured_content
    assert (w["sample_count"], w["sparsing"]) == (700, 2)


def test_scope_screenshot(fake_ds4: FakeDS4000Resource) -> None:
    [content] = call("scope_screenshot").content
    assert content.mime_type == "image/png"
    assert base64.b64decode(content.data).startswith(b"\x89PNG")


def test_measurement_without_ds4000_command_reaches_model(fake_ds4: FakeDS4000Resource) -> None:
    with pytest.raises(ToolError, match="DS4014 has no measurement 'stdev'"):
        call("scope_measure", channel="C1", parameters=["stdev"])


@pytest.mark.parametrize("tool", ["scope_math", "scope_cursors", "scope_cursors_off"])
def test_math_and_cursors_reach_model_as_not_supported(fake_ds4: FakeDS4000Resource, tool: str) -> None:
    with pytest.raises(ToolError, match="not supported for the RIGOL DS4014"):
        call(tool)


def test_auto_connect_ds4014(monkeypatch: pytest.MonkeyPatch) -> None:
    server.scope.set(None)
    res = FakeDS4000Resource()
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [DS4_INFO])

    class RM:
        def __init__(self, backend: str) -> None:
            pass

        def open_resource(self, address: str, **kwargs) -> FakeDS4000Resource:
            return res

    monkeypatch.setattr(server.pyvisa, "ResourceManager", RM)
    try:
        assert call("scope_identify").structured_content["model"] == "DS4014"
        assert isinstance(server.scope.device(), RigolDS4000)
    finally:
        server.scope.set(None)


def test_two_oscilloscopes_need_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    server.scope.set(None)
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [SDS_INFO, DS4_INFO])
    with pytest.raises(ToolError, match="More than one oscilloscope found: .*DS4014 DS4A000000001.*SDS1104X-E SDSMMGKQ800001"):
        call("scope_status")
