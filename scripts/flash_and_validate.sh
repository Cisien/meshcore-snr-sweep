#!/usr/bin/env bash
# Build, flash, and validate the MeshCore KISS modem on a Station G2 and/or G3.
#
# Both radios are ESP32-S3. In bootloader/DFU mode they present as
# "ESP32 USB JTAG/serial debug unit", but once the KISS firmware boots the USB
# product string CHANGES (G3 -> "Station G3 ESP32"). The only stable identifier
# across DFU/firmware states is the hardware serial number, which is embedded in
# the /dev/serial/by-id symlink name. We therefore key on the serial hex:
#
#   G2 serial hex: ECDA3B46B8F8
#   G3 serial hex: 98A316CECBE0
# (inspect with: ls /dev/serial/by-id/ ; the hex is the part after the product
#  name. May differ on other machines.)
#
# Phases:
#   check     cdc_acm + list by-id serial devices
#   build     build both KISS firmwares with PlatformIO
#   flash     flash a radio:   --radio g2|g3   (uses the matching by-id path)
#   validate  KISS ping + quick noise read on the radio
#
# Usage:
#   ./flash_and_validate.sh check
#   ./flash_and_validate.sh build
#   ./flash_and_validate.sh flash    --radio g2
#   ./flash_and_validate.sh flash    --radio g3
#   ./flash_and_validate.sh validate --radio g2
#   ./flash_and_validate.sh validate --port /dev/serial/by-id/usb-...if00

set -euo pipefail

MC_REPO="${MC_REPO:-$HOME/src/MeshCore}"
PROJECT="${PROJECT:-$HOME/src/meshcore-snr-sweep}"
PIO="$PROJECT/.venv/bin/pio"
ESPTOOL="$PROJECT/.venv/bin/esptool"
BAUD="${BAUD:-921600}"
BYID=/dev/serial/by-id

log()  { printf '\033[1;34m[flash]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[flash]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[flash]\033[0m %s\n' "$*" >&2; exit 1; }

# Known serial-number -> radio map (hardware hex, no colons). Overridable via
# G2_SERIAL / G3_SERIAL. The by-id symlink embeds the serial as colons, so we
# search for the hex as a substring of the symlink basename.
G2_SERIAL="${G2_SERIAL:-ECDA3B46B8F8}"
G3_SERIAL="${G3_SERIAL:-98A316CECBE0}"

PORT="" ; RADIO=""

# Resolve a radio name to a live serial path (or use --port if given).
# Searches /dev/serial/by-id/ for a symlink whose name contains the serial hex.
byid_for_serial() {
  local serial="$1" f
  for f in "$BYID"/*; do
    local base; base=$(basename "$f")
    # match hex ignoring colons: compare uppercased, colons stripped
    if echo "$base" | tr -d ':' | tr '[:lower:]' '[:upper:]' | grep -q "$serial"; then
      echo "$f"; return 0
    fi
  done
  return 1
}

resolve_port() {
  if [ -n "$PORT" ]; then echo "$PORT"; return; fi
  case "$RADIO" in
    g2) local p; p=$(byid_for_serial "$G2_SERIAL");;
    g3) local p; p=$(byid_for_serial "$G3_SERIAL");;
    *) die "radio must be g2 or g3";;
  esac
  [ -n "$p" ] && [ -e "$p" ] || die "no live serial device for radio $RADIO (run 'check'; is it attached / in DFU / booted?)"
  echo "$p"
}

cmd_check() {
  log "kernel: $(uname -r)"
  log "cdc_acm module:"; ( lsmod | grep -q cdc_acm && echo "  loaded" || echo "  not listed (may be built-in / modprobe on demand)" )
  log "stable serial devices (by-id):"
  for f in "$BYID"/*; do [ -e "$f" ] && echo "  $(basename "$f") -> $(readlink -f "$f")" || echo "  (none)"; done
  log "espressif USB:"; lsusb 2>/dev/null | grep -iE "espressif|303a" || echo "  (none seen)"
}

cmd_build() {
  [ -d "$MC_REPO" ] || die "MeshCore repo not found at $MC_REPO (git clone --recursive https://github.com/meshcore-dev/MeshCore)"
  [ -x "$PIO" ] || die "pio not found at $PIO"
  cd "$MC_REPO"
  for env in Station_G2_kiss_modem Station_G3_ESP32_kiss_modem; do
    log "building $env ..."
    "$PIO" run -e "$env" || die "build failed for $env (see $MC_REPO/.pio)"
  done
  log "merged firmware artifacts:"
  ls -la .pio/build/Station_G2_kiss_modem/*-merged.bin 2>/dev/null
  ls -la .pio/build/Station_G3_ESP32_kiss_modem/*-merged.bin 2>/dev/null
}

cmd_flash() {
  [ -n "$RADIO" ] || [ -n "$PORT" ] || die "flash needs --radio g2|g3 (or --port)"
  case "$RADIO" in
    g2) env=Station_G2_kiss_modem ;; g3) env=Station_G3_ESP32_kiss_modem ;;
    *) [ -n "$PORT" ] && env="" || die "radio must be g2 or g3";;
  esac
  local p; p=$(resolve_port)
  log "flashing -> $p"
  if [ -n "$env" ]; then
    local merged; merged="$MC_REPO/.pio/build/$env/firmware-merged.bin"
    [ -f "$merged" ] || die "merged firmware missing: $merged (run 'build' first)"
    log "  image: $merged (write to 0x0)"
    "$ESPTOOL" --port "$p" --baud "$BAUD" --before no-reset --after hard-reset \
      write-flash 0x0 "$merged"
  else
    warn "no radio specified; specify --radio to auto-select the image. Use:"
    warn "  $ESPTOOL --port $p write-flash 0x0 <firmware-merged.bin>"
  fi
  log "flash complete. Re-run: validate --radio $RADIO"
}

cmd_validate() {
  local p; p=$(resolve_port)
  log "KISS ping/version + radio commands on $p"
  ( cd "$PROJECT" && . .venv/bin/activate
    PYTHONPATH=py python - "$p" <<'PY'
import sys
from snr_sweep.kiss_client import KissClient
p = sys.argv[1]
with KissClient(p) as c:
    print("  ping   =", c.ping())
    print("  version=", c.get_version())
    if c.ping():
        ok = c.set_radio(902_200_000, 500_000, 8, 5)
        print("  SetRadio(902.2MHz,500kHz,SF8,CR4/5) ->", ok)
        import time; time.sleep(1.0)
        print("  noise floor dBm =", c.get_noise_floor())
        print("  instant RSSI dBm =", c.get_current_rssi())
        print("  channel busy   ?", c.is_channel_busy())
PY
  )
}

usage() { sed -n '2,22p' "$0"; }

while [ $# -gt 0 ]; do case "$1" in
  check|build|flash|validate) CMD="$1"; shift;;
  --port) PORT="$2"; shift 2;;
  --radio) RADIO="$2"; shift 2;;
  -h|--help|*) usage; exit 0;;
esac; done
[ -n "${CMD:-}" ] || { usage; exit 1; }
"cmd_${CMD}"
