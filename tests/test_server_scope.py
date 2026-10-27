import asyncio
import base64
from collections.abc import Iterator

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from lab_mcp import server
from lab_mcp.discovery import DiscoveredInstrument
from lab_mcp.instruments.oscilloscopes import SiglentSDS1000XE

from fake_siglent_sds1000xe import FakeSDSResource

SDS_INFO = DiscoveredInstrument(
    address="USB0::0xF4EC::0xEE38::SDSMMGKQ800001::INSTR", backend="@py",
    manufacturer="Siglent Technologies", model="SDS1104X-E", serial="SDSMMGKQ800001", firmware="8.2.6.1.37R9",
)


@pytest.fixture(autouse=True)
def fake_scope(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSDSResource]:
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [])
    res = FakeSDSResource()
    server.sds.set(SiglentSDS1000XE(res, sleep=lambda s: None), SDS_INFO)
    yield res
    server.sds.set(None)


def call(name: str, **arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_scope_tools_are_registered() -> None:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "scope_connect", "scope_identify", "scope_status", "scope_configure_channel", "scope_configure_timebase",
        "scope_configure_trigger", "scope_run", "scope_stop", "scope_single", "scope_measure", "scope_waveform",
        "scope_screenshot", "scope_math", "scope_configure_math", "scope_math_waveform", "scope_cursors",
        "scope_set_time_cursors", "scope_cursors_off",
    } <= names


def test_read_only_tools_are_annotated() -> None:
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("scope_identify", "scope_status", "scope_measure", "scope_screenshot", "scope_math", "scope_cursors"):
        assert tools[name].annotations.read_only_hint is True
    assert not (tools["scope_waveform"].annotations and tools["scope_waveform"].annotations.read_only_hint)


def test_scope_status(fake_scope: FakeSDSResource) -> None:
    r = call("scope_status").structured_content
    assert (r["acquisition"], r["timebase"]["seconds_per_div"], len(r["channels"])) == ("triggered", 1e-6, 4)
    assert fake_scope.writes == []


def test_scope_configure_and_waveform(fake_scope: FakeSDSResource) -> None:
    call("scope_configure_channel", channel="C1", volts_per_div=0.5, offset_v=-0.5)
    call("scope_configure_timebase", seconds_per_div=5e-9)
    assert call("scope_stop").structured_content["acquisition"] == "stopped"
    fake_scope.memory["C1"] = bytes([0x02, 0xFC])
    wf = call("scope_waveform", channel="C1").structured_content
    assert wf["voltages_v"] == pytest.approx([0.54, 0.42])
    assert (wf["sample_count"], wf["first_sample_time_s"], wf["volts_per_div"]) == (2, pytest.approx(-35e-9), 0.5)


def test_scope_measure(fake_scope: FakeSDSResource) -> None:
    fake_scope.pava[("C1", "PKPK")] = "4.20E+00V"
    results = call("scope_measure", channel="C1", parameters=["pkpk", "freq", "pkpk"]).structured_content["result"]
    assert [(r["parameter"], r["value"], r["unit"], r["valid"]) for r in results] == [
        ("pkpk", 4.2, "V", True), ("freq", None, "Hz", False),
    ]


def test_scope_single(fake_scope: FakeSDSResource) -> None:
    assert call("scope_single", timeout_s=0).structured_content["acquisition"] == "ready"
    assert fake_scope.trmd == "SINGLE"


def test_invalid_setting_reaches_model(fake_scope: FakeSDSResource) -> None:
    with pytest.raises(ToolError, match="1-2-5 sequence"):
        call("scope_configure_timebase", seconds_per_div=3e-6)
    with pytest.raises(ToolError):
        call("scope_configure_channel", channel="C5")
    with pytest.raises(ToolError):
        call("scope_measure", channel="C1", parameters=["delay"])
    assert fake_scope.writes == []


def test_incomplete_transfer_closes_connection(fake_scope: FakeSDSResource) -> None:
    fake_scope.raw_reply = [b"C1:WF DAT2,#9000000010\x01\x02"]
    with pytest.raises(ToolError, match="Communication with the instrument failed"):
        call("scope_waveform", channel="C1")
    assert server.sds.connected_info is None


def test_scope_auto_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    server.sds.set(None)
    res = FakeSDSResource()
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [SDS_INFO])
    opened = {}

    class RM:
        def __init__(self, backend: str) -> None:
            pass

        def open_resource(self, address: str, **kwargs) -> FakeSDSResource:
            opened.update(kwargs, address=address)
            return res

    monkeypatch.setattr(server.pyvisa, "ResourceManager", RM)
    assert call("scope_identify").structured_content["model"] == "SDS1104X-E"
    assert opened["address"] == SDS_INFO.address
    assert res.writes == []


def test_scope_selects_by_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    server.sds.set(None)
    other = SDS_INFO.model_copy(update={"serial": "SDSMMGKQ800002", "address": "USB0::0xF4EC::0xEE38::SDSMMGKQ800002::INSTR"})
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [SDS_INFO, other])
    with pytest.raises(ToolError, match="More than one SIGLENT SDS found"):
        call("scope_identify")

    class RM:
        def __init__(self, backend: str) -> None:
            pass

        def open_resource(self, address: str, **kwargs) -> FakeSDSResource:
            return FakeSDSResource()

    monkeypatch.setattr(server.pyvisa, "ResourceManager", RM)
    assert call("scope_connect", serial="SDSMMGKQ800002").structured_content["address"] == other.address


def test_other_siglent_model_is_not_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    server.sds.set(None)
    other = SDS_INFO.model_copy(update={"model": "SDS2104X Plus"})
    monkeypatch.setattr(server.discovery, "discover", lambda **kwargs: [other] if kwargs["match"](other) else [])
    with pytest.raises(ToolError, match="No SIGLENT SDS found.*USB-TMC driver"):
        call("scope_status")


def test_scope_screenshot_returns_png_image(fake_scope: FakeSDSResource) -> None:
    [content] = call("scope_screenshot").content
    assert (content.type, content.mime_type) == ("image", "image/png")
    assert base64.b64decode(content.data).startswith(bytes([0x89]) + b"PNG")
    assert fake_scope.writes == ["SCDP"]


def test_scope_math(fake_scope: FakeSDSResource) -> None:
    cfg = call("scope_configure_math", operation="add", source1="C1", source2="C1", volts_per_div=2, position_div=-1)
    assert (cfg.structured_content["equation"], cfg.structured_content["position_div"]) == ("C1+C1", -1.0)
    fake_scope.math_memory = bytes([75, 0])  # 75 codes = 3 div -> 6 V - (-2 V)
    wf = call("scope_math_waveform", max_points=10).structured_content
    assert wf["values"] == pytest.approx([8.0, 2.0])
    with pytest.raises(ToolError, match="needs source2"):
        call("scope_configure_math", operation="add", source1="C1")


def test_scope_cursors(fake_scope: FakeSDSResource) -> None:
    fake_scope.tdiv = 2e-4
    c = call("scope_set_time_cursors", source="C1", x1_s=-5e-4, x2_s=5e-4).structured_content
    assert (c["mode"], c["inverse_delta_hz"]) == ("manual", pytest.approx(1000.0))
    assert call("scope_cursors_off").structured_content["mode"] == "off"
