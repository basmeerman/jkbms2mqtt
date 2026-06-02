# jkbms2mqtt

A lightweight, fully-tested **JK-BMS to MQTT bridge** for Home Assistant.
Python 3.12 + asyncio. Runs as a Home Assistant add-on **or** as a standalone
Docker container.

## Features

- **Four transports**:
  - **TCP gateway** (RS485-over-IP via USR-W630, ser2net, etc.) — full read + write.
  - **USB serial** (RS485 via USB adapter; FTDI / CH340 / etc.) — full read + write.
  - **RS485 broadcast / listen** — read-only, multi-BMS demuxed by the BMS-reported unit number.
  - **CAN bus** (SocketCAN `can0` at 500 kbps) — read-only.
- **HA MQTT Discovery** — every sensor, binary sensor, number, and switch
  registers automatically.
- **Bidirectional writes** over TCP gateway and USB serial, with a proper bus
  arbiter, Modbus ack parsing, and structured error reporting on
  `<bms_name>/error`.
- **Two-tier write gating** — basic operational settings and safety-critical
  thresholds gated independently, both off by default.
- **Built-in traffic recorder** — JSONL byte logs that double as test fixtures.
- **100% branch coverage** on every module, **property tests** via
  `hypothesis`, and **mutation testing** via `mutmut`.

## Architecture sketch

```
JK-BMS ─ RS485 ─ TCP gateway ─ TCP ─┐
                                   │
                            ┌──────▼──────┐
                            │  Transport  │
                            └──────┬──────┘
                                   │
                            ┌──────▼──────┐    ┌────────────┐
                            │ BusArbiter  │◀───┤WriteExecutor│
                            │ (asyncio    │    └─────────────┘
                            │  Lock)      │
                            └──────┬──────┘
                                   │
                            ┌──────▼──────┐
                            │ FrameCodec  │
                            │ (pure fns)  │
                            └──────┬──────┘
                                   │
                            ┌──────▼──────┐
                            │ MQTTPublisher│
                            │ (aiomqtt +  │
                            │  HA disco)  │
                            └─────────────┘
```

Every layer is independently testable. The protocol layer is pure-functional
(no I/O), which is why mutation testing has good signal there.

## Quick start

### As a Home Assistant add-on

This repository is also a Home Assistant add-on repository — it contains
`repository.yaml` at the root and the add-on manifest in the `jkbms2mqtt/`
subdirectory.

1. In Home Assistant, open **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
   (in the newer "Apps" UI: **Settings → Apps → Repositories**).
2. Paste `https://github.com/basmeerman/jkbms2mqtt` and click **Add**.
3. The **jkbms2mqtt** add-on appears in the store; install it.
4. Configure under **Configuration**:
   - `gateway_host` / `gateway_port` of your serial-over-IP device, or
     `transport: usb_serial` + `jkbms_path: /dev/ttyUSB0`.
   - `jkbms_count` if you have a multi-BMS bus.
   - Leave `enable_basic_writes` / `enable_safety_writes` off until you have
     read [`MIGRATION.md`](MIGRATION.md#write-tiers).
5. Start. Devices auto-appear under **Settings → Devices**.

The add-on pulls the multi-arch image `ghcr.io/basmeerman/jkbms2mqtt:<version>`
published by this repo's CI — no local build needed.

### As a standalone container

```bash
docker run --rm \
  -e JKBMS2MQTT_TRANSPORT=tcp_gateway \
  -e JKBMS2MQTT_GATEWAY_HOST=192.168.1.100 \
  -e JKBMS2MQTT_GATEWAY_PORT=502 \
  -e JKBMS2MQTT_MQTT_HOST=mqtt.example.com \
  ghcr.io/basmeerman/jkbms2mqtt:latest
```

Every option in `config.yaml` accepts an `JKBMS2MQTT_<UPPER>` env override.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest --cov=jkbms2mqtt --cov-branch --cov-fail-under=100
.venv/bin/mutmut run --paths-to-mutate src/jkbms2mqtt/protocol
```

## Project layout

```
repository.yaml         # Home Assistant add-on repo manifest
jkbms2mqtt/             # the Home Assistant add-on (config.yaml + DOCS.md)
Dockerfile              # used by CI to build the multi-arch image
build.yaml              # per-arch base images for HA supervisor builds
run.sh                  # container entrypoint

src/jkbms2mqtt/
├── config.py          # pydantic Settings + load_settings()
├── app.py             # orchestrator wiring transport+codec+mqtt
├── bus_arbiter.py     # asyncio.Lock with inter-frame gap
├── mqtt.py            # HA Discovery + state publisher
├── write_executor.py  # WriteQueue consumer; ack parsing
├── recorder.py        # JSONL traffic logging + replay
├── entities.py        # single declarative entity table
├── listen_runner.py   # broadcast/listen mode runner
├── can_runner.py      # CAN bus runner
├── transport/
│   ├── base.py        # Transport Protocol
│   ├── backoff.py     # shared exponential backoff
│   ├── tcp_gateway.py
│   ├── usb_serial.py
│   └── can_bus.py
└── protocol/
    ├── modbus.py      # CRC16, frame encoders, ack parser
    ├── jk_frame.py    # JK-BMS reply parser (never raises)
    ├── scanner.py     # stream frame splitter for broadcast mode
    ├── decoder.py     # decoded telemetry / setup / device-info dataclasses
    ├── encoder.py     # Python value → register payload (with bounds)
    ├── registers.py   # declarative writable register table
    ├── capabilities.py # (transport, topology) → read/write matrix
    └── can_protocol.py # JK CAN ID decoders
```

## License

MIT.
