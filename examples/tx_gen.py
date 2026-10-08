#!/usr/bin/env python3
"""
tx_gen.py - ISDB-T mode 3 / GI 1/16 baseband generator (gr-isdbt TX blocks) for offline
tests of the receiver. Each present layer carries a synthetic TS (PID 0x100+L,
continuity counter incrementing, payload = packet number) so ts_check can verify
continuity after decoding.

  tx_gen.py -o out.cfile -s 4 --partial 1 \
      --layer A:1:4:2:1 --layer B:12:64:2:2          # seg:const:rate_idx:I

rate_idx: 0=1/2 1=2/3 2=3/4 3=5/6 4=7/8; I in mode 3: 0, 1, 2, 4.
Several files can be concatenated (cat T1 T2 > seq.cfile) to emulate a
modulator that changes its parameters; the receiver sees a resync at each joint.
Used to validate isdbt_dynamic.py without the modulator (regression library).
"""
import argparse
import os
import numpy as np
import pmt
from gnuradio import gr, blocks, fft, digital, channels, dtv
from gnuradio.fft import window
try:
    import isdbt                      # GNU Radio 3.8
except ImportError:
    from gnuradio import isdbt        # GNU Radio 3.9+

MODE = 3
SAMP_RATE = 8e6 * 64 / 63
TOTAL = 2 ** (10 + MODE)
CR = [dtv.C1_2, dtv.C2_3, dtv.C3_4, dtv.C5_6, dtv.C7_8]
MOD = {4: dtv.MOD_QPSK, 16: dtv.MOD_16QAM, 64: dtv.MOD_64QAM}


def make_ts(path, pid, npk=16 * 8192):
    pk = np.zeros((npk, 188), np.uint8)
    pk[:, 0] = 0x47
    pk[:, 1] = (pid >> 8) & 0x1F
    pk[:, 2] = pid & 0xFF
    pk[:, 3] = 0x10 | (np.arange(npk) % 16)
    pk[:, 4:8] = np.arange(npk, dtype='<u4').view(np.uint8).reshape(npk, 4)
    pk[:, 8:] = (np.arange(180)[None, :] + np.arange(npk)[:, None]) & 0xFF
    pk.tofile(path)


class tx(gr.top_block):
    def __init__(self, out, seconds, partial, layers, noise, tsdir):
        gr.top_block.__init__(self, "tx_gen")
        segs = [layers.get(L, (0, 64, 0, 0))[0] for L in "ABC"]
        assert sum(segs) == 13, segs
        comb = isdbt.hierarchical_combinator(MODE, *segs)
        self.keep = [comb]
        for idx, L in enumerate("ABC"):
            if L not in layers:
                continue
            seg, const, rate, I = layers[L]
            ts = os.path.join(tsdir, "tx_ts_%s.ts" % L)
            make_ts(ts, 0x100 + idx)
            src = blocks.file_source(gr.sizeof_char, ts, True)
            s2v = blocks.stream_to_vector(gr.sizeof_char, 188)
            rs = dtv.dvbt_reed_solomon_enc(2, 8, 0x11d, 255, 239, 8, 51, 1)
            ed = isdbt.energy_dispersal(MODE, const, rate, seg)
            bi = isdbt.byte_interleaver(MODE, const, rate, seg)
            ic = dtv.dvbt_inner_coder(1, 1512 * 4, MOD[const], dtv.ALPHA4, CR[rate])
            v2s = blocks.vector_to_stream(gr.sizeof_char, 1512 * 4)
            cm = isdbt.carrier_modulation(MODE, seg, const)
            self.connect(src, s2v, rs, ed, bi, ic, v2s, cm, (comb, idx))
            self.keep += [src, s2v, rs, ed, bi, ic, v2s, cm]
        Ls = [layers.get(L, (0, 4, 0, 0)) for L in "ABC"]
        ti = isdbt.time_interleaver(MODE, Ls[0][0], Ls[0][3], Ls[1][0], Ls[1][3], Ls[2][0], Ls[2][3])
        fi = isdbt.frequency_interleaver(partial, MODE)
        sk = blocks.skiphead(gr.sizeof_gr_complex * 13 * 96 * 4, 2)
        ps = isdbt.pilot_signals(MODE)
        te = isdbt.tmcc_encoder(MODE, partial, Ls[0][1], Ls[1][1], Ls[2][1],
                                Ls[0][2], Ls[1][2], Ls[2][2],
                                Ls[0][3], Ls[1][3], Ls[2][3],
                                Ls[0][0], Ls[1][0], Ls[2][0])
        ff = fft.fft_vcc(TOTAL, False, window.rectangular(TOTAL), True, 1)
        cp = digital.ofdm_cyclic_prefixer(TOTAL, TOTAL + TOTAL // 16, 0, '')
        # scale so the OFDM signal has ~unit power, then AWGN (noise = voltage)
        sc = blocks.multiply_const_cc(1.0 / np.sqrt(5617.0))
        ch = channels.channel_model(noise_voltage=noise, frequency_offset=0.0,
                                    epsilon=1.0, taps=[1.0], noise_seed=0, block_tags=False)
        hd = blocks.head(gr.sizeof_gr_complex, int(seconds * SAMP_RATE))
        sink = blocks.file_sink(gr.sizeof_gr_complex, out, False)
        self.connect(comb, ti, fi, sk, ps, te, ff, cp, sc, ch, hd, sink)
        self.keep += [ti, fi, sk, ps, te, ff, cp, sc, ch, hd, sink]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("-s", "--seconds", type=float, default=4.0)
    ap.add_argument("--partial", type=int, default=1)
    ap.add_argument("--noise", type=float, default=0.03)
    ap.add_argument("--layer", action="append", required=True,
                    help="L:segments:const:rate_idx:I  e.g. B:12:64:2:2")
    a = ap.parse_args()
    layers = {}
    for spec in a.layer:
        L, s, c, r, i = spec.split(":")
        layers[L] = (int(s), int(c), int(r), int(i))
    tsdir = os.path.dirname(os.path.abspath(a.out))
    tb = tx(a.out, a.seconds, bool(a.partial), layers, a.noise, tsdir)
    tb.run()
    print("wrote", a.out, os.path.getsize(a.out) // 8, "samples")


if __name__ == "__main__":
    main()
