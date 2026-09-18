# Survey data recorder image (optional containerization).
#
# The recorder is a plain Python process (see docs/DEPLOY.md §3). This image
# is just a convenience if you want to run it in a container instead.
#
#   podman build -f scripts/Dockerfile -t snr-recorder:latest .
#   podman run -it --rm \
#       -e MQTT_HOST=192.168.1.1 -e MQTT_PORT=1883 \
#       -v $PWD/data:/data \
#       -p 8080:8080 snr-recorder:latest
#
# The container runs snr-recorder: it connects to the MQTT broker
# (MQTT_HOST/MQTT_PORT, plus MQTT_USER/MQTT_PASS/MQTT_TLS=1 for TLS+auth),
# persists meshcore/snr/# to a SQLite DB at SNR_DB, and serves the HTTP API
# on SNR_BIND.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml ./
COPY py ./py
RUN pip install --no-cache-dir .

# Non-root user; the DB lives on a mounted PVC at /data.
RUN useradd -u 10001 -m app && mkdir -p /data && chown -R app:app /data /app
USER app
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "snr_sweep.recorder"]
