#!/usr/bin/env python3
"""
ts_check.py - quick MPEG-TS sanity check (sync, TEI, continuity, PAT/PMT).

Usage:
    python3 ts_check.py /tmp/ts_layer_a /tmp/ts_layer_b
    python3 ts_check.py --json /tmp/ts_layer_b     # one JSON line per file
"""
import json
import sys
from collections import Counter, defaultdict

PKT = 188

STREAM_TYPES = {
    0x01: "MPEG-1 video", 0x02: "MPEG-2 video", 0x03: "MPEG-1 audio",
    0x04: "MPEG-2 audio", 0x05: "private sections", 0x06: "PES private data",
    0x0B: "DSM-CC U-N", 0x0D: "DSM-CC sections", 0x0F: "AAC ADTS",
    0x11: "AAC LATM", 0x1B: "H.264 video", 0x24: "HEVC video", 0x08: "DSM-CC",
}


def section_payload(pkt):
    """Returns the section bytes of a packet with payload_unit_start set, or None."""
    if not (pkt[1] & 0x40):
        return None
    afc = (pkt[3] >> 4) & 0x3
    pos = 4
    if afc in (2, 3):
        pos += 1 + pkt[4]
    if afc == 2 or pos >= PKT:
        return None
    pointer = pkt[pos]
    pos += 1 + pointer
    return pkt[pos:] if pos < PKT else None


def parse_pat(sec):
    if not sec or sec[0] != 0x00:
        return {}
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    body = sec[8:3 + length - 4]
    progs = {}
    for i in range(0, len(body) - 3, 4):
        prog = (body[i] << 8) | body[i + 1]
        pid = ((body[i + 2] & 0x1F) << 8) | body[i + 3]
        if prog != 0:
            progs[prog] = pid
    return progs


def parse_pmt(sec):
    if not sec or sec[0] != 0x02:
        return []
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    pinfo = ((sec[10] & 0x0F) << 8) | sec[11]
    pos = 12 + pinfo
    end = 3 + length - 4
    streams = []
    while pos + 5 <= end and pos + 5 <= len(sec):
        stype = sec[pos]
        pid = ((sec[pos + 1] & 0x1F) << 8) | sec[pos + 2]
        esl = ((sec[pos + 3] & 0x0F) << 8) | sec[pos + 4]
        streams.append((pid, stype))
        pos += 5 + esl
    return streams


def check(path, quiet=False):
    out = print if not quiet else (lambda *a, **k: None)
    res = {"file": path, "bytes": 0, "packets": 0, "sync_errors": 0, "tei": 0,
           "cc_errors": 0, "nulls": 0, "null_pct": None, "useful": 0,
           "loss_pct": None, "pids": 0, "pid_list": [], "pat": False,
           "result": "missing"}
    try:
        data = open(path, "rb").read()
    except OSError as e:
        out(f"{path}: {e}")
        return res
    n = len(data) // PKT
    res.update(bytes=len(data), packets=n)
    out(f"=== {path}")
    out(f"size: {len(data)} bytes, packets: {n}, leftover: {len(data) % PKT} bytes")
    if n == 0:
        out("EMPTY FILE")
        res["result"] = "empty"
        return res

    sync_bad, tei = 0, 0
    first_bad = []
    pids = Counter()
    tei_pids = Counter()
    last_cc = {}
    cc_err = Counter()
    cc_missing = Counter()   # packets inferred as lost from CC gaps
    pat, pmts = {}, {}

    for i in range(n):
        p = data[i * PKT:(i + 1) * PKT]
        if p[0] != 0x47:
            sync_bad += 1
            if len(first_bad) < 5:
                first_bad.append(i * PKT)
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        pids[pid] += 1
        if p[1] & 0x80:
            tei += 1
            tei_pids[pid] += 1
            continue
        afc = (p[3] >> 4) & 0x3
        cc = p[3] & 0x0F
        if pid != 0x1FFF and afc in (1, 3):
            if pid in last_cc:
                prev = last_cc[pid]
                if cc != (prev + 1) % 16 and cc != prev:
                    cc_err[pid] += 1
                    cc_missing[pid] += (cc - prev - 1) % 16
            last_cc[pid] = cc
        if pid == 0 and not pat:
            pat = parse_pat(section_payload(p))
        elif pid in pat.values() and pid not in pmts:
            s = parse_pmt(section_payload(p))
            if s:
                pmts[pid] = s

    pct = 100.0 * sync_bad / n
    out(f"sync byte errors : {sync_bad} ({pct:.2f}%)" +
          (f"  first offsets: {first_bad}" if first_bad else ""))
    out(f"TEI (uncorrectable) packets: {tei} ({100.0 * tei / n:.2f}%)")
    out(f"continuity errors: {sum(cc_err.values())}")
    nulls = pids.get(0x1FFF, 0)
    out(f"null packets     : {nulls} ({100.0 * nulls / n:.2f}%), useful packets: {n - nulls - sync_bad}")
    cc_rx = sum(c for p_, c in pids.items() if p_ != 0x1FFF)
    miss = sum(cc_missing.values())
    if cc_rx:
        loss = 100.0 * miss / (cc_rx + miss)
        out(f"estimated loss   : >= {loss:.1f}% (from CC gaps on {cc_rx} non-null packets, {miss} missing)")
    out(f"distinct PIDs    : {len(pids)}")
    for pid, cnt in pids.most_common(10):
        extra = []
        if cc_err[pid]:
            extra.append(f"cc_err={cc_err[pid]}")
        if tei_pids[pid]:
            extra.append(f"tei={tei_pids[pid]}")
        out(f"  PID 0x{pid:04X}: {cnt:8d} ({100.0 * cnt / n:5.1f}%) {' '.join(extra)}")
    if pat:
        out("PAT programs     : " + ", ".join(f"{p}->PMT 0x{v:04X}" for p, v in pat.items()))
    else:
        out("PAT              : not found")
    for pmt_pid, streams in pmts.items():
        desc = ", ".join(f"0x{pid:04X}:{STREAM_TYPES.get(t, hex(t))}" for pid, t in streams)
        out(f"  PMT 0x{pmt_pid:04X}: {desc}")
    if sync_bad == 0 and tei == 0 and sum(cc_err.values()) == 0:
        res["result"] = "clean"
    elif pct > 50:
        res["result"] = "no_sync"
    else:
        res["result"] = "errors"
    out({"clean": "RESULT: clean TS",
         "no_sync": "RESULT: not a valid TS (no sync) -> decoding chain problem",
         "errors": "RESULT: valid TS structure with errors"}[res["result"]])
    out()
    res.update(sync_errors=sync_bad, tei=tei, cc_errors=sum(cc_err.values()),
               nulls=nulls, null_pct=round(100.0 * nulls / n, 2),
               useful=n - nulls - sync_bad,
               loss_pct=round(100.0 * miss / (cc_rx + miss), 1) if cc_rx else None,
               pids=len(pids), pid_list=[f"0x{p_:04X}" for p_, _ in pids.most_common()],
               pat=bool(pat))
    return res


if __name__ == "__main__":
    args = sys.argv[1:]
    as_json = "--json" in args
    files = [a for a in args if a != "--json"]
    if not files:
        print(__doc__)
        sys.exit(1)
    for f in files:
        r = check(f, quiet=as_json)
        if as_json:
            print(json.dumps(r))
