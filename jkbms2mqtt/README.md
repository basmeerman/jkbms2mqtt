# jkbms2mqtt — add-on internals

The Home Assistant add-on + Python package. **Setting it up?** Start at the
[repository README](../README.md); operating it is covered by the
[add-on manual](DOCS.md). This file is the developer/architecture reference.

A lightweight, fully-tested JK-BMS → MQTT bridge: Python 3.12 + asyncio on
`pymodbus`. Runs as a Home Assistant add-on **or** a standalone Docker
container. Speaks the official **JK BMS RS485 Modbus V1.0 / V1.1** protocol —
plain Modbus RTU on UART1, function 0x03 reads against register base `0x1200`,
function 0x10 writes against `0x1000–0x1118`.

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
no `FrameScanner`, no hand-rolled CRC. The MQTT topic-naming convention and the
two-tier write policy are documented in [`../MIGRATION.md`](../MIGRATION.md).

## Project layout

```
src/jkbms2mqtt/
├── app.py             # orchestrator (signal handling, MQTT setup, task spawn)
├── bms_runner.py      # per-BMS read/decode/publish loop
├── config.py          # pydantic Settings + load_settings()
├── dashboard.py       # Lovelace dashboard generator (auto-install + CLI)
├── entities.py        # declarative entity table → MQTT topic suffixes
├── mqtt.py            # HA Discovery payload + state-message builders
├── transport.py       # pymodbus client factory + connect-with-backoff
├── write_executor.py  # /set MQTT command consumer; write tier gating
└── protocol/
    ├── jk_modbus.py   # register-offset decoder for blocks 0x1200, 0x1400
    └── jk_settings.py # writable register table + value→words encoder
```

## Development

```bash
cd jkbms2mqtt
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest --cov --cov-branch --cov-fail-under=100   # 100% branch coverage gate
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
.venv/bin/mutmut run --paths-to-mutate src/jkbms2mqtt/protocol
```

Property tests use `hypothesis`; mutation testing uses `mutmut`. The full
contributor guide — CI jobs, the dashboard generator/drift checks, and the
protocol-verification docs — is in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## Running standalone (without Home Assistant)

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

Every option in `config.yaml` accepts a `JKBMS2MQTT_<UPPER>` env override;
`BMS_IDS` is comma-separated.

## Sources & acknowledgements

Independent implementation, cross-checked against — and standing on the work
of — several projects:

- **JIKONG (JK BMS) RS485 Modbus protocol, V1.0 / V1.1** — the protocol this
  add-on speaks. Every register address and encoding is grounded in JIKONG's
  official spec PDFs, mirrored for reference from
  [ciciban/jkbms-PB2A16S20P](https://github.com/ciciban/jkbms-PB2A16S20P) and
  [syssi/esphome-jk-bms](https://github.com/syssi/esphome-jk-bms) (Apache-2.0).
  The PDFs are © JIKONG Electronic Technology Co., Ltd.; see
  [`docs/specifications/README.md`](docs/specifications/README.md).
- **[phinix-org/Multiple-JK-BMS-by-Modbus-RS485](https://github.com/phinix-org/Multiple-JK-BMS-by-Modbus-RS485)**
  (Apache-2.0) — the wiring photos/diagrams under [`docs/wiring/`](docs/wiring/)
  are reused unmodified, and its YAML register definitions were one cross-check
  column in [`docs/FIELD_MATRIX.md`](docs/FIELD_MATRIX.md).
- **"JK-BMS wired management" add-on** by smartphoton
  ([domosimple.eu](https://domosimple.eu/forum/thread-917.html)) — the MQTT
  topic-naming convention follows this widely-used add-on so existing dashboards
  keep working (see [`../MIGRATION.md`](../MIGRATION.md)). No code included.
- **[syssi/esphome-jk-bms](https://github.com/syssi/esphome-jk-bms)**
  (Apache-2.0) — general JK-BMS protocol reference.

Only **facts** (register addresses, encodings, topic names) were drawn from the
references above — no source code was copied. The sole copied artifacts are the
phinix-org wiring images.

## License

MIT — see [`../LICENSE`](../LICENSE). The wiring photos/diagrams under
[`docs/wiring/`](docs/wiring/) are **not** MIT: reused from
[phinix-org/Multiple-JK-BMS-by-Modbus-RS485](https://github.com/phinix-org/Multiple-JK-BMS-by-Modbus-RS485)
(Copyright 2025 Radek) under the **Apache License 2.0**; a copy travels with
them in [`docs/wiring/LICENSE-Apache-2.0`](docs/wiring/LICENSE-Apache-2.0).
