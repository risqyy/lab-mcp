# rigol-mcp

MCP-Server zur Steuerung von Rigol-Laborgeräten über SCPI/VISA (pyvisa + pyvisa-py).
Erstes unterstütztes Gerät: **DP832 / DP832A** Netzteil.

## Installation

```bash
poetry install
```

Für USB-TMC zusätzlich `poetry install -E usb` (braucht libusb bzw. unter Windows einen WinUSB/libusb-Treiber).
LAN (`TCPIP::<ip>::INSTR`) funktioniert mit pyvisa-py ohne weitere Treiber.

## Konfiguration

| Variable                | Bedeutung                                              | Default |
|-------------------------|--------------------------------------------------------|---------|
| `RIGOL_DP832_RESOURCE`  | VISA-Adresse, z. B. `TCPIP::192.168.1.50::INSTR`       | –       |
| `RIGOL_VISA_BACKEND`    | pyvisa-Backend (`@py` = pyvisa-py, `""` = NI-VISA)     | `@py`   |
| `RIGOL_VISA_TIMEOUT_MS` | I/O-Timeout                                            | `5000`  |

Ohne `RIGOL_DP832_RESOURCE` kann die Verbindung zur Laufzeit per Tool `dp832_connect` aufgebaut werden.

## Einbinden in Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "rigol": {
      "command": "poetry",
      "args": ["-C", "C:/Users/mail/Documents/Work/risqyy/rigol-mcp", "run", "rigol-mcp"],
      "env": { "RIGOL_DP832_RESOURCE": "TCPIP::192.168.1.50::INSTR" }
    }
  }
}
```

## Tools

| Tool                     | Funktion                                                     |
|--------------------------|--------------------------------------------------------------|
| `list_visa_resources`    | Sichtbare VISA-Geräte auflisten                              |
| `dp832_connect`          | Verbindung zu einer Adresse aufbauen, liefert `*IDN?`        |
| `dp832_identify`         | `*IDN?`                                                      |
| `dp832_status`           | Ausgang, Sollwerte, OVP/OCP, CV/CC je Kanal                  |
| `dp832_measure`          | Ist-Spannung, -Strom, -Leistung                              |
| `dp832_set`              | Spannung und/oder Strombegrenzung setzen                     |
| `dp832_output`           | Ausgang ein/aus                                              |
| `dp832_all_off`          | Alle Ausgänge aus                                            |
| `dp832_set_protection`   | OVP/OCP konfigurieren                                        |
| `dp832_clear_protection` | Ausgelöste OVP/OCP zurücksetzen                              |

Sollwerte werden vor dem Senden gegen die Kanalgrenzen geprüft (CH1/CH2: 30 V / 3 A, CH3: 5 V / 3 A).

## Tests

```bash
poetry run pytest
```

Die Tests laufen gegen ein simuliertes DP832 (`tests/fake_dp832.py`), es wird keine Hardware benötigt.
