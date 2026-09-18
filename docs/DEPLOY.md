# Deployment Guide

Everything in this document targets the homelab Kubernetes cluster (Talos
control plane, `nas-nfs` / `proxmox-ceph-rbd` storage classes, `registry.cisien.com`
zot registry). The goal: a LAN-reachable MQTT broker + a recorder that persists
all survey data, plus flashed Station G3 radios.

## 0. Prerequisites

* `kubectl` and `talosctl` authenticated to the cluster.
* A zot registry reachable at `registry.cisien.com` (already in this cluster).
* Two Station G3 radios, each attached to a Raspberry Pi over USB.
* `podman` on the host you build the recorder image from (any node that can
  reach the registry).

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

## 2. Deploy the MQTT broker

```bash
kubectl apply -f kubernetes/mqtt-broker/manifest.yaml
kubectl -n mqtt rollout status deploy/mosquitto
kubectl -n mqtt get pods -l app.kubernetes.io/name=mosquitto
```

Verify from a node (or the Pi):

```bash
# in-cluster service name
kubectl run --rm -it --image=eclipse-mosquitto:2 mosq-test -- sh \
  -c "mosquitto_sub -h mosquitto.mqtt.svc.cluster.local -t '#' -C 1 -v & sleep 1; \
      mosquitto_pub -h mosquitto.mqtt.svc.cluster.local -t test/hello -m hi"
```

You should see `test/hello hi`.

### Exposing the broker to the LAN (for the PIs)

The PIs need to reach the broker by a plain LAN address (private IP, no TLS —
per the LAN-only convention). Three options, pick one:

1. **Cilium L2 (recommended if active).** Uncomment the `cilium.io/l2-priority`
   + `externalIPs` annotations on the `mosquitto-lan` Service and set a free LAN
   address. AdGuard already serves `*.local.cisien.com`; point
   `mosquitto.local.cisien.com` at that address (or use the bare IP).
2. **NodePort.** Use `http://<any-node-ip>:30883`. No DNS needed; just less
   stable than L2.
3. **Cluster-internal only.** Keep `mqtt_host = mosquitto.mqtt.svc.cluster.local`
   and run the Pi scripts *inside* the cluster (not recommended for a Pi on the
   bench).

Set `mqtt_host` in `config/sweep.toml` on each Pi to whatever you chose.

## 3. Build and deploy the recorder

Build the recorder image from this repo and push it to the cluster registry:

```bash
cd meshcore-snr-sweep
podman login registry.cisien.com        # if not already logged in
podman build -f scripts/Dockerfile \
  -t registry.cisien.com/meshcore/snr-recorder:latest .
podman push registry.cisien.com/meshcore/snr-recorder:latest
```

Deploy:

```bash
kubectl apply -f kubernetes/snr-recorder/manifest.yaml
kubectl -n mqtt rollout status deploy/snr-recorder
kubectl -n mqtt get pods -l app.kubernetes.io/name=snr-recorder
```

Verify the recorder connected to the broker:

```bash
kubectl -n mqtt logs -l app.kubernetes.io/name=snr-recorder \
  | grep -i "subscribed\|connected\|HTTP API"
```

You should see `recorder connected to ... subscribed meshcore/snr/#` and
`HTTP API on 0.0.0.0:8080`.

### Pulling data

The recorder's HTTP API is in-cluster at `http://snr-recorder.mqtt.svc:8080`
and on the LAN via the `snr-recorder-lan` NodePort (default `30884`):

```bash
# counts
curl http://<node-ip>:30884/
# noise summary (ranked by cleanest floor)
curl http://<node-ip>:30884/noise/summary?node=tower
# per-frequency link delivery
curl http://<node-ip>:30884/link/summary?node=field
# raw CSV export
curl -OJ 'http://<node-ip>:30884/export?kind=noise&node=tower'
```

Endpoints: `/health`, `/` (counts), `/noise`, `/noise/summary`, `/link`,
`/link/summary`, `/status`, `/export?kind=noise|link`. Details in `docs/RUN.md`.

## 4. Smoke test (end to end, no radios)

Confirm the recorder is actually persisting MQTT traffic by publishing a test
message from any node that can reach the broker:

```bash
kubectl run --rm -it --image=eclipse-mosquitto:2 pub-test -- sh -c \
 'mosquitto_pub -h mosquitto.mqtt.svc.cluster.local \
   -t meshcore/snr/alpha/noise/sample \
   -m "{\"node\":\"alpha\",\"freq_hz\":902000000,\"channel_index\":0,
        \"sample_index\":0,\"noise_floor_dbm\":-112,\"rssi_dbm\":-113,\"ts\":\"test\"}"'
# then
curl http://<node-ip>:30884/noise?node=alpha | head
```

The test reading should appear.

## 5. Teardown

```bash
kubectl delete -f kubernetes/snr-recorder/manifest.yaml
kubectl delete -f kubernetes/mqtt-broker/manifest.yaml
podman rmi registry.cisien.com/meshcore/snr-recorder:latest
```
