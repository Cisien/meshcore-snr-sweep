# meshcore-snr-sweep

Survey the 902–915 MHz ISM band at 500 kHz LoRa bandwidth to pick a clean
center frequency for a future **MeshCore** deployment.

* **Primary objective.** Characterize the noise floor of every 100 kHz center
  from 902.3 to 915.0 MHz (128 centers). RX-only; no transmitter needed.
* **Secondary objective.** Coordinate two Station G3 radios at different
  locations and measure real two-way delivery + SNR across packet sizes on the
  swept centers. This is the **link test** the rest of this guide covers.
* **Logging.** Every reading publishes to MQTT. A recorder service persists it
  to SQLite and serves an HTTP API, so results are durable and reproducible.

**No custom radio firmware.** The radios run the stock MeshCore **KISS Modem**
firmware, which already exposes everything the survey needs (retune
center/bandwidth/SF/CR, read noise floor + RSSI, transmit opaque packets,
report per-packet SNR/RSSI). All intelligence lives in the Python scripts on
the attached Raspberry Pi.

---

## How it works

```
   +------------------------------+              +------------------------------+
   | Site A  (Pi + G3 "tower")     |              | Site B  (Pi + G3 "field")     |
   |  snr-link-worker --node tower |              |  snr-link-worker --node field |
   +---------------|---------------+              +---------------|---------------+
                   | USB CDC serial: KISS modem                     |
                   v                                               v
        +--------------------------------------------------+  +--------------------------------------------------+
        | Station G3 (ESP32-S3, SX1262) KISS Modem          |  | Station G3 (ESP32-S3, SX1262) KISS Modem          |
        +--------------------------------------------------+  +--------------------------------------------------+
                   |   LoRa over the air (902–915 MHz)   |
                   |<------------------------------------|

   Both sites -> one MQTT broker -> snr-recorder (SQLite + HTTP API)
   The link_coordinator (no radio) drives every trial over MQTT.
```

* **link_worker** (one per site): holds a G3, executes `tx`/`rx` commands it
  receives over MQTT, publishes results.
* **link_coordinator** (no radio): sequences every
  (center × size × trial × direction) and aggregates the results.
* **recorder**: subscribes to `meshcore/snr/#`, persists everything to SQLite,
  serves an HTTP API for retrieval.

The one thing both sites **must** share is the same MQTT broker. The
coordinator sends commands to a worker through that broker. If a worker's
broker is not the coordinator's broker, the commands never arrive.

---

## Repository layout

```
config/
  sweep.toml             survey parameters (band, timing, sizes, node, broker)
  public.example.toml    public-broker TEMPLATE: TLS + username/password (committed)
  local-lan.toml         LAN-broker example (plain TCP, private IP)
  public.toml            <- YOUR public-broker config (gitignored; copy the example)
py/snr_sweep/
  kiss_client.py         low-level KISS modem client (framing + events)
  serial_port.py         USB CDC auto-discovery + by-id lookup
  config.py              sweep-parameter loading + channel derivation
  mqtt.py                topics / payloads / make_client / publisher
  linkproto.py           shared link-test command payloads
  link_worker.py         link-test WORKER       (CLI: snr-link-worker)
  link_coordinator.py    link-test SEQUENCER    (CLI: snr-link-coordinator)
  noise_sweep.py         noise-floor sweep      (CLI: snr-noise-sweep)
  recorder.py            data recorder          (CLI: snr-recorder)
  chart.py               render a link run as an HTML chart (python -m snr_sweep.chart)
py/tests/                unit + loopback tests (no hardware / broker needed)
kubernetes/
  mqtt-broker/manifest.yaml   mosquitto + namespace + PVC + LAN Service
  snr-recorder/manifest.yaml  recorder Deployment + PVC + HTTP + LAN Service
scripts/Dockerfile           recorder image
docs/
  PROTOCOL.md                KISS + MQTT wire reference (authoritative)
  DEPLOY.md                  broker + recorder + flash deployment
  RUN.md                     full run guide (both objectives)
```

---

## One-time setup (each Pi)

```bash
git clone <this repo> meshcore-snr-sweep && cd meshcore-snr-sweep
python3 -m venv .venv
. .venv/bin/activate
pip install .          # pulls pyserial + paho-mqtt, installs the CLI entry points
```

The entry points (`snr-link-worker`, `snr-link-coordinator`,
`snr-noise-sweep`, `snr-recorder`) land in `.venv/bin`. If your `.venv` is not
activated, call them as `python -m snr_sweep.<module>` instead.

Confirm a flashed G3 is reachable (KISS modem up):

```bash
PYTHONPATH=py .venv/bin/python -c "
from snr_sweep.kiss_client import KissClient
from snr_sweep.serial_port import find_port
p = find_port()
with KissClient(p) as c: print('port', p, 'ping', c.ping(), 'ver', c.get_version())"
```

Run the test suite (no hardware, no broker needed):

```bash
python -m pytest py/tests -q
```

### Choosing the right serial path

The G3 enumerates as `/dev/ttyACM*`, and the number **shifts every time the
radio reboots or you re-flash it**. Use the stable `by-id` path instead:

```bash
ls -l /dev/serial/by-id/ | grep -i meshcore
# usb-..._if00        <- use this full path as --device
```

### Which config to use

A config file sets the **broker** and the **survey parameters**. It does not
set the node role or the radio — those come from the CLI flags (`--node`,
`--device`). Pick the config that points at the broker you will actually reach:

| Config | Broker | When to use |
|--------|--------|-------------|
| `config/public.toml` | `mqtt.cisien.com:8883`, **TLS + username/password** | Reaching the broker across networks / over the internet. **Not committed** — copy `config/public.example.toml` → `public.toml` and fill in the password |
| `config/local-lan.toml` | `192.168.1.42:1883`, plain TCP | All on the LAN, no TLS |
| `config/sweep.toml` | your cluster's LAN split-DNS name | Default template; edit to your broker |

To point a worker at a broker that is **not** in any config file, use the
overrides instead of editing a file:

```bash
snr-link-worker --config config/sweep.toml \
  --mqtt-host mqtt.cisien.com --mqtt-port 8883 \
  --node field --device /dev/serial/by-id/...
```

---

## Running the link test

The link test has **three moving parts** and they must all agree on two things:
the **broker** and the **node names**.

* `--node` on each worker is its name. The recorder uses it to separate sites.
* `--node-a` / `--node-b` on the coordinator **must equal** the `--node` of the
  two workers, in either order. They drive the two radios by name.

Both workers and the coordinator connect to **the same broker** and read the
**same `mqtt_user` / `mqtt_pass` / `mqtt_tls`** settings. If the broker needs
TLS + credentials, use `config/public.toml` (or the `--mqtt-host/--mqtt-port`
flags) on all three.

Two common ways to run it:

---

### Mode A — one person runs the whole test

You hold both radios (two PIs, or one Pi with two G3s) and you run all three
processes. You already have an MQTT broker running. Use one config that points
at that broker for every process.

```bash
# 1. Broker is up. It is mqtt.cisien.com:8883 (TLS + auth) here.
#    config/public.toml already points at it.

# 2. Start the TOWER worker (Pi A / radio A).
snr-link-worker --config config/public.toml \
  --node tower --device /dev/serial/by-id/usb-..._tower_if00 --tx-power 14
#   -> "worker tower ready (radio + MQTT)". Leave it running.

# 3. Start the FIELD worker (Pi B / radio B, or a 2nd radio on the same Pi).
snr-link-worker --config config/public.toml \
  --node field --device /dev/serial/by-id/usb-..._field_if00 --tx-power 14
#   -> "worker field ready (radio + MQTT)". Leave it running.

# 4. Run the coordinator wherever you have a terminal that can reach the broker.
#    It needs no radio. --node-a/--node-b must match the two --node names above.
snr-link-coordinator --config config/public.toml \
  --node-a tower --node-b field
```

The coordinator prints an estimate first. The default sweep is the full band
(128 centers × `packet_sizes` × `link_trials` × 2 directions). To smoke-test
first, run it on one center:

```bash
snr-link-coordinator --config config/public.toml \
  --node-a tower --node-b field --freq-only 902.3 --sizes 1,255 --trials 1
```

All three write local copies to `data/` (CSV + JSON). The coordinator writes
`data/link_test-tower-field-<timestamp>.json` at the end.

---

### Mode B — distributed: a central runner + a colleague's field worker

You run the **coordinator** (and usually a **tower worker**) on your central
test runner. A **colleague runs the field worker** on their Pi, from a
different location. You coordinate **out of band** — before starting, agree on:

1. **The broker** everyone uses: host, port, and (if TLS) the username +
   password. It must be reachable from the colleague's site. For cross-network
   or internet reach, use the public broker `mqtt.cisien.com:8883`
   (`config/public.toml`). On one LAN, use the LAN broker.
2. **The node names**: `--node tower` (yours) and `--node field` (theirs).
3. **When to start**: the field worker must already be connected and idle
   before you launch the coordinator, or the first trials time out.

**Colleague (field site):**

```bash
# One-time: clone + venv + flash (see "One-time setup"). Then:
snr-link-worker --config config/public.toml \
  --node field --device /dev/serial/by-id/usb-..._field_if00 --tx-power 14
#   -> "worker field ready". Leave it running and idle.
```

**You (central test runner):**

```bash
# If you also operate the tower radio here:
snr-link-worker --config config/public.toml \
  --node tower --device /dev/serial/by-id/usb-..._tower_if00 --tx-power 14

# Then drive the test:
snr-link-coordinator --config config/public.toml \
  --node-a tower --node-b field
```

If you are the **only** one at the central runner and your colleague supplies
**both** radios' far side, just flip the roles: you run `--node tower` and they
run `--node field`. What matters is that the two `--node` values match the
coordinator's `--node-a`/`--node-b`, and that every process uses the same
broker + credentials.

**Verifying the colleague is connected before you start.** The coordinator does
not check liveness. Confirm the field worker is up first — from anywhere that
can read the recorder (or the broker) — that a `field` status is present:

```bash
curl 'http://<recorder>/status'            # should list a node "field" ready
# or, if no recorder, subscribe and watch for a status message:
mosquitto_sub -h mqtt.cisien.com -p 8883 --cafile /etc/ssl/certs/ca-certificates.crt \
  -u meshcore -P <pass> -t 'meshcore/snr/field/status' -v
```

Only start the coordinator once both `tower` and `field` are ready.

---

## Reading the results

**Recorder HTTP API** (see `docs/DEPLOY.md` for exposure):

```bash
curl 'http://<recorder>/link/summary?node=field'
curl 'http://<recorder>/link?node=field&freq_hz=902300000&packet_size=101'
```

**Chart** (one line per packet-size delivery rate on the left axis, median SNR
on the right axis):

```bash
python -m snr_sweep.chart                       # most recent data/link_test-*.json
python -m snr_sweep.chart --input data/link_test-tower-field-....json
```

Key metrics per (center, direction, size): delivery rate (`ok/n`) and median
SNR. The **recommended MeshCore center** is one with a clean noise floor
(Objective 1) **and** the best delivery + SNR across the sizes.

---

## Survey parameters (defaults in `sweep.toml`)

| Parameter | Default | Notes |
|-----------|---------|-------|
| Band | 902.3 → 915.0 MHz | US 902–928 MHz ISM |
| Center step | 100 kHz | 128 centers |
| LoRa bandwidth | 500 kHz | target MeshCore BW |
| SF / CR | 7 / 4/5 | survey target config |
| Noise sampling | 10 over 10 s | per channel |
| Link packet sizes | 1,51,101,151,201,255 | ~50-byte steps |
| Link trials | 3 | per size, per center |
| Directions | A→B and B→A | both hops characterized |

Tighten or relax the link timing with `link_settle_s`, `link_rx_lead_s`,
`link_rx_window_s`, `link_tx_wait_s`. The SX1262 retunes in tens of ms, so a
fast matrix (0.3 s settle, 0.7 s lead, 3.0 s window, 0.5 s tx-wait) is ~9 s per
channel.

---

## Hardware (Station G3)

* ESP32-S3, 240 MHz, 16 MB flash, OPI PSRAM; USB CDC on boot.
* LoRa: Semtech **SX1262** over SPI. Module `BQ35LORA900V1M` (900 MHz).
* The LNA must be on for a meaningful, repeatable noise floor. Keep link-test
  TX power modest (we use 14 dBm) so the two radios do not desense each other.

Full bring-up detail (SPI pin map, FEM, display, flash tooling) is in
`docs/DEPLOY.md` §1 and `docs/VALIDATION.md`.

---

## Reproducibility

* **No custom firmware** — only the stock KISS modem, versioned in the MeshCore
  repo, so the air interface is stable and documented (`docs/PROTOCOL.md`).
* **All data durable in the cluster** — the recorder's SQLite is the source of
  truth; pull it over the HTTP API without touching the radios.
* **Config-driven** — the channel list and sampling derive entirely from the
  config; keep a copy of the one you used.
* **Unit + loopback tests** — the KISS client is tested against a fake serial
  radio, and the coordinator/recorder are tested without a broker, so the logic
  is verified before the radios are attached.

---

## Regulatory note

902–915 MHz is inside the US 902–928 MHz ISM band. The noise sweep is RX-only.
The link test **transmits** LoRa and must stay within local rules (FCC Part 95
/ local). Keep the chosen 500 kHz channel inside the legal band and use modest
TX power.

---

## License

MIT.
