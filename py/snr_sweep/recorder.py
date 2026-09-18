"""Survey data RECORDER (homelab cluster service).

Subscribes to the whole ``meshcore/snr/#`` topic tree, persists every message
(noise samples, link results, status, and coordinator tx/rx commands) to a
durable SQLite database, and serves it back over a small HTTP API. This is the
single source of truth that makes the survey independently reproducible: anyone
can pull the raw data from the cluster without touching the radios.

Topics captured (see docs/PROTOCOL.md):

* ``meshcore/snr/{node}/noise/sample``    -> table ``noise_sample``
* ``meshcore/snr/{node}/noise/done``      -> table ``noise_done``
* ``meshcore/snr/{node}/link/result``     -> table ``link_result``
* ``meshcore/snr/{node}/status``          -> table ``status``
* ``meshcore/snr/coordinator/{freq}/{node}/cmd`` -> table ``coord_cmd``
* everything else under the prefix        -> table ``raw``

Run (in-cluster, or anywhere with broker + a writable DB path):

    python -m snr_sweep.recorder --host mqtt.local.cisien.com --port 1883 \
        --db /data/snr.sqlite3 --bind 0.0.0.0:8080
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import paho.mqtt.client as mqtt

from . import __version__
from .mqtt import make_client

log = logging.getLogger("snr_sweep.recorder")

TOPIC_PREFIX = "meshcore/snr"
DEFAULT_DB = "/data/snr.sqlite3"


def utc_now_iso() -> str:
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


# --------------------------------------------------------------------------- schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS noise_sample (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, node TEXT NOT NULL,
    freq_mhz REAL, freq_hz INTEGER, channel_index INTEGER, sample_index INTEGER,
    noise_floor_dbm REAL, rssi_dbm REAL, busy INTEGER,
    recv_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS noise_done (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, channel_index INTEGER, center_mhz REAL, center_hz INTEGER,
    samples INTEGER, floor_min_dbm REAL, floor_median_dbm REAL, floor_mean_dbm REAL,
    floor_max_dbm REAL, rssi_median_dbm REAL, busy_fraction REAL,
    first_ts TEXT, last_ts TEXT, recv_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS link_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, node TEXT NOT NULL,
    freq_mhz REAL, freq_hz INTEGER, channel_index INTEGER,
    packet_size INTEGER, trial INTEGER, direction TEXT,
    rx_ok INTEGER, snr_db REAL, rssi_dbm REAL, tx_ok INTEGER, seq INTEGER,
    recv_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, node TEXT NOT NULL, state TEXT,
    detail TEXT, recv_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coord_cmd (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, node TEXT NOT NULL, freq_hz INTEGER,
    cmd TEXT, size INTEGER, trial INTEGER, seq INTEGER, window_s REAL,
    recv_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL, ts TEXT, body TEXT, recv_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_noise_sample_freq ON noise_sample(node, freq_hz, ts);
CREATE INDEX IF NOT EXISTS idx_link_result_freq ON link_result(node, freq_hz, packet_size);
"""


class Store:
    """A tiny thread-safe SQLite store for the survey data."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def execute(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> List[dict]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # -- inserts -------------------------------------------------------------
    def insert_noise_sample(self, o: dict) -> None:
        self.execute(
            "INSERT INTO noise_sample(ts,node,freq_mhz,freq_hz,channel_index,"
            "sample_index,noise_floor_dbm,rssi_dbm,busy,recv_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (o.get("ts"), o.get("node"), o.get("freq_mhz"), o.get("freq_hz"),
             o.get("channel_index"), o.get("sample_index"), o.get("noise_floor_dbm"),
             o.get("rssi_dbm"), (1 if o.get("busy") else 0), int(time.time())))

    def insert_noise_done(self, o: dict) -> None:
        self.execute(
            "INSERT INTO noise_done(ts,channel_index,center_mhz,center_hz,samples,"
            "floor_min_dbm,floor_median_dbm,floor_mean_dbm,floor_max_dbm,"
            "rssi_median_dbm,busy_fraction,first_ts,last_ts,recv_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.get("ts"), o.get("channel_index"), o.get("center_mhz"),
             o.get("center_hz"), o.get("samples"), o.get("floor_min_dbm"),
             o.get("floor_median_dbm"), o.get("floor_mean_dbm"), o.get("floor_max_dbm"),
             o.get("rssi_median_dbm"), o.get("busy_fraction"), o.get("first_ts"),
             o.get("last_ts"), int(time.time())))

    def insert_link_result(self, o: dict) -> None:
        self.execute(
            "INSERT INTO link_result(ts,node,freq_mhz,freq_hz,channel_index,"
            "packet_size,trial,direction,rx_ok,snr_db,rssi_dbm,tx_ok,seq,recv_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.get("ts"), o.get("node"), o.get("freq_mhz"), o.get("freq_hz"),
             o.get("channel_index"), o.get("packet_size"), o.get("trial"),
             o.get("direction"), (1 if o.get("rx_ok") else 0), o.get("snr_db"),
             o.get("rssi_dbm"), (None if o.get("tx_ok") is None else (1 if o.get("tx_ok") else 0)),
             o.get("seq"), int(time.time())))

    def insert_status(self, o: dict) -> None:
        self.execute(
            "INSERT INTO status(ts,node,state,detail,recv_ts) VALUES(?,?,?,?,?)",
            (o.get("ts"), o.get("node"), o.get("state"),
             json.dumps({k: v for k, v in o.items() if k not in ("ts", "node", "state")}),
             int(time.time())))

    def insert_coord_cmd(self, topic: str, o: dict) -> None:
        self.execute(
            "INSERT INTO coord_cmd(ts,node,freq_hz,cmd,size,trial,seq,window_s,recv_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (o.get("ts"), o.get("node"), o.get("freq_hz"), o.get("cmd"),
             o.get("size"), o.get("trial"), o.get("seq"), o.get("window_s"),
             int(time.time())))

    def insert_raw(self, topic: str, o: Optional[dict], body: bytes) -> None:
        self.execute(
            "INSERT INTO raw(topic,ts,body,recv_ts) VALUES(?,?,?,?)",
            (topic, (o or {}).get("ts") if o else None,
             body.decode("utf-8", "replace"), int(time.time())))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------- routing

def route(topic: str, store: Store, payload: bytes) -> None:
    try:
        o = json.loads(payload.decode("utf-8"))
    except Exception:
        o = None
    if not topic.startswith(TOPIC_PREFIX + "/"):
        return
    if o is None:
        o = {}
    if not o.get("ts"):
        o["ts"] = utc_now_iso()
    parts = topic.split("/")
    # meshcore/snr/...
    if parts[2] == "coordinator":
        # meshcore/snr/coordinator/{node}/cmd
        if len(parts) >= 5 and parts[4] == "cmd":
            node = parts[3]
            o = o or {}
            o["node"] = node
            store.insert_coord_cmd(topic, o)
        else:
            store.insert_raw(topic, o, payload)
        return
    node = parts[2]
    if len(parts) >= 5:
        cat, sub = parts[3], parts[4]
        if cat == "noise" and sub == "sample":
            o = o or {}
            o.setdefault("node", node)
            store.insert_noise_sample(o)
            return
        if cat == "noise" and sub == "done":
            store.insert_noise_done(o or {})
            return
        if cat == "link" and sub == "result":
            o = o or {}
            o.setdefault("node", node)
            store.insert_link_result(o)
            return
    if len(parts) >= 4 and parts[3] == "status":
        o = o or {}
        o.setdefault("node", node)
        store.insert_status(o)
        return
    store.insert_raw(topic, o, payload)


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    store: Store = None  # type: ignore

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, indent=2).encode())

    def _csv(self, rows: List[dict], code: int = 200) -> None:
        if not rows:
            self._send(200, b"")
            return
        cols = list(rows[0].keys())
        out = [",".join(cols)]
        for r in rows:
            out.append(",".join(_csv_cell(r.get(c)) for c in cols))
        self._send(code, "\n".join(out).encode(), "text/csv")

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path.rstrip("/") or "/"
        try:
            if p == "/health":
                self._json({"ok": True, "version": __version__, "time": time.time()})
            elif p == "/":
                self._json(self._counts())
            elif p == "/noise":
                self._csv(self._query_noise(q))
            elif p == "/noise/summary":
                self._json(self._noise_summary(q))
            elif p == "/link":
                self._csv(self._query_link(q))
            elif p == "/link/summary":
                self._json(self._link_summary(q))
            elif p == "/status":
                self._csv(self._query_status(q))
            elif p == "/export":
                self._export(q)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # pragma: no cover
            self._json({"error": str(e)}, 500)

    # -- queries -------------------------------------------------------------
    def _query_noise(self, q) -> List[dict]:
        sql, params = "SELECT * FROM noise_sample", []
        where = []
        if q.get("node"):
            where.append("node=?"); params.append(q["node"])
        if q.get("freq_hz"):
            where.append("freq_hz=?"); params.append(int(q["freq_hz"]))
        if q.get("channel_index"):
            where.append("channel_index=?"); params.append(int(q["channel_index"]))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY freq_hz, ts"
        lim = min(int(q.get("limit", 5000)), 50000)
        sql += f" LIMIT {lim}"
        return self.store.query(sql, tuple(params))

    def _query_link(self, q) -> List[dict]:
        sql, params = "SELECT * FROM link_result", []
        where = []
        for key, col in (("node", "node"), ("freq_hz", "freq_hz"),
                         ("packet_size", "packet_size"), ("direction", "direction")):
            if q.get(key):
                where.append(f"{col}=?"); params.append(q[key])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY freq_hz, packet_size, trial, direction"
        lim = min(int(q.get("limit", 5000)), 50000)
        sql += f" LIMIT {lim}"
        return self.store.query(sql, tuple(params))

    def _query_status(self, q) -> List[dict]:
        sql, params = "SELECT * FROM status", []
        if q.get("node"):
            sql += " WHERE node=?"; params.append(q["node"])
        sql += " ORDER BY recv_ts DESC"
        lim = min(int(q.get("limit", 500)), 5000)
        sql += f" LIMIT {lim}"
        return self.store.query(sql, tuple(params))

    def _noise_summary(self, q) -> dict:
        node = q.get("node", "*")
        where, params = ("WHERE node=?", (node,)) if node not in ("*", None) else ("", ())
        rows = self.store.query(
            "SELECT channel_index, freq_mhz, COUNT(*) n, "
            "MIN(noise_floor_dbm) fmin, AVG(noise_floor_dbm) fmean, "
            "MAX(noise_floor_dbm) fmax, AVG(rssi_dbm) rmean, "
            "SUM(busy)*1.0/COUNT(*) busy "
            f"FROM noise_sample {where} "
            "GROUP BY channel_index, freq_mhz ORDER BY freq_mhz", params)
        ranked = sorted(rows, key=lambda r: (r["fmin"] if r["fmin"] is not None else 1e9))
        top = ranked[:5]
        return {"node": node, "channel_count": len(rows), "cleanest_top5": top, "all": rows}

    def _link_summary(self, q) -> dict:
        node = q.get("node", "*")
        where, params = ("WHERE node=?", (node,)) if node not in ("*", None) else ("", ())
        rows = self.store.query(
            "SELECT freq_hz, direction, packet_size, COUNT(*) n, SUM(rx_ok) ok, "
            "AVG(CASE WHEN rx_ok=1 THEN snr_db END) snr_med "
            f"FROM link_result {where} "
            "GROUP BY freq_hz, direction, packet_size ORDER BY freq_hz, direction, packet_size", params)
        return {"node": node, "groups": len(rows), "rows": rows}

    def _counts(self) -> dict:
        out = {}
        for t in ("noise_sample", "noise_done", "link_result", "status",
                  "coord_cmd", "raw"):
            try:
                out[t] = self.store.query(f"SELECT COUNT(*) c FROM {t}")
                out[t] = out[t][0]["c"] if out[t] else 0
            except Exception:
                out[t] = -1
        return out

    def _export(self, q) -> None:
        kind = q.get("kind", "noise")
        node = q.get("node", "*")
        if kind == "noise":
            rows = self._query_noise({"node": q.get("node"), "limit": "50000"})
            fname = f"noise_{node}_{time.strftime('%Y%m%d', time.gmtime())}.csv"
        elif kind == "link":
            rows = self._query_link({"node": q.get("node"), "limit": "50000"})
            fname = f"link_{node}_{time.strftime('%Y%m%d', time.gmtime())}.csv"
        else:
            self._json({"error": "kind must be noise|link"}, 400)
            return
        self._csv(rows)


def _csv_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if any(c in s for c in (',', '"', '\n')):
        s = '"' + s.replace('"', '""') + '"'
    return s


# --------------------------------------------------------------------------- server

class Recorder:
    def __init__(self, host: str, port: int, db: str, client_id: str = "snr-recorder",
                 username: Optional[str] = None, password: Optional[str] = None,
                 use_tls: bool = False):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.username = username
        self.password = password
        self.store = Store(db)
        self.mqtt = make_client(
            client_id=client_id, username=username, password=password, tls=use_tls,
        )
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message
        self.mqtt.on_disconnect = self._on_disconnect
        self._httpd: Optional[ThreadingHTTPServer] = None

    def _on_connect(self, c, _ud, _f, rc) -> None:
        c._up = (rc == 0)
        if rc == 0:
            c.subscribe(f"{TOPIC_PREFIX}/#", qos=1)
            log.info("recorder connected to %s:%d, subscribed %s/#", self.host, self.port, TOPIC_PREFIX)

    def _on_disconnect(self, c, *_a) -> None:
        c._up = False

    def _on_message(self, c, _ud, msg) -> None:
        try:
            route(msg.topic, self.store, msg.payload)
        except Exception:
            log.exception("failed to store message on %s", msg.topic)

    def start_mqtt(self, timeout: float = 10.0) -> None:
        self.mqtt.connect(self.host, self.port, keepalive=30)
        self.mqtt.loop_start()
        for _ in range(int(timeout * 10)):
            if getattr(self.mqtt, "_up", False):
                return
            time.sleep(0.1)
        raise ConnectionError(f"cannot reach broker {self.host}:{self.port}")

    def start_http(self, bind: str) -> None:
        host, port = bind.rsplit(":", 1)
        Handler.store = self.store
        self._httpd = ThreadingHTTPServer((host, int(port)), Handler)
        log.info("HTTP API on %s (endpoints: /health /noise /noise/summary /link "
                 "/link/summary /status /export?kind=noise|link / )", bind)

    def serve_forever(self) -> None:
        assert self._httpd is not None
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
        self.mqtt.loop_stop()
        self.mqtt.disconnect()
        self.store.close()


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--db", default=os.environ.get("SNR_DB", DEFAULT_DB))
    ap.add_argument("--bind", default=os.environ.get("SNR_BIND", "0.0.0.0:8080"))
    ap.add_argument("--client-id", default="snr-recorder")
    ap.add_argument("--username", default=os.environ.get("MQTT_USER"))
    ap.add_argument("--password", default=os.environ.get("MQTT_PASS"))
    ap.add_argument("--tls", action="store_true",
                    default=os.environ.get("MQTT_TLS", "").lower() in ("1", "true", "yes"),
                    help="connect to the broker over TLS (public broker)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    rec = Recorder(args.host, args.port, args.db, args.client_id, args.username,
                   args.password, use_tls=args.tls)
    try:
        rec.start_mqtt()
        rec.start_http(args.bind)
        rec.serve_forever()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        rec.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
