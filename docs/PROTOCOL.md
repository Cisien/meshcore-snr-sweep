# Protocol Reference

This is the authoritative wire-level reference for everything the survey sends
over the air and over MQTT. It has two parts:

1. **KISS modem commands** — how the Pi talks to a Station G3 running the stock
   MeshCore KISS Modem firmware (no custom firmware).
2. **MQTT topics & payloads** — how the Pi and the recorder exchange data.

Verified against the firmware source (`meshcore-dev/MeshCore` `examples/kiss_modem/`)
and the official protocol doc (`docs.meshcore.io/kiss_modem_protocol`).

---

## 1. KISS modem

### 1.1 Serial

* 115200 baud, 8N1, no flow control.
* Transport: USB CDC on the Station G3 (ESP32-S3). The Pi sees it as
  `/dev/ttyACM*`.
* The firmware's KISS env is `Station_G3_ESP32_kiss_modem`
  (`variants/station_g3_esp32/platformio.ini`). Flash via
  `flasher.meshcore.io` or `pio run -e Station_G3_ESP32_kiss_modem`.

### 1.2 Framing (standard KISS)

```
┌──────┬───────────┬──────────────┬──────┐
│ FEND │ Type Byte │ Data (esc)   │ FEND │
│ 0xC0 │  1 byte   │  0..N bytes  │ 0xC0 │
└──────┴───────────┴──────────────┴──────┘
```

* FEND = `0xC0`, FESC = `0xDB`, TFEND = `0xDC`, TFESC = `0xDD`.
* `FEND` in data → `FESC TFEND`; `FESC` in data → `FESC TFESC`.
* Type byte: bits 7-4 = port (0), bits 3-0 = command.

### 1.3 Commands the survey uses

Host → TNC (Data = raw LoRa packet, SetHardware = 0x06 extensions):

| Command | Type | Data | Purpose |
|---------|------|------|---------|
| Data    | 0x00 | 1..255 bytes | Queue one raw packet for TX |
| SetHardware | 0x06 | sub-cmd + data | MeshCore extension |

SetHardware sub-commands (first data byte):

| Sub-cmd | Value | Request data | Response sub-cmd | Response data |
|---------|-------|--------------|------------------|---------------|
| SetRadio        | 0x09 | freq(4 LE) + bw(4 LE) + sf(1) + cr(1) | 0xF0 OK / 0xF1 Err | – |
| SetTxPower      | 0x0A | power dBm (1)                          | 0xF0 OK / 0xF1 Err | – |
| GetRadio        | 0x0B | –                                      | 0x8B | freq(4)+bw(4)+sf(1)+cr(1) |
| GetCurrentRssi  | 0x0D | –                                      | 0x8D | rssi dBm (1, signed) |
| IsChannelBusy   | 0x0E | –                                      | 0x8E | 0x00 clear / 0x01 busy |
| GetAirtime      | 0x0F | packet len (1)                         | 0x8F | ms (4 LE) |
| GetNoiseFloor   | 0x10 | –                                      | 0x90 | noise floor dBm (2, int16 LE) |
| GetVersion      | 0x11 | –                                      | 0x91 | version (1) + reserved (1) |
| Ping            | 0x17 | –                                      | 0x97 | – |
| SetSignalReport | 0x19 | enable (1)                             | 0x9A | status (1) |

Response codes = request sub-cmd `| 0x80`; generic/unsolicited use `0xF0+`.

### 1.4 Unsolicited frames (TNC → host)

These arrive with type 0x06:

| Sub-cmd | Value | Data | Meaning |
|---------|-------|------|---------|
| TxDone  | 0xF8  | result (1): 0x00 fail / 0x01 success | after a Data frame is transmitted |
| RxMeta  | 0xF9  | SNR (1, signed, **value × 4**) + RSSI (1, signed dBm) | after each received raw packet |

**SNR scaling:** the firmware stores `SNR = raw × 4`, so divide the byte by 4
to get dB. A strong near link reads +SNR; a weak link reads −SNR.

**RX flow:** each received LoRa packet from the air is delivered as a
type-0x00 Data frame (the raw bytes, no metadata), immediately followed by an
0xF9 RxMeta. The Pi reads the packet length from the Data frame and the
SNR/RSSI from the following RxMeta. This is the core of the link test.

### 1.5 Key properties that make the survey work on stock firmware

* **Opaque TX.** A KISS Data frame is transmitted verbatim via
  `startSendRaw` with **no** MeshCore application-layer framing. Arbitrary
  1..255-byte payloads are legal — exactly what the packet-size link test
  needs. (The Data payload limit is `KISS_MAX_PACKET_SIZE = 255`.)
* **SetRadio sets center + bandwidth + SF + CR.** One call retunes the radio to
  any of the swept channels. The noise floor is re-estimated by the firmware
  (recalibrated every 2 s, AGC reset every 30 s), so 1-minute-interval
  sampling at a fixed center is valid.
* **No custom firmware.** Everything the survey needs is already exposed.

---

## 2. MQTT

All topics live under `meshcore/snr`. All payloads are JSON. QoS 1, not
retained. Timestamps are UTC ISO-8601 (`...Z`).

Broker: LAN-only, plain TCP (no TLS). Default address in
`config/sweep.toml` is `mqtt.local.cisien.com:1883` (see `docs/DEPLOY.md`).

### 2.1 Topic tree

| Topic | Publisher | Meaning |
|-------|-----------|---------|
| `meshcore/snr/{node}/noise/sample` | noise_sweep | one noise-floor reading |
| `meshcore/snr/{node}/noise/done`   | noise_sweep | a channel sweep completed |
| `meshcore/snr/{node}/link/result`  | link_worker (rx) | one link-test trial result |
| `meshcore/snr/{node}/status`       | noise_sweep / link_worker | liveness / state |
| `meshcore/snr/coordinator/{node}/cmd` | link_coordinator | `tx` / `rx` command to a worker |
| `meshcore/snr/coordinator/coordinator/heartbeat` | link_coordinator | optional liveness |

The recorder subscribes to `meshcore/snr/#` and persists every message.

> **Why the command topic is `coordinator/{node}/cmd` (no frequency segment):**
> a frequency-based segment would let one worker's subscription match the
> peer's command when the peer's node-id equals a frequency segment. The
> frequency is carried in the payload, so the segment is a fixed literal.

### 2.2 Payloads

**noise/sample** (per reading, every 60 s):
```json
{"ts":"2026-09-15T23:50:01.420Z","node":"alpha",
 "freq_mhz":902.2,"freq_hz":902200000,"channel_index":22,
 "sample_index":0,"noise_floor_dbm":-114,"rssi_dbm":-113.0,"busy":false}
```

**noise/done** (per channel, after all samples):
```json
{"ts":"...","channel_index":22,"center_mhz":902.2,"center_hz":902200000,
 "samples":10,"floor_min_dbm":-115.0,"floor_median_dbm":-113.6,
 "floor_mean_dbm":-113.4,"floor_max_dbm":-111.0,
 "rssi_median_dbm":-112.5,"busy_fraction":0.1,"first_ts":"...","last_ts":"..."}
```

**link/result** (per trial, from the receiving worker):
```json
{"ts":"...","node":"beta","freq_hz":902200000,"seq":42,
 "packet_size":101,"trial":1,"rx_ok":true,"snr_db":8.5,"rssi_dbm":-96.0,
 "tx_ok":null}
```
* `seq` is the coordinator's per-trial sequence number, used to match the
  result back to the trial. `snr_db`/`rssi_dbm` are null when `rx_ok` is false.

**coordinator/{node}/cmd** (`tx` or `rx`):
```json
{"cmd":"tx","freq_hz":902200000,"size":101,"trial":1,"seq":42}
{"cmd":"rx","freq_hz":902200000,"size":101,"trial":1,"seq":42,"window_s":6.0}
```

**status** (retained, latest state wins):
```json
{"ts":"...","node":"alpha","state":"ready"}
{"ts":"...","node":"beta","state":"rx_done","seq":42,"rx_ok":true}
```

### 2.3 Recorder storage schema

The recorder maps each topic to a table: `noise_sample`, `noise_done`,
`link_result`, `status`, `coord_cmd`, and `raw` (fallback). All tables carry a
`recv_ts` (epoch seconds) so you can reconstruct ordering even if a payload
`ts` is missing. See `docs/RUN.md` for the HTTP endpoints.
