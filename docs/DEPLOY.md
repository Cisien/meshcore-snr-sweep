# Deployment Guide

What you need to get the survey running: flashed radios, **one MQTT broker**,
and (optionally) a **recorder process** that persists the data. The recorder
is a plain Python process — no cluster required. The only thing that *might*
live in Kubernetes is the broker, and even that only if you don't already
have an MQTT broker somewhere.

## 1. Flash the radios (KISS modem firmware)

For each G3:

```bash
# Option A: browser flasher (simplest)
open https://flasher.meshcore.io   # select Station G3 ESP32 -> KISS Modem

# Option B: PlatformIO
git clone --recursive https://github.com/meshcore-dev/MeshCore
cd MeshCore
pio install
pio run -e Station_G3_ESP32_kiss_modem -t upload
```

Confirm the firmware: on the Pi, the G3 enumerates as a USB CDC serial port
(`/dev/ttyACM*`). A quick check:

```bash
# from this repo, with the venv
PYTHONPATH=py .venv/bin/python -c "
from snr_sweep.kiss_client import KissClient
from snr_sweep.serial_port import find_port
p = find_port()
print('port', p)
with KissClient(p) as c:
    print('ping', c.ping())
    print('version', c.get_version())
"
```

`ping=True` and a version number confirm the KISS modem is up.

## 2. Get an MQTT broker running

You need exactly one broker that all workers, the coordinator, and the
recorder can reach. Any of these works:

* **mosquitto on any machine** (a Pi, a server, this box):
  ```bash
  sudo apt install mosquitto        # Debian/Ubuntu
  # default listener: 1883, unauthenticated
  ```
* **mosquitto with TLS + username/password** for cross-network use — see
  `kubernetes/mqtt-broker/manifest.yaml` for a complete example of a TLS
  listener with a password file and an init container that handles
  mosquitto's `openat()` quirk with K8s Secret symlinks. You can lift that
  ConfigMap/Secret setup into a plain host-level `mosquitto.conf` too.
* **A broker you already have** — just point the configs at it.

Note on TLS + credentials: if the broker requires TLS and/or a
username/password, set `mqtt_tls = true` and `mqtt_user` / `mqtt_pass` in
the config file you pass to each process (see `config/public.example.toml`
for the shape). All processes that participate in the same test must agree
on the same broker address and the same credentials.

## 3. Run the recorder (optional, recommended)

The recorder is a normal Python process. Run it on any machine that can
reach the broker and has a writable path for the SQLite DB:

```bash
cd meshcore-snr-sweep
. .venv/bin/activate

# plain-TCP broker:
snr-recorder --host 192.168.1.1 --port 1883 \
    --db ./data/snr.sqlite3 --bind 0.0.0.0:8080

# TLS + credentials broker:
snr-recorder --host mqtt.example.com --port 8883 \
    --username meshcore --password <pass> --tls \
    --db ./data/snr.sqlite3 --bind 0.0.0.0:8080
```

You should see:

```
recorder connected to <host>:<port>, subscribed meshcore/snr/#
HTTP API on 0.0.0.0:8080 (endpoints: /health /noise /noise/summary /link
/link/summary /status /export?kind=noise|link / )
```

All survey data lands in the SQLite file you gave as `--db`. The HTTP API
lets you read it back without touching the radios:

```bash
# counts
curl http://<recorder-host>:8080/
# noise summary (ranked by cleanest floor)
curl http://<recorder-host>:8080/noise/summary?node=tower
# per-frequency link delivery
curl http://<recorder-host>:8080/link/summary?node=field
# raw CSV export
curl -OJ 'http://<recorder-host>:8080/export?kind=noise&node=tower'
```

Endpoints: `/health`, `/` (counts), `/noise`, `/noise/summary`, `/link`,
`/link/summary`, `/status`, `/export?kind=noise|link`. Details in `docs/RUN.md`.

If you want to containerize the recorder instead of running it directly,
`scripts/Dockerfile` builds an image that runs `snr-recorder` with the same
`MQTT_HOST` / `MQTT_PORT` / `SNR_DB` / `SNR_BIND` env vars.

## 4. Smoke test (end to end, no radios)

Confirm the recorder is actually persisting MQTT traffic by publishing a test
message from any machine that can reach the broker:

```bash
mosquitto_pub -h <broker> -p 1883 \
   -t meshcore/snr/alpha/noise/sample \
   -m '{"node":"alpha","freq_hz":902300000,"channel_index":0,
        "sample_index":0,"noise_floor_dbm":-112,"rssi_dbm":-113,"ts":"test"}'
# then
curl http://<recorder-host>:8080/noise?node=alpha | head
```

The test reading should appear.

## 5. Teardown

Stop the recorder process (`Ctrl-C`), stop the broker if you stood one up
for the test, and keep the SQLite DB — that is the durable record of the
run.
