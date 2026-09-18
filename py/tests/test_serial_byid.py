"""Tests for serial-number-based port resolution (stable across DFU/boot)."""

from __future__ import annotations

import os
import tempfile

from snr_sweep import serial_port as SP


def test_resolve_by_serial_from_byid(tmp_path):
    # Simulate /dev/serial/by-id layout with a real device behind the symlink.
    byid = tmp_path / "by-id"
    byid.mkdir()
    dev = tmp_path / "ttyACM0"
    dev.write_text("")
    link = byid / "usb-Espressif_Systems_Station_G3_ESP32_98A316CECBE0-if00"
    os.symlink("../ttyACM0", link)

    # G3 serial (colons stripped) -> the /dev/ttyACM0 path
    path = SP.resolve_by_serial("98A316CECBE0", by_id_dir=str(byid))
    assert path and path.endswith("ttyACM0"), path

    # G2 serial present but symlink uses the bootloader product name
    link2 = byid / "usb-Espressif_USB_JTAG_serial_debug_unit_EC:DA:3B:46:B8:F8-if00"
    dev2 = tmp_path / "ttyACM1"
    dev2.write_text("")
    os.symlink("../ttyACM1", link2)
    path2 = SP.resolve_by_serial("ECDA3B46B8F8", by_id_dir=str(byid))
    assert path2 and path2.endswith("ttyACM1"), path2


def test_resolve_by_serial_missing(tmp_path):
    byid = tmp_path / "by-id"
    byid.mkdir()
    assert SP.resolve_by_serial("DEADBEEF0000", by_id_dir=str(byid)) is None
