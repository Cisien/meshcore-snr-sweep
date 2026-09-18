"""Render a completed link test into a line chart (delivery rate + SNR per channel).

Reads a ``link_test-*.json`` artifact written by :mod:`snr_sweep.link_coordinator`
and emits a self-contained HTML file (Chart.js) with one line per packet-size
delivery rate (left axis, %) plus one median-SNR line on a second axis (right,
dB). No bars, no mixed chart types.

Usage::

    python -m snr_sweep.chart                      # newest data/link_test-*.json -> data/link_chart.html
    python -m snr_sweep.chart --input data/link_test-tower-field-...json --out data/ch.html
    python -m snr_sweep.chart --latest             # explicit: most recent artifact
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional


@dataclass
class ChannelPoint:
    freq_mhz: float
    channel_index: int
    # packet_size -> {"ok": int, "total": int}
    sizes: Dict[int, Dict[str, int]]
    snrs: List[float]


def load_artifact(path: Path) -> List[dict]:
    with open(path) as f:
        rows = json.load(f)
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"no rows in {path}")
    return rows


def aggregate(rows: List[dict]) -> List[ChannelPoint]:
    chans: Dict[int, ChannelPoint] = {}
    for r in rows:
        ci = r["channel_index"]
        if ci not in chans:
            chans[ci] = ChannelPoint(
                freq_mhz=r["freq_mhz"],
                channel_index=ci,
                sizes={},
                snrs=[],
            )
        cp = chans[ci]
        size = int(r["packet_size"])
        bucket = cp.sizes.setdefault(size, {"ok": 0, "total": 0})
        bucket["total"] += 1
        if r.get("rx_ok"):
            bucket["ok"] += 1
        if r.get("snr_db") is not None:
            cp.snrs.append(float(r["snr_db"]))
    return [chans[k] for k in sorted(chans)]


def build_series(channels: List[ChannelPoint]):
    """Return (labels, datasets) where datasets match the accepted chart layout.

    Three delivery-rate lines (one per size, % on left axis) plus one dashed
    median-SNR line on the right axis.
    """
    labels = [round(c.freq_mhz, 3) for c in channels]
    sizes = sorted({s for c in channels for s in c.sizes})
    colors = {0: "#4aa3ff", 1: "#5bc45b", 2: "#e69b4a", 3: "#c05bc4"}
    datasets: List[dict] = []
    for i, size in enumerate(sizes):
        data = [
            round(100.0 * c.sizes[size]["ok"] / c.sizes[size]["total"], 1)
            if c.sizes.get(size, {}).get("total") else None
            for c in channels
        ]
        datasets.append({
            "label": f"{size} B delivery",
            "data": data,
            "yAxisID": "y",
            "borderColor": colors.get(i, "#888888"),
            "backgroundColor": colors.get(i, "#888888"),
            "borderWidth": 1.5,
            "pointRadius": 0,
            "tension": 0.25,
            "spanGaps": True,
        })
    # median SNR per channel (matches coordinator's per-frequency median)
    snr_data = [round(median(c.snrs), 2) if c.snrs else None for c in channels]
    datasets.append({
        "label": "median SNR",
        "data": snr_data,
        "yAxisID": "y1",
        "borderColor": "#d4a017",
        "backgroundColor": "#d4a017",
        "borderWidth": 2,
        "borderDash": [6, 4],
        "pointRadius": 0,
        "tension": 0.25,
        "spanGaps": True,
    })
    return labels, datasets


def render_html(labels: List[float], datasets: List[dict], title: str, sub: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ margin:0; padding:16px; }}
  .title {{ font-size:15px; font-weight:600; color:var(--foreground); margin-bottom:3px; }}
  .sub {{ font-size:11px; color:var(--muted-foreground); margin-bottom:12px; }}
  .cwrap {{ position:relative; height:340px; }}
</style></head>
<body>
<div class="title">{title}</div>
<div class="sub">{sub}</div>
<div class="cwrap"><canvas id="c"></canvas></div>
<script>
(function(){{
  var labels = {json.dumps(labels)};
  var ds = {json.dumps(datasets)};
  new Chart(document.getElementById("c").getContext("2d"), {{
    type: "line",
    data: {{ labels: labels, datasets: ds }},
    options: {{
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: {{ mode: "index", intersect: false }},
      plugins: {{
        legend: {{ position: "top", labels: {{ color: "var(--foreground)", font: {{ size: 11 }} }} }},
        tooltip: {{ callbacks: {{ label: function(item){{
           var v = item.parsed.y;
           if (v === null) return "";
           var u = (item.dataset.yAxisID === "y1") ? " dB" : " %";
           return item.dataset.label + ": " + v + u;
        }} }} }}
      }},
      scales: {{
        x: {{ ticks: {{ color:"var(--muted-foreground)", font:{{size:9}}, autoSkip:true, maxTicksLimit:26, maxRotation:0 }},
             grid: {{ display:false }},
             title: {{ display:true, text:"Center frequency (MHz)", color:"var(--muted-foreground)", font:{{size:10}} }} }},
        y:  {{ position:"left", min:0, max:100,
             ticks: {{ color:"var(--muted-foreground)", stepSize:20, callback:function(v){{return v+"%";}} }},
             grid: {{ color:"rgba(128,128,128,0.12)" }},
             title: {{ display:true, text:"Delivery rate", color:"var(--muted-foreground)", font:{{size:10}} }} }},
        y1: {{ position:"right", min:-5, max:16,
             ticks: {{ color:"var(--muted-foreground)", stepSize:5, callback:function(v){{return v+" dB";}} }},
             grid: {{ drawOnChartArea:false }},
             title: {{ display:true, text:"SNR", color:"var(--muted-foreground)", font:{{size:10}} }} }}
      }}
    }}
  }});
}})();
</script>
</body></html>
"""


def newest_link_artifact(data_dir: Path) -> Path:
    arts = sorted(data_dir.glob("link_test-*.json"))
    if not arts:
        raise SystemExit(f"no link_test-*.json under {data_dir}")
    return arts[-1]


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=None, help="link_test-*.json to render")
    ap.add_argument("--latest", action="store_true", help="use the most recent data/link_test-*.json")
    ap.add_argument("--data-dir", type=Path, default=Path("data"), help="directory scanned for --latest (default: data)")
    ap.add_argument("--out", type=Path, default=None, help="output HTML path (default: <input>_chart.html)")
    ap.add_argument("--title", default="MeshCore 500 kHz Link Test — delivery rate & SNR per channel")
    args = ap.parse_args(argv)

    if args.input:
        src = args.input
    else:
        src = newest_link_artifact(args.data_dir)

    rows = load_artifact(src)
    channels = aggregate(rows)
    labels, datasets = build_series(channels)

    f0 = channels[0].freq_mhz
    f1 = channels[-1].freq_mhz
    sizes = sorted({s for c in channels for s in c.sizes})
    sub = (f"{src.stem.replace('link_test-', '').replace('_', ' ')} · "
           f"{f0:.1f}–{f1:.1f} MHz · {len(channels)} ch · "
           f"sizes {', '.join(str(s) for s in sizes)} · delivery on left (%), SNR on right (dB, dashed)")

    out = args.out or src.with_name(src.stem + "_chart.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(labels, datasets, args.title, sub))
    print(f"wrote {out}  ({len(channels)} channels, sizes {sizes})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
