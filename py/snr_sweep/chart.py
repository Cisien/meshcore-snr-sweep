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
from typing import Dict, List, Optional, Tuple


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


def filter_direction(rows: List[dict], direction: str) -> List[dict]:
    out = [r for r in rows if r.get("direction") == direction]
    if not out:
        raise SystemExit(f"no rows with direction={direction!r}")
    return out


def channel_delivery_min(c: ChannelPoint) -> Optional[float]:
    rates = []
    for b in c.sizes.values():
        if b["total"]:
            rates.append(100.0 * b["ok"] / b["total"])
    return min(rates) if rates else None


def good_bands(channels: List[ChannelPoint], threshold: float = 80.0) -> List[dict]:
    """Contiguous runs where every packet-size delivery rate is >= threshold."""
    runs: List[dict] = []
    start_i: Optional[int] = None
    for i, c in enumerate(channels):
        mn = channel_delivery_min(c)
        good = mn is not None and mn >= threshold
        if good and start_i is None:
            start_i = i
        if not good and start_i is not None:
            a, b = start_i, i - 1
            runs.append(_band(channels, a, b))
            start_i = None
    if start_i is not None:
        runs.append(_band(channels, start_i, len(channels) - 1))
    return runs


def _band(channels: List[ChannelPoint], a: int, b: int) -> dict:
    f0 = round(channels[a].freq_mhz, 3)
    f1 = round(channels[b].freq_mhz, 3)
    label = f"{f0:.1f}" if a == b else f"{f0:.1f}–{f1:.1f}"
    return {"start_i": a, "end_i": b, "start_mhz": f0, "end_mhz": f1, "label": label}


def assign_label_rows(bands: List[dict], min_sep: int = 10) -> List[dict]:
    """Give nearby band labels different ``row`` values so they do not overlap."""
    occupied: List[float] = []
    out: List[dict] = []
    for b in bands:
        mid = (b["start_i"] + b["end_i"]) / 2.0
        row = 0
        while row < len(occupied) and mid - occupied[row] < min_sep:
            row += 1
        if row == len(occupied):
            occupied.append(mid)
        else:
            occupied[row] = mid
        out.append({**b, "row": row})
    return out


def mhz_grid_indices(labels: List[float]) -> List[int]:
    """Category indices that fall on a whole MHz (for 100 kHz channel steps)."""
    out = []
    for i, f in enumerate(labels):
        if abs(f - round(f)) < 0.051:
            out.append(i)
    return out


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


def snr_axis_range(datasets: List[dict], pad: float = 2.0) -> Tuple[float, float]:
    """Y-axis min/max for the SNR series so negative values are not clipped."""
    vals = []
    for d in datasets:
        if d.get("yAxisID") != "y1":
            continue
        for v in d.get("data") or []:
            if v is not None:
                vals.append(float(v))
    if not vals:
        return -12.0, 16.0
    lo, hi = min(vals), max(vals)
    lo = min(lo, 0.0) - pad  # always show through 0 if anything is negative
    hi = max(hi, 0.0) + pad
    return (round(lo - 0.5), round(hi + 0.5))


def render_html(labels: List[float], datasets: List[dict], title: str, sub: str,
                bands: Optional[List[dict]] = None,
                mhz_idx: Optional[List[int]] = None,
                y1_min: float = -12.0, y1_max: float = 16.0) -> str:
    bands = bands or []
    mhz_idx = mhz_idx or []
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ margin:0; padding:16px; }}
  .title {{ font-size:15px; font-weight:600; color:var(--foreground); margin-bottom:3px; }}
  .sub {{ font-size:11px; color:var(--muted-foreground); margin-bottom:12px; }}
  .cwrap {{ position:relative; height:360px; }}
</style></head>
<body>
<div class="title">{title}</div>
<div class="sub">{sub}</div>
<div class="cwrap"><canvas id="c"></canvas></div>
<script>
(function(){{
  var labels = {json.dumps(labels)};
  var ds = {json.dumps(datasets)};
  var bands = {json.dumps(bands)};
  var mhzIdx = {json.dumps(mhz_idx)};
  var mhzSet = {{}};
  mhzIdx.forEach(function(i){{ mhzSet[i] = true; }});
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
        x: {{ ticks: {{ color:"var(--muted-foreground)", font:{{size:9}}, autoSkip:false, maxRotation:0,
                       callback: function(val, i){{
                         return mhzSet[i] ? Number(labels[i]).toFixed(0) : "";
                       }} }},
             grid: {{ display:false }},
             title: {{ display:true, text:"Center frequency (MHz)", color:"var(--muted-foreground)", font:{{size:10}} }} }},
        y:  {{ position:"left", min:0, max:100,
             ticks: {{ color:"var(--muted-foreground)", stepSize:20, callback:function(v){{return v+"%";}} }},
             grid: {{ color:"rgba(128,128,128,0.12)" }},
             title: {{ display:true, text:"Delivery rate", color:"var(--muted-foreground)", font:{{size:10}} }} }},
        y1: {{ position:"right", min:{y1_min}, max:{y1_max},
             ticks: {{ color:"var(--muted-foreground)", stepSize:4, callback:function(v){{return v+" dB";}} }},
             grid: {{ drawOnChartArea:false }},
             title: {{ display:true, text:"SNR", color:"var(--muted-foreground)", font:{{size:10}} }} }}
      }}
    }},
    plugins: [{{
      id: "mhzAndBands",
      beforeDatasetsDraw: function(chart) {{
        var x = chart.scales.x, y = chart.scales.y, ctx = chart.ctx;
        ctx.save();
        bands.forEach(function(b) {{
          var x0 = x.getPixelForValue(b.start_i);
          var x1 = x.getPixelForValue(b.end_i);
          var pad = (x.getPixelForValue(1) - x.getPixelForValue(0)) / 2;
          if (!isFinite(pad)) pad = 4;
          ctx.fillStyle = "rgba(64, 180, 110, 0.18)";
          ctx.fillRect(x0 - pad, y.top, (x1 - x0) + 2 * pad, y.bottom - y.top);
        }});
        mhzIdx.forEach(function(i) {{
          var px = x.getPixelForValue(i);
          ctx.strokeStyle = "rgba(128,128,128,0.35)";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(px, y.top);
          ctx.lineTo(px, y.bottom);
          ctx.stroke();
        }});
        ctx.restore();
      }},
      afterDatasetsDraw: function(chart) {{
        var x = chart.scales.x, y = chart.scales.y, ctx = chart.ctx;
        ctx.save();
        ctx.fillStyle = "var(--foreground)";
        ctx.font = "10px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        var lastRight = [];
        bands.forEach(function(b) {{
          var x0 = x.getPixelForValue(b.start_i);
          var x1 = x.getPixelForValue(b.end_i);
          var cx = (x0 + x1) / 2;
          var text = b.label + " ≥80%";
          var w = ctx.measureText(text).width;
          var left = cx - w / 2, right = cx + w / 2;
          var row = b.row || 0;
          while (row < lastRight.length && left < lastRight[row] + 6) row++;
          lastRight[row] = right;
          ctx.fillText(text, cx, y.top + 2 + row * 12);
        }});
        ctx.restore();
      }}
    }}]
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
    ap.add_argument("--direction", default=None,
                    help="only this direction (e.g. tower->field or field->tower)")
    ap.add_argument("--title", default="MeshCore 500 kHz Link Test — delivery rate & SNR per channel")
    args = ap.parse_args(argv)

    if args.input:
        src = args.input
    else:
        src = newest_link_artifact(args.data_dir)

    rows = load_artifact(src)
    if args.direction:
        rows = filter_direction(rows, args.direction)
    channels = aggregate(rows)
    labels, datasets = build_series(channels)
    bands = assign_label_rows(good_bands(channels, threshold=80.0))
    mhz_idx = mhz_grid_indices(labels)

    f0 = channels[0].freq_mhz
    f1 = channels[-1].freq_mhz
    sizes = sorted({s for c in channels for s in c.sizes})
    dir_bit = f"{args.direction} · " if args.direction else ""
    band_bit = (f" · ≥80% all sizes: " + ", ".join(b["label"] for b in bands)
                if bands else " · no ≥80% all-size bands")
    sub = (f"{src.stem.replace('link_test-', '').replace('_', ' ')} · "
           f"{dir_bit}{f0:.1f}–{f1:.1f} MHz · {len(channels)} ch · "
           f"sizes {', '.join(str(s) for s in sizes)}{band_bit}")

    if args.out:
        out = args.out
    elif args.direction:
        safe = args.direction.replace("->", "-to-")
        out = src.with_name(f"{src.stem}_{safe}_chart.html")
    else:
        out = src.with_name(src.stem + "_chart.html")
    title = args.title
    if args.direction and args.title == ap.get_default("title"):
        title = f"{args.title} ({args.direction})"
    out.parent.mkdir(parents=True, exist_ok=True)
    y1_min, y1_max = snr_axis_range(datasets)
    out.write_text(render_html(labels, datasets, title, sub, bands=bands,
                               mhz_idx=mhz_idx, y1_min=y1_min, y1_max=y1_max))
    print(f"wrote {out}  ({len(channels)} channels, sizes {sizes})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
