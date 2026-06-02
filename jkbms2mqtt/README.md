# jkbms2mqtt

A lightweight, fully-tested **JK-BMS to MQTT bridge** for Home Assistant.
Python 3.12 + asyncio on top of `pymodbus`. Runs as a Home Assistant add-on
**or** as a standalone Docker container.

Speaks the official **JK BMS RS485 Modbus V1.0 / V1.1** protocol — plain
Modbus RTU on UART1, function 0x03 reads against the documented register
base `0x1200`, function 0x10 writes against `0x1000–0x1118`.

## Features

- **Two transports**:
  - **TCP gateway** — serial-over-IP bridge (Elfin EW10/EW11, USR-W630,
    Waveshare, ser2net, etc.) in transparent mode.
  - **USB serial** — USB-to-RS485 adapter (FT232, CH340, CP2102) attached to
    the HA host.
- **List-based multi-BMS configuration** — `bms_ids: [1, 2, 3, 4, 5, 6]`,
  non-contiguous slave IDs supported.
- **HA MQTT Discovery** — sensors, binary sensors, numbers, switches appear
  automatically.
- **Two-tier writes** — basic operational settings vs. safety-critical
  thresholds, gated independently and off by default.
- **Recording slider** — turn on to route every Modbus transaction's hex dump
  to the add-on log.
- **`force=True` logging** — DEBUG output reliably reaches the log regardless
  of which dependency configures logging first.
- **100% branch coverage** across all 11 source modules (347 tests),
  property tests via `hypothesis`, mutation testing via `mutmut`.

## Architecture

```
JK-BMS ─ RS485 ─ TCP gateway ─ TCP ─┐
            (or USB serial)         │
                                    │
                       ┌────────────▼──────────┐
                       │  pymodbus Async client │
                       │  (FramerType.RTU)      │
                       └────────────┬──────────┘
                                    │
                       ┌────────────▼──────────┐
                       │  BmsRunner per slave  │  reads three register
                       │  (0x1200, 0x1278,     │  blocks per cycle
                       │   0x12F0)             │
                       └────────────┬──────────┘
                                    │
                       ┌────────────▼──────────┐
                       │  jk_modbus decoder    │  pure functions on
                       │  → JkRealtime         │  register lists
                       └────────────┬──────────┘
                                    │
                       ┌────────────▼──────────┐
                       │  aiomqtt + HA         │
                       │  Discovery            │
                       └───────────────────────┘
```

`pymodbus` handles framing, CRC, timeout, transaction serialisation, and the
RTU-over-TCP pass-through pattern — so the add-on itself has no `BusArbiter`,
no `FrameScanner`, no hand-rolled CRC.

## MQTT topic naming

Each BMS publishes under `<bms_name>/<topic_suffix>` where `bms_name` is
`<bms_name_prefix>_<slave_id>` — default prefix `BMS`, so e.g. `BMS_1`,
`BMS_2`, …, `BMS_6`. HA's device id is `BMS_<n>_device`.

Topic suffixes follow the conventional naming used by other JK-BMS HA
integrations (`Total_Voltage_V`, `Cell_1_volt`, `Mos_temp`, `SOC_percentage`,
…) so existing dashboards / automations work unchanged.

## Quick start

### As a Home Assistant add-on

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Paste `https://github.com/basmeerman/jkbms2mqtt`, click **Add**.
3. The **jkbms2mqtt** add-on appears in the store; install it.
4. Configure under **Configuration**:
   - `transport: tcp_gateway` + `gateway_host` / `gateway_port` for your
     serial-over-IP device, or `transport: usb_serial` + `jkbms_path`
     (e.g. `/dev/ttyUSB0`).
   - `bms_ids: [1, 2, 3, 4, 5, 6]` for the actual DIP-switch addresses on
     your bus.
   - Leave `enable_basic_writes` / `enable_safety_writes` off until you've
     read the write-tier policy in [`DOCS.md`](DOCS.md#writes).
5. Start. The supervisor builds the image locally from the Dockerfile in this
   directory.
6. Devices appear under **Settings → Devices** as `BMS_1` … `BMS_<n>`.

### As a standalone container

```bash
docker run --rm \
  -e JKBMS2MQTT_TRANSPORT=tcp_gateway \
  -e JKBMS2MQTT_GATEWAY_HOST=192.168.1.100 \
  -e JKBMS2MQTT_GATEWAY_PORT=502 \
  -e JKBMS2MQTT_BMS_IDS=1,2,3,4,5,6 \
  -e JKBMS2MQTT_MQTT_HOST=mqtt.example.com \
  -e JKBMS2MQTT_MQTT_USER=hass \
  -e JKBMS2MQTT_MQTT_PASSWORD=secret \
  $(docker build -q .)
```

Every option in `config.yaml` accepts a `JKBMS2MQTT_<UPPER>` env override.
`BMS_IDS` is comma-separated.

## Development

```bash
cd jkbms2mqtt
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest --cov --cov-branch --cov-fail-under=100
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
.venv/bin/mutmut run --paths-to-mutate src/jkbms2mqtt/protocol
```

## Project layout

```
src/jkbms2mqtt/
├── app.py             # orchestrator (signal handling, MQTT setup, task spawn)
├── bms_runner.py      # per-BMS read/decode/publish loop
├── config.py          # pydantic Settings + load_settings()
├── entities.py        # declarative entity table → MQTT topic suffixes
├── mqtt.py            # HA Discovery payload + state-message builders
├── transport.py       # pymodbus client factory + connect-with-backoff
├── write_executor.py  # /set MQTT command consumer; write tier gating
└── protocol/
    ├── jk_modbus.py   # register-offset decoder for blocks 0x1200, 0x1400
    └── jk_settings.py # writable register table + value→words encoder
```

That's the whole source tree — the `pymodbus` pivot dropped roughly 900 lines
of hand-rolled protocol code (CRC, framer, scanner, ack parser, BusArbiter,
JSONL recorder, broadcast/CAN runners) compared to the previous revision.

## License

MIT. Wiring diagrams under `docs/wiring/` are reused from
[phinix-org/Multiple-JK-BMS-by-Modbus-RS485](https://github.com/phinix-org/Multiple-JK-BMS-by-Modbus-RS485)
under Apache 2.0.
