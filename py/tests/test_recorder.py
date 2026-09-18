"""Tests for the recorder's storage + topic routing (no broker needed)."""

from __future__ import annotations

import json
import tempfile

from snr_sweep import recorder as R


def _store(tmp) -> R.Store:
    return R.Store(tmp + "/snr.sqlite3")


def test_insert_and_query_noise_sample(tmp_path):
    s = _store(str(tmp_path))
    s.insert_noise_sample({"ts": "t", "node": "alpha", "freq_mhz": 902.0,
                           "freq_hz": 902_000_000, "channel_index": 0, "sample_index": 0,
                           "noise_floor_dbm": -112, "rssi_dbm": -113.0, "busy": False})
    rows = s.query("SELECT * FROM noise_sample WHERE node=?", ("alpha",))
    assert len(rows) == 1
    assert rows[0]["noise_floor_dbm"] == -112
    assert rows[0]["busy"] == 0
    s.close()


def test_route_dispatches_by_topic(tmp_path):
    s = _store(str(tmp_path))
    # noise sample
    R.route("meshcore/snr/alpha/noise/sample", s,
            json.dumps({"ts": "t", "node": "alpha", "freq_hz": 902_000_000,
                        "noise_floor_dbm": -110}).encode())
    # link result
    R.route("meshcore/snr/beta/link/result", s,
            json.dumps({"ts": "t", "node": "beta", "freq_hz": 902_100_000,
                        "packet_size": 255, "trial": 1, "rx_ok": True, "seq": 5}).encode())
    # coordinator command
    R.route("meshcore/snr/coordinator/alpha/cmd", s,
            json.dumps({"cmd": "tx", "freq_hz": 902_000_000, "size": 255,
                        "trial": 1, "seq": 1}).encode())
    # status
    R.route("meshcore/snr/alpha/status", s,
            json.dumps({"ts": "t", "node": "alpha", "state": "ready"}).encode())

    assert len(s.query("SELECT * FROM noise_sample")) == 1
    assert len(s.query("SELECT * FROM link_result")) == 1
    assert len(s.query("SELECT * FROM coord_cmd")) == 1
    assert len(s.query("SELECT * FROM status")) == 1
    ccmd = s.query("SELECT * FROM coord_cmd")[0]
    assert ccmd["node"] == "alpha"
    assert ccmd["freq_hz"] == 902_000_000
    assert ccmd["cmd"] == "tx"
    s.close()


def test_route_ignores_unknown_topics(tmp_path):
    s = _store(str(tmp_path))
    R.route("somewhere/else", s, b"not our topic")
    assert len(s.query("SELECT * FROM raw")) == 0
    s.close()


def test_route_keeps_raw_on_bad_json(tmp_path):
    s = _store(str(tmp_path))
    R.route("meshcore/snr/alpha/noise/unknown", s, b"not json")
    assert len(s.query("SELECT * FROM raw")) == 1
    s.close()


def test_summary_queries(tmp_path):
    s = _store(str(tmp_path))
    s.insert_noise_sample({"ts": "t", "node": "alpha", "freq_mhz": 902.0,
                           "freq_hz": 902_000_000, "channel_index": 0, "sample_index": 0,
                           "noise_floor_dbm": -112, "rssi_dbm": -113.0, "busy": False})
    s.insert_noise_sample({"ts": "t", "node": "alpha", "freq_mhz": 902.1,
                           "freq_hz": 902_100_000, "channel_index": 1, "sample_index": 0,
                           "noise_floor_dbm": -108, "rssi_dbm": -109.0, "busy": False})
    rows = s.query(
        "SELECT channel_index, freq_mhz, COUNT(*) n, MIN(noise_floor_dbm) fmin "
        "FROM noise_sample WHERE node=? GROUP BY channel_index, freq_mhz "
        "ORDER BY freq_mhz", ("alpha",))
    assert len(rows) == 2
    assert rows[0]["fmin"] == -112  # 902.0 is cleanest
    s.close()
