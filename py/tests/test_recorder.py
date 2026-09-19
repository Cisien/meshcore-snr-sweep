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


def test_main_smoke_without_network(tmp_path, monkeypatch):
    """Exercise main()'s CLI orchestration with every external component faked.

    Replaces R.Recorder with a finite fake that prevents the production
    recorder's side effects: no MQTT client, HTTP socket, background loop,
    SQLite Store, or DB file. Asserts the captured constructor values and
    the exact startup/shutdown lifecycle.
    """
    events = []
    db_path = tmp_path / "smoke.sqlite3"
    assert not db_path.exists()

    class FakeRecorder:
        def __init__(self, host, port, db, client_id="snr-recorder",
                     username=None, password=None, use_tls=False):
            events.append(
                ("init", host, port, db, client_id, username, password, use_tls))

        def start_mqtt(self):
            events.append(("start_mqtt",))

        def start_http(self, bind):
            events.append(("start_http", bind))

        def serve_forever(self):
            events.append(("serve_forever",))
            # return immediately so the test never blocks

        def stop(self):
            events.append(("stop",))

    monkeypatch.setattr(R, "Recorder", FakeRecorder)

    host = "mqtt.invalid"
    port = 1883
    client_id = "snr-sweep-smoke"
    username = "user-test"
    password = "pass-test"
    bind = "127.0.0.1:0"

    argv = [
        "--host", host,
        "--port", str(port),
        "--db", str(db_path),
        "--bind", bind,
        "--client-id", client_id,
        "--username", username,
        "--password", password,
        "--tls",
    ]

    rc = R.main(argv)

    assert rc == 0

    # constructor captured exactly the parsed values
    assert ("init", host, port, str(db_path), client_id, username,
            password, True) in events

    # exact lifecycle order: construct, start_mqtt, start_http, serve_forever, stop
    assert [e[0] for e in events] == [
        "init", "start_mqtt", "start_http", "serve_forever", "stop"]

    # start_http received the supplied bind
    assert ("start_http", bind) in events

    # no persistent storage side effect leaked through the fake
    assert not db_path.exists()
