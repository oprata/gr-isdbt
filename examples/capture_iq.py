#!/usr/bin/env python3
"""
capture_iq.py - record raw IQ from the LimeSDR Mini with the same RF settings
used by stbcast_analyzer.py, so the ISDB-T chain can be re-run offline.

Usage:
    python3 capture_iq.py                       # 10 s, gain 30 dB, /dev/shm/iq_capture.cfile
    python3 capture_iq.py -g 30 -s 10 -o /dev/shm/iq_capture.cfile

The output is gr_complex (complex64): 8 bytes per sample, ~65 MB per second.
Writing to /dev/shm (RAM) avoids disk stalls that could make the LimeSDR drop
samples during the capture.
"""
import argparse
import os
import shutil
import sys
import time

from gnuradio import blocks, gr
import limesdr

SAMP_RATE = 8e6 * 64 / 63
CENTER_FREQ = 473142857


class Capture(gr.top_block):
    def __init__(self, gain, nsamples, path):
        gr.top_block.__init__(self, "capture_iq")
        src = limesdr.source('', 0, '')
        src.set_sample_rate(SAMP_RATE)
        src.set_center_freq(CENTER_FREQ, 0)
        src.set_bandwidth(8e6, 0)
        src.set_digital_filter(SAMP_RATE, 0)
        src.set_gain(int(gain), 0)
        src.set_antenna(3, 0)
        src.calibrate(8e6, 0)
        self.head = blocks.head(gr.sizeof_gr_complex, nsamples)
        sink = blocks.file_sink(gr.sizeof_gr_complex, path, False)
        sink.set_unbuffered(False)
        self.connect(src, self.head, sink)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-g", "--gain", type=float, default=30)
    ap.add_argument("-s", "--seconds", type=float, default=10)
    ap.add_argument("-o", "--output", default="/dev/shm/iq_capture.cfile")
    a = ap.parse_args()

    nsamples = int(a.seconds * SAMP_RATE)
    need = nsamples * 8
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(a.output))).free
    if free < need * 1.1:
        sys.exit(f"ERROR: not enough space for {need / 1e6:.0f} MB in "
                 f"{os.path.dirname(a.output)} ({free / 1e6:.0f} MB free). "
                 f"Use -s with fewer seconds or another -o.")

    print(f"Capturing {a.seconds:g} s ({nsamples} samples, {need / 1e6:.0f} MB), "
          f"gain {a.gain:g} dB -> {a.output}")
    tb = Capture(a.gain, nsamples, a.output)
    t0 = time.time()
    tb.start()
    tb.wait()
    dt = time.time() - t0
    size = os.path.getsize(a.output)
    print(f"Done: {size} bytes ({size // 8} samples) in {dt:.1f} s "
          f"(includes stream start-up; expected >= {a.seconds:g} s)")
    if size // 8 != nsamples:
        print("WARNING: sample count differs from the requested amount")


if __name__ == "__main__":
    main()
