"""Shared payloads / constants for the link test.

The link test is the secondary objective: after the noise sweep, two G3 nodes
at different locations test the radio link. For every 100 kHz center frequency,
packets of sizes ~1..255 (in ~50-byte steps) are sent 3 times; the receiving
node records whether the packet arrived plus its SNR/RSSI.

Roles
-----
* **worker**     -- runs on each Pi, holds one G3, executes ``tx`` / ``rx``
  commands it receives on its own MQTT control topic.
* **coordinator**-- pure MQTT sequencer (no radio). It drives the whole matrix
  of (freq, size, trial, direction) and aggregates the results it hears back.

Direction naming: ``a->b`` means node ``a`` transmits and node ``b`` receives.
By default the coordinator tests both directions per trial so each hop is
characterized in both ways.
"""

from __future__ import annotations

from typing import Any, Dict

# link/result command kinds the coordinator sends to a worker.
CMD_TX = "tx"
CMD_RX = "rx"


def tx_command(*, freq_hz: int, size: int, trial: int, seq: int) -> Dict[str, Any]:
    return {"cmd": CMD_TX, "freq_hz": int(freq_hz), "size": int(size),
            "trial": int(trial), "seq": int(seq)}


def rx_command(*, freq_hz: int, size: int, trial: int, seq: int,
              window_s: float) -> Dict[str, Any]:
    return {"cmd": CMD_RX, "freq_hz": int(freq_hz), "size": int(size),
            "trial": int(trial), "seq": int(seq), "window_s": float(window_s)}


def direction_label(tx_node: str, rx_node: str) -> str:
    return f"{tx_node}->{rx_node}"


def default_directions(nodes) -> list:
    """Both directions for two nodes; a list for >2 nodes is the pairings given."""
    if len(nodes) == 2:
        a, b = nodes
        return [(a, b), (b, a)]
    return [(nodes[0], nodes[1])]
