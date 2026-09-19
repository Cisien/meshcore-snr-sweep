"""Low-level KISS modem client for MeshCore KISS-TNC radios.

This module speaks the standard KISS framing (KA9Q/K3MC) plus the MeshCore
``SetHardware`` (0x06) extension commands to a Station G3 running the stock
``Station_G3_ESP32_kiss_modem`` firmware. No custom firmware is required.

It is confirmed against the firmware source (``examples/kiss_modem/``) and the
protocol doc at ``docs.meshcore.io/kiss_modem_protocol``:

* Frame:      ``0xC0 <type> <escaped data> 0xC0``
* Host->TNC Data (queue a raw LoRa packet):  type 0x00, payload 1..255 bytes
* Host->TNC SetHardware:                     type 0x06, first data byte = sub-cmd
* SetHardware responses:                     type 0x06, first data byte = sub-cmd | 0x80
                                             (or 0xF0 OK / 0xF1 Error)
* Unsolicited (also type 0x06):              0xF8 TxDone, 0xF9 RxMeta
* Received raw LoRa packet from air:         type 0x00, followed by 0xF9 RxMeta

Sub-commands used here (see ``docs/PROTOCOL.md`` for the full table):

* 0x09 SetRadio        data = freq(4 LE) + bw(4 LE) + sf(1) + cr(1)  -> OK/Err
* 0x0A SetTxPower      data = power dBm (1)                          -> OK/Err
* 0x0B GetRadio        -> 0x8B  freq(4) + bw(4) + sf(1) + cr(1)
* 0x0D GetCurrentRssi  -> 0x8D  rssi dBm (1, signed)
* 0x0E IsChannelBusy   -> 0x8E  0x00 clear / 0x01 busy
* 0x0F GetAirtime      data = packet len (1) -> 0x8F  ms (4 LE)
* 0x10 GetNoiseFloor   -> 0x90  noise floor dBm (2, int16 LE)
* 0x11 GetVersion      -> 0x91  version (1) + reserved (1)
* 0x17 Ping            -> 0x97  (Pong, empty)
* 0x19 SetSignalReport data = enable (1)          -> 0x9A  status (1)

TX is opaque: a KISS Data frame is transmitted verbatim via ``startSendRaw``
with no MeshCore application-layer framing, so arbitrary 1..255 byte payloads
are legal. This is what the packet-size link test relies on.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Set

import serial

log = logging.getLogger("snr_sweep.kiss")

# KISS framing bytes.
FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

# Host->TNC command type bytes.
CMD_DATA = 0x00
CMD_TXDELAY = 0x01
CMD_PERSISTENCE = 0x02
CMD_SLOTTIME = 0x03
CMD_FULLDUPLEX = 0x05
CMD_SETHARDWARE = 0x06

# SetHardware sub-commands.
HW_GET_IDENTITY = 0x01
HW_SET_RADIO = 0x09
HW_SET_TX_POWER = 0x0A
HW_GET_RADIO = 0x0B
HW_GET_CURRENT_RSSI = 0x0D
HW_IS_CHANNEL_BUSY = 0x0E
HW_GET_AIRTIME = 0x0F
HW_GET_NOISE_FLOOR = 0x10
HW_GET_VERSION = 0x11
HW_GET_BATTERY = 0x13
HW_PING = 0x17
HW_SET_SIGNAL_REPORT = 0x19

# SetHardware response / unsolicited sub-commands (data[0]).
RESP_RADIO = 0x8B
RESP_TX_POWER = 0x8C
RESP_CURRENT_RSSI = 0x8D
RESP_CHANNEL_BUSY = 0x8E
RESP_AIRTIME = 0x8F
RESP_NOISE_FLOOR = 0x90
RESP_VERSION = 0x91
RESP_DEVICE_NAME = 0x96
RESP_PONG = 0x97
RESP_SIGNAL_REPORT = 0x9A
RESP_OK = 0xF0
RESP_ERROR = 0xF1
RESP_TX_DONE = 0xF8
RESP_RX_META = 0xF9

MAX_PACKET_SIZE = 255
DEFAULT_TIMEOUT_S = 2.0


class KissError(Exception):
    """Raised when a SetHardware request errors or times out."""


def i8(value: int) -> int:
    """Interpret an unsigned byte as a signed 8-bit integer."""
    return value - 256 if value >= 128 else value


def i16_le(lo: int, hi: int) -> int:
    """Interpret two little-endian bytes as a signed 16-bit integer."""
    v = lo | (hi << 8)
    return v - 65536 if v >= 32768 else v


def u32_le(b: bytes) -> int:
    return int.from_bytes(b[:4], "little")


@dataclass
class Event:
    """An unsolicited / out-of-band frame from the radio.

    ``kind`` is one of ``"rx_packet"``, ``"rx_meta"``, ``"tx_done"``.
    ``data`` carries the relevant bytes; convenience fields may be populated.
    """

    kind: str
    data: bytes
    # rx_meta
    snr: Optional[float] = None
    rssi: Optional[float] = None
    # tx_done
    ok: Optional[bool] = None
    # rx_packet
    length: Optional[int] = None

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        if self.kind == "rx_meta":
            return f"RxMeta snr={self.snr:+.2f} dB rssi={self.rssi} dBm"
        if self.kind == "tx_done":
            return f"TxDone ok={self.ok}"
        return f"RxPacket len={self.length}"


class KissClient:
    """A serial KISS modem for a MeshCore radio.

    Usage::

        with KissClient("/dev/ttyACM0") as mc:
            if not mc.ping():
                raise RuntimeError("radio not responding")
            mc.set_radio(902_250_000, 500_000, 8, 5)
            mc.set_tx_power(7)
            print(mc.get_noise_floor())
    """

    def __init__(self, port: str, baud: int = 115200, timeout: float = DEFAULT_TIMEOUT_S):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser: Optional[serial.Serial] = None
        self._running = False
        self._reader: Optional[threading.Thread] = None
        # Synchronous request/response handshake (owned by _resp_cond).
        self._resp_cond = threading.Condition()
        self._responses: List[bytes] = []  # full SetHardware response frames
        # Out-of-band event queue (rx_packet / rx_meta / tx_done).
        self._events: List[Event] = []
        self._event_cond = threading.Condition()
        self._rx_bytes: int = 0
        self._tx_bytes: int = 0

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> "KissClient":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        if self._ser is not None:
            return
        self._ser = serial.Serial(self.port, baudrate=self.baud, timeout=0.1)
        self._ser.reset_input_buffer()
        self._running = True
        self._reader = threading.Thread(target=self._reader_loop, name="kiss-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._running = False
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    # ------------------------------------------------------------- framing (encode)

    @staticmethod
    def _encode_frame(type_byte: int, data: bytes) -> bytes:
        out = bytearray()
        out.append(FEND)
        out.append(type_byte)
        for b in data:
            if b == FEND:
                out.extend((FESC, TFEND))
            elif b == FESC:
                out.extend((FESC, TFESC))
            else:
                out.append(b)
        out.append(FEND)
        return bytes(out)

    def _write_frame(self, type_byte: int, data: bytes) -> None:
        frame = self._encode_frame(type_byte, data)
        if self._ser is None:
            raise KissError("serial port is not open")
        self._ser.write(frame)
        self._ser.flush()
        self._tx_bytes += len(frame)

    # ------------------------------------------------------------- framing (decode)

    def _reader_loop(self) -> None:
        buf: bytearray = bytearray()
        in_frame = False
        escaped = False
        while self._running:
            if self._ser is None:
                break
            data = self._ser.read(512)
            for b in data:
                if b == FEND:
                    if in_frame:
                        if len(buf) > 0:
                            self._dispatch(buf[0], bytes(buf[1:]))
                    buf = bytearray()
                    escaped = False
                    in_frame = True
                elif b == FESC:
                    if in_frame:
                        escaped = True
                else:
                    if in_frame:
                        if escaped:
                            if b == TFEND:
                                buf.append(FEND)
                            elif b == TFESC:
                                buf.append(FESC)
                            escaped = False
                        else:
                            buf.append(b)

    def _dispatch(self, type_byte: int, data: bytes) -> None:
        if type_byte == CMD_DATA:
            self._push_event(Event(kind="rx_packet", data=data, length=len(data)))
            return
        if type_byte == CMD_SETHARDWARE and data:
            sub = data[0]
            payload = data[1:]
            if sub == RESP_TX_DONE:
                ok = bool(payload[0]) if payload else False
                self._push_event(Event(kind="tx_done", data=payload, ok=ok))
                return
            if sub == RESP_RX_META and len(payload) >= 2:
                self._push_event(
                    Event(kind="rx_meta", data=payload, snr=i8(payload[0]) / 4.0, rssi=float(i8(payload[1])))
                )
                return
            self._push_response(data)
        # Other frames (e.g. TNC control commands we did not ask for) are ignored.

    def _push_response(self, data: bytes) -> None:
        """Queue a full SetHardware response (data[0] = sub-command)."""
        with self._resp_cond:
            self._responses.append(data)
            self._resp_cond.notify_all()

    # ------------------------------------------------------------- request/response

    def _request(self, sub_cmd: int, payload: bytes, acceptable: Set[int], parse, timeout: Optional[float] = None):
        """Send a SetHardware request and wait for the matching response.

        ``acceptable`` lists the response sub-cmds that satisfy this request;
        other responses are drained and discarded. The single ``_resp_cond``
        lock makes this safe against the reader thread and against stray
        out-of-band frames. ``parse`` receives the full response data
        (``data[0]`` = sub-command, ``data[1:]`` = payload).
        """
        if self._ser is None:
            raise KissError("serial port is not open")
        with self._resp_cond:
            self._write_frame(CMD_SETHARDWARE, bytes([sub_cmd]) + payload)
            deadline = (timeout or self.timeout)
            end = time.monotonic() + deadline
            while True:
                for i, d in enumerate(self._responses):
                    if d and d[0] in acceptable:
                        del self._responses[i]
                        return parse(d)
                    del self._responses[i]  # drain non-matching responses
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise KissError(f"no response to SetHardware 0x{sub_cmd:02X} within {deadline}s")
                self._resp_cond.wait(timeout=remaining)

    # ------------------------------------------------------------- high-level commands

    def ping(self) -> bool:
        """Return True if the radio replies to a Ping (0x97 Pong)."""
        try:
            self._request(HW_PING, b"", {RESP_PONG, RESP_OK}, lambda p: True)
            return True
        except KissError:
            return False

    def get_version(self) -> int:
        def parse(p):
            if p and p[0] == RESP_VERSION and len(p) >= 2:
                return p[1]
            raise KissError(f"bad version response: {p!r}")

        return self._request(HW_GET_VERSION, b"", {RESP_VERSION}, parse)

    def set_radio(self, freq_hz: int, bw_hz: int, sf: int, cr: int) -> bool:
        """Tune the radio. Returns True on OK, False on error."""

        def parse(p):
            if p and p[0] == RESP_OK:
                return True
            raise KissError(f"SetRadio rejected: {p!r}")

        payload = int(freq_hz).to_bytes(4, "little") + int(bw_hz).to_bytes(4, "little") + bytes([int(sf), int(cr)])
        return self._request(HW_SET_RADIO, payload, {RESP_OK, RESP_ERROR}, parse)

    def set_tx_power(self, dbm: int) -> bool:
        def parse(p):
            if p and p[0] == RESP_OK:
                return True
            raise KissError(f"SetTxPower rejected: {p!r}")

        return self._request(HW_SET_TX_POWER, bytes([int(dbm)]), {RESP_OK, RESP_ERROR}, parse)

    def get_current_rssi(self) -> float:
        def parse(p):
            if p and p[0] == RESP_CURRENT_RSSI and len(p) >= 2:
                return float(i8(p[1]))
            raise KissError(f"bad RSSI response: {p!r}")

        return self._request(HW_GET_CURRENT_RSSI, b"", {RESP_CURRENT_RSSI}, parse)

    def get_noise_floor(self) -> int:
        def parse(p):
            if p and p[0] == RESP_NOISE_FLOOR and len(p) >= 3:
                return i16_le(p[1], p[2])
            raise KissError(f"bad noise floor response: {p!r}")

        return self._request(HW_GET_NOISE_FLOOR, b"", {RESP_NOISE_FLOOR}, parse)

    def is_channel_busy(self) -> bool:
        def parse(p):
            if p and p[0] == RESP_CHANNEL_BUSY and len(p) >= 2:
                return p[1] == 0x01
            raise KissError(f"bad channel busy response: {p!r}")

        return self._request(HW_IS_CHANNEL_BUSY, b"", {RESP_CHANNEL_BUSY}, parse)

    def get_airtime(self, packet_len: int) -> int:
        def parse(p):
            if p and p[0] == RESP_AIRTIME and len(p) >= 5:
                return u32_le(p[1:5])
            raise KissError(f"bad airtime response: {p!r}")

        return self._request(HW_GET_AIRTIME, bytes([int(packet_len)]), {RESP_AIRTIME}, parse)

    def set_signal_report(self, enabled: bool = True) -> bool:
        def parse(p):
            if p and p[0] == RESP_SIGNAL_REPORT:
                return bool(p[1]) if len(p) >= 2 else enabled
            return enabled

        return self._request(HW_SET_SIGNAL_REPORT, bytes([1 if enabled else 0]), {RESP_SIGNAL_REPORT}, parse)

    # ------------------------------------------------------------- transmit / receive

    def transmit(self, payload: bytes | bytearray) -> None:
        """Queue one raw LoRa packet for transmission (opaque, 1..255 bytes).

        The radio transmits it via ``startSendRaw`` with no MeshCore framing.
        Completion is reported asynchronously as a ``tx_done`` event; poll it
        with :meth:`wait_tx_done` or :meth:`drain_events`.
        """
        payload = bytes(payload)
        if not (1 <= len(payload) <= MAX_PACKET_SIZE):
            raise ValueError(f"payload must be 1..{MAX_PACKET_SIZE} bytes, got {len(payload)}")
        self._write_frame(CMD_DATA, payload)

    def set_txdelay(self, units_10ms: int) -> None:
        """KISS TXDELAY in 10 ms units (firmware default 50 = 500 ms)."""
        self._write_frame(CMD_TXDELAY, bytes([max(0, int(units_10ms)) & 0xFF]))

    def set_fullduplex(self, enabled: bool) -> None:
        """KISS full-duplex: nonzero bypasses CSMA (packets TX after TXDELAY)."""
        self._write_frame(CMD_FULLDUPLEX, bytes([1 if enabled else 0]))

    def wait_tx_done(self, timeout: Optional[float] = None) -> bool:
        """Block until the next TxDone event. Returns True if transmission succeeded."""
        deadline = timeout if timeout is not None else self.timeout
        with self._event_cond:
            while not any(e.kind == "tx_done" for e in self._events):
                if not self._event_cond.wait(timeout=deadline):
                    raise KissError(f"no TxDone within {deadline}s")
            for i, e in enumerate(self._events):
                if e.kind == "tx_done":
                    del self._events[i]
                    return bool(e.ok)
        return False  # pragma: no cover

    def wait_rx_packet(self, timeout: Optional[float] = None, expect_len: Optional[int] = None) -> Optional[Event]:
        """Block until a received raw LoRa packet arrives.

        If ``expect_len`` is given, keep waiting until a packet of that length
        is seen (ignoring other-length packets). Returns the matching Event or
        None on timeout.
        """
        deadline = timeout if timeout is not None else self.timeout
        with self._event_cond:
            end = time.monotonic() + deadline
            while True:
                for i, e in enumerate(self._events):
                    if e.kind == "rx_packet" and (expect_len is None or e.length == expect_len):
                        del self._events[i]
                        return e
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                self._event_cond.wait(timeout=remaining)

    def drain_events(self) -> List[Event]:
        """Return and clear all currently-queued out-of-band events."""
        with self._event_cond:
            out, self._events = self._events, []
            return out

    def next_rx_meta(self, timeout: Optional[float] = None) -> Optional[Event]:
        """Block for the next RxMeta (SNR/RSSI) event, or None on timeout."""
        deadline = timeout if timeout is not None else self.timeout
        with self._event_cond:
            while not any(e.kind == "rx_meta" for e in self._events):
                if not self._event_cond.wait(timeout=deadline):
                    return None
            for i, e in enumerate(self._events):
                if e.kind == "rx_meta":
                    del self._events[i]
                    return e

    # ------------------------------------------------------------- introspection

    @property
    def bytes_rx(self) -> int:
        return self._rx_bytes

    @property
    def bytes_tx(self) -> int:
        return self._tx_bytes

    def _push_event(self, event: Event) -> None:
        with self._event_cond:
            self._events.append(event)
            self._event_cond.notify_all()
