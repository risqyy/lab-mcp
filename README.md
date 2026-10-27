# lab-mcp

[![Tests](https://github.com/risqyy/lab-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/risqyy/lab-mcp/actions/workflows/tests.yml)

lab-mcp is a Model Context Protocol (MCP) server for laboratory instruments.
Its goal is to let an AI assistant find, configure, and read instruments through one MCP connection.
The server finds supported instruments automatically. You usually do not need to enter device addresses or ports.

## Current capabilities

- Control supported DC power supplies, digital multimeters, function generators, and oscilloscopes.
- Find instruments through USB-TMC, LAN (SCPI), and USB serial connections, as supported by each model.
- Read measurements, waveforms, and status, set supported parameters, and switch outputs on or off.
- Connect an MCP client through Streamable HTTP or stdio.

Support and test coverage differ by model. Some functions have only simulated tests or protocol checks.
LAN operation is not tested on real instruments. Generator support is limited to basic waveforms.
Refer to [Supported devices](SUPPORTED_DEVICES.md) for models, tools, test scope, limits, and troubleshooting.

## Before use

**WARNING:** An AI assistant can switch instrument outputs on. Disconnect sensitive loads before you use the server.
Read the limits and safety notes for your device in [Supported devices](SUPPORTED_DEVICES.md).
The server checks values before it sends them to an instrument. It refuses values outside the permitted range.
These checks cannot verify the wiring or the connected load.

**CAUTION:** For current measurements, use the multimeter current input and connect the instrument in series with the load.
If you connect the current input across a voltage source, the multimeter fuse blows.
The server cannot detect the position of the test leads.

**WARNING:** The server has no authentication. Anyone who can connect to it can control the instruments.
The default HTTP address accepts connections only from the same computer.
Use `--host 0.0.0.0` only in a network that you trust.

## Get started

### Requirements

- Git
- Python 3.10 or later
- [Poetry](https://python-poetry.org/) 2.x
- An MCP client with Streamable HTTP or stdio support
- A supported instrument and its required interface driver for hardware use

Check the [connection and discovery notes](SUPPORTED_DEVICES.md#connection-and-discovery) and the notes for your device before connection.

### Install

Clone the repository and install its dependencies:

```sh
git clone https://github.com/risqyy/lab-mcp.git
cd lab-mcp
poetry install
```

### Start the server

Run this command from the project directory:

```sh
poetry run lab-mcp
```

Keep the process running while the HTTP client is connected. The default endpoint is:

```text
http://127.0.0.1:10765/mcp
```

| Option | Default | Function |
|--------|---------|----------|
| `--port` | `10765` | Set the HTTP port. |
| `--host` | `127.0.0.1` | Set the HTTP address. |
| `--transport` | `http` | Select `http` or `stdio`. |

### Connect an MCP client

For a client with Streamable HTTP support, add the endpoint above as an MCP server.
Use the configuration format required by your client. For clients that accept `mcpServers` entries:

```json
{
  "mcpServers": {
    "lab-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:10765/mcp"
    }
  }
}
```

For a client with stdio support, use this configuration instead.
The client starts the server process; you do not need a separate HTTP server.
Replace `/path/to/lab-mcp` with the absolute project path.
On Windows, use forward slashes in the JSON path, for example `C:/work/lab-mcp`.

```json
{
  "mcpServers": {
    "lab-mcp": {
      "command": "poetry",
      "args": ["-C", "/path/to/lab-mcp", "run", "lab-mcp", "--transport", "stdio"]
    }
  }
}
```

### Find an instrument

Connect and switch on the instrument. Ask the MCP client to call `discover_instruments`.
Check the returned model and serial number before you send commands.
If more than one instrument of a type is found, use its connection tool with the serial number to select it.
Refer to [Supported devices](SUPPORTED_DEVICES.md) for the applicable tool and operating limits.

## Development

Run the software tests with `poetry run pytest`. The tests use simulated instruments; no hardware is required.
Drivers are in `src/lab_mcp/instruments/`. MCP tools are in `src/lab_mcp/tools/`.
Refer to [Extend device support](SUPPORTED_DEVICES.md#extend-device-support) to add an instrument.
Follow the repository instructions in [AGENTS.md](AGENTS.md).

## License

lab-mcp uses the MIT License. Refer to [LICENSE](LICENSE).
