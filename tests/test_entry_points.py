"""Start the real server process and examine that all tools are registered."""

import asyncio
import sys

import pytest
from mcp import Client, StdioServerParameters

from lab_mcp import server

EXPECTED_TOOLS = {"discover_instruments", "all_outputs_off", "dp_status", "hcs_status", "sig_status", "sig_set"}


def _list_tools(args: list[str]) -> set[str]:
    async def run() -> set[str]:
        params = StdioServerParameters(command=sys.executable, args=[*args, "--transport", "stdio"])
        async with Client(params) as client:
            return {t.name for t in (await client.list_tools()).tools}

    return asyncio.run(run())


@pytest.mark.parametrize("args", [["-m", "lab_mcp.server"], ["-c", "from lab_mcp.server import main; main()"]])
def test_server_process_has_all_tools(args: list[str]) -> None:
    names = _list_tools(args)
    assert EXPECTED_TOOLS <= names
    assert len(names) == len(asyncio.run(server.mcp.list_tools()))
