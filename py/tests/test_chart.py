"""Tests for the link-test chart renderer (no broker, no browser)."""

from __future__ import annotations

import json

from snr_sweep import chart as C


def _rows():
    # 2 channels, 3 sizes, 2 directions, 1 trial = 12 rows.
    # ch0: all delivered, snr spread; ch1: one 255B miss + no snr on one row.
    rows = []
    seq = 0
    for ci, freq in ((0, 902.3), (1, 902.4)):
        for size in (1, 128, 255):
            for direction in ("tower->field", "field->tower"):
                seq += 1
                rx_ok = not (ci == 1 and size == 255 and direction == "field->tower")
                snr = None if (ci == 1 and direction == "field->tower" and size == 255) else round(10 + ci, 2)
                rows.append({
                    "channel_index": ci, "freq_mhz": freq, "freq_hz": int(freq * 1e6),
                    "packet_size": size, "direction": direction, "trial": 1,
                    "rx_ok": rx_ok, "snr_db": snr, "rssi_dbm": -50.0, "seq": seq,
                    "ts": "t", "node_rx": "x", "tx_ok": None,
                })
    return rows


def test_aggregate_delivery_and_snr():
    chans = C.aggregate(_rows())
    assert [c.channel_index for c in chans] == [0, 1]
    c0, c1 = chans
    # ch0: every direction delivered for every size (2/2 each)
    for s in (1, 128, 255):
        assert c0.sizes[s] == {"ok": 2, "total": 2}
    # ch1: 255B has 1 miss out of 2
    assert c1.sizes[255] == {"ok": 1, "total": 2}
    # snr: ch0 all 10.0 (6 rows), ch1 has 1 row with snr=None (the 255B field->tower miss)
    assert len(c0.snrs) == 6
    assert len(c1.snrs) == 5


def test_build_series_shapes():
    labels, ds = C.build_series(C.aggregate(_rows()))
    assert labels == [902.3, 902.4]
    # 3 delivery lines + 1 snr line
    assert len(ds) == 4
    delivery = [d for d in ds if d["yAxisID"] == "y"]
    snr = [d for d in ds if d["yAxisID"] == "y1"]
    assert len(delivery) == 3 and len(snr) == 1
    # ch1 255B delivery = 50%, ch1 snr = median of 3 values = 11.0
    by_label = {d["label"]: d for d in ds}
    assert by_label["255 B delivery"]["data"][1] == 50.0
    assert by_label["1 B delivery"]["data"][0] == 100.0
    assert snr[0]["data"][1] == 11.0


def test_render_html_is_selfcontained(tmp_path):
    labels, ds = C.build_series(C.aggregate(_rows()))
    html = C.render_html(labels, ds, "T", "S")
    out = tmp_path / "c.html"
    out.write_text(html)
    text = out.read_text()
    assert 'id="c"' in text
    assert "chart.js@4" in text
    assert json.dumps(labels) in text
    assert "animation: false" in text


def test_load_and_newest(tmp_path):
    a = tmp_path / "link_test-a-b-1.json"
    b = tmp_path / "link_test-a-b-2.json"
    a.write_text(json.dumps(_rows()))
    b.write_text(json.dumps(_rows()))
    assert C.newest_link_artifact(tmp_path) == b
    assert len(C.load_artifact(b)) == 12
