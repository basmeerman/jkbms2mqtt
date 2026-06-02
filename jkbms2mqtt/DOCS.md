# jkbms2mqtt

Lightweight JK-BMS to MQTT bridge.

## Quick configuration

1. **Transport** — pick one:
   - `tcp_gateway` (default) — set `gateway_host` and `gateway_port` to your
     serial-over-IP bridge (USR-W630, ser2net, etc.).
   - `usb_serial` — set `jkbms_path` to e.g. `/dev/ttyUSB0`.
2. **MQTT** — defaults assume the Mosquitto add-on (`core-mosquitto`, port
   1883). Set `mqtt_user` / `mqtt_password` if your broker requires auth.
3. **`jkbms_count`** — the number of BMS units on the bus (1–15).
4. Leave `enable_basic_writes` and `enable_safety_writes` off until you have
   read the migration / write-policy notes in the repository's `MIGRATION.md`.

## Topology

- `master_poll` (default) — bridge polls the BMSes. Required for writes.
- `broadcast` — bridge listens to a bus where one BMS is the master.
  Multi-BMS auto-demuxing by unit number; read-only.
- `can` — SocketCAN read-only telemetry.

## Logs

The add-on writes JSON-structured logs to stdout; tail them from the add-on
"Log" tab. Set `log_level: debug` for verbose tracing.
