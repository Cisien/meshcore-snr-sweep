# Local hardware validation handoff

## ✅ VALIDATED (2026-09-15) — real two-radio results

Both radios flashed to the KISS modem and the full survey pipeline was run
against them. Summary of what was proven end-to-end:

| Objective | Result |
|-----------|--------|
| G2 flash (`Station_G2_kiss_modem` → `firmware-merged.bin` @ 0x0) | ✅ hash-verified |
| G3 flash (`Station_G3_ESP32_kiss_modem` → `firmware-merged.bin` @ 0x0) | ✅ hash-verified |
| KISS up on both (ping + version) | ✅ both `ping=True, version=1` |
| Survey config accepted: `SetRadio(902.2 MHz, 500 kHz, SF8, CR 4/5)` | ✅ both |
| **Primary** — noise sweep on G2 (902.2 MHz, 2 samples, 3 s) | ✅ real floor ≈ −65 dBm; wrote `data/noise_sweep-g2-*.json` |
| **Secondary** — 2-radio link test on 902.2 MHz, sizes 1/51/255, both directions | ✅ **6/6 delivered, median SNR +13.5 dB** |

Both objectives produced real data artifacts under `data/` and, for the link
test, exercised the full MQTT control plane (local mosquitto, QoS1:
coordinator→workers `cmd`, workers→coordinator `link/result` + `status`).

### Stable device identity (this bench)

The USB **product string changes** between bootloader ("ESP32 USB JTAG/serial
debug unit") and the booted KISS firmware (G3 → "Station G3 ESP32"), and
`/dev/ttyACM*` numbering shifts on re-flash. The only stable identifier is the
hardware serial number, embedded in the `/dev/serial/by-id` symlink name:

- **G2** serial `ECDA3B46B8F8` (by-id stays `...JTAG_serial_debug_unit_...`)
- **G3** serial `98A316CECBE0` (by-id becomes `...Station_G3_ESP32_...` on boot)

`scripts/flash_and_validate.sh` keys on the serial hex (overridable via
`G2_SERIAL`/`G3_SERIAL`), so it resolves correctly in both DFU and booted states.

### How to re-run the two-radio link test on this bench

```bash
cd /home/cisien/src/meshcore-snr-sweep
mosquitto -v -p 1883 &                                   # local broker
PYTHONPATH=py .venv/bin/python -m snr_sweep.link_worker --node g2 --radio g2 \
    --mqtt-host 127.0.0.1 --mqtt-port 1883 &
PYTHONPATH=py .venv/bin/python -m snr_sweep.link_worker --node g3 --radio g3 \
    --mqtt-host 127.0.0.1 --mqtt-port 1883 &
PYTHONPATH=py .venv/bin/python -m snr_sweep.link_coordinator \
    --node-a g2 --node-b g3 --freq-only 902.2 \
    --sizes 1,51,101,151,201,255 --trials 3 \
    --config config/local-test.toml
```

(See `config/local-test.toml` for the 127.0.0.1 broker override used above.)

---

## (History below — initial setup notes)

Status snapshot for validating the survey against the two real radios
(Station G2 + Station G3) on this host. Re-read this after the reboot.

## What's done

- `esptool` installed in the project venv: `.venv/bin/esptool`.
- KISS modem firmware source confirmed in the MeshCore repo
  (`examples/kiss_modem/`), built per-variant by PlatformIO.
  - G2 env:   `Station_G2_kiss_modem`       (board `station-g2`)
  - G3 env:   `Station_G3_ESP32_kiss_modem` (board `station-g3-esp32`)
  - Build via `sh build.sh build-firmware <env>` from the repo root
    (or `pio run -e <env>`). There is NO prebuilt KISS `.bin` in the
    GitHub releases — it must be built.
- 38/38 unit tests pass; all 4 CLI entry points resolve.

## Blocking issue: kernel lacks the CDC-ACM driver

This host runs a custom **7.1.9-arch1-2** kernel. Findings:

- `CONFIG_USB_ACM=m` — the ESP32 CDC-ACM serial driver is a **module**, not
  built into the running kernel.
- There is **no** `/lib/modules/7.1.9-arch1-2/` tree at all (only `7.2.2-arch1-1`
  is installed, and it does not contain `cdc_acm.ko` either).
- `modprobe cdc_acm` → `Module cdc_acm not found in directory /lib/modules/7.1.9`.
- Result: the two Espressif devices are seen on the USB bus but **no kernel
  driver binds**, so **no `/dev/ttyACM*` node is created**:
  - G2 = Bus 003 Port 002 (ID 303a:1001 "BQ Station G2", CDC-ACM)
  - G3 = Bus 003 Port 003 (ID 303a:1001 "ESP32 USB JTAG/serial debug unit")
  Both show `Driver=[none]` on their interfaces.

### User's stated fix

> "my system occasionally forgets it knows how to cdc_acm. I'll have to reboot."

So the plan is: **reboot** (expected to restore a working `cdc_acm`), then:

1. Verify: `lsmod | grep cdc_acm` and `ls /dev/ttyACM*`.
2. Confirm both radios enumerate. The G3 (ESP32-S3) exposes its native USB
   JTAG/serial on the same CDC device; the G2 is a classic CDC-ACM.

## Next steps after reboot

### A. Confirm the serial devices

```bash
ls -la /dev/ttyACM*
lsusb | grep -i espressif
# G2 = "BQ Station G2" ; G3 = "ESP32 USB JTAG/serial debug unit"
```

If a port still has `Driver=[none]`, load the module (post-reboot it should be
present): `sudo modprobe cdc_acm`. If `/lib/modules/$(uname -r)` is still missing,
install a matching kernel-modules package (Arch: `sudo pacman -S linux` for the
running kernel) — that is the root cause if it recurs.

### B. Build the two KISS firmwares (one-time)

```bash
cd /home/cisien/src
git clone --recursive https://github.com/meshcore-dev/MeshCore
cd MeshCore
# install PlatformIO if absent:
#   python3 -m pip install platformio   (or: pipx install platformio / see repo)
sh build.sh build-firmware Station_G2_kiss_modem
sh build.sh build-firmware Station_G3_ESP32_kiss_modem
# artifacts land under .pio/build/<env>/*.bin
#   G2  -> .pio/build/Station_G2_kiss_modem/firmware.bin
#   G3  -> .pio/build/Station_G3_ESP32_kiss_modem/firmware.bin
```

### C. Flash each radio

Both use standard ESP32 esptool flashing over their CDC serial port. The radios
"don't have any relevant firmware" so they should already be in / accept the
bootloader (esptool will reset-into-DTR/RTS). Run for each:

```bash
# G2 (ESP32 classic). Put into download mode if esptool can't connect:
#   hold BOOT, tap RESET, release BOOT, then run.
.venv-path/esptool.py --port /dev/ttyACM? baud 921600 \
  --before default_reset --after hard_reset \
  write_flash 0x0 <MeshCore>/.pio/build/Station_G2_kiss_modem/firmware.bin

# G3 (ESP32-S3, native USB). Download mode: hold BOOT while tapping the
# reset button on the G3's USB reset, or simply reconnect USB.
.venv-path/esptool.py --port /dev/ttyACM? baud 921600 \
  --before no_reset --after hard_reset \
  write_flash 0x0 <MeshCore>/.pio/build/Station_G3_ESP32_kiss_modem/firmware.bin
```

(Use the actual `/dev/ttyACM*` device that `lsusb` maps to each radio. The G3's
ESP32-S3 reset-into-download over native USB is reliable; if it stalls, unplug +
replug to retry.)

> NOTE: confirm the flash address (default `0x0`) and whether the built
> firmware is a "merged" image (bootloader+partition+app in one file, like the
> release `*-merged.bin`) vs a bare `firmware.bin` that also needs the
> bootloader/partition-table. Check the PlatformIO build flags / `build.sh`
> merge step before writing. `scripts/flash_and_validate.sh` below encodes the
> assumed layout and prints what it's doing.

### D. Validate with the survey client (the real goal)

For each radio, confirm KISS is up and the survey commands work:

```bash
cd /home/cisien/src/meshcore-snr-sweep
. .venv/bin/activate

# 1. basic KISS up
PYTHONPATH=py python -c "
from snr_sweep.kiss_client import KissClient
from snr_sweep.serial_port import find_port
with KissClient(find_port()) as c:
    print('ping', c.ping(), 'version', c.get_version())"

# 2. primary objective: single 100 kHz center, 2 quick samples, no MQTT
snr-noise-sweep --freq-only 902.2 --count 2 --interval 5 --no-mqtt

# 3. secondary objective smoke test (needs both radios, or loopback later):
snr-link-coordinator --node-a g2 --node-b g3 \
    --freq-only 902.2 --sizes 1,255 --trials 1   # needs workers running
```

## Open items / watch-outs

- **G2 vs G3 hardware differences.** G2 = `station-g2` board (ESP32 classic,
  CDC-ACM serial). G3 = `station-g3-esp32` (ESP32-S3, native USB). Confirm the
  G2 actually exposes a serial CDC that `esptool` can talk to — some G2 units
  need a physical USB-serial bridge. If `/dev/ttyACM*` shows only one device
  after reboot, that's the one to investigate.
- **KISS firmware variant correctness.** The KISS modem must match the radio's
  RF hardware (SX1262 for 900 MHz). Both `Station_G2_kiss_modem` and
  `Station_G3_ESP32_kiss_modem` are the correct targets.
- **500 kHz bandwidth + SF.** At 500 kHz BW the valid SF range is narrower.
  Default config uses SF8/CR 4/5; if `SetRadio` is rejected, drop SF or adjust
  (see `docs/RUN.md` failure table).
- **Regulatory.** This validation transmits LoRa in 902–928 MHz ISM. Keep TX
  power modest (default 7 dBm) and duration short.

## Files created / touched

- This file: `docs/VALIDATION.md`
- `scripts/flash_and_validate.sh` — one-shot build+flash+validate (assumes
  MeshCore is cloned to `~/src/MeshCore`).
