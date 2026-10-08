#!/usr/bin/env python3
"""
udp_capture.py - records the TS datagrams of one layer exactly as the analyzer
sends them (content + arrival time), so the live stream can be analyzed and
replayed elsewhere with the same timing.

  python3 udp_capture.py 5004 20 cap_a          # layer A, 20 s
  -> cap_a.ts    (payloads concatenated: a normal TS file)
     cap_a.tim   (arrival time of each datagram, float64 seconds, + its length)

Run it while the analyzer runs and NO viewer is playing that port (leave the
analyzer on the Constellation tab), because only one program receives a
unicast UDP port.
"""
import socket
import struct
import sys
import time

port, secs, out = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
s.bind(("", port))
s.settimeout(1.0)
n = nbytes = 0
t0 = time.monotonic()
with open(out + ".ts", "wb") as fts, open(out + ".tim", "wb") as ftim:
    while time.monotonic() - t0 < secs:
        try:
            d = s.recv(65536)
        except socket.timeout:
            continue
        t = time.monotonic() - t0
        if d[:1] != b"\x47" and len(d) >= 12 and (d[0] >> 6) == 2:
            d = d[12:]                       # RTP header (if --ts-proto rtp)
        fts.write(d)
        ftim.write(struct.pack("<dI", t, len(d)))
        n += 1
        nbytes += len(d)
print("%d datagrams, %.1f MB, %.2f Mb/s average" % (n, nbytes / 1e6, nbytes * 8 / secs / 1e6))
