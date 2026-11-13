import asyncio
import base64
from collections.abc import Iterator

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.spectrum_analyzers import RigolDSA800

from fake_rigol_dsa800 import FakeDSA800Resource

DSA_INFO = DiscoveredInstrument(
    address="USB0::0x1AB1::0x0960::DSA8H000000001::INSTR", backend="@py",
    manufacturer="Rigol Technologies", model="DSA832E", serial="DSA8H000000001", firmware="00.01.06.00.04",
)


@pytest.fixture
def fake_sa(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDSA800Resource]:
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    res = FakeDSA800Resource()
    server.spectrum_analyzer.set(RigolDSA800(res, sleep=lambda s: None), DSA_INFO)
    yield res
    server.spectrum_analyzer.set(None)


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_slot_accepts_only_the_dsa832e() -> None:
    assert server._is_spectrum_analyzer(DSA_INFO)
    assert not server._is_spectrum_analyzer(DSA_INFO.model_copy(update={"model": "DS4014"}))
    assert not server._is_spectrum_analyzer(DSA_INFO.model_copy(update={"manufacturer": "Siglent Technologies"}))


def test_sa_tools_are_registered() -> None:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "sa_connect", "sa_identify", "sa_status", "sa_configure_frequency", "sa_configure_bandwidth",
        "sa_configure_amplitude", "sa_configure_trace", "sa_run", "sa_single", "sa_trace", "sa_marker_peak",
        "sa_set_marker", "sa_markers_off", "sa_screenshot",
    } <= names


def test_status(fake_sa: FakeDSA800Resource) -> None:
    st = call("sa_status").structured_content
    assert st["frequency"]["span_hz"] == 3.2e9
    assert [t["mode"] for t in st["traces"]] == ["clear_write", "blank", "blank"]


def test_configure_single_trace_and_marker(fake_sa: FakeDSA800Resource) -> None:
    f = call("sa_configure_frequency", center_hz=100e6, span_hz=6e6).structured_content
    assert (f["start_hz"], f["stop_hz"]) == (97e6, 103e6)
    assert call("sa_single", timeout_s=5).structured_content["complete"] is True
    t = call("sa_trace", trace=1, max_points=601).structured_content
    assert max(t["amplitudes"]) == -20.0
    m = call("sa_marker_peak").structured_content
    assert (m["x"], m["amplitude"]) == (100e6, -20.0)


def test_invalid_value_reaches_model(fake_sa: FakeDSA800Resource) -> None:
    with pytest.raises(ToolError, match="1-3-10 sequence"):
        call("sa_configure_bandwidth", rbw_hz=2000)
    assert fake_sa.writes == []


def test_screenshot(fake_sa: FakeDSA800Resource) -> None:
    [content] = call("sa_screenshot").content
    assert content.mime_type == "image/png"
    assert base64.b64decode(content.data).startswith(b"\x89PNG")


def test_auto_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    server.spectrum_analyzer.set(None)
    res = FakeDSA800Resource()
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [DSA_INFO])

    class RM:
        def __init__(self, backend: str) -> None:
            pass

        def open_resource(self, address: str, **kwargs) -> FakeDSA800Resource:
            assert kwargs["timeout"] == server.SA_IO_TIMEOUT_MS
            return res

    monkeypatch.setattr(server.pyvisa, "ResourceManager", RM)
    try:
        assert call("sa_identify").structured_content["model"] == "DSA832E"
        assert isinstance(server.spectrum_analyzer.device(), RigolDSA800)
    finally:
        server.spectrum_analyzer.set(None)


def test_two_analyzers_need_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    server.spectrum_analyzer.set(None)
    other = DSA_INFO.model_copy(update={"serial": "DSA8H000000002", "address": "USB0::0x1AB1::0x0960::DSA8H000000002::INSTR"})
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [other, DSA_INFO])
    with pytest.raises(ToolError, match="More than one spectrum analyzer found"):
        call("sa_status")


def test_no_analyzer(monkeypatch: pytest.MonkeyPatch) -> None:
    server.spectrum_analyzer.set(None)
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    with pytest.raises(ToolError, match="No spectrum analyzer found"):
        call("sa_status")
