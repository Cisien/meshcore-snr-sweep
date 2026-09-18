"""MQTT transport for the survey.

All nodes and the recorder talk to one broker. This module defines the topic
layout, payload encoding, and a small publish helper. The topic tree is
documented in ``docs/PROTOCOL.md``.

Topics (all under ``meshcore/snr``):

* ``meshcore/snr/{node}/noise/sample``   a single noise-floor reading
* ``meshcore/snr/{node}/noise/done``     a channel sweep completed
* ``meshcore/snr/{node}/link/result``    one link-test trial (rx side)
* ``meshcore/snr/{node}/link/report``    aggregated per-size result
* ``meshcore/snr/{node}/status``         liveness / state
* ``meshcore/snr/coordinator/{freq_hz}/cmd``  coordinator -> nodes (control)

Every payload is JSON. Timestamps are UTC ISO-8601. Retained=0, QoS=1.
"""

from __future__ import annotations

import json
import ssl
import time
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt

TOPIC_PREFIX = "meshcore/snr"


def now_iso() -> str:
    """UTC timestamp with millisecond precision, e.g. 2026-09-15T23:50:01.420Z."""
    t = time.time()
    sec = int(t)
    millis = int(round((t - sec) * 1000))
    if millis == 1000:
        millis = 0
        sec += 1
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec))
    return f"{base}.{millis:03d}Z"


# --- topic builders -----------------------------------------------------------

def noise_sample_topic(node: str) -> str:
    return f"{TOPIC_PREFIX}/{node}/noise/sample"


def noise_done_topic(node: str) -> str:
    return f"{TOPIC_PREFIX}/{node}/noise/done"


def link_result_topic(node: str) -> str:
    return f"{TOPIC_PREFIX}/{node}/link/result"


def link_report_topic(node: str) -> str:
    return f"{TOPIC_PREFIX}/{node}/link/report"


def status_topic(node: str) -> str:
    return f"{TOPIC_PREFIX}/{node}/status"


def coordinator_cmd_topic(node: str) -> str:
    """The coordinator's command channel for one node (fixed segment, not freq).

    A frequency-based segment would make a worker's wildcard subscription
    (``coordinator/+/{node}/cmd``) also match the peer's command when the peer's
    node-id equals a frequency segment. The frequency is carried in the payload
    instead, so the segment is a fixed literal.
    """
    return f"{TOPIC_PREFIX}/coordinator/{node}/cmd"


def coordinator_status_topic(node: str = "coordinator") -> str:
    """Coordinator liveness / progress state (subscribed by ops, ignored by workers)."""
    return f"{TOPIC_PREFIX}/coordinator/{node}/status"


def coordinator_heartbeat_topic(coordinator: str = "coordinator") -> str:
    return f"{TOPIC_PREFIX}/{coordinator}/heartbeat"


def subscribe_for_node(client_id_node: str, coordinator: str = "coordinator") -> list:
    """Topics a node must subscribe to: its own control channel + coordinator heartbeat."""
    return [
        f"{TOPIC_PREFIX}/coordinator/{client_id_node}/cmd",
        f"{TOPIC_PREFIX}/{coordinator}/heartbeat",
    ]


# --- payload helpers ----------------------------------------------------------

def json_payload(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


# --- client factory -----------------------------------------------------------

def make_client(client_id: str, *, username: Optional[str] = None,
                password: Optional[str] = None, tls: bool = False,
                cafile: Optional[str] = None) -> "mqtt.Client":
    """Create a paho MQTT client, optionally with TLS and username/password auth.

    TLS verification uses the system CA bundle when ``cafile`` is unset, so the
    public broker's Let's Encrypt certificate validates with no extra config.
    """
    client = mqtt.Client(client_id=client_id)
    if username:
        client.username_pw_set(username, password)
    if tls:
        # paho-mqtt 1.x API: verify the broker cert against the system CA
        # bundle (or an explicit CA file). Server-cert-only; no client cert.
        client.tls_set(
            cert_reqs=ssl.CERT_REQUIRED,
            ca_certs=cafile,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
    return client


def noise_sample_payload(*, node: str, freq_mhz: float, freq_hz: int,
                        channel_index: int, sample_index: int,
                        noise_floor_dbm: float, rssi_dbm: float,
                        busy: Optional[bool] = None) -> Dict[str, Any]:
    return {
        "ts": now_iso(),
        "node": node,
        "freq_mhz": round(freq_mhz, 3),
        "freq_hz": int(freq_hz),
        "channel_index": int(channel_index),
        "sample_index": int(sample_index),
        "noise_floor_dbm": round(float(noise_floor_dbm), 1),
        "rssi_dbm": float(rssi_dbm),
        "busy": busy,
    }


def link_result_payload(*, node: str, freq_mhz: float, freq_hz: int,
                        channel_index: int, packet_size: int, trial: int,
                        rx_ok: bool, snr_db: Optional[float] = None,
                        rssi_dbm: Optional[float] = None,
                        tx_ok: Optional[bool] = None) -> Dict[str, Any]:
    return {
        "ts": now_iso(),
        "node": node,
        "freq_mhz": round(freq_mhz, 3),
        "freq_hz": int(freq_hz),
        "channel_index": int(channel_index),
        "packet_size": int(packet_size),
        "trial": int(trial),
        "rx_ok": bool(rx_ok),
        "snr_db": snr_db,
        "rssi_dbm": rssi_dbm,
        "tx_ok": tx_ok,
    }


# --- publisher ----------------------------------------------------------------

class MqttPublisher:
    """A minimal MQTT 3.1.1 publisher used by node scripts."""

    def __init__(self, host: str, port: int = 1883, client_id: str = "snr-node",
                 username: Optional[str] = None, password: Optional[str] = None,
                 use_tls: bool = False, keepalive: int = 30):
        self.host = host
        self.port = port
        self.keepalive = keepalive
        self.client = make_client(
            client_id=client_id,
            username=username,
            password=password,
            tls=use_tls,
        )
        self._connected = False

    def connect(self, timeout: float = 10.0) -> None:
        self.client.connect(self.host, self.port, keepalive=self.keepalive)
        self.client.loop_start()
        for _ in range(int(timeout * 10)):
            if self._connected:
                return
            time.sleep(0.1)
        raise ConnectionError(f"could not reach MQTT broker at {self.host}:{self.port}")

    def _on_connect(self, _client, _userdata, _flags, rc) -> None:
        self._connected = rc == 0

    def _on_disconnect(self, *_args) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def publish(self, topic: str, obj: Dict[str, Any], qos: int = 1, retain: bool = False) -> bool:
        info = self.client.publish(topic, json_payload(obj), qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        # Give the broker a moment; loop is running in a background thread.
        info.wait_for_publish(timeout=3.0)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def close(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
