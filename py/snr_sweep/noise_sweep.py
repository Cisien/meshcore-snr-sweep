"""Primary objective: noise-floor / ambient sweep.

Tunes the G3 to each 100 kHz center frequency from 902.0 to 915.0 MHz and, at
each center, records a noise-floor reading at the configured interval (default
60 s) for the configured number of readings (default 10 -> 10 minutes per
channel). Every reading is published to MQTT so the homelab recorder captures
it, and a CSV/JSON summary is written locally for offline analysis.

Usage (on the Pi attached to a G3):

    python -m snr_sweep.noise_sweep --config config/sweep.toml
    python -m snr_sweep.noise_sweep --freq-only 902.25 --count 10   # quick

Exit codes: 0 on success, non-zero on a fatal radio/MQTT problem.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import SweepConfig, channels, load_config, median, stdev, fmt_duration
from .kiss_client import KissClient, KissError
from .mqtt import (MqttPublisher, noise_done_topic, noise_sample_payload,
                   noise_sample_topic, status_topic)
from .serial_port import find_port

log = logging.getLogger("snr_sweep.noise_sweep")

_STOP = {"flag": False}


def _install_signal_handlers() -> None:
    def _handler(sig, frame):
        _STOP["flag"] = True
        log.warning("stop requested (signal %s); finishing current sample then exiting", sig)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


class NoiseSweeper:
    def __init__(self, cfg: SweepConfig, publisher: Optional[MqttPublisher] = None,
                 client: Optional[KissClient] = None):
        self.cfg = cfg
        self.publisher = publisher
        self.client = client

    # ----------------------------------------------------------------- setup
    def _connect(self) -> None:
        port = find_port(self.cfg.serial_port, required=not self.cfg.dry_run)
        if port is None:
            raise LookupError("no serial port resolved for the radio")
        log.info("opening radio on %s (baud %d)", port, self.cfg.serial_baud)
        self.client = KissClient(port, baud=self.cfg.serial_baud)
        self.client.open()
        if not self.client.ping():
            raise KissError("radio did not answer Ping; is it running the KISS modem?")
        try:
            self.client.set_signal_report(True)
        except KissError:
            pass
        # One-time radio configuration (bandwidth, SF, CR; frequency is set
        # per channel in the sweep loop).
        first = channels(self.cfg)[0].center_hz
        if not self.client.set_radio(first, self.cfg.bandwidth_hz, self.cfg.sf, self.cfg.cr):
            raise KissError("SetRadio rejected by the modem")
        self.client.set_tx_power(self.cfg.tx_power_dbm)
        log.info("radio configured: BW=%d kHz SF=%d CR=%d TX=%d dBm",
                 self.cfg.bandwidth_khz, self.cfg.sf, self.cfg.cr, self.cfg.tx_power_dbm)

    # ----------------------------------------------------------------- run
    def run(self, freq_only: Optional[float] = None, count: Optional[int] = None) -> int:
        chs = channels(self.cfg)
        if freq_only is not None:
            chs = [c for c in chs if abs(c.center_mhz - freq_only) < 0.001]
            if not chs:
                log.error("no channel near %.2f MHz in the band", freq_only)
                return 2
        count = count or self.cfg.sample_count
        if self.cfg.dry_run:
            return self._dry_run(chs, count)

        self._connect()
        assert self.client is not None
        client = self.client
        self._publish_status("starting", total_channels=len(chs), samples_per_channel=count)

        results: List[dict] = []
        baseline: List[dict] = []
        try:
            # Reference reading at MeshCore's default deployment settings
            # (910.525 MHz / 62.5 kHz / SF7 / CR 4/5) before the 500 kHz sweep.
            baseline = self._capture_baseline(count)
            for ch in chs:
                if _STOP["flag"]:
                    break
                log.info("=== channel %03d  center=%.1f MHz  (%d samples / %ds) ===",
                         ch.index, ch.center_mhz, count, self.cfg.channel_duration_s)
                # Tune + settle so the AGC stabilizes before we sample.
                if not client.set_radio(ch.center_hz, self.cfg.bandwidth_hz, self.cfg.sf, self.cfg.cr):
                    log.warning("SetRadio failed at %.2f MHz; skipping channel", ch.center_mhz)
                    continue
                time.sleep(self.cfg.settle_s)
                ch_readings = self._sample_channel(ch, count, self.cfg.channel_duration_s)
                if ch_readings:
                    results.append(self._channel_summary(ch, ch_readings))
                    self._publish_done(self._channel_summary(ch, ch_readings))
        finally:
            self._publish_status("done", channels_completed=len(results))
            if self.client is not None:
                self.client.close()

        out = self._write_results(results, baseline)
        log.info("wrote results to %s", out)
        return 0 if results else 1

    # ----------------------------------------------------------------- sampling
    def _sample_one(self, ch, index: int, baseline: bool = False) -> Optional[dict]:
        """One noise-floor / RSSI reading at the tuned frequency."""
        assert self.client is not None
        try:
            nf = self.client.get_noise_floor()
            rssi = self.client.get_current_rssi()
            busy = self.client.is_channel_busy()
        except KissError as e:
            log.warning("sample %d @ %.2f MHz failed: %s", index, ch.center_mhz, e)
            return None
        payload = {
            "node": self.cfg.node_id,
            "freq_mhz": ch.center_mhz,
            "freq_hz": ch.center_hz,
            "channel_index": ch.index,
            "sample_index": index,
            "noise_floor_dbm": round(nf, 1),
            "rssi_dbm": rssi,
            "busy": busy,
            "ts": now_ts(),
        }
        if baseline:
            payload["baseline"] = True
            payload["bandwidth_hz"] = self.cfg.baseline_bandwidth_hz
        log.info("  sample %02d%s  floor=%.1f dBm rssi=%.0f dBm busy=%s",
                 index, " (baseline)" if baseline else "", nf, rssi, busy)
        return payload

    def _sample_channel(self, ch, count: int, window_s: float,
                        baseline: bool = False) -> List[dict]:
        """Take ``count`` samples for one channel, spread evenly over ``window_s``.

        Each sample is one noise-floor/RSSI reading. The samples are spaced
        ``window_s / count`` apart so the full window is covered. Returns the
        list of readings.
        """
        gap = window_s / count if count else 0.0
        readings: List[dict] = []
        for i in range(count):
            if _STOP["flag"]:
                break
            reading = self._sample_one(ch, i, baseline=baseline)
            if reading is None:
                continue
            readings.append(reading)
            self._publish_sample(reading)
            if i < count - 1:
                time.sleep(gap)
        return readings

    def _capture_baseline(self, count: int) -> List[dict]:
        """Capture a reference noise floor at the baseline settings.

        Tunes the radio to 910.525 MHz / 62.5 kHz / SF7 / CR 4/5 (MeshCore's
        default deployment settings) and samples the same burst-of-N pattern the
        main sweep uses. Returns the list of baseline readings.
        """
        if not self.cfg.baseline_enabled:
            return []
        assert self.client is not None
        log.info("=== baseline  %.3f MHz  %d Hz  SF%d  CR=%d  (%d samples / %ds) ===",
                 self.cfg.baseline_freq_mhz, self.cfg.baseline_bandwidth_hz,
                 self.cfg.baseline_sf, self.cfg.baseline_cr, count,
                 self.cfg.channel_duration_s)
        if not self.client.set_radio(int(round(self.cfg.baseline_freq_mhz * 1e6)),
                                     self.cfg.baseline_bandwidth_hz,
                                     self.cfg.baseline_sf, self.cfg.baseline_cr):
            log.warning("baseline SetRadio rejected; skipping baseline capture")
            return []
        time.sleep(self.cfg.settle_s)
        # A lightweight channel-shaped object for the sampler.
        class _BaselineCh:
            center_mhz = self.cfg.baseline_freq_mhz
            center_hz = int(round(self.cfg.baseline_freq_mhz * 1e6))
            index = -1
        readings = self._sample_channel(_BaselineCh, count,
                                        self.cfg.channel_duration_s, baseline=True)
        if readings:
            log.info("baseline complete: %d readings, mean floor=%.1f dBm",
                     len(readings),
                     sum(r["noise_floor_dbm"] for r in readings) / len(readings))
        return readings

    def _channel_summary(self, ch, readings: List[dict]) -> dict:
        floors = [r["noise_floor_dbm"] for r in readings]
        rssi = [r["rssi_dbm"] for r in readings]
        busy_frac = sum(1 for r in readings if r.get("busy")) / len(readings)
        return {
            "channel_index": ch.index,
            "center_mhz": ch.center_mhz,
            "center_hz": ch.center_hz,
            "samples": len(readings),
            "floor_min_dbm": min(floors),
            "floor_median_dbm": round(median(floors), 1),
            "floor_mean_dbm": round(sum(floors) / len(floors), 1),
            "floor_max_dbm": max(floors),
            "rssi_median_dbm": round(median(rssi), 1),
            "busy_fraction": round(busy_frac, 3),
            "first_ts": readings[0]["ts"],
            "last_ts": readings[-1]["ts"],
        }

    # ----------------------------------------------------------------- io / pub
    def _publish_sample(self, reading: dict) -> None:
        if self.publisher is None:
            return
        payload = noise_sample_payload(
            node=reading["node"], freq_mhz=reading["freq_mhz"], freq_hz=reading["freq_hz"],
            channel_index=reading["channel_index"], sample_index=reading["sample_index"],
            noise_floor_dbm=reading["noise_floor_dbm"], rssi_dbm=reading["rssi_dbm"],
            busy=reading.get("busy"))
        self.publisher.publish(noise_sample_topic(reading["node"]), payload)

    def _publish_done(self, summary: dict) -> None:
        if self.publisher is None:
            return
        self.publisher.publish(noise_done_topic(self.cfg.node_id), {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            **summary,
        })

    def _publish_status(self, state: str, **extra) -> None:
        if self.publisher is None:
            return
        self.publisher.publish(status_topic(self.cfg.node_id), {
            "ts": now_ts(), "state": state, "node": self.cfg.node_id, **extra})

    def _write_results(self, results: List[dict], baseline: Optional[List[dict]] = None) -> str:
        out_dir = Path(self.cfg.log_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base = out_dir / f"noise_sweep-{self.cfg.node_id}-{stamp}"
        payload = {
            "generated": now_ts(),
            "config": self._cfg_dict(),
            "baseline": baseline or [],
            "channels": results,
        }
        with open(base.with_suffix(".json"), "w") as f:
            json.dump(payload, f, indent=2)
        with open(base.with_suffix(".csv"), "w", newline="") as f:
            if results:
                writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
                writer.writeheader()
                for row in results:
                    writer.writerow(row)
        return str(base.with_suffix(".json"))

    def _cfg_dict(self) -> dict:
        return {
            "band": [self.cfg.band_start_mhz, self.cfg.band_end_mhz],
            "step_khz": self.cfg.step_khz, "bandwidth_khz": self.cfg.bandwidth_khz,
            "sf": self.cfg.sf, "cr": self.cfg.cr, "tx_power_dbm": self.cfg.tx_power_dbm,
            "sample_count": self.cfg.sample_count,
            "channel_duration_s": self.cfg.channel_duration_s,
            "baseline": {
                "enabled": self.cfg.baseline_enabled,
                "freq_mhz": self.cfg.baseline_freq_mhz,
                "bandwidth_hz": self.cfg.baseline_bandwidth_hz,
                "sf": self.cfg.baseline_sf,
                "cr": self.cfg.baseline_cr,
            },
        }

    def _dry_run(self, chs, count) -> int:
        per_channel = self.cfg.settle_s + self.cfg.channel_duration_s
        total = len(chs) * per_channel
        log.info("[dry-run] %d channels x %d samples / %ds = %s total",
                 len(chs), count, self.cfg.channel_duration_s, fmt_duration(total))
        for ch in chs[:5]:
            log.info("[dry-run]   ch %03d center=%.1f MHz", ch.index, ch.center_mhz)
        if len(chs) > 5:
            log.info("[dry-run]   ... and %d more", len(chs) - 5)
        return 0


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/sweep.toml")
    ap.add_argument("--node", help="override node_id")
    ap.add_argument("--device", help="serial device path (e.g. /dev/ttyACM0)")
    ap.add_argument("--serial", help=argparse.SUPPRESS)  # alias for --device
    ap.add_argument("--freq-only", type=float, help="only this center frequency (MHz)")
    ap.add_argument("--count", type=int, help="samples per channel (override)")
    ap.add_argument("--duration", type=int, help="seconds to sample each channel (default: channel_duration_s)")
    ap.add_argument("--tx-power", type=int, help="TX power in dBm (default: tx_power_dbm from config)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch no hardware")
    ap.add_argument("--no-mqtt", action="store_true", help="do not publish to MQTT")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    serial = args.device or args.serial
    cfg = load_config(args.config, {
        "node_id": args.node, "serial_port": serial,
        "sample_count": args.count, "channel_duration_s": args.duration,
        "tx_power_dbm": args.tx_power,
        "dry_run": args.dry_run or None,
    })
    publisher = None
    if not args.no_mqtt and not args.dry_run:
        publisher = MqttPublisher(cfg.mqtt_host, cfg.mqtt_port,
                                  client_id=f"snr-sweep-{cfg.node_id}",
                                  username=cfg.mqtt_user, password=cfg.mqtt_pass,
                                  use_tls=cfg.mqtt_tls)
        try:
            publisher.connect()
            log.info("MQTT connected to %s:%d", cfg.mqtt_host, cfg.mqtt_port)
        except ConnectionError as e:
            log.error("MQTT unavailable: %s", e)
            log.info("continuing without publishing (local results still written)")
            publisher = None

    _install_signal_handlers()
    sweeper = NoiseSweeper(cfg, publisher=publisher)
    try:
        rc = sweeper.run(freq_only=args.freq_only, count=args.count)
    finally:
        if publisher is not None:
            publisher.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
