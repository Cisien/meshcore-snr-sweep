"""Configuration loading and sweep-parameter derivation.

Everything a run needs is in one TOML file (``config/sweep.toml``). This module
loads it, fills defaults, and derives the concrete list of channel centers so
the drivers never recompute channel math.

Defaults are chosen to match the survey the user specified:
* band 902.0 -> 915.0 MHz in 100 kHz center steps (131 centers)
* 500 kHz LoRa bandwidth
* SF7, coding rate 4/5 (CR=5)
* 10 noise readings per center at 60 s spacing (10 minutes per channel)
* link test: packet sizes ~1..255 in ~50-byte steps, 3 trials each
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class RadioDefaults:
    sf: int = 7
    cr: int = 5  # 5 == 4/5, 6 == 4/6, ... (KISS CR field)
    tx_power_dbm: int = 7


@dataclass
class Channel:
    """One swept center frequency."""

    index: int
    center_mhz: float
    center_hz: int

    @property
    def label(self) -> str:
        return f"{self.center_mhz:.1f}"


@dataclass
class SweepConfig:
    # band
    band_start_mhz: float = 902.3
    band_end_mhz: float = 915.0
    step_khz: int = 100
    bandwidth_khz: int = 500
    # radio
    sf: int = 7
    cr: int = 5
    tx_power_dbm: int = 7
    # baseline (MeshCore default deployment settings, captured before the sweep)
    baseline_enabled: bool = True
    baseline_freq_mhz: float = 910.525
    baseline_bandwidth_hz: int = 62500  # 62.5 kHz (standard SX1262 BW)
    baseline_sf: int = 7
    baseline_cr: int = 5
    # noise sampling
    sample_count: int = 10          # samples per channel
    channel_duration_s: int = 10    # seconds to sample each channel (samples spread evenly)
    settle_s: int = 5               # AGC settle after a frequency change

    @property
    def bandwidth_hz(self) -> int:
        return self.bandwidth_khz * 1000

    # link test
    packet_sizes: List[int] = field(
        default_factory=lambda: [1, 128, 255]
    )
    link_trials: int = 1           # unused by the burst sequencer (kept for CLI compat)
    link_burst_count: int = 10     # packets per (channel, size, direction); known denominator
    link_settle_s: float = 0.3     # AGC settle after a frequency change (both nodes)
    link_rx_lead_s: float = 0.3    # coordinator waits this long before the TX burst
    link_rx_window_s: float = 2.2  # rx capture window covering the whole TX burst
    link_tx_wait_s: float = 0.5    # wait for TxDone between burst packets
    # topology / identity
    node_id: str = "alpha"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: Optional[str] = None    # username/password auth (public broker)
    mqtt_pass: Optional[str] = None
    mqtt_tls: bool = False            # TLS to broker (public broker: True)
    serial_port: Optional[str] = None  # None => auto-discover
    serial_baud: int = 115200
    log_dir: str = "./data"
    dry_run: bool = False  # skip radio + MQTT, just print the plan


def _channels(start_mhz: float, end_mhz: float, step_khz: int) -> List[Channel]:
    start_khz = int(round(start_mhz * 1000))
    end_khz = int(round(end_mhz * 1000))
    if step_khz <= 0:
        raise ValueError("step_khz must be positive")
    n = (end_khz - start_khz) // step_khz
    out: List[Channel] = []
    for i in range(n + 1):
        khz = start_khz + i * step_khz
        hz = khz * 1000
        out.append(Channel(index=i, center_mhz=khz / 1000.0, center_hz=hz))
    return out


def load_config(path: Optional[str | Path] = None, overrides: Optional[dict] = None) -> SweepConfig:
    """Load a sweep TOML and apply overrides.

    ``path`` may be None; in that case built-in defaults are used (useful for
    tests). ``overrides`` is a flat dict applied on top of the file.
    """
    data: dict = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with open(p, "rb") as f:
                data = tomllib.load(f)

    cfg = SweepConfig()
    for key in (
        "band_start_mhz", "band_end_mhz", "step_khz", "bandwidth_khz",
        "sf", "cr", "tx_power_dbm", "sample_count", "channel_duration_s",
        "settle_s", "link_trials", "link_burst_count", "link_settle_s",
        "link_rx_lead_s", "link_rx_window_s", "link_tx_wait_s",
        "baseline_enabled", "baseline_freq_mhz", "baseline_bandwidth_hz",
        "baseline_sf", "baseline_cr",
        "node_id", "mqtt_host", "mqtt_port", "mqtt_user", "mqtt_pass",
        "mqtt_tls", "serial_baud", "log_dir", "dry_run",
    ):
        if key in data:
            setattr(cfg, key, data[key])
    if "packet_sizes" in data:
        cfg.packet_sizes = [int(x) for x in data["packet_sizes"]]
    if "serial_port" in data:
        cfg.serial_port = data["serial_port"] or None

    if overrides:
        for key, value in overrides.items():
            if not hasattr(cfg, key):
                continue
            if key == "packet_sizes" and isinstance(value, list):
                value = [int(x) for x in value]
            if value is None:
                continue
            setattr(cfg, key, value)

    _validate(cfg)
    return cfg


def _validate(cfg: SweepConfig) -> None:
    if cfg.band_start_mhz >= cfg.band_end_mhz:
        raise ValueError("band_start_mhz must be < band_end_mhz")
    if not (62 <= cfg.bandwidth_khz <= 500):
        raise ValueError(f"bandwidth_khz out of SX1262 range: {cfg.bandwidth_khz}")
    if not (5 <= cfg.sf <= 12):
        raise ValueError(f"sf out of range 5..12: {cfg.sf}")
    if not (5 <= cfg.cr <= 8):
        raise ValueError(f"cr out of range 5..8: {cfg.cr}")
    if not cfg.packet_sizes:
        raise ValueError("packet_sizes must be non-empty")
    for size in cfg.packet_sizes:
        if not (1 <= size <= 255):
            raise ValueError(f"packet size out of 1..255: {size}")


def channels(cfg: SweepConfig) -> List[Channel]:
    return _channels(cfg.band_start_mhz, cfg.band_end_mhz, cfg.step_khz)


def sweep_duration_s(cfg: SweepConfig) -> float:
    """Total wall time of a full noise sweep.

    Per channel: AGC settle + the sampling window. All ``sample_count`` samples
    are spread evenly across ``channel_duration_s``.
    """
    per_channel = cfg.settle_s + cfg.channel_duration_s
    return len(channels(cfg)) * per_channel


def fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def median(values: List[float]) -> float:
    if not values:
        return float("nan")
    vs = sorted(values)
    n = len(vs)
    mid = n // 2
    return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2.0


def stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / len(values))
