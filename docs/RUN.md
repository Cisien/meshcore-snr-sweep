# Run Guide

How to actually run the survey on the two PIs, read the results, and reproduce
anything. Assumes you have flashed G3s and an MQTT broker running (see
`docs/DEPLOY.md`). A recorder process is recommended but not required —
workers write local CSV/JSON regardless.

## 1. One-time Pi setup

On each Pi, clone this repo and create the venv:

```bash
git clone <this repo> meshcore-snr-sweep
cd meshcore-snr-sweep
python3 -m venv .venv
. .venv/bin/activate
pip install .          # pulls pyserial + paho-mqtt, installs the CLI entry points
```

Install the entry-point commands into PATH if you like:

```bash
# they land in .venv/bin; or:
hash -r
```

Confirm the radio is reachable (KISS modem up):

```bash
PYTHONPATH=py .venv/bin/python -c "
from snr_sweep.kiss_client import KissClient
from snr_sweep.serial_port import find_port
p = find_port()
with KissClient(p) as c: print('port', p, 'ping', c.ping(), 'ver', c.get_version())
"
```

Pick a node id per site (they must differ so the recorder can tell the two
locations apart). Use role labels that match the deployment: `node_id = "tower"`
on the tower site, `node_id = "field"` on the field site. Set `mqtt_host` to
the broker address.

## 2. Objective 1 — noise floor / ambient sweep (primary)

From the Pi with the radio you want to sweep:

```bash
# sanity check first: just print the plan, touch nothing
snr-noise-sweep --dry-run

# one channel, one burst, to confirm the whole path works end to end
snr-noise-sweep --device /dev/ttyACM0 --freq-only 902.3 --count 1

# the full sweep. Run in a terminal that stays alive; it publishes every
# reading to the recorder as it goes.
snr-noise-sweep --node tower --device /dev/serial/by-id/usb-...if00
```

The sweep first captures a **baseline** reading at the current deployment
settings (910.525 MHz / 62.5 kHz / SF7 / CR 4/5), then tunes the radio to each
100 kHz center (902.3 → 915.0, 128 channels). For each channel it settles 5 s
for the AGC, then takes 10 **bursts**; each burst is 10 noise samples 1 s apart
(averaged), with the bursts 10 s apart (≈ 195 s per channel). Every reading is
published to `meshcore/snr/{node}/noise/sample` and a per-channel summary to
`noise/done`. Local copies land in `data/` as CSV + JSON.

Run both PIs' sweeps (you can run them concurrently — they measure the same
band from two locations; the recorder separates them by `node`).

### Reading the noise results

From the recorder API (or the local JSON):

```bash
curl 'http://<recorder-host>:8080/noise/summary?node=tower'
```

`cleanest_top5` lists the centers with the lowest `fmin` (minimum floor across
the 10 readings). A quiet, well-antenna'd G3 should read roughly
**−110 to −120 dBm**. If you see −120 pinned, the AGC may be stuck or the
antenna/LNA is wrong (see §Hardware in `README.md`, and the pin map in
`research/meshcore-500khz-snr-sweep-HANDOFF.md`). Also look at
`fmax`/`rssi_median` for spike/interferer presence and `busy_fraction` for how
often the channel was in use.

**Pick candidate centers:** the centers with the lowest floor *and* few spikes
*and* low busy fraction. You'll typically shortlist 5–10 for the link test.

## 3. Objective 2 — coordinated link test (secondary)

This tests real two-way delivery and SNR across the packet-size range, on each
shortlisted center. It needs **both** G3s, one Pi each, and the coordinator.

### 3a. Start the two workers (one per Pi)

On the tower Pi (one radio attached):
```bash
snr-link-worker --node tower --device /dev/serial/by-id/usb-...if00
```
On the field Pi (one radio attached):
```bash
snr-link-worker --node field --device /dev/ttyACM0
```
Each prints `worker <id> ready (radio + MQTT)` when it has the radio and is
subscribed. Leave both running.

### 3b. Run the coordinator

The coordinator is pure MQTT (no radio) — run it from any machine that can
reach the broker.
It drives every (center, size, trial, direction) combination. `--node-a` /
`--node-b` must match the `--node` each worker was started with.

```bash
# quick: one center, two sizes, one trial, to confirm coordination works
snr-link-coordinator --node-a tower --node-b field \
    --freq-only 902.3 --sizes 1,255 --trials 1

# full: all 128 centers, ~50-byte step sizes, 3 trials, both directions
snr-link-coordinator --node-a tower --node-b field \
    --sizes 1,51,101,151,201,255 --trials 3
```

For each trial the coordinator: tells the **receiver** to tune + open a capture
window, waits `link_rx_lead_s` (3 s) so the window is open, then tells the
**transmitter** to send one `size`-byte packet, and records whether the
receiver saw it plus its SNR/RSSI. It tests **both directions** per trial by
default. Results go to `meshcore/snr/{node}/link/result` and are summarized at
the end (per-center delivered/total and median SNR). Local CSV + JSON land in
`data/`.

### 3c. Reading the link results

```bash
curl 'http://<recorder-host>:8080/link/summary?node=field'
curl 'http://<recorder-host>:8080/link?node=field&freq_hz=902300000&packet_size=101'
```

Key metrics per (center, direction, size): delivery rate (`ok/n`) and median
SNR. The **recommended MeshCore center** is one that (a) had a clean noise floor
from Objective 1 and (b) shows the best real-SNR / delivery across the sizes.

To render the completed run as an HTML chart (line: one per packet-size delivery
rate on the left % axis + median SNR on the right dB axis):

```bash
# render the most recent data/link_test-*.json
python -m snr_sweep.chart
# or a specific artifact -> data/link_test-tower-field-..._chart.html
python -m snr_sweep.chart --input data/link_test-tower-field-...json
```

## 4. Reproducing results

* All raw data is durable in the recorder's SQLite DB file. Anyone can pull it
  from the HTTP API without touching the radios.
* Local `data/*.csv` / `data/*.json` from each Pi are byte-for-byte reproducible
  given the same config and the same radios.
* To replay a full sweep deterministically, keep a copy of the `config/sweep.toml`
  you used; the channel list and sampling are derived from it, not from time.

## 5. Failure checklist

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ping=False` | Not KISS firmware, or wrong port | Re-flash KISS modem; `find_port()` |
| SetRadio rejected | SF/BW combo out of range at 500 kHz | Try SF7–SF11; check Semtech SF×BW limits |
| Floor pinned at −120 dBm | AGC stuck / no antenna | Check antenna + LNA; reset AGC (firmware does this every 30 s) |
| No link results | rx window open too late, or radios out of range | Increase `link_rx_lead_s`; reduce distance/power |
| `TxBusy` | A prior TX didn't finish | Bigger `link_tx_wait_s` |
| Recorder not storing | Not connected to broker | Check recorder process logs / `journalctl` |
