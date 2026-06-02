# jkbms2mqtt

This is a **Home Assistant add-on repository**. It contains one add-on,
**jkbms2mqtt** — a lightweight Python 3.12 asyncio JK-BMS to MQTT bridge with
Home Assistant Discovery.

## Add the repository to Home Assistant

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Paste `https://github.com/basmeerman/jkbms2mqtt` and click **Add**.
3. The **jkbms2mqtt** add-on appears in the store; install it.

The supervisor builds the add-on locally on your machine from the Dockerfile
in `jkbms2mqtt/` — no pre-built images are published.

## Layout

```
.
├── repository.yaml       # Home Assistant add-on repository manifest
└── jkbms2mqtt/           # the add-on itself + the Python project
    ├── config.yaml       # add-on configuration schema
    ├── Dockerfile        # supervisor builds this image locally
    ├── build.yaml        # per-arch base images
    ├── run.sh            # container entrypoint
    ├── pyproject.toml    # Python package
    ├── src/jkbms2mqtt/   # source
    ├── tests/            # 347 tests, 100% branch coverage
    ├── scripts/          # CI helper scripts
    ├── README.md         # full project documentation
    └── DOCS.md           # add-on docs shown in HA UI
```

See [`jkbms2mqtt/README.md`](jkbms2mqtt/README.md) for full project
documentation (features, architecture, transports, development workflow).

See [`MIGRATION.md`](MIGRATION.md) for the MQTT topic naming convention and
the write-tier safety policy.
