# lab-mcp

[![Tests](https://github.com/risqyy/lab-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/risqyy/lab-mcp/actions/workflows/tests.yml)

lab-mcp is an MCP server for Rigol laboratory instruments.
An AI assistant can use this server to control the instruments.
The server uses SCPI commands through VISA (pyvisa and pyvisa-py).

## Devices

| Device | Type                               | Supported | Tested                           |
|--------|------------------------------------|-----------|----------------------------------|
| DP832  | DC power supply with three outputs | Yes       | Yes (USB, firmware 00.01.14)     |
| DP832A | DC power supply with three outputs | Yes       | No                               |

"Tested" means that a test on a real instrument was done with all tools.
The LAN connection is not tested on a real instrument.

## Requirements

- Python 3.10 or later
- [Poetry](https://python-poetry.org/) 2.x

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/risqyy/lab-mcp.git
   ```

2. Go to the project directory:

   ```bash
   cd lab-mcp
   ```

3. Install the dependencies:

   ```bash
   poetry install
   ```

## Start the server

Start the server:

```bash
poetry run lab-mcp
```

The server uses Streamable HTTP at this URL:

```text
http://127.0.0.1:10765/mcp
```

The server needs no other configuration.

### Options

| Option              | Default     | Function                                          |
|---------------------|-------------|---------------------------------------------------|
| `--port`            | `10765`     | Set the HTTP port.                                |
| `--host`            | `127.0.0.1` | Set the HTTP address.                             |
| `--transport stdio` | `http`      | Use stdio instead of HTTP.                        |

**WARNING:** The server has no authentication. Each person who can connect to the server can switch the outputs.
By default, the server accepts only connections from the same computer.
Use `--host 0.0.0.0` only in a network that you trust.

## Connect the server to an MCP client

### Claude Code

Add the server:

```bash
claude mcp add --transport http rigol http://127.0.0.1:10765/mcp
```

### Other clients (HTTP)

Add this entry to the MCP configuration of your client:

```json
{
  "mcpServers": {
    "rigol": {
      "type": "http",
      "url": "http://127.0.0.1:10765/mcp"
    }
  }
}
```

### Clients that support only stdio

The client starts the server. Replace `/path/to/lab-mcp` with the path of your project directory.

```json
{
  "mcpServers": {
    "rigol": {
      "command": "poetry",
      "args": ["-C", "/path/to/lab-mcp", "run", "lab-mcp", "--transport", "stdio"]
    }
  }
}
```

## Automatic discovery

The server finds the instrument automatically at the first tool call.
You do not need to know the VISA address.

The server does these steps:

1. It searches all available VISA backends.
   It uses an installed IVI VISA (for example, NI-VISA) first, then pyvisa-py.
2. It examines USB devices with the Rigol vendor ID (0x1AB1).
3. It examines LAN instruments that answer the VXI-11 broadcast.
4. If it finds no instrument, it scans the local networks for TCP port 5555.
   It scans only private networks, and only the /24 network of each interface.
5. It sends `*IDN?` to each candidate. It uses only instruments that identify as DP832 or DP832A.

If the instrument does not answer, the server closes the connection.
At the next tool call, the server finds the instrument again.

If there is more than one DP832, the server uses the DP832 with the lowest serial number.
To use a different DP832, use the tool `dp832_connect` with the serial number.

## Tools

| Tool                     | Function                                                         |
|--------------------------|------------------------------------------------------------------|
| `discover_instruments`   | Find all Rigol instruments on USB and on the LAN.                |
| `dp832_connect`          | Connect to a DP832 by serial number or by VISA address.          |
| `dp832_identify`         | Show the address, model, serial number and firmware.             |
| `dp832_status`           | Read the output state, setpoints, OVP, OCP and CV/CC mode.       |
| `dp832_measure`          | Measure the voltage, current and power of the outputs.           |
| `dp832_set`              | Set the voltage and the current limit of one channel.            |
| `dp832_output`           | Switch the output of one channel on or off.                      |
| `dp832_all_off`          | Switch all outputs off.                                          |
| `dp832_set_protection`   | Set the over-voltage protection (OVP) and over-current protection (OCP). |
| `dp832_clear_protection` | Reset a tripped OVP or OCP.                                      |

## Safety

The server examines each value before it sends the value to the instrument.
The server does not send values that are out of range.

| Channel | Voltage   | Current  | OVP max. | OCP max. |
|---------|-----------|----------|----------|----------|
| CH1     | 0 to 30 V | 0 to 3 A | 33 V     | 3.3 A    |
| CH2     | 0 to 30 V | 0 to 3 A | 33 V     | 3.3 A    |
| CH3     | 0 to 5 V  | 0 to 3 A | 5.5 V    | 3.3 A    |

**WARNING:** The AI assistant can switch the outputs on. Disconnect sensitive loads before you use the server.

## Troubleshooting

| Problem                                   | Solution                                                                 |
|-------------------------------------------|--------------------------------------------------------------------------|
| The server does not find a USB instrument | Install a USB-TMC driver. Use NI-VISA, or use WinUSB on Windows (for example, with Zadig). |
| The server does not find a LAN instrument | Connect the instrument and the computer to the same network. Make sure that a firewall does not block TCP port 5555. |
| The server finds the wrong instrument     | Use `discover_instruments`. Then use `dp832_connect` with the serial number. |

## Development

Run the tests:

```bash
poetry run pytest
```

The tests use a simulated DP832 (`tests/fake_dp832.py`). You do not need an instrument for the tests.
