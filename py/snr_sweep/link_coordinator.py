"""Secondary objective: link-test COORDINATOR.

A pure-MQTT sequencer (no radio). It drives the (frequency, size, direction)
matrix for two G3 nodes. Each cell is a **burst**: the receiver opens one
capture window, the transmitter fires a fixed number of packets back-to-back,
and delivery rate is captured/sent (sent is always ``link_burst_count``).

Sequence per (freq, size, direction)::

    1. send ``burst_rx`` to the receiving worker (tune + open a capture window)
    2. wait ``link_rx_lead_s`` so the window is open
    3. send ``burst_tx`` to the transmitting worker (N packets, one tune)
    4. await ``burst_rx_done`` and collect the N (padded) results

Workers are dumb executors. Results are written to a local CSV/JSON.

Usage::

    python -m snr_sweep.link_coordinator --config config/public.toml
    python -m snr_sweep.link_coordinator --node-a tower --node-b field \\
        --freq-only 902.3 --sizes 1,128,255 --burst-count 10
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import paho.mqtt.client as mqtt

from . import __version__
from .config import SweepConfig, channels, load_config, fmt_duration
from .linkproto import (CMD_RX, CMD_TX, CMD_BURST_RX, CMD_BURST_TX,
                        default_directions, direction_label,
                        burst_rx_command, burst_tx_command,
                        rx_command, tx_command)
from .mqtt import (link_result_topic, link_report_topic, make_client, status_topic,
                   TOPIC_PREFIX)

log = logging.getLogger("snr_sweep.link_coordinator")


@dataclass
class LinkResult:
    node_rx: str
    freq_hz: int
    packet_size: int
    trial: int
    rx_ok: bool
    freq_mhz: float = 0.0
    channel_index: int = -1
    direction: str = ""
    snr_db: Optional[float] = None
    rssi_dbm: Optional[float] = None
    tx_ok: Optional[bool] = None
    ts: str = ""
    seq: int = 0


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


class Coordinator:
    def __init__(self, cfg: SweepConfig, node_a: str, node_b: str,
                 directions: Optional[List[tuple]] = None):
        self.cfg = cfg
        self.node_a = node_a
        self.node_b = node_b
        self.directions = directions or default_directions([node_a, node_b])
        self.results: List[LinkResult] = []
        self._seq = 0
        self._client: Optional[mqtt.Client] = None
        self._result_box: List[LinkResult] = []
        self._status_box: List[dict] = []
        self._cond = threading.Condition()

    # ----------------------------------------------------------------- mqtt
    def connect(self, timeout: float = 10.0) -> None:
        import threading
        c = make_client(
            client_id="snr-coordinator",
            username=self.cfg.mqtt_user,
            password=self.cfg.mqtt_pass,
            tls=self.cfg.mqtt_tls,
        )
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=30)
        c.loop_start()
        self._client = c
        for _ in range(int(timeout * 10)):
            if getattr(c, "_up", False):
                break
            time.sleep(0.1)
        if not getattr(c, "_up", False):
            raise ConnectionError(f"cannot reach broker {self.cfg.mqtt_host}:{self.cfg.mqtt_port}")
        log.info("coordinator connected to %s:%d", self.cfg.mqtt_host, self.cfg.mqtt_port)

    def _on_connect(self, c, _ud, _f, rc) -> None:
        c._up = (rc == 0)
        if rc == 0:
            c.subscribe(f"{TOPIC_PREFIX}/+/status", qos=1)
            c.subscribe(f"{TOPIC_PREFIX}/+/link/result", qos=1)
            c.subscribe(f"{TOPIC_PREFIX}/+/link/report", qos=1)

    def _on_message(self, c, _ud, msg):
        import json
        topic = msg.topic
        try:
            obj = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if topic.endswith("/link/result"):
            self._record_result(obj)
        elif topic.endswith("/status"):
            state = obj.get("state")
            if state in ("burst_rx_done", "burst_tx_done"):
                with self._cond:
                    self._status_box.append(obj)
                    self._cond.notify_all()

    def _record_result(self, obj: dict) -> None:
        try:
            lr = LinkResult(
                node_rx=obj["node"], freq_hz=obj.get("freq_hz", 0),
                packet_size=obj["packet_size"], trial=obj.get("trial", 0),
                rx_ok=bool(obj["rx_ok"]),
                snr_db=obj.get("snr_db"), rssi_dbm=obj.get("rssi_dbm"),
                tx_ok=obj.get("tx_ok"), ts=obj.get("ts", ""),
                seq=obj.get("seq", 0))
        except (KeyError, TypeError):
            return
        with self._cond:
            self._result_box.append(lr)
            self._cond.notify_all()

    # ----------------------------------------------------------------- sequencing
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _send(self, topic: str, obj: dict) -> None:
        assert self._client is not None
        self._client.publish(topic, json.dumps(obj, separators=(",", ":"), sort_keys=True), qos=1)

    def _wait_result(self, expected_seq: int, node_rx: str, timeout: float) -> Optional[LinkResult]:
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                for i, r in enumerate(self._result_box):
                    if r.seq == expected_seq and r.node_rx == node_rx:
                        del self._result_box[i]
                        return r
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=min(remaining, 1.0))

    def _wait_status(self, expected_seq: int, node: str, state: str,
                     timeout: float) -> Optional[dict]:
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                for i, s in enumerate(self._status_box):
                    if s.get("seq") == expected_seq and s.get("node") == node \
                            and s.get("state") == state:
                        del self._status_box[i]
                        return s
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=min(remaining, 0.2))

    def _drain_results(self, expected_seq: int, node_rx: str) -> List[LinkResult]:
        captured: List[LinkResult] = []
        with self._cond:
            keep: List[LinkResult] = []
            for r in self._result_box:
                if r.seq == expected_seq and r.node_rx == node_rx:
                    captured.append(r)
                else:
                    keep.append(r)
            self._result_box = keep
        return captured

    def _collect_burst(self, seq: int, rx_node: str, count: int,
                       freq_hz: int, size: int, timeout: float) -> List[LinkResult]:
        """Wait for burst_rx_done, drain captured packets, pad misses to ``count``."""
        self._wait_status(seq, rx_node, "burst_rx_done", timeout)
        captured = self._drain_results(seq, rx_node)
        out = captured[:count]
        while len(out) < count:
            out.append(LinkResult(
                node_rx=rx_node, rx_ok=False, seq=seq,
                freq_hz=freq_hz, packet_size=size,
                trial=len(out) + 1, ts=now_ts()))
        for i, r in enumerate(out, start=1):
            r.trial = i
        return out

    # ----------------------------------------------------------------- run
    def run(self, freq_only: Optional[float] = None, sizes: Optional[List[int]] = None,
            trials: int = 1, window_s: float = 5.0,
            burst_count: Optional[int] = None) -> int:
        sizes = sizes or self.cfg.packet_sizes
        count = burst_count or self.cfg.link_burst_count
        chs = channels(self.cfg)
        if freq_only is not None:
            chs = [c for c in chs if abs(c.center_mhz - freq_only) < 0.001]
            if not chs:
                log.error("no channel near %.2f MHz", freq_only)
                return 2

        n_bursts = len(chs) * len(sizes) * len(self.directions)
        n_pkts = n_bursts * count
        per_burst = self.cfg.link_rx_lead_s + window_s
        log.info("link test: %d channels x %d sizes x %d directions x %d pkt/burst = %d packets",
                 len(chs), len(sizes), len(self.directions), count, n_pkts)
        log.info("estimated wall time ~ %s (at ~%.1f s/burst, %d bursts)",
                 fmt_duration(n_bursts * per_burst), per_burst, n_bursts)

        for ch in chs:
            for size in sizes:
                for (tx_node, rx_node) in self.directions:
                    seq = self._next_seq()
                    direction = direction_label(tx_node, rx_node)
                    log.info("ch=%03d size=%3d dir=%s seq=%d burst=%d",
                             ch.index, size, direction, seq, count)
                    self._send(
                        f"{TOPIC_PREFIX}/coordinator/{rx_node}/cmd",
                        burst_rx_command(freq_hz=ch.center_hz, size=size,
                                         count=count, window_s=window_s, seq=seq))
                    time.sleep(self.cfg.link_rx_lead_s)
                    self._send(
                        f"{TOPIC_PREFIX}/coordinator/{tx_node}/cmd",
                        burst_tx_command(freq_hz=ch.center_hz, size=size,
                                         count=count, seq=seq))
                    got = self._collect_burst(
                        seq, rx_node, count, ch.center_hz, size,
                        timeout=window_s + 4.0)
                    ok = sum(1 for r in got if r.rx_ok)
                    for r in got:
                        r.freq_mhz = ch.center_mhz
                        r.freq_hz = ch.center_hz
                        r.channel_index = ch.index
                        r.direction = direction
                    self.results.extend(got)
                    log.info("  %d/%d delivered", ok, count)
        self._flush()
        return 0 if self.results else 1

    def _flush(self) -> None:
        out_dir = Path(self.cfg.log_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base = out_dir / f"link_test-{self.node_a}-{self.node_b}-{stamp}"
        with open(base.with_suffix(".json"), "w") as f:
            json.dump([r.__dict__ for r in self.results], f, indent=2)
        with open(base.with_suffix(".csv"), "w", newline="") as f:
            if self.results:
                w = csv.DictWriter(f, fieldnames=list(self.results[0].__dict__.keys()))
                w.writeheader()
                for r in self.results:
                    w.writerow(r.__dict__)
        log.info("wrote link results to %s", base.with_suffix(".json"))
        self._print_summary()

    def _print_summary(self) -> None:
        by: Dict[float, List[LinkResult]] = {}
        for r in self.results:
            by.setdefault(r.freq_mhz, []).append(r)
        log.info("per-frequency summary (delivered/total, median SNR):")
        for freq in sorted(by):
            rs = by[freq]
            ok = sum(1 for r in rs if r.rx_ok)
            snrs = [r.snr_db for r in rs if r.snr_db is not None]
            med = sorted(snrs)[len(snrs) // 2] if snrs else None
            log.info("  %.2f MHz: %3d/%3d delivered  median SNR=%s dB",
                     freq, ok, len(rs), f"{med:+.1f}" if med is not None else "n/a")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/sweep.toml")
    ap.add_argument("--node-a", default="tower", help="first node id (default: tower)")
    ap.add_argument("--node-b", default="field", help="second node id (default: field)")
    ap.add_argument("--freq-only", type=float)
    ap.add_argument("--sizes", help="comma list of packet sizes (default: 1,128,255)")
    ap.add_argument("--burst-count", type=int, default=None,
                    help="packets per (channel, size, direction); known delivery denominator")
    ap.add_argument("--trials", type=int, default=None,
                    help="alias for --burst-count (kept for CLI compat)")
    ap.add_argument("--window", type=float, default=None,
                    help="rx capture window seconds (default: link_rx_window_s from config)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    node_a = args.node_a
    node_b = args.node_b
    sizes = None
    if args.sizes:
        sizes = [int(x) for x in args.sizes.split(",")]
    window_s = args.window if args.window is not None else cfg.link_rx_window_s
    burst_count = args.burst_count or args.trials or cfg.link_burst_count

    coord = Coordinator(cfg, node_a, node_b)
    coord.connect()
    try:
        return coord.run(freq_only=args.freq_only, sizes=sizes,
                         burst_count=burst_count, window_s=window_s)
    finally:
        if coord._client:
            coord._client.loop_stop()
            coord._client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
