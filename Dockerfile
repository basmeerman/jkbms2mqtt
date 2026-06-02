ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip

WORKDIR /opt/jkbms2mqtt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --break-system-packages --no-cache-dir ".[all]"

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
