# Contributing & developer guide

The single entry point for working *on* jkbms2mqtt. Using it instead? See the
[repository README](README.md) and the [add-on manual](jkbms2mqtt/DOCS.md).

## Repository layout

```
.
├── README.md            # front door (users)
├── CONTRIBUTING.md      # this file
├── MIGRATION.md         # MQTT topic naming + write-tier policy
├── repository.yaml      # Home Assistant add-on repository manifest
├── dashboards/          # Lovelace dashboard generator, sample output, docs
└── jkbms2mqtt/          # the add-on + the Python package
    ├── config.yaml      # add-on options schema (HA Supervisor)
    ├── Dockerfile       # supervisor builds this image locally
    ├── build.yaml       # per-arch base images
    ├── run.sh           # container entrypoint
    ├── DOCS.md          # add-on manual (shown in the HA UI)
    ├── README.md        # add-on internals / architecture
    ├── pyproject.toml   # Python package + tool config
    ├── src/jkbms2mqtt/  # source
    ├── tests/           # unit + property + integration; 100% branch coverage
    ├── scripts/         # dev/verification helpers + hardware captures
    └── docs/            # entity reference + protocol-verification docs
```

The supervisor builds the add-on locally from `jkbms2mqtt/Dockerfile` — no
pre-built images are published.

## Development setup

```bash
cd jkbms2mqtt
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Quality gates (all run in CI — see `.github/workflows/ci.yml`):

```bash
.venv/bin/pytest --cov --cov-branch --cov-fail-under=100   # 100% branch coverage
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
.venv/bin/mutmut run --paths-to-mutate src/jkbms2mqtt/protocol
```

CI jobs: **lint** (ruff + mypy), **test** (pytest + 100% coverage),
**dashboards** (lint the generator, assert the committed sample is in sync, and
the entity-drift check in both naming modes), and **docker-build** (validates
the supervisor's local-build path).

## Architecture

See [`jkbms2mqtt/README.md`](jkbms2mqtt/README.md) for the data-flow diagram,
the `pymodbus` transport model, and the source-tree map.

## Dashboards

The dashboard is generated, not hand-maintained. The generator lives in the
package (`src/jkbms2mqtt/dashboard.py`); the add-on imports it to auto-install
on startup, and `dashboards/generate.py` is a thin CLI wrapper.

- [`dashboards/README.md`](dashboards/README.md) — usage + the two entity-naming
  modes (`device` for fresh installs, `legacy` for sticky old ones).
- [`dashboards/PLAN.md`](dashboards/PLAN.md) — design of record.
- [`dashboards/TESTPLAN.md`](dashboards/TESTPLAN.md) — the live-HA verification
  procedure.
- `dashboards/check_entities.py` — reconciles the dashboard's entity references
  against the bridge's entity table; CI runs it in both naming modes so any
  added/removed/renamed entity fails the build.

## Protocol & verification

Every register offset and encoding is grounded in primary sources and audited:

- [`jkbms2mqtt/docs/ENTITIES.md`](jkbms2mqtt/docs/ENTITIES.md) — the full
  published-entity reference.
- [`jkbms2mqtt/docs/specifications/`](jkbms2mqtt/docs/specifications/) — JIKONG's
  RS485 Modbus V1.0 / V1.1 + CAN PDFs (primary sources).
- [`jkbms2mqtt/docs/FIELD_AUDIT.md`](jkbms2mqtt/docs/FIELD_AUDIT.md) — spec vs
  implementation vs hardware, field by field.
- [`jkbms2mqtt/docs/FIELD_MATRIX.md`](jkbms2mqtt/docs/FIELD_MATRIX.md) — spec vs
  other implementations, by register address.
- [`jkbms2mqtt/docs/VERIFICATION_RUNBOOK.md`](jkbms2mqtt/docs/VERIFICATION_RUNBOOK.md)
  — the full-block sweep + BLE cross-check procedure.
- `jkbms2mqtt/scripts/` — `dump_registers.py`, `dump_full_sweep.py`,
  `build_field_matrix.py`, and the real-hardware captures under
  `scripts/captures/` used as test fixtures.

## Sources & licensing

Facts only (register addresses, encodings, topic names) were drawn from the
reference projects credited in [`jkbms2mqtt/README.md`](jkbms2mqtt/README.md);
no source code was copied. Code is MIT ([`LICENSE`](LICENSE)); the phinix-org
wiring images under `jkbms2mqtt/docs/wiring/` are Apache-2.0.
