# lab-mcp

[![Tests](https://github.com/risqyy/lab-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/risqyy/lab-mcp/actions/workflows/tests.yml)

lab-mcp is an MCP server for laboratory instruments.
An AI assistant can use this server to control the instruments.
The server finds the instruments automatically. You do not need to configure addresses or ports.

## Devices

| Device       | Type                               | Interface             | Supported | Tested                                  |
|--------------|------------------------------------|-----------------------|-----------|-----------------------------------------|
| Rigol DP832  | DC power supply with three outputs | USB-TMC, LAN (SCPI)   | Yes       | Yes (USB, firmware 00.01.14)            |
| Rigol DP832A | DC power supply with three outputs | USB-TMC, LAN (SCPI)   | Yes       | No                                      |
| Manson HCS-3404 | DC power supply with one output | USB virtual COM port  | Yes       | Yes (USB, no load)                      |

"Tested" means that a test on a real instrument was done with all tools.
The LAN connection is not tested on a real instrument.

Other models of the Manson HCS series use the same protocol. They can possibly operate, but they are not tested.

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
claude mcp add --transport http --scope user lab-mcp http://127.0.0.1:10765/mcp
```

### Codex

Add the server:

```bash
codex mcp add lab-mcp --url http://127.0.0.1:10765/mcp
```

### Other clients (HTTP)

Add this entry to the MCP configuration of your client:

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

### Clients that support only stdio

The client starts the server. Replace `/path/to/lab-mcp` with the path of your project directory.

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

## Automatic discovery

At the first tool call for an instrument type, the server finds the instrument automatically.

The server does these steps:

1. It searches all available VISA backends.
   It uses an installed IVI VISA (for example, NI-VISA) first, then pyvisa-py.
2. It examines USB devices with the Rigol vendor ID (0x1AB1).
3. It examines LAN instruments that answer the VXI-11 broadcast.
4. If steps 1 to 3 find no instrument, it scans the local networks for TCP port 5555.
   It scans only private networks, and only the /24 network of each interface.
5. It sends `*IDN?` to each VISA candidate.
6. It examines the serial ports of Silicon Labs CP210x USB-serial bridges (USB ID 10C4:EA60).
   It sends `GMOD` to each of these ports. It uses only instruments that identify as HCS.
   The server does not send data to other serial ports.

If an instrument does not answer, the server closes the connection.
At the next tool call, the server finds the instrument again.

If there is more than one instrument of a type, the server uses the instrument with the lowest serial number.
To use a different instrument, use `dp832_connect` or `hcs_connect` with the serial number.

## Tools

### All instruments

| Tool                     | Function                                                         |
|--------------------------|------------------------------------------------------------------|
| `discover_instruments`   | Find all supported instruments on USB, on the LAN and on serial ports. |
| `all_outputs_off`        | Switch off all outputs of all instruments that the server finds. |

### Rigol DP832

| Tool                     | Function                                                         |
|--------------------------|------------------------------------------------------------------|
| `dp832_connect`          | Connect to a DP832 by serial number or by VISA address.          |
| `dp832_identify`         | Show the address, model, serial number and firmware.             |
| `dp832_status`           | Read the output state, setpoints, OVP, OCP and CV/CC mode.       |
| `dp832_measure`          | Measure the voltage, current and power of the outputs.           |
| `dp832_set`              | Set the voltage and the current limit of one channel.            |
| `dp832_output`           | Switch the output of one channel on or off.                      |
| `dp832_all_off`          | Switch all outputs of the DP832 off.                             |
| `dp832_set_protection`   | Set the over-voltage protection (OVP) and over-current protection (OCP). |
| `dp832_clear_protection` | Reset a tripped OVP or OCP.                                      |

### Manson HCS

| Tool                     | Function                                                         |
|--------------------------|------------------------------------------------------------------|
| `hcs_connect`            | Connect to an HCS by USB serial number or by serial port.        |
| `hcs_identify`           | Show the port, model and USB serial number.                      |
| `hcs_status`             | Read the setpoints, the upper limits and the limits of the model. |
| `hcs_measure`            | Measure the voltage and current, and read the CV/CC mode.        |
| `hcs_set`                | Set the voltage and the current limit.                           |
| `hcs_output`             | Switch the output on or off.                                     |
| `hcs_set_upper_limits`   | Set the upper voltage limit and the upper current limit.         |
| `hcs_presets`            | Read the three preset memories.                                  |
| `hcs_load_preset`        | Load one preset memory into the setpoints.                       |

## Safety

The server examines each value before it sends the value to the instrument.
The server does not send values that are out of range.

Rigol DP832:

| Channel | Voltage   | Current  | OVP max. | OCP max. |
|---------|-----------|----------|----------|----------|
| CH1     | 0 to 30 V | 0 to 3 A | 33 V     | 3.3 A    |
| CH2     | 0 to 30 V | 0 to 3 A | 33 V     | 3.3 A    |
| CH3     | 0 to 5 V  | 0 to 3 A | 5.5 V    | 3.3 A    |

Manson HCS: the server reads the limits from the instrument (`GMAX`).
The HCS-3404 that was tested sends 60.5 V and 11.0 A.
The server also examines the upper limits (`SOVP`, `SOCP`) before it sends a setpoint or loads a preset.

**CAUTION:** The HCS protocol cannot read the output state.
The server shows the last state that it set. Before the first switch command, the state is unknown.

**CAUTION:** The HCS-3404 keeps a preset that is above the upper limits. It loads the preset later,
when the upper limit increases. If the output is on, the output changes without a command.
Thus the server does not send such a preset.

### Manson HCS: behavior of the instrument

These items were found in a test with an HCS-3404:

| Item                                          | Behavior                                                       |
|-----------------------------------------------|----------------------------------------------------------------|
| Setpoint above the upper limit                | The instrument does not reply. The server refuses the setpoint before it sends it. |
| Preset above the upper limit                  | The instrument keeps the command and loads the preset later. The server refuses the preset. |
| Decrease of the voltage without a load        | The voltage decreases slowly. The instrument cannot discharge the output. |
| `GETD` with the output off                    | The instrument sends the actual output voltage, not the setpoint. |
| MODE switch on the rear panel                 | USB control operates only in the position "Normal". In the position "Remote Ctrl", the instrument does not reply on USB. |

**WARNING:** The AI assistant can switch the outputs on. Disconnect sensitive loads before you use the server.

## Troubleshooting

| Problem                                    | Solution                                                                 |
|--------------------------------------------|--------------------------------------------------------------------------|
| The server does not find a USB instrument  | Install a USB-TMC driver. Use NI-VISA, or use WinUSB on Windows (for example, with Zadig). |
| The server does not find a LAN instrument  | Connect the instrument and the computer to the same network. Make sure that a firewall does not block TCP port 5555. |
| The server does not find a Manson HCS      | Set the MODE switch on the rear panel to "Normal". Make sure that the instrument is on. Make sure that the operating system shows a COM port for the CP210x bridge. If not, install the Silicon Labs CP210x driver. Close other programs that use the port. |
| The server finds the wrong instrument      | Use `discover_instruments`. Then use `dp832_connect` or `hcs_connect` with the serial number. |

## Development

Run the tests:

```bash
poetry run pytest
```

The tests use simulated instruments (`tests/<category>/fake_*.py`). You do not need an instrument for the tests.

### Project structure

The drivers and the tools have one folder or module for each instrument category.

```text
src/lab_mcp/
├── discovery.py               # discovery on USB, LAN and serial ports
├── server.py                  # MCP server, connection slots, discover_instruments, all_outputs_off
├── instruments/
│   └── power_supplies/
│       ├── rigol_dp800.py     # Rigol DP832
│       └── manson_hcs.py      # Manson HCS series
└── tools/
    └── power_supplies.py      # dp832_* and hcs_* tools
tests/
├── test_*.py                  # discovery, server and start of the server process
└── power_supplies/            # driver tests and simulated instruments
```

### Add an instrument

1. Find the category of the instrument, for example `power_supplies`, `multimeters` or `oscilloscopes`.
   If the category folder does not exist, make it in `instruments/`, and make the module in `tools/`.
2. Add the driver to `instruments/<category>/`. Use the manufacturer and the series for the file name, for example `rigol_dp800.py`.
3. Add a connection slot in `server.py`, if the instrument needs a new slot.
4. Add the tools to `tools/<category>.py`. If the module is new, import it at the end of `server.py`.
5. Add a simulated instrument (`tests/<category>/fake_*.py`) and the tests. Add a new category folder to `tests/conftest.py`.
6. Add the instrument to the device table in this README.
