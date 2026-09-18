"""Loopback tests for KissClient using a fake serial port.

The fake implements just enough of the KISS modem behavior (frame decode +
canned responses) to exercise the real reader thread, dispatcher, and
synchronous request/response path. This is the closest thing to a real radio
without hardware.
"""

from __future__ import annotations

import time

from snr_sweep.kiss_client import (FEND, FESC, TFEND, TFESC, KissClient,
                                   KissError)


def decode_frames(buf: bytearray):
    """Yield (type, data) frames from a KISS byte stream, leaving remainder."""
    frames = []
    buf = bytearray(buf)
    while True:
        start = buf.find(bytes([FEND]))
        if start < 0:
            return frames, bytearray()
        end = buf.find(bytes([FEND]), start + 1)
        if end < 0:
            return frames, buf[start:]  # incomplete; keep from last FEND
        raw = bytearray()
        escaped = False
        for b in buf[start + 1:end]:
            if escaped:
                if b == TFEND:
                    raw.append(FEND)
                elif b == TFESC:
                    raw.append(FESC)
                escaped = False
            elif b == FESC:
                escaped = True
            else:
                raw.append(b)
        if raw:
            frames.append((raw[0], bytes(raw[1:])))
        buf = buf[end + 1:]
    # unreachable


class FakeKissRadio:
    """Simulates the G3 KISS modem enough for the client to talk to it.

    * Responds to SetHardware requests with canned responses.
    * Emits a TxDone when a Data (transmit) frame arrives.
    * ``push_tx`` lets a test inject an unsolicited rx packet + RxMeta.
    """

    def __init__(self):
        self.out = bytearray()
        self.rx = bytearray()
        self.transmitted = []  # raw payloads the host queued to transmit

    def read(self, n=512):
        if not self.out:
            time.sleep(0.005)
            return b""
        take = bytes(self.out[:n])
        del self.out[:n]
        return take

    def write(self, b):
        self.rx.extend(b)
        # Pull out any complete frames and respond.
        frames, self.rx = decode_frames(self.rx)
        for t, data in frames:
            self._respond(t, data)
        return len(b)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.out.clear()

    def close(self):
        pass

    # -- canned responses --------------------------------------------------
    def _emit(self, type_byte, data):
        out = bytearray([FEND, type_byte])
        for b in data:
            if b == FEND:
                out += (FESC, TFEND)
            elif b == FESC:
                out += (FESC, TFESC)
            else:
                out.append(b)
        out.append(FEND)
        self.out.extend(out)

    def _respond(self, t, data):
        if t == 0x00:  # host wants to transmit a raw packet
            self.transmitted.append(data)
            self._emit(0x06, bytes([0xF8, 0x01]))  # TxDone ok
            return
        if t != 0x06 or not data:
            return
        sub = data[0]
        if sub == 0x17:      # Ping
            self._emit(0x06, bytes([0x97]))
        elif sub == 0x09:    # SetRadio
            self._emit(0x06, bytes([0xF0]))
        elif sub == 0x0A:    # SetTxPower
            self._emit(0x06, bytes([0xF0]))
        elif sub == 0x10:    # GetNoiseFloor -> -110 dBm
            self._emit(0x06, bytes([0x90, 0x92, 0xFF]))
        elif sub == 0x0D:    # GetCurrentRssi -> -110 dBm
            self._emit(0x06, bytes([0x8D, 0x92]))
        elif sub == 0x0E:    # IsChannelBusy -> clear
            self._emit(0x06, bytes([0x8E, 0x00]))
        elif sub == 0x0F:    # GetAirtime -> 250 ms
            self._emit(0x06, bytes([0x8F, 0xFA, 0x00, 0x00, 0x00]))
        elif sub == 0x11:    # GetVersion -> 1
            self._emit(0x06, bytes([0x91, 0x01, 0x00]))
        elif sub == 0x19:    # SetSignalReport
            self._emit(0x06, bytes([0x9A, 0x01]))
        else:
            self._emit(0x06, bytes([0xF1, 0x05]))  # unknown

    # -- test helpers ------------------------------------------------------
    def push_rx_packet(self, payload: bytes, snr_x4: int, rssi: int):
        self._emit(0x00, payload)
        self._emit(0x06, bytes([0xF9, snr_x4 & 0xFF, rssi & 0xFF]))


def _open_client(fake: FakeKissRadio) -> KissClient:
    c = KissClient("fake")
    c._ser = fake
    c._running = True
    c._reader = None
    import threading
    c._reader = threading.Thread(target=c._reader_loop, daemon=True)
    c._reader.start()
    return c


def test_ping_roundtrip():
    fake = FakeKissRadio()
    c = _open_client(fake)
    try:
        assert c.ping() is True
    finally:
        c.close()


def test_set_radio_ok():
    fake = FakeKissRadio()
    c = _open_client(fake)
    try:
        assert c.set_radio(902_250_000, 500_000, 8, 5) is True
    finally:
        c.close()


def test_get_noise_floor():
    fake = FakeKissRadio()
    c = _open_client(fake)
    try:
        assert c.get_noise_floor() == -110
    finally:
        c.close()


def test_get_current_rssi():
    fake = FakeKissRadio()
    c = _open_client(fake)
    try:
        assert c.get_current_rssi() == -110.0
    finally:
        c.close()


def test_is_channel_busy():
    fake = FakeKissRadio()
    c = _open_client(fake)
    try:
        assert c.is_channel_busy() is False
    finally:
        c.close()


def test_get_airtime():
    fake = FakeKissRadio()
    c = _open_client(fake)
    try:
        assert c.get_airtime(255) == 250
    finally:
        c.close()


def test_transmit_emits_tx_done():
    fake = FakeKissRadio()
    c = _open_client(fake)
    try:
        c.transmit(bytes(51))
        assert len(fake.transmitted) == 1
        assert fake.transmitted[0] == bytes(51)
        assert c.wait_tx_done(timeout=2.0) is True
    finally:
        c.close()


def test_transmit_rejects_bad_size():
    fake = FakeKissRadio()
    c = _open_client(fake)
    try:
        try:
            c.transmit(b"")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        try:
            c.transmit(bytes(256))
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    finally:
        c.close()


def test_wait_rx_packet_and_meta():
    fake = FakeKissRadio()
    c = _open_client(fake)
    try:
        fake.push_rx_packet(bytes(101), snr_x4=40, rssi=-90)
        pkt = c.wait_rx_packet(timeout=2.0, expect_len=101)
        assert pkt is not None
        assert pkt.length == 101
        assert len(pkt.data) == 101
        meta = c.next_rx_meta(timeout=1.0)
        assert meta is not None
        assert meta.snr == 10.0  # 40 / 4
        assert meta.rssi == -90.0
    finally:
        c.close()


def test_request_timeout():
    fake = FakeKissRadio()
    # Nothing will answer this sub-cmd; force a short timeout.
    c = _open_client(fake)
    c.timeout = 0.3
    try:
        from snr_sweep.kiss_client import HW_GET_BATTERY
        try:
            c._request(HW_GET_BATTERY, b"", {0x93}, lambda p: p)
            raise AssertionError("expected KissError on timeout")
        except KissError:
            pass
    finally:
        c.close()


def test_dry_run_plan():
    """noise_sweep dry-run prints a plan without touching hardware or MQTT."""
    from snr_sweep import noise_sweep as ns
    from snr_sweep.config import load_config

    cfg = load_config(None, {"dry_run": True, "sample_count": 10})
    sweeper = ns.NoiseSweeper(cfg, publisher=None)
    rc = sweeper.run()
    assert rc == 0


def test_noise_sweep_channel_summary_math():
    from snr_sweep import noise_sweep as ns
    from snr_sweep.config import SweepConfig, Channel
    from snr_sweep.mqtt import noise_sample_payload

    cfg = SweepConfig()
    ch = Channel(index=0, center_mhz=902.0, center_hz=902_000_000)
    readings = [
        {"node": "alpha", "freq_mhz": 902.0, "freq_hz": 902_000_000,
         "channel_index": 0, "sample_index": i, "noise_floor_dbm": f,
         "rssi_dbm": r, "busy": b, "ts": "t"}
        for i, (f, r, b) in enumerate([
            (-112.0, -113.0, False), (-110.0, -111.0, False),
            (-114.0, -115.0, True),  (-111.0, -112.0, False),
        ])
    ]
    summary = ns.NoiseSweeper(cfg)._channel_summary(ch, readings)
    assert summary["samples"] == 4
    assert summary["floor_min_dbm"] == -114
    assert summary["floor_max_dbm"] == -110
    assert 0.0 <= summary["busy_fraction"] <= 1.0
    # A sample payload must round-trip the key fields.
    p = noise_sample_payload(node="alpha", freq_mhz=902.0, freq_hz=902_000_000,
                             channel_index=0, sample_index=0,
                             noise_floor_dbm=-112, rssi_dbm=-113.0, busy=False)
    assert p["freq_hz"] == 902_000_000
    assert p["noise_floor_dbm"] == -112


def test_link_directions_and_payloads():
    from snr_sweep.linkproto import default_directions, direction_label, rx_command, tx_command

    dirs = default_directions(["alpha", "beta"])
    assert dirs == [("alpha", "beta"), ("beta", "alpha")]
    assert direction_label("alpha", "beta") == "alpha->beta"

    tx = tx_command(freq_hz=902_250_000, size=255, trial=1, seq=7)
    assert tx["cmd"] == "tx" and tx["size"] == 255 and tx["seq"] == 7

    rx = rx_command(freq_hz=902_250_000, size=1, trial=2, seq=8, window_s=5.0)
    assert rx["cmd"] == "rx" and rx["size"] == 1 and rx["window_s"] == 5.0


def test_mqtt_topic_layout():
    from snr_sweep import mqtt as M
    assert M.noise_sample_topic("alpha") == "meshcore/snr/alpha/noise/sample"
    assert M.link_result_topic("beta") == "meshcore/snr/beta/link/result"
    # Fixed-segment command topic (no frequency segment).
    assert M.coordinator_cmd_topic("beta") == "meshcore/snr/coordinator/beta/cmd"
    subs = M.subscribe_for_node("alpha")
    assert "meshcore/snr/coordinator/alpha/cmd" in subs
    # A worker must NOT match the peer's command topic.
    assert "meshcore/snr/coordinator/alpha/cmd" not in M.subscribe_for_node("beta")


def test_now_iso_format():
    from snr_sweep.mqtt import now_iso
    s = now_iso()
    # e.g. 2026-09-15T23:50:01.420Z
    assert s.endswith("Z")
    assert "." in s and "T" in s
    assert len(s) == 24


def test_baseline_capture_reads_at_baseline_settings():
    """_capture_baseline tunes to the baseline settings and produces tagged readings."""
    from unittest import mock
    from snr_sweep import noise_sweep as ns
    from snr_sweep.config import SweepConfig

    cfg = SweepConfig()
    cfg.sample_count = 2             # small to keep it fast
    cfg.channel_duration_s = 0       # no gap between samples
    cfg.settle_s = 0
    cfg.baseline_enabled = True

    fake = mock.Mock()
    fake.set_radio.return_value = True
    fake.get_noise_floor.return_value = -110.0
    fake.get_current_rssi.return_value = -95.0
    fake.is_channel_busy.return_value = False

    sweeper = ns.NoiseSweeper(cfg, publisher=None, client=fake)
    readings = sweeper._capture_baseline(count=3)

    # 3 bursts requested -> 3 readings
    assert len(readings) == 3
    # Tuned to baseline frequency / bandwidth / SF / CR, not the sweep band.
    call = fake.set_radio.call_args[0]
    assert call[0] == 910525000                      # 910.525 MHz in Hz
    assert call[1] == cfg.baseline_bandwidth_hz      # 62.5 kHz
    assert call[2] == cfg.baseline_sf                # SF7
    assert call[3] == cfg.baseline_cr                # CR 4/5
    # Every reading is tagged as baseline and carries the baseline bandwidth.
    for r in readings:
        assert r["baseline"] is True
        assert r["bandwidth_hz"] == cfg.baseline_bandwidth_hz
        assert r["channel_index"] == -1


def test_baseline_disabled_skips_capture():
    from unittest import mock
    from snr_sweep import noise_sweep as ns
    from snr_sweep.config import SweepConfig

    cfg = SweepConfig()
    cfg.baseline_enabled = False
    fake = mock.Mock()
    sweeper = ns.NoiseSweeper(cfg, publisher=None, client=fake)
    assert sweeper._capture_baseline(count=3) == []
    fake.set_radio.assert_not_called()
