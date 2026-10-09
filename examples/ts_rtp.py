"""
ts_rtp.py - sends the TS of a layer over UDP, 7 TS packets per datagram, either
plain (default, what the TS viewer plays) or with an RTP header (RFC 2250, MP2T
payload type 33, for external receivers), and inserts a PAT in layers that do
not carry one.

Rodada 48: plain UDP is the default. libVLC 3 (3.0.9 on the stb, 3.0.20 in the
sandbox) freezes on RTP/MP2T at these rates: its rtp module hands the TS to a
chained demuxer and every picture arrives "too late to be displayed" - it
shows one frame and stops. The same happens with ffmpeg's rtp_mpegts sender,
so it is not this sender. The same datagrams without the RTP header play
smoothly (udp://@:port).

Rodada 49: the PCR is regenerated on the network output (pcr_restamp). The
PCR carried by the bench signal is invalid (not monotonic, reserved bits
wrong): a file still plays (VLC falls back on the PTS), but a live player
locks its clock to the PCR and freezes after a few frames. The new PCR is the
packet position at the exact layer rate (from the TMCC) anchored to the PTS.

Why the PAT insertion: in the bench signal the PAT is only transmitted in
layer B, although it lists the one-seg program whose PMT (0x1FC8) is carried in
layer A. Without a PAT a player cannot open layer A. The PatRegistry keeps the
last PAT seen in any layer; a layer whose own stream has no PAT gets, every
100 ms, a PAT with only the programs whose PMT PIDs actually occur in that
layer (plus the NIT entry if the NIT PID occurs). The PAT replaces a null
packet when there is one in the datagram (keeps the bit rate), otherwise it is
appended. Only the RTP output is changed; a recorded file keeps the TS exactly
as received.
"""

import collections
import socket
import struct
import threading
import time

import numpy as np
from gnuradio import gr

TS = 188
PKTS_PER_DGRAM = 7
NULL_PID = 0x1FFF


# ---------------------------------------------------------------- PAT helpers
def _crc32_table():
    tab = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        tab.append(c & 0xFFFFFFFF)
    return tab


_CRC_TAB = _crc32_table()


def crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TAB[((crc >> 24) ^ b) & 0xFF]
    return crc


def parse_pat(pkt):
    """188-byte TS packet with PID 0 and payload_unit_start -> (ts_id,
    version, [(program_number, pid), ...]) or None."""
    if pkt[0] != 0x47 or not (pkt[1] & 0x40):
        return None
    afc = (pkt[3] >> 4) & 3
    pos = 4
    if afc in (2, 3):
        pos += 1 + pkt[4]
    if afc == 2 or pos >= TS:
        return None
    pos += 1 + pkt[pos]                      # pointer field
    if pos + 8 > TS or pkt[pos] != 0x00:     # table_id PAT
        return None
    slen = ((pkt[pos + 1] & 0x0F) << 8) | pkt[pos + 2]
    end = pos + 3 + slen
    if end > TS or slen < 9:
        return None
    if crc32_mpeg(bytes(pkt[pos:end])) != 0:
        return None
    ts_id = (pkt[pos + 3] << 8) | pkt[pos + 4]
    version = (pkt[pos + 5] >> 1) & 0x1F
    progs = []
    for p in range(pos + 8, end - 4, 4):
        num = (pkt[p] << 8) | pkt[p + 1]
        pid = ((pkt[p + 2] & 0x1F) << 8) | pkt[p + 3]
        progs.append((num, pid))
    return ts_id, version, progs


def build_pat(ts_id, version, progs, cc):
    """One TS packet carrying a PAT section with the given programs."""
    body = bytearray()
    for num, pid in progs:
        body += struct.pack("!HH", num, 0xE000 | pid)
    slen = 5 + len(body) + 4
    sec = bytearray([0x00, 0xB0 | ((slen >> 8) & 0x0F), slen & 0xFF,
                     ts_id >> 8, ts_id & 0xFF, 0xC1 | ((version & 0x1F) << 1), 0, 0])
    sec += body
    sec += struct.pack("!I", crc32_mpeg(sec))
    pkt = bytearray([0x47, 0x40, 0x00, 0x10 | (cc & 0x0F), 0x00]) + sec
    pkt += b"\xff" * (TS - len(pkt))
    return bytes(pkt)


# ---------------------------------------------------------------- PCR helpers
PTS_PERIOD = float(2 ** 33) / 90000.0      # 33-bit 90 kHz clock wraps after ~26.5 h
CR_VALUE = [1 / 2, 2 / 3, 3 / 4, 5 / 6, 7 / 8]


def layer_packet_rate(mode, segments, constellation, rate_idx, guard=1 / 16.0):
    """Exact TS packet rate (packets/s) of an ISDB-T layer: per OFDM frame of
    204 symbols a layer carries segments * C * bits * code_rate / 8 packets
    (C = 96 * 2**(mode-1) carriers per segment). Mode 3, GI 1/16, 1 segment
    QPSK 3/4 -> 72 packets per 218.48 ms frame = 329.5 packets/s."""
    if not segments or constellation not in (4, 16, 64) or not 0 <= rate_idx <= 4:
        return None
    bits = {4: 2, 16: 4, 64: 6}[constellation]
    carriers = 96 * 2 ** (int(mode) - 1)
    per_frame = segments * carriers * bits * CR_VALUE[rate_idx] / 8.0
    tu = 2048 * 2 ** (int(mode) - 1) / (512e6 / 63)      # useful symbol time
    frame = 204 * tu * (1 + guard)
    return per_frame / frame


def pes_pts(pkt):
    """PTS (seconds) of an audio/video PES starting in this TS packet, or None."""
    if not (pkt[1] & 0x40):
        return None
    afc = (pkt[3] >> 4) & 3
    if afc not in (1, 3):
        return None
    pos = 4 if afc == 1 else 5 + int(pkt[4])
    if pos + 14 > TS or pkt[pos] or pkt[pos + 1] or pkt[pos + 2] != 1:
        return None
    if not 0xC0 <= pkt[pos + 3] <= 0xEF:      # audio / video streams only
        return None
    if not (pkt[pos + 7] & 0x80):
        return None
    q = [int(v) for v in pkt[pos + 9:pos + 14]]
    v = (((q[0] >> 1) & 7) << 30) | (q[1] << 22) | ((q[2] >> 1) << 15) | (q[3] << 7) | (q[4] >> 1)
    return v / 90000.0


def has_pcr(pkt):
    return ((pkt[3] >> 4) & 2) and pkt[4] >= 7 and (pkt[5] & 0x10)


class PatRegistry(object):
    """Last PAT seen in any layer (shared by the RTP sinks of one receiver)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pat = None                      # (ts_id, version, progs)

    def update(self, pat):
        with self.lock:
            self.pat = pat

    def get(self):
        with self.lock:
            return self.pat


# ---------------------------------------------------------------- the block
class ts_rtp_sink(gr.sync_block):
    """Input: vectors of 1316 bytes (7 TS packets). Sends one RTP datagram per
    vector to (host, port). Sync block, any number of items per call.

    Rodada 48 - paced output. The decoder delivers the TS in bursts (the
    s2v before this block holds up to ~800 datagrams = 1 MB, plus the
    time/byte deinterleavers). Sending a burst back to back overflows the
    receiver's UDP buffer (Linux default ~208 KB): VLC loses datagrams and the
    H.264 picture freezes until the next key frame. work() now only queues the
    datagrams; a sender thread spreads them at the measured layer rate, keeping
    about `delay` seconds in the queue, and stamps each datagram with its own
    send time (90 kHz)."""

    def __init__(self, host, port, registry=None, inject_pat=True, ssrc=None, ttl=1,
                 paced=True, delay=0.3, max_queue_s=3.0, rtp_header=True,
                 pkt_rate=None, pcr_restamp=True, pcr_margin=0.5):
        gr.sync_block.__init__(self, name="ts_rtp_sink",
                               in_sig=[(np.uint8, TS * PKTS_PER_DGRAM)], out_sig=None)
        self.addr = (host, int(port))
        self.ttl = ttl
        self.rtp_header = rtp_header         # False: plain TS over UDP (7 packets/datagram)
        self.sock = None
        self._open()
        self.seq = int(np.random.randint(0, 65536))
        self.ssrc = ssrc if ssrc is not None else int(np.random.randint(1, 2 ** 31))
        self.registry = registry
        self.inject_pat = inject_pat and registry is not None
        self.pids = set()                    # PIDs seen in this layer
        self.own_pat_time = 0.0              # last time this layer had its own PAT
        self.last_inject = 0.0
        self.pat_cc = 0
        self.pat_key = None
        self.pat_body = None
        self.injecting = False               # shown by the viewer ("PAT inserted")
        self.datagrams = 0
        self.errors = 0
        self.dropped = 0                     # queue overflow (output slower than input)
        # PCR regeneration (Rodada 49): the PCR of the bench signal is invalid
        # (not monotonic, reserved bits wrong). A live player locks its clock
        # to the PCR and freezes. The PCR is rewritten from the packet position
        # at the exact layer rate (pkt_rate, from the TMCC), anchored to the
        # stream's own PTS so that every frame arrives >= pcr_margin before
        # its presentation time.
        self.pkt_rate = pkt_rate
        self.pcr_restamp = bool(pcr_restamp and pkt_rate)
        self.pcr_margin = float(pcr_margin)
        self.pkt_index = 0                   # packets received (original stream)
        self.min_offset = None               # min(PTS - t) seen before anchoring
        self.anchor = None                   # PCR(t) = anchor + t
        self.pcr_discontinuity = False       # flag the next PCR after a re-anchor
        self.reanchors = 0
        self.pcr_rewritten = 0
        # pacing
        self.paced = paced
        self.delay = float(delay)
        self.max_queue_s = float(max_queue_s)
        self.queue = collections.deque()
        self.cond = threading.Condition()
        self.rate = 0.0                      # datagrams / s, measured at the input
        self._rate_t0 = None
        self._rate_n = 0
        self._running = False
        self._thread = None

    def _open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
            if int(self.addr[0].split(".")[0]) in range(224, 240):
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
        except (OSError, ValueError):
            pass

    def start(self):
        if self.sock is None:
            self._open()
        if self.paced and (self._thread is None or not self._thread.is_alive()):
            self._running = True
            self._thread = threading.Thread(target=self._sender, name="rtp%d" % self.addr[1])
            self._thread.daemon = True
            self._thread.start()
        return True

    def _pat_for_layer(self):
        pat = self.registry.get() if self.registry else None
        if pat is None:
            return None
        ts_id, version, progs = pat
        keep = [(n, p) for n, p in progs if p in self.pids]
        if not any(n != 0 for n, _ in keep):
            return None                      # no PMT of the PAT occurs in this layer
        key = (ts_id, version, tuple(keep))
        if key != self.pat_key:
            self.pat_key = key
            self.pat_body = (ts_id, version, keep)
        return self.pat_body

    def _send(self, payload):
        if self.rtp_header:
            ts90 = int(time.monotonic() * 90000) & 0xFFFFFFFF     # per datagram
            payload = struct.pack("!BBHII", 0x80, 33, self.seq, ts90, self.ssrc) + payload
            self.seq = (self.seq + 1) & 0xFFFF
        try:
            if self.sock is None:
                self._open()
            self.sock.sendto(payload, self.addr)
            self.datagrams += 1
        except OSError:
            self.errors += 1

    def _measure_rate(self, n, now):
        """Input rate in datagrams/s over windows of >= 1 s (smoothed)."""
        if self._rate_t0 is None:          # the first chunk only starts the clock
            self._rate_t0 = now
            self._rate_n = 0
            return
        self._rate_n += n
        dt = now - self._rate_t0
        if dt >= 1.0:
            r = self._rate_n / dt
            self.rate = r if self.rate == 0 else 0.8 * self.rate + 0.2 * r
            self._rate_t0 = now
            self._rate_n = 0

    def _sender(self):
        step = 0.004                         # pacing granularity (s)
        credit = 0.0
        last = time.monotonic()
        while self._running:
            with self.cond:
                if not self.queue:
                    self.cond.wait(0.1)
                    credit = 0.0
                    last = time.monotonic()
                    continue
                rate = self.rate
                q = len(self.queue)
            now = time.monotonic()
            dt = now - last
            last = now
            if rate <= 0 or (self.pcr_restamp and self.anchor is None):
                # rate (or PCR anchor) not known yet (first ~1 s after start /
                # rebuild): hold the data (pre-buffer) instead of guessing
                time.sleep(step)
                continue
            # keep about `delay` seconds queued: speed up / slow down gently
            # (a fast catch-up is itself a burst for the receiver)
            target = rate * self.delay
            send_rate = rate * min(1.25, max(0.8, 1.0 + 0.5 * (q - target) / max(target, 1.0)))
            credit = min(credit + send_rate * dt, 64.0)
            n = int(credit)
            if n:
                credit -= n
                for _ in range(n):
                    with self.cond:
                        if not self.queue:
                            break
                        item = self.queue.popleft()
                    self._send(self._restamp(*item))
            time.sleep(step)

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        if n == 0:
            return 0
        pk = x.reshape(n * PKTS_PER_DGRAM, TS)
        pid = ((pk[:, 1].astype(np.int32) & 0x1F) << 8) | pk[:, 2]
        self.pids.update(np.unique(pid).tolist())
        now = time.time()
        pat_rows = np.nonzero((pid == 0) & ((pk[:, 1] & 0x40) != 0))[0]
        if len(pat_rows):
            self.own_pat_time = now
            parsed = parse_pat(pk[pat_rows[-1]])
            if parsed is not None and self.registry is not None:
                self.registry.update(parsed)
        need_pat = (self.inject_pat and now - self.own_pat_time > 0.5)
        self.injecting = need_pat and self._pat_for_layer() is not None
        base_idx = self.pkt_index
        self.pkt_index += n * PKTS_PER_DGRAM
        pcr_rows = set()
        if self.pcr_restamp:
            pcr_rows = set(np.nonzero(((pk[:, 3] >> 4) & 2 > 0) & (pk[:, 4] >= 7)
                                      & ((pk[:, 5] & 0x10) > 0))[0].tolist())
            self._track_pts(pk, base_idx)
        out = []
        for i in range(n):
            payload = x[i].tobytes()
            rows = tuple(r - i * PKTS_PER_DGRAM for r in range(i * PKTS_PER_DGRAM, (i + 1) * PKTS_PER_DGRAM)
                         if r in pcr_rows)
            if self.injecting and now - self.last_inject >= 0.1:
                ts_id, version, progs = self.pat_body
                pat = build_pat(ts_id, version, progs, self.pat_cc)
                self.pat_cc = (self.pat_cc + 1) & 0x0F
                self.last_inject = now
                nulls = np.nonzero(pid[i * PKTS_PER_DGRAM:(i + 1) * PKTS_PER_DGRAM] == NULL_PID)[0]
                if len(nulls):
                    j = int(nulls[0]) * TS
                    payload = payload[:j] + pat + payload[j + TS:]
                else:
                    # own 188-byte datagram: a larger datagram is truncated by
                    # VLC, which sizes its buffer on the first one (1316 bytes)
                    out.append((pat, None, ()))
            out.append((payload, base_idx + i * PKTS_PER_DGRAM, rows))
        if not self.paced:
            for item in out:
                self._send(self._restamp(*item))
            return n
        self._measure_rate(n, time.monotonic())
        with self.cond:
            self.queue.extend(out)
            holding = self.rate <= 0 or (self.pcr_restamp and self.anchor is None)
            limit = 400000 if holding else max(4000, int(self.rate * self.max_queue_s))
            while len(self.queue) > limit:   # sender cannot keep up: drop oldest
                self.queue.popleft()
                self.dropped += 1
            self.cond.notify()
        return n

    def _track_pts(self, pk, base_idx):
        """Anchor (and, if the content jumps, re-anchor) the regenerated PCR
        on the PTS of the audio/video PES of this layer."""
        rows = np.nonzero((pk[:, 1] & 0x40) != 0)[0]
        for r in rows.tolist():
            pts = pes_pts(pk[r])
            if pts is None:
                continue
            t = (base_idx + r) / self.pkt_rate
            if self.anchor is None:
                off = pts - t
                if self.min_offset is not None:   # keep near the first (wrap-safe)
                    off = self.min_offset + ((off - self.min_offset + PTS_PERIOD / 2) % PTS_PERIOD
                                             - PTS_PERIOD / 2)
                self.min_offset = off if self.min_offset is None else min(self.min_offset, off)
            else:
                d = (pts - (self.anchor + t) + PTS_PERIOD / 2) % PTS_PERIOD - PTS_PERIOD / 2
                if d < 0.0 or d > 10.0:           # content jumped (loop / restart)
                    self.anchor = pts - t - self.pcr_margin
                    self.pcr_discontinuity = True
                    self.reanchors += 1
        if self.anchor is None:
            elapsed = self.pkt_index / self.pkt_rate
            if self.min_offset is not None and elapsed >= 0.8:
                self.anchor = self.min_offset - self.pcr_margin
            elif elapsed >= 1.5:                  # no PTS at all: leave the PCR alone
                self.pcr_restamp = False

    def _restamp(self, payload, idx0, rows):
        if not rows or idx0 is None or not self.pcr_restamp or self.anchor is None:
            return payload
        b = bytearray(payload)
        for r in rows:
            pcr = (self.anchor + (idx0 + r) / self.pkt_rate) % PTS_PERIOD
            base = int(pcr * 90000) & 0x1FFFFFFFF
            o = r * TS
            if self.pcr_discontinuity:
                b[o + 5] |= 0x80                  # discontinuity_indicator
                self.pcr_discontinuity = False
            b[o + 6] = (base >> 25) & 0xFF
            b[o + 7] = (base >> 17) & 0xFF
            b[o + 8] = (base >> 9) & 0xFF
            b[o + 9] = (base >> 1) & 0xFF
            b[o + 10] = ((base & 1) << 7) | 0x7E
            b[o + 11] = 0
            self.pcr_rewritten += 1
        return bytes(b)

    def queue_seconds(self):
        return len(self.queue) / self.rate if self.rate > 0 else 0.0

    def stop(self):
        self._running = False
        if self._thread is not None:
            with self.cond:
                self.cond.notify()
            self._thread.join(1.0)
            self._thread = None
        self.queue.clear()
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
        return True
