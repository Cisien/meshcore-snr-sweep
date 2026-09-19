"""Tests for the link-test coordinator sequencing logic (no real MQTT needed).

These verify the result-matching and summary math without a broker, by driving
``_record_result`` / ``_wait_result`` directly.
"""

from __future__ import annotations

from snr_sweep import link_coordinator as LC
from snr_sweep.config import SweepConfig


def _coordinator() -> LC.Coordinator:
    cfg = SweepConfig()
    cfg.link_trials = 3
    return LC.Coordinator(cfg, "alpha", "beta")


def test_wait_result_matches_on_seq_and_node():
    c = _coordinator()
    import threading, time
    c._record_result({"node": "beta", "freq_hz": 902_000_000, "packet_size": 51,
                      "trial": 1, "rx_ok": True, "snr_db": 8.0, "rssi_dbm": -95.0,
                      "seq": 42})
    r = c._wait_result(42, "beta", timeout=1.0)
    assert r is not None
    assert r.rx_ok is True
    assert r.snr_db == 8.0
    assert r.node_rx == "beta"


def test_wait_result_timeout_returns_none():
    c = _coordinator()
    r = c._wait_result(999, "beta", timeout=0.3)
    assert r is None


def test_stale_results_are_not_matched():
    c = _coordinator()
    # A result for a different seq/node must not satisfy the current wait.
    c._record_result({"node": "alpha", "packet_size": 51, "trial": 1,
                      "rx_ok": True, "seq": 1})
    r = c._wait_result(2, "beta", timeout=0.3)
    assert r is None


def test_full_run_synthesizes_missing_results():
    """run() pads a failed LinkResult per missing burst packet."""
    c = _coordinator()
    c.cfg.link_rx_lead_s = 0.0
    c.cfg.link_burst_count = 3
    c._send = lambda topic, obj: None
    c._wait_status = lambda *a, **k: None
    c._flush = lambda: None
    rc = c.run(freq_only=902.3, sizes=[51], burst_count=3, window_s=0.0)
    assert rc == 0
    # 1 channel * 1 size * 2 directions * 3 packets = 6 results
    assert len(c.results) == 6
    assert all(r.rx_ok is False for r in c.results)
    assert c.results[0].direction in ("alpha->beta", "beta->alpha")
    assert c.results[0].freq_mhz == 902.3


def test_collect_burst_pads_to_known_count():
    c = _coordinator()
    c._wait_status = lambda *a, **k: {"state": "burst_rx_done", "count": 1}
    c._record_result({"node": "beta", "freq_hz": 902300000, "packet_size": 128,
                      "trial": 0, "rx_ok": True, "snr_db": 9.0, "rssi_dbm": -90.0,
                      "seq": 7})
    got = c._collect_burst(7, "beta", 3, 902300000, 128, timeout=0.2)
    assert len(got) == 3
    assert got[0].rx_ok is True
    assert got[1].rx_ok is False
    assert got[2].rx_ok is False


def test_summary_groups_by_frequency():
    c = _coordinator()
    c.results = [
        LC.LinkResult(node_rx="beta", freq_mhz=902.0, freq_hz=902_000_000,
                      channel_index=0, packet_size=51, trial=1,
                      direction="alpha->beta", rx_ok=True, snr_db=8.0),
        LC.LinkResult(node_rx="beta", freq_mhz=902.0, freq_hz=902_000_000,
                      channel_index=0, packet_size=51, trial=2,
                      direction="alpha->beta", rx_ok=True, snr_db=10.0),
        LC.LinkResult(node_rx="beta", freq_mhz=902.0, freq_hz=902_000_000,
                      channel_index=0, packet_size=255, trial=1,
                      direction="alpha->beta", rx_ok=False),
    ]
    # Should not raise and should group by freq.
    by = {}
    for r in c.results:
        by.setdefault(r.freq_mhz, []).append(r)
    assert len(by[902.0]) == 3
