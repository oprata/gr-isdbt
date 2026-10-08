#!/usr/bin/env python3
"""
run_offline.py - run stbcast_analyzer.py on a recorded IQ file instead of the
LimeSDR, as fast as the CPU allows, then check the layer B transport stream.

Reports:
  - real-time factor (>= 1.0x: this PC processes the chain faster than the
    signal arrives)
  - throughput vs layer B capacity, using the TMCC frames actually decoded
  - CPU time per GNU Radio thread (the busiest thread is the bottleneck)
  - ts_check of /tmp/ts_layer_b

Options let one parameter of the flowgraph be overridden without touching the
.grc, so the SAME IQ file can be decoded with different settings:
    --interp 0|1     OFDM Synchronization "Interpolate" (sampling clock correction)
    --throttle       feed the file at the real sample rate (emulates the live
                     pacing of the LimeSDR instead of "as fast as possible")
    --taps FILE.json hash the output of each block of the layer B chain (per
                     64 KiB segment) and save it; compare two runs with
                     compare_taps.py to find the first block whose output differs

Usage (from the examples/ directory):
    stdbuf -oL -eL python3 run_offline.py /dev/shm/iq_capture.cfile 2>&1 | tee offline.log
    stdbuf -oL -eL python3 run_offline.py --interp 1 /dev/shm/iq_capture.cfile 2>&1 | tee offline_interp1.log
"""
import argparse
import os
import sys
import threading
import time

from gnuradio import blocks, gr
import limesdr

SAMP_RATE = 8e6 * 64 / 63
LAYER_B_PKTS_PER_S = 11865      # mode 3, 64QAM, CR 3/4, GI 1/16, 12 segments
FRAME_S = 0.2184                # ISDB-T frame, mode 3, GI 1/16
TS_FILE = "/tmp/ts_layer_b"


class FileAsLimeSource(gr.hier_block2):
    """Looks like limesdr.source to the generated code; streams a .cfile."""

    def __init__(self, path, throttle=False):
        gr.hier_block2.__init__(self, "file_as_lime_source",
                                gr.io_signature(0, 0, 0),
                                gr.io_signature(1, 1, gr.sizeof_gr_complex))
        self.src = blocks.file_source(gr.sizeof_gr_complex, path, False)
        if throttle:
            self.thr = blocks.throttle(gr.sizeof_gr_complex, SAMP_RATE, True)
            self.connect(self.src, self.thr, self)
        else:
            self.connect(self.src, self)

    # RF setters called by the generated flowgraph: ignored offline
    def set_sample_rate(self, *a, **k): pass
    def set_center_freq(self, *a, **k): pass
    def set_bandwidth(self, *a, **k): pass
    def set_digital_filter(self, *a, **k): pass
    def set_gain(self, *a, **k): pass
    def set_antenna(self, *a, **k): pass
    def calibrate(self, *a, **k): pass
    def set_nco(self, *a, **k): pass
    def set_tcxo_dac(self, *a, **k): pass


class ThreadProfiler(threading.Thread):
    """Samples /proc/self/task/*/stat; keeps the last CPU time seen per thread."""

    def __init__(self, period=0.5):
        super().__init__(daemon=True)
        self.period = period
        self.ticks = {}          # tid -> (name, utime+stime)
        self.running = True
        self.hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])

    def sample(self):
        base = "/proc/self/task"
        for tid in os.listdir(base):
            try:
                with open(f"{base}/{tid}/stat") as f:
                    s = f.read()
            except OSError:
                continue
            name = s[s.index("(") + 1:s.rindex(")")]
            fields = s[s.rindex(")") + 2:].split()
            self.ticks[tid] = (name, int(fields[11]) + int(fields[12]))

    def run(self):
        while self.running:
            self.sample()
            time.sleep(self.period)

    def report(self, wall, top=12):
        rows = sorted(self.ticks.values(), key=lambda x: -x[1])[:top]
        print(f"CPU per thread (top {top}; 100% = one full core during {wall:.1f} s):")
        for name, t in rows:
            sec = t / self.hz
            print(f"  {name:<18s} {sec:7.1f} s  {100.0 * sec / wall:5.0f}%")


# Layer B chain of the generated flowgraph (ids as in stbcast_analyzer.py) and how
# to convert the item counters of each port into "TS packets" (204-byte packets
# before Reed-Solomon / 188-byte after), for mode 3, 64QAM, CR 3/4, 12 segments.
SYM_PER_OFDM_B = 12 * 384                 # 64QAM symbols of layer B per OFDM symbol
BYTES_PER_SYM = 6 * 3 / 4 / 8             # 6 bits x CR 3/4 / 8 = 0.5625 decoded bytes
CHAIN_B = [
    # (attribute, port kind, port, label, items -> packets)
    ("isdbt_tmcc_decoder_0", "out", 0, "tmcc_decoder out (OFDM symbols)",
     lambda n: n * SYM_PER_OFDM_B * BYTES_PER_SYM / 204),
    ("isdbt_time_deinterleaver_0", "out", 0, "time_deinterleaver out",
     lambda n: n * SYM_PER_OFDM_B * BYTES_PER_SYM / 204),
    ("isdbt_symbol_demapper_0", "out", 1, "symbol_demapper out B",
     lambda n: n * SYM_PER_OFDM_B * BYTES_PER_SYM / 204),
    ("isdbt_bit_deinterleaver_0", "out", 0, "bit_deinterleaver B out (symbols)",
     lambda n: n * BYTES_PER_SYM / 204),
    ("isdbt_viterbi_decoder_0", "in", 0, "viterbi B in (symbols)",
     lambda n: n * BYTES_PER_SYM / 204),
    ("isdbt_viterbi_decoder_0", "out", 0, "viterbi B out (bytes)",
     lambda n: n / 204),
    ("isdbt_byte_deinterleaver_0", "in", 0, "byte_deinterleaver B in (bytes)",
     lambda n: n / 204),
    ("isdbt_byte_deinterleaver_0", "out", 0, "byte_deinterleaver B out (packets)",
     lambda n: n),
    ("isdbt_energy_descrambler_0", "out", 0, "energy_descrambler B out",
     lambda n: n),
    ("isdbt_reed_solomon_dec_isdbt_0", "in", 0, "reed_solomon B in (204 B)",
     lambda n: n),
    ("isdbt_reed_solomon_dec_isdbt_0", "out", 0, "reed_solomon B out (188 B)",
     lambda n: n),
]


TAP_POINTS = [
    ("isdbt_ofdm_synchronization_0", 0),
    ("isdbt_tmcc_decoder_0", 0),
    ("isdbt_frequency_deinterleaver_0", 0),
    ("isdbt_time_deinterleaver_0", 0),
    ("isdbt_symbol_demapper_0", 1),
    ("isdbt_bit_deinterleaver_0", 0),
    ("isdbt_viterbi_decoder_0", 0),
    ("isdbt_byte_deinterleaver_0", 0),
    ("isdbt_energy_descrambler_0", 0),
    ("isdbt_reed_solomon_dec_isdbt_0", 0),
]
SEG_BYTES = 64 * 1024


def make_tap(itemsize):
    """Sink that hashes the raw bytes of a stream in fixed 64 KiB segments."""
    import hashlib
    import numpy

    class HashTap(gr.sync_block):
        def __init__(self):
            gr.sync_block.__init__(self, name="hash_tap",
                                   in_sig=[(numpy.uint8, itemsize)], out_sig=None)
            self.h = hashlib.sha1()
            self.fill = 0
            self.hashes = []
            self.items = 0

        def work(self, input_items, output_items):
            data = input_items[0].tobytes()
            self.items += len(input_items[0])
            pos = 0
            while pos < len(data):
                take = min(SEG_BYTES - self.fill, len(data) - pos)
                self.h.update(data[pos:pos + take])
                self.fill += take
                pos += take
                if self.fill == SEG_BYTES:
                    self.hashes.append(self.h.hexdigest()[:16])
                    self.h = hashlib.sha1()
                    self.fill = 0
            return len(input_items[0])

    return HashTap()


def add_taps(tb):
    taps = []
    for attr, port in TAP_POINTS:
        blk = getattr(tb, attr, None)
        if blk is None:
            print(f"(tap: block {attr} not found)")
            continue
        itemsize = blk.output_signature().sizeof_stream_item(port)
        tap = make_tap(itemsize)
        tb.connect((blk, port), (tap, 0))
        taps.append((f"{attr}:{port}", itemsize, tap))
    return taps


def save_taps(taps, path, info):
    import json
    out = {"info": info, "seg_bytes": SEG_BYTES, "stages": []}
    for name, itemsize, tap in taps:
        out["stages"].append({"name": name, "itemsize": itemsize,
                              "items": tap.items, "hashes": tap.hashes})
    with open(path, "w") as f:
        json.dump(out, f)
    print(f"Tap hashes saved to {path}")


def chain_report(tb, nframes):
    """Item counters along the layer B chain, converted to TS packets."""
    expected = nframes * 2592 if nframes else None
    print("Layer B chain (item counters converted to TS packets"
          + (f"; {nframes} frames x 2592 = {expected} expected" if expected else "") + "):")
    prev = None
    for attr, kind, port, label, conv in CHAIN_B:
        blk = getattr(tb, attr, None)
        if blk is None:
            print(f"  {label:<38s} (block {attr} not found)")
            continue
        try:
            n = blk.nitems_written(port) if kind == "out" else blk.nitems_read(port)
        except Exception as e:
            print(f"  {label:<38s} (counter unavailable: {e})")
            continue
        pk = conv(n)
        delta = "" if prev is None else f"  delta {pk - prev:+10.0f}"
        pct = f"  {100.0 * pk / expected:5.1f}%" if expected else ""
        print(f"  {label:<38s} {n:12d} items = {pk:10.0f} pkts{pct}{delta}")
        prev = pk


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iq_file")
    ap.add_argument("--interp", choices=["0", "1"],
                    help="override OFDM Synchronization 'Interpolate'")
    ap.add_argument("--throttle", action="store_true",
                    help="feed the IQ file at the real sample rate")
    ap.add_argument("--taps", metavar="FILE.json",
                    help="save per-block output hashes for compare_taps.py")
    a = ap.parse_args()

    iq_path = os.path.abspath(a.iq_file)
    if not os.path.isfile(iq_path):
        sys.exit(f"ERROR: {iq_path} not found")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    nsamples = os.path.getsize(iq_path) // 8
    duration = nsamples / SAMP_RATE
    print(f"IQ file: {iq_path}  ({nsamples} samples = {duration:.2f} s of signal)")

    # Replace the LimeSDR by the file before the flowgraph is built
    limesdr.source = lambda *x, **k: FileAsLimeSource(iq_path, a.throttle)

    import isdbt
    overrides = ["throttle (real-time pace)"] if a.throttle else []
    if a.interp is not None:
        orig_sync = isdbt.ofdm_synchronization
        interp = a.interp == "1"
        isdbt.ofdm_synchronization = lambda mode, cp, _i: orig_sync(mode, cp, interp)
        overrides.append(f"ofdm_synchronization interpolate={interp}")
    print("Overrides: " + (", ".join(overrides) if overrides else "none (as in the .grc)"))

    from PyQt5 import Qt
    qapp = Qt.QApplication(sys.argv[:1])      # the generated class is a QWidget
    import stbcast_analyzer

    if os.path.exists(TS_FILE):
        os.remove(TS_FILE)

    tb = stbcast_analyzer.stbcast_analyzer()

    # Count TMCC frames through the 'tmcc' message port (patched tmcc_decoder)
    frames = {"ok": 0, "bad": 0}
    counter = None
    try:
        import pmt

        class TmccCounter(gr.basic_block):
            def __init__(self):
                gr.basic_block.__init__(self, name="tmcc_counter", in_sig=None, out_sig=None)
                self.message_port_register_in(pmt.intern("in"))
                self.set_msg_handler(pmt.intern("in"), self.handle)

            def handle(self, msg):
                valid = pmt.to_bool(pmt.dict_ref(msg, pmt.intern("valid"), pmt.PMT_F))
                frames["ok" if valid else "bad"] += 1

        counter = TmccCounter()
        tb.msg_connect((tb.isdbt_tmcc_decoder_0, "tmcc"), (counter, "in"))
    except Exception as e:                    # unpatched library or different id
        print(f"(TMCC frame counter not available: {e})")

    taps = add_taps(tb) if a.taps else []

    prof = ThreadProfiler()
    prof.start()
    t0 = time.time()
    tb.start()
    waiter = threading.Thread(target=tb.wait, daemon=True)
    waiter.start()
    waiter.join(timeout=max(120.0, 30 * duration))
    wall = time.time() - t0
    prof.running = False
    if waiter.is_alive():
        print("WARNING: flowgraph did not finish by itself; stopping it")
        tb.stop()
        tb.wait()
    else:
        tb.stop()
    qapp.processEvents()

    rtf = duration / wall if wall > 0 else 0.0
    print()
    print(f"Processing time : {wall:.1f} s for {duration:.2f} s of signal")
    if a.throttle:
        print("Real-time factor: n/a (throttled to the real sample rate)")
    else:
        print(f"Real-time factor: {rtf:.2f}x  "
              f"({'faster than real time' if rtf >= 1 else 'SLOWER than real time -> live receiver cannot keep up'})")

    import ts_check
    r = ts_check.check(TS_FILE, quiet=True)
    nframes = frames["ok"] + frames["bad"]
    if nframes:
        thr = 100.0 * r["packets"] / (nframes * FRAME_S * LAYER_B_PKTS_PER_S)
        print(f"TMCC frames     : {frames['ok']} OK, {frames['bad']} not OK")
        print(f"Throughput      : {thr:.1f}% of layer B capacity for the decoded frames "
              f"({r['packets']} packets)")
    else:
        print(f"TS packets      : {r['packets']}  (capacity for {duration:.2f} s ~ "
              f"{duration * LAYER_B_PKTS_PER_S:.0f})")
    print(f"Est. loss (CC)  : {r['loss_pct']}%   sync errors: {r['sync_errors']}   "
          f"nulls: {r['null_pct']}%   PIDs: {', '.join(r['pid_list'])}")
    print(f"Result          : {r['result']}")
    print()
    chain_report(tb, nframes)
    if a.taps:
        save_taps(taps, a.taps, {"iq": iq_path, "throttle": a.throttle,
                                 "interp": a.interp, "frames": nframes})
    print()
    prof.report(wall)
    print()
    print("Full report:")
    ts_check.check(TS_FILE)


if __name__ == "__main__":
    main()
