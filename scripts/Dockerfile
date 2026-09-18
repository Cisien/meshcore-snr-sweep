# Survey data recorder image.
#
# Build and push to the cluster's zot registry (see docs/DEPLOY.md):
#
#   podman build -f scripts/Dockerfile -t registry.cisien.com/meshcore/snr-recorder:latest .
#   podman push registry.cisien.com/meshcore/snr-recorder:latest
#
# The image runs snr-recorder: it connects to the MQTT broker (MQTT_HOST/MQTT_PORT),
# persists meshcore/snr/# to a SQLite DB at SNR_DB, and serves the HTTP API on SNR_BIND.

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
