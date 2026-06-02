#!/usr/bin/with-contenv bashio
# Home Assistant addon entrypoint. The Python service reads /data/options.json
# directly via jkbms2mqtt.config.load_settings, so no env munging is needed here.
exec python3 -m jkbms2mqtt
