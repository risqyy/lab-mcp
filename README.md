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
| Rigol DP932A | DC power supply with three outputs | USB-TMC, LAN (SCPI)   | Yes       | No                                      |
| Rigol DP932U | DC power supply with three outputs | USB-TMC, LAN (SCPI)   | Yes       | No                                      |
| Rigol DP932E | DC power supply with three outputs | USB-TMC, LAN (SCPI)   | Yes       | No                                      |
| Manson HCS-3404 | DC power supply with one output | USB virtual COM port  | Yes       | Yes (USB, no load)                      |
| Rigol DM3058E | Digital multimeter, 5½ digits     | USB-TMC (SCPI)        | Yes       | Partially (USB, firmware 01.01.00.02.03.01, open inputs and DC voltage) |
| Rigol DM3058 | Digital multimeter, 5½ digits      | USB-TMC, LAN (SCPI)   | Yes       | No                                      |
| GW Instek AFG-2105 | Function generator, 5 MHz   | USB virtual COM port (SCPI) | Yes | Protocol checks only (USB, firmware V2.02); hardware acceptance is separate |

"Tested" means that a test on a real instrument was done with all tools.
The LAN connection is not tested on a real instrument.

The DP900 support (DP932A, DP932U, DP932E) agrees with the DP900 programming guide (July 2024). It is not tested on a real instrument.

The DM3058E is tested with open inputs (all functions, ranges, overload, speed, series and command set)
and with DC voltage from a DP832 (1.5 V to 12.4 V: the difference to the DP832 measurement is less than 13 mV).
The test with components, current and AC signals is open (issue #10).

Other models of the Manson HCS series use the same protocol. They can possibly operate, but they are not tested.

The AFG-2105 supports sine, square, ramp/triangle and noise. Arbitrary waveforms, modulation,
sweep and the frequency counter are not supported by the tools. Other AFG-2000/2100 models
are not yet supported. The software tests use a simulated instrument. Voltage and frequency
measurements of the real generator remain in [hardware test #15](https://github.com/risqyy/lab-mcp/issues/15),
including the AC items of #10.

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
7. It examines GW Instek USB CDC ports (USB ID 2184:001C) with `*IDN?`.
   It accepts the AFG-2105 and removes the `SN:` prefix from its serial number.
   The server does not send data to serial ports with other USB IDs.

If an instrument does not answer, the server closes the connection.
At the next tool call, the server finds the instrument again.

If there is more than one instrument of a type, the server does not select one.
A command to the wrong instrument can be dangerous. The tools then send an error message with a list of the instruments.
Use `dp_connect`, `hcs_connect`, `dmm_connect` or `sig_connect` with the serial number to select one.

## Tools

### All instruments

| Tool                     | Function                                                         |
|--------------------------|------------------------------------------------------------------|
| `discover_instruments`   | Find all supported instruments on USB, on the LAN and on serial ports. |
| `all_outputs_off`        | Switch off all outputs of all instruments that the server finds. |

### Rigol DP800 and DP900 series

| Tool                     | Function                                                         |
|--------------------------|------------------------------------------------------------------|
| `dp_connect`             | Connect to a DP by serial number or by VISA address.             |
| `dp_identify`            | Show the address, model, serial number and firmware.             |
| `dp_status`              | Read the output state, setpoints, OVP, OCP, CV/CC mode, limits and coupling. |
| `dp_measure`             | Measure the voltage, current and power of the outputs.           |
| `dp_set`                 | Set the voltage and the current limit of one channel.            |
| `dp_output`              | Switch the output of one channel on or off.                      |
| `dp_all_off`             | Switch all outputs of the DP off.                                |
| `dp_set_protection`      | Set the over-voltage protection (OVP) and over-current protection (OCP). |
| `dp_clear_protection`    | Reset a tripped OVP or OCP. The output stays off.                |

### Rigol DM3058 and DM3058E

| Tool                     | Function                                                         |
|--------------------------|------------------------------------------------------------------|
| `dmm_connect`            | Connect to a multimeter by serial number or by VISA address.     |
| `dmm_identify`           | Show the address, model, serial number and firmware.             |
| `dmm_configuration`      | Read the function, range and speed, and the available ranges.    |
| `dmm_configure`          | Set the function, range and speed. Do not measure.               |
| `dmm_measure`            | Set the function and measure one value.                          |
| `dmm_read`               | Measure one value with the present configuration.                |
| `dmm_measure_series`     | Measure a series of values, with minimum, maximum, mean and standard deviation. |

Functions: `dc_voltage`, `ac_voltage`, `dc_current`, `ac_current`, `resistance_2w`, `resistance_4w`,
`frequency`, `period`, `continuity`, `diode`, `capacitance`.

Range: give the largest value that you expect (`expected_max`). The server selects the smallest range
that includes this value. Use `auto` for autorange. The reading shows the range that the instrument used,
also with autorange. A value out of the range gives `overload: true` and no value.

### Rigol DM3058E: behavior of the instrument

These items were found in a test with a DM3058E (firmware 01.01.00.02.03.01) with open inputs:

| Item                                    | Behavior                                                       |
|-----------------------------------------|----------------------------------------------------------------|
| Overload                                | The instrument sends `9.000000E+15`, not `9.9E+37` (SCPI convention). |
| `:FUNCtion?`                            | Short replies: `DCV`, `ACV`, `DCI`, `ACI`, `2WR`, `4WR`, `FREQ`, `PERI`, `CONT`, `DIODE`, `CAP`. |
| `:MEASure:<function>:RANGe?` with autorange | Sends the range that the instrument selected.              |
| `:RATE:<function>?`                     | Sends `SLOW`, `MEDIUM` or `FAST`, not the letter of the command. |
| Time for one reading                    | Slow approximately 450 ms, medium 70 ms, fast 16 ms. Frequency and period approximately 0.8 s. The first resistance reading with autorange can take 2.4 s. |
| 4-wire resistance with open inputs      | The instrument shows a random value (for example 117 Ω), not an overload. Make sure that all four test leads are connected. |
| AC voltage and frequency with open inputs | The instrument shows approximately 10 mV and 50 Hz (mains interference on the test leads). |

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

### GW Instek AFG-2105 tools

| Tool | Function |
|------|----------|
| `sig_connect` | Connect to a generator by serial number or serial port. |
| `sig_identify` | Show the port, model, serial number and firmware. |
| `sig_status` | Read the waveform, frequency, amplitude, offset, duty cycle, ramp symmetry, load, output state and limits. |
| `sig_set` | Set a basic waveform and its parameters. The output state does not change. |
| `sig_output` | Switch the output on or off, and read the status. |

`sig_set` accepts `waveform` (`sine`, `square`, `ramp`, `noise`), `frequency_hz`,
`amplitude_vpp`, `offset_v`, `duty_cycle_percent`, `symmetry_percent` and `load`
(`50_ohm` or `high_z`). Omit a parameter to keep its value. Use `ramp` with
`symmetry_percent=50` for a triangle. Noise has no frequency setting.

Amplitude is always in Vpp, even if the front panel uses Vrms or dBm. `sig_set` selects
VPP on the display. `sig_status` only reads the settings and does not change the display unit.
The reported limits apply to the present settings; for example, more offset means less available amplitude.

The server checks the complete target configuration before it writes settings. It also checks
the current device limits before each numeric command, reads all settings back, and reads the
error queue. A communication failure can leave partial settings. Read `sig_status` before continuing.

## Safety

The server examines each value before it sends the value to the instrument.
The server does not send values that are out of range.

### GW Instek AFG-2105: limits and load setting

Sine and square: 0.1 Hz to 5 MHz. Ramp: 0.1 Hz to 1 MHz.
Square-wave duty cycle: 1 % to 99 % below 100 kHz, 20 % to 80 % below 5 MHz.
At the 5 MHz boundary, the driver uses the narrower 40 % to 60 % range from the manual.
The driver also checks the limits returned by the instrument.

At 50 Ohm, amplitude is 1 mVpp to 10 Vpp and `abs(offset_v) + amplitude_vpp / 2` must not
exceed 5 V. The corresponding High-Z values are twice as large. The load setting is a
reference for voltage values; it does not measure the load or change the internal 50 Ohm impedance.
If the setting is `50_ohm` but the connected input is high impedance, that input gets twice
the reported amplitude and offset. Select the correct load before setting the voltage.

Change `load` only with the output off. If amplitude and offset are omitted during a load
change, their physical values stay the same and their reported values change. To specify new
values, include them in the same `sig_set` call; they refer to the new load setting.

The driver refuses settings that would cause an implicit frequency or duty-cycle clamp.
Supply a compatible frequency or duty cycle in the same call. Disable modulation and sweep
on the front panel before setting a basic waveform or enabling the output. Switching off
with `sig_output` or `all_outputs_off` does not depend on these modes.

### GW Instek AFG-2105: behavior of the instrument

Protocol checks on firmware V2.02 found these differences and requirements.
These checks are not a measurement of the output signal.
The reference is the [AFG-2000 Series User Manual](https://www.gwinstek.com/en-global/products/downloadSeriesDownNew/5374/442).

| Item | Behavior |
|------|----------|
| USB | USB CDC, ID 2184:001C. Windows uses its USB Serial Device driver. The baud rate has no effect; the server uses 115200 baud. |
| Commands | No leading colon. Commands end with LF; replies end with CR LF. |
| `APPLy` | Also enables the output. The driver does not use it. |
| Voltage queries | `AMPlitude?` returns Vpp into 50 Ohm, including when the display uses VRMS. `DCOffset?` returns volts into 50 Ohm. MIN/MAX queries use the same reference, also with `OUTPut:LOAD INF`. |
| Voltage writes | Use the selected load. With High-Z, writing 3.3 Vpp gives a raw amplitude query of 1.65 Vpp. The driver converts both amplitude and offset queries to the selected load. |
| Out-of-range values | The instrument clamps the value and puts an error in its queue. The driver checks limits and readback instead of accepting this change. |
| Error queue | `SYST:ERR?` returns one error code, then `No Error`. `*ESR?` and `*STB?` do not reply; the driver does not use them. |

### Rigol DP800 and DP900 series

The server identifies the model with `*IDN?`:

| Model              | Channel  | Voltage   | Current  | OVP max. | OCP max. |
|--------------------|----------|-----------|----------|----------|----------|
| DP832, DP832A      | CH1, CH2 | 0 to 30 V | 0 to 3 A | 33 V     | 3.3 A    |
| DP832, DP832A      | CH3      | 0 to 5 V  | 0 to 3 A | 5.5 V    | 3.3 A    |
| DP932A, DP932U     | CH1, CH2 | 0 to 32 V | 0 to 3 A | 35.2 V   | 3.3 A    |
| DP932A, DP932U     | CH3      | 0 to 6 V  | 0 to 3 A | 6.6 V    | 3.3 A    |
| DP932E             | CH1, CH2 | 0 to 30 V | 0 to 3 A | 33 V     | 3.3 A    |
| DP932E             | CH3      | 0 to 6 V  | 0 to 3 A | 6.6 V    | 3.3 A    |

The voltage and current limits are the rated values. The DP900 accepts 5 % more, but the server does not use this range.

**CAUTION:** While tracking is on, a change of CH1 also changes CH2. On the DP900, CH1 and CH2 can be connected
in series or in parallel (internal). In these modes, the server does not change CH1 or CH2.
It can always switch the outputs off. Set the mode on the instrument. `dp_status` shows the mode.

### Rigol DP: behavior of the instrument

| Item                                            | Behavior                                                       |
|-------------------------------------------------|----------------------------------------------------------------|
| `[:SOURce<n>]:VOLTage\|CURRent:PROTection:CLEar` | Clears the trip and switches the output on (tested on the DP832, documented for the DP900). The server does not use this command. |
| `:OUTPut:OVP:CLEar CHx`, `:OUTPut:OCP:CLEar CHx` | Clears the trip. The output stays off. The server uses these commands. |
| `:OUTP:OVP:CLE` (short form)                    | The DP832 firmware 00.01.14 does not accept it (error -113). |
| `:OUTP:PAIR?`                                   | Only the DP900. The DP832 does not reply. |
| Boolean replies                                 | DP800: `ON`/`OFF` and `YES`/`NO`. DP900: `1`/`0`. |

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

**CAUTION:** For a current measurement with the multimeter, the test leads must be in the current input,
and the multimeter must be in series with the load. If the leads are in the current input and you connect them
across a voltage source, the fuse of the multimeter blows. The server cannot detect the position of the test leads.

## Troubleshooting

| Problem                                    | Solution                                                                 |
|--------------------------------------------|--------------------------------------------------------------------------|
| The server does not find a USB instrument  | Install a USB-TMC driver. Use NI-VISA, or use WinUSB on Windows (for example, with Zadig). |
| The server does not find a LAN instrument  | Connect the instrument and the computer to the same network. Make sure that a firewall does not block TCP port 5555. |
| The server does not find a Manson HCS      | Set the MODE switch on the rear panel to "Normal". Make sure that the instrument is on. Make sure that the operating system shows a COM port for the CP210x bridge. If not, install the Silicon Labs CP210x driver. Close other programs that use the port. |
| The server does not find a Rigol DP        | Close other programs that use the instrument. Then try again. |
| The server does not find a multimeter      | Install a USB-TMC driver. The DM3058E has no LAN interface. Close other programs that use the instrument. |
| The server does not find an AFG-2105 | Connect USB and check for a GW Instek serial port (USB CDC). Do not replace its driver with a USB-TMC driver. Close other programs that use the port. |
| The server finds more than one instrument  | Use `discover_instruments`. Then use the appropriate `dp_connect`, `hcs_connect`, `dmm_connect` or `sig_connect` tool with the serial number. |

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
│   ├── power_supplies/
│   │   ├── rigol_dp.py        # Rigol DP800 and DP900 series
│   │   └── manson_hcs.py      # Manson HCS series
│   ├── multimeters/
│   │   └── rigol_dm3058.py    # Rigol DM3058 and DM3058E
│   └── signal_generators/
│       └── gwinstek_afg2000.py # GW Instek AFG-2105
└── tools/
    ├── power_supplies.py      # dp_* and hcs_* tools
    ├── multimeters.py         # dmm_* tools
    └── signal_generators.py   # sig_* tools
tests/
├── test_*.py                  # discovery, server and start of the server process
├── power_supplies/            # driver tests and simulated instruments
├── multimeters/
└── signal_generators/
```

### Add an instrument

1. Find the category of the instrument, for example `power_supplies`, `multimeters` or `oscilloscopes`.
   If the category folder does not exist, make it in `instruments/`, and make the module in `tools/`.
2. Add the driver to `instruments/<category>/`. Use the manufacturer and the series for the file name, for example `rigol_dp.py`.
3. Add a connection slot in `server.py`, if the instrument needs a new slot.
4. Add the tools to `tools/<category>.py`. If the module is new, import it at the end of `server.py`.
5. Add a simulated instrument (`tests/<category>/fake_*.py`) and the tests. Add a new category folder to `tests/conftest.py`.
6. Add the instrument to the device table in this README.

## License

lab-mcp uses the MIT License. Refer to [LICENSE](LICENSE).
