"""USB serial port discovery for the MeshCore Station G3 (ESP32-S3 CDC).

The G3 exposes a USB CDC-ACM serial interface when running the KISS modem
firmware. This helper finds it without hard-coding a device path, so the same
scripts work across different PIs and cable positions.

Precedence:
1. An explicit ``serial_port`` from config / CLI (a device path or ``VID:PID``).
2. An auto-detected CDC port (preferring Espressif VID 0x303A, then any
   CDC-ACM/ACM0 port, then the first serial port that is not a known system
   console).
"""

from __future__ import annotations

import logging
from typing import List, Optional

import serial.tools.list_ports
from serial.tools.list_ports_common import ListPortInfo

log = logging.getLogger("snr_sweep.serial")

EXPRESSIF_VID = 0x303A
# Ports that are almost never the radio.
_SKIP_KEYWORDS = ("console", "ttyS0", "ttyS1")


def list_candidates() -> List[ListPortInfo]:
    ports = list(serial.tools.list_ports.comports())
    return sorted(ports, key=_score, reverse=True)


def resolve_port(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve a serial port to a device path.

    ``explicit`` may be:
    * a device path (``/dev/ttyACM0``) -> returned as-is
    * ``VID:PID`` (``303A:0000``) -> the first matching port
    * ``None`` -> the top auto-detected candidate, or None
    """
    if explicit:
        explicit = explicit.strip()
        if explicit.startswith("/"):
            return explicit
        if ":" in explicit:
            want = explicit.upper().split(":", 1)
            try:
                vid, pid = (int(w, 0) for w in want)
            except ValueError:
                raise ValueError(f"cannot parse VID:PID from {explicit!r}")
            for p in list_candidates():
                if p.vid == vid and (pid is None or p.pid == pid):
                    return p.device
            raise LookupError(f"no serial port matches {explicit}")
        return explicit

    for p in list_candidates():
        log.debug("candidate: %s (%s)", p.device, p.description)
    candidates = list_candidates()
    if not candidates:
        return None
    top = candidates[0]
    # Only trust the auto pick if it has a meaningful score (USB CDC-ish).
    if _score(top) <= 0:
        log.warning("no clear CDC serial port found; candidates: %s",
                    [c.device for c in candidates])
        return top.device if len(candidates) == 1 else None
    log.info("auto-selected serial port %s (%s)", top.device, top.description)
    return top.device


def resolve_by_serial(
    serial_hex: str,
    *,
    by_id_dir: str = "/dev/serial/by-id",
) -> Optional[str]:
    """Return the device path for the radio whose hardware serial number matches.

    ``serial_hex`` is the Espressif hardware serial without colons, e.g.
    ``"ECDA3B46B8F8"``. This is the only identifier that is stable across
    bootloader ("USB JTAG/serial debug unit") and booted ("Station G3 ESP32")
    USB product-name changes.

    Resolution order:
    1. ``/dev/serial/by-id`` symlinks (the canonical stable path on Linux).
    2. Fallback: scan ``comports()`` for a matching ``serial_number``.
    """
    import os, re

    serial_hex_norm = serial_hex.upper().replace(":", "")

    if os.path.isdir(by_id_dir):
        for name in sorted(os.listdir(by_id_dir)):
            path = os.path.join(by_id_dir, name)
            if not os.path.islink(path) and not os.path.isfile(path):
                continue
            name_norm = re.sub(r"[^A-Za-z0-9]", "", name).upper()
            if serial_hex_norm in name_norm:
                return os.path.realpath(path)

    for p in list_candidates():
        if p.serial_number and serial_hex_norm in p.serial_number.upper().replace(":", ""):
            return p.device

    return None


def _score(p: ListPortInfo) -> int:
    desc = f"{p.manufacturer or ''} {p.product or ''} {p.hwid or ''}".lower()
    s = 0
    if p.vid == EXPRESSIF_VID:
        s += 100
    if "cdc" in desc or "acm" in desc:
        s += 40
    if p.device.startswith("/dev/ttyACM"):
        s += 25
    for kw in _SKIP_KEYWORDS:
        if kw in (p.device or "").lower():
            s -= 50
    return s


def find_port(explicit: Optional[str] = None, required: bool = True) -> Optional[str]:
    """Convenience wrapper that raises a helpful error when nothing is found."""
    port = resolve_port(explicit)
    if port is None and required:
        raise LookupError(
            "Could not find a MeshCore radio on the USB bus. "
            "Plug in the Station G3 and confirm it enumerates as a CDC "
            "serial port, or set serial_port in the config."
        )
    return port
