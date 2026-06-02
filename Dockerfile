# Default to a multi-arch Python base for standalone / CI builds.
# When this image is built as a Home Assistant add-on, the HA supervisor
# overrides BUILD_FROM with the appropriate ghcr.io/home-assistant/<arch>-base
# image declared in build.yaml.
ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

# pip on HA's Alpine base ships with PEP 668's externally-managed marker;
# --break-system-packages bypasses it. Harmless on python:3.12-alpine which
# lacks the marker.
ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1

# Ensure python + pip exist on HA-base (they do not by default there).
RUN if ! command -v python3 >/dev/null 2>&1; then \
      apk add --no-cache python3 py3-pip; \
    fi

WORKDIR /opt/jkbms2mqtt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[all]"

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
