"""Unit tests for sweep-parameter derivation and KISS framing."""

from __future__ import annotations

from snr_sweep import config as C
from snr_sweep.kiss_client import KissClient


def test_default_channel_count_is_128():
    cfg = C.SweepConfig()
    chs = C.channels(cfg)
    # 902.3 -> 915.0 in 100 kHz steps inclusive = 128 centers.
    assert len(chs) == 128
    assert chs[0].center_mhz == 902.3
    assert chs[-1].center_mhz == 915.0


def test_channel_centers_step_100khz():
    cfg = C.SweepConfig()
    chs = C.channels(cfg)
    for a, b in zip(chs, chs[1:]):
        assert abs((b.center_mhz - a.center_mhz) - 0.1) < 1e-9
    assert chs[1].center_mhz == 902.4


def test_channel_center_hz_exact():
    cfg = C.SweepConfig()
    chs = C.channels(cfg)
    assert chs[0].center_hz == 902_300_000
    assert chs[1].center_hz == 902_400_000


def test_sweep_duration_default():
    cfg = C.SweepConfig()
    # 128 channels * (5 s settle + 10 s default sampling window)
    assert C.sweep_duration_s(cfg) == 128 * 15


def test_tx_power_override():
    """load_config: --tx-power override wins, None preserves the default."""
    cfg = C.load_config(None, {"tx_power_dbm": 16})
    assert cfg.tx_power_dbm == 16
    cfg = C.load_config(None, {"tx_power_dbm": 14})
    assert cfg.tx_power_dbm == 14
    # Omitting the key preserves the built-in default (7 dBm).
    cfg = C.load_config(None, {})
    assert cfg.tx_power_dbm == 7


def test_bandwidth_hz_property():
    cfg = C.SweepConfig()
    assert cfg.bandwidth_hz == 500_000


def test_packet_sizes_in_range():
    cfg = C.SweepConfig()
    assert all(1 <= s <= 255 for s in cfg.packet_sizes)
    assert cfg.packet_sizes[0] == 1
    assert cfg.packet_sizes[-1] == 255
    # ~50-byte steps: differences should be ~50 (last step smaller).
    diffs = [b - a for a, b in zip(cfg.packet_sizes, cfg.packet_sizes[1:])]
    assert all(40 <= d <= 60 for d in diffs[:-1])


def test_validate_rejects_bad_bw():
    cfg = C.SweepConfig()
    cfg.bandwidth_khz = 1234  # not a legal SX1262 BW
    try:
        C._validate(cfg)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_median_odd_even():
    assert C.median([5, 1, 3]) == 3
    assert C.median([4, 1, 3, 2]) == 2.5
    assert C.median([7]) == 7


def test_median_empty_is_nan():
    import math
    assert math.isnan(C.median([]))


def test_stdev_empty_zero():
    assert C.stdev([]) == 0.0
    assert C.stdev([5]) == 0.0


def test_encode_frame_basic():
    out = KissClient._encode_frame(0x06, b"\x09")
    assert out == bytes([0xC0, 0x06, 0x09, 0xC0])


def test_encode_frame_escaping():
    # Data containing FEND and FESC must be escaped.
    out = KissClient._encode_frame(0x00, bytes([0xC0, 0xDB, 0x01]))
    # FEND->DB DC, FESC->DB DD
    assert out == bytes([0xC0, 0x00, 0xDB, 0xDC, 0xDB, 0xDD, 0x01, 0xC0])


def test_i8_and_i16():
    from snr_sweep.kiss_client import i8, i16_le
    assert i8(0xFF) == -1
    assert i8(0x01) == 1
    assert i8(0x80) == -128
    assert i16_le(0xFF, 0xFF) == -1
    assert i16_le(0x00, 0x01) == 256
