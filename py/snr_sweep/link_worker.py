"""Secondary objective: link-test WORKER.

Runs on a Pi that has one G3 attached. Subscribes to its own MQTT control
topic, and for each command it receives it either:

* ``tx``  -- tunes the radio and transmits one opaque packet of ``size`` bytes,
  publishing a ``tx_done`` result; or
* ``rx``  -- tunes the radio and listens for ``window_s`` seconds, publishing a
  ``link/result`` with whether a packet of the expected size arrived plus its
  SNR/RSSI.

Commands and results are defined in :mod:`snr_sweep.linkproto`. This script
never decides the test order; the :mod:`snr_sweep.link_coordinator` does that
over MQTT.

Usage (on each Pi, one radio attached):

    # tower site
    python -m snr_sweep.link_worker --node tower --device /dev/serial/by-id/usb-...if00
    # field site
    python -m snr_sweep.link_worker --node field --device /dev/ttyACM1
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import signal
import sys
import threading
import time
from typing import Optional

import paho.mqtt.client as mqtt

from . import __version__
from .config import SweepConfig, load_config
from .kiss_client import KissClient, KissError
from .linkproto import CMD_RX, CMD_TX, CMD_BURST_RX, CMD_BURST_TX, default_directions
from .mqtt import link_result_topic, make_client, status_topic, subscribe_for_node
from .serial_port import find_port

log = logging.getLogger("snr_sweep.link_worker")

_STOP = {"flag": False}


def _install_signal_handlers() -> None:
    def _handler(sig, frame):
        _STOP["flag"] = True
        log.warning("stop requested (signal %s)", sig)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


class LinkWorker:
    def __init__(self, cfg: SweepConfig, client: Optional[KissClient] = None,
                 mqttc: Optional[mqtt.Client] = None):
        self.cfg = cfg
        self.client = client
        self.mqttc = mqttc
        self._cmd_queue: "queue.Queue[dict]" = queue.Queue()
        self._dispatch_thread: Optional[threading.Thread] = None

    def connect(self) -> None:
        port = find_port(self.cfg.serial_port, required=True)
        if port is None:
            raise LookupError("no serial port resolved for the radio")
        log.info("opening radio on %s", port)
        self.client = KissClient(port, baud=self.cfg.serial_baud)
        self.client.open()
        if not self.client.ping():
            raise KissError("radio did not answer Ping; is it running the KISS modem?")
        try:
            self.client.set_signal_report(True)
        except KissError:
            pass
        self.client.set_tx_power(self.cfg.tx_power_dbm)

        self.mqttc = make_client(
            client_id=f"link-{self.cfg.node_id}",
            username=self.cfg.mqtt_user,
            password=self.cfg.mqtt_pass,
            tls=self.cfg.mqtt_tls,
        )
        self.mqttc.on_connect = self._on_connect
        self.mqttc.on_message = self._on_message
        self.mqttc.on_disconnect = self._on_disconnect
        self.mqttc.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=30)
        self.mqttc.loop_start()
        for _ in range(100):
            if getattr(self.mqttc, "_connected", False):
                break
            time.sleep(0.1)
        # Dispatch commands on their own thread so the MQTT network loop never
        # blocks on a multi-second rx capture window.
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop,
                                                 daemon=True, name="link-dispatch")
        self._dispatch_thread.start()
        log.info("worker %s ready (radio + MQTT)", self.cfg.node_id)

    def _on_connect(self, _c, _ud, _f, rc) -> None:
        assert self.mqttc is not None
        self.mqttc._connected = (rc == 0)
        if rc == 0:
            subs = subscribe_for_node(self.cfg.node_id)
            for t in subs:
                self.mqttc.subscribe(t, qos=1)
            self._publish_status("ready")
            log.info("subscribed to %s", subs)

    def _on_disconnect(self, *_a) -> None:
        if self.mqttc is not None:
            self.mqttc._connected = False

    def _on_message(self, _c, _ud, msg):
        try:
            cmd = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if not isinstance(cmd, dict) or "cmd" not in cmd:
            return
        # Enqueue; the dispatch thread executes it so the MQTT loop stays free.
        self._cmd_queue.put(cmd)

    def _dispatch_loop(self) -> None:
        while True:
            try:
                cmd = self._cmd_queue.get(timeout=0.5)
            except queue.Empty:
                if _STOP["flag"]:
                    return
                continue
            if _STOP["flag"]:
                break
            log.info("cmd: %s", cmd)
            try:
                if cmd["cmd"] == CMD_TX:
                    self._do_tx(cmd)
                elif cmd["cmd"] == CMD_RX:
                    self._do_rx(cmd)
                elif cmd["cmd"] == CMD_BURST_TX:
                    self._do_burst_tx(cmd)
                elif cmd["cmd"] == CMD_BURST_RX:
                    self._do_burst_rx(cmd)
            except KissError as e:
                log.warning("command %s failed: %s", cmd.get("cmd"), e)
                self._publish_status("error", error=str(e))

    # ----------------------------------------------------------------- actions
    def _tune(self, freq_hz: int) -> None:
        assert self.client is not None
        if not self.client.set_radio(freq_hz, self.cfg.bandwidth_hz, self.cfg.sf, self.cfg.cr):
            raise KissError(f"SetRadio rejected at {freq_hz} Hz")
        time.sleep(self.cfg.link_settle_s)

    def _do_tx(self, cmd: dict) -> None:
        assert self.client is not None
        size = int(cmd["size"])
        payload = bytes(range(1, size + 1))  # deterministic, exactly `size` bytes
        if not (1 <= len(payload) <= 255):
            raise ValueError(f"bad size {size}")
        self._tune(int(cmd["freq_hz"]))
        self.client.transmit(payload)
        ok = self.client.wait_tx_done(timeout=self.cfg.link_tx_wait_s + 2.0)
        self._publish_status("tx_done", seq=cmd.get("seq"), ok=ok, size=size,
                             freq_hz=cmd["freq_hz"])

    def _do_rx(self, cmd: dict) -> None:
        assert self.client is not None
        size = int(cmd["size"])
        window = float(cmd.get("window_s", 5.0))
        self._tune(int(cmd["freq_hz"]))
        # Drain any stale events before we open the window.
        self.client.drain_events()
        pkt = self.client.wait_rx_packet(timeout=window, expect_len=size)
        # If a packet of the wrong length came first, keep waiting briefly for
        # the expected size (covers a stray/co-channel packet).
        if pkt is None:
            pkt = self.client.wait_rx_packet(timeout=1.0, expect_len=size)
        rx_ok = pkt is not None
        snr = rssi = None
        if rx_ok:
            meta = self.client.next_rx_meta(timeout=1.0)
            if meta is not None:
                snr, rssi = meta.snr, meta.rssi
        self._publish_result(
            freq_hz=int(cmd["freq_hz"]), size=size, trial=int(cmd.get("trial", 0)),
            seq=cmd.get("seq"), rx_ok=rx_ok, snr_db=snr, rssi_dbm=rssi,
            tx_ok=None)
        self._publish_status("rx_done", seq=cmd.get("seq"), rx_ok=rx_ok, size=size)

    def _do_burst_tx(self, cmd: dict) -> None:
        """Tune once, then fire exactly ``count`` packets of ``size`` bytes."""
        assert self.client is not None
        size = int(cmd["size"])
        count = int(cmd["count"])
        payload = bytes(range(1, size + 1))
        if not (1 <= size <= 255):
            raise ValueError(f"bad size {size}")
        self._tune(int(cmd["freq_hz"]))
        t0 = time.monotonic()
        sent = 0
        for _ in range(count):
            self.client.transmit(payload)
            self.client.wait_tx_done(timeout=self.cfg.link_tx_wait_s + 1.0)
            sent += 1
        log.info("burst_tx seq=%s: %d/%d packets in %.2fs (size=%d)",
                 cmd.get("seq"), sent, count, time.monotonic() - t0, size)
        self._publish_status("burst_tx_done", seq=cmd.get("seq"), ok=True,
                             size=size, sent=sent,
                             freq_hz=cmd["freq_hz"],
                             span_s=round(time.monotonic() - t0, 3))

    def _do_burst_rx(self, cmd: dict) -> None:
        """Tune once, then capture up to ``count`` packets of ``size`` within ``window_s``."""
        assert self.client is not None
        size = int(cmd["size"])
        count = int(cmd["count"])
        window = float(cmd["window_s"])
        seq = cmd.get("seq")
        self._tune(int(cmd["freq_hz"]))
        self.client.drain_events()
        end = time.monotonic() + window
        received = 0
        while received < count:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            pkt = self.client.wait_rx_packet(timeout=min(remaining, 0.25),
                                             expect_len=size)
            if pkt is None:
                continue
            received += 1
            meta = self.client.next_rx_meta(timeout=0.05)
            snr = meta.snr if meta else None
            rssi = meta.rssi if meta else None
            self._publish_result(
                freq_hz=int(cmd["freq_hz"]), size=size, trial=0,
                seq=seq, rx_ok=True, snr_db=snr, rssi_dbm=rssi,
                tx_ok=None)
        log.info("burst_rx seq=%s: %d/%d captured in %.2fs window",
                 seq, received, count, window)
        self._publish_status("burst_rx_done", seq=seq, size=size,
                             count=received, expected=count,
                             window_s=round(window, 2))

    # ----------------------------------------------------------------- publishing
    def _publish_result(self, **kw) -> None:
        assert self.mqttc is not None
        self.mqttc.publish(
            link_result_topic(self.cfg.node_id),
            _json({
                "ts": now_ts(), "node": self.cfg.node_id,
                "freq_hz": kw["freq_hz"],
                "seq": kw["seq"],
                "packet_size": kw["size"], "trial": kw["trial"],
                "rx_ok": kw["rx_ok"], "snr_db": kw["snr_db"],
                "rssi_dbm": kw["rssi_dbm"], "tx_ok": kw["tx_ok"],
            }), qos=1)

    def _publish_status(self, state: str, **extra) -> None:
        assert self.mqttc is not None
        self.mqttc.publish(status_topic(self.cfg.node_id),
                           _json({"ts": now_ts(), "node": self.cfg.node_id,
                                  "state": state, **extra}), qos=1, retain=True)

    def run_forever(self) -> int:
        while not _STOP["flag"]:
            time.sleep(0.5)
        log.info("worker shutting down")
        return 0


def _json(obj) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/sweep.toml")
    ap.add_argument("--node", help="node_id (also the MQTT control suffix)")
    ap.add_argument("--device", help="serial device path (e.g. /dev/ttyACM0 or /dev/serial/by-id/usb-...)")
    ap.add_argument("--serial", help=argparse.SUPPRESS)  # alias for --device
    ap.add_argument("--radio", choices=["g2", "g3"],
                    help="(bench only) select radio by hardware serial")
    ap.add_argument("--mqtt-host", help="override broker host")
    ap.add_argument("--mqtt-port", type=int, help="override broker port")
    ap.add_argument("--tx-power", type=int, help="TX power in dBm (default: tx_power_dbm from config)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")

    # Resolve the serial device path: --device/--serial take a path directly;
    # --radio is a bench shortcut (g2/g3 → by-id lookup by hardware serial).
    serial = args.device or args.serial
    if not serial and args.radio:
        from .serial_port import resolve_by_serial
        _radio_serials = {"g2": "ECDA3B46B8F8", "g3": "98A316CECBE0"}
        serial = resolve_by_serial(_radio_serials[args.radio])
        if not serial:
            raise SystemExit(f"--radio {args.radio}: no matching serial device (check /dev/serial/by-id)")

    cfg = load_config(args.config, {
        "node_id": args.node, "serial_port": serial,
        "mqtt_host": args.mqtt_host, "mqtt_port": args.mqtt_port,
        "tx_power_dbm": args.tx_power,
    })
    _install_signal_handlers()
    worker = LinkWorker(cfg)
    worker.connect()
    try:
        return worker.run_forever()
    finally:
        if worker.mqttc:
            worker.mqttc.loop_stop()
            worker.mqttc.disconnect()
        if worker.client:
            worker.client.close()


if __name__ == "__main__":
    sys.exit(main())
