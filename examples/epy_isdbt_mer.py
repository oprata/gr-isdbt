"""
ISDB-T MER per layer (Embedded Python Block for GRC 3.8, also used by
isdbt_dynamic.py).

Input : time_deinterleaver output, one vector of data carriers per OFDM symbol
        (13*96*2**(mode-1) complex: layer A carriers first, then B, then C).
Msg in: 'tmcc' (from the patched tmcc_decoder) - updates segments and
        constellation of each layer automatically (optional).
Output: MER in dB of layers A, B and C, one value per input symbol (sync
        block). The value is the MER of the last complete window of
        `avg_symbols` OFDM symbols; with stride N only 1 of every N symbols is
        measured, to save CPU. An absent layer (or no complete window yet)
        outputs 0.

MER = 10*log10( sum|ideal|^2 / sum|received - ideal|^2 ), where "ideal" is the
nearest point of the layer's constellation (QPSK, 16QAM, 64QAM normalized to
unit average power, the same scaling used by isdbt.symbol_demapper).

Rodada 43: was a decim_block(avg_symbols). With 40 KB vectors the GR 3.8
buffers hold only ~4 items, so a block that needs 204 input items at once
can never run and stalls the whole flowgraph. Now it is a sync block that
accumulates internally and needs only 1 input item per call.
"""

import numpy as np
import pmt
from gnuradio import gr


def qam_decide(x, m):
    """Nearest point of a square M-QAM with unit average power (M = 4, 16, 64)."""
    k = int(round(np.sqrt(m)))
    norm = np.sqrt(2.0 * (m - 1) / 3.0)          # sqrt(2), sqrt(10), sqrt(42)
    def axis(v):
        lv = 2.0 * np.floor(v * norm / 2.0) + 1.0
        return np.clip(lv, -(k - 1), k - 1) / norm
    return axis(x.real) + 1j * axis(x.imag)


def mer_sums(x, m):
    """Returns (signal power sum, error power sum) for the carriers in x."""
    ideal = qam_decide(x, m)
    err = x - ideal
    return float(np.sum(np.abs(ideal) ** 2)), float(np.sum(np.abs(err) ** 2))


class blk(gr.sync_block):
    def __init__(self, mode=3, avg_symbols=204,
                 seg_a=1, const_a=4, seg_b=12, const_b=64, seg_c=0, const_c=64,
                 stride=1):
        self.carriers_per_segment = 96 * 2 ** (int(mode) - 1)
        self.vlen = 13 * self.carriers_per_segment
        gr.sync_block.__init__(
            self,
            name="ISDB-T MER",
            in_sig=[(np.complex64, self.vlen)],
            out_sig=[np.float32, np.float32, np.float32])
        self.mode = int(mode)
        self.avg_symbols = max(1, int(avg_symbols))
        self.stride = max(1, int(stride))     # use 1 of every `stride` symbols (CPU)
        self.layers = [(int(seg_a), int(const_a)),
                       (int(seg_b), int(const_b)),
                       (int(seg_c), int(const_c))]
        self.count = 0                         # symbols seen in the current window
        self.sig = [0.0, 0.0, 0.0]
        self.err = [0.0, 0.0, 0.0]
        self.value = [0.0, 0.0, 0.0]           # MER of the last complete window
        self.message_port_register_in(pmt.intern("tmcc"))
        self.set_msg_handler(pmt.intern("tmcc"), self.handle_tmcc)

    def handle_tmcc(self, msg):
        if not pmt.is_dict(msg):
            return
        if not pmt.to_bool(pmt.dict_ref(msg, pmt.intern("valid"), pmt.PMT_F)):
            return
        new = []
        for name in ("A", "B", "C"):
            present = pmt.to_bool(pmt.dict_ref(msg, pmt.intern(name + "_present"), pmt.PMT_F))
            seg = pmt.to_long(pmt.dict_ref(msg, pmt.intern(name + "_segments"), pmt.from_long(0)))
            const = pmt.to_long(pmt.dict_ref(msg, pmt.intern(name + "_constellation"), pmt.from_long(0)))
            new.append((seg, const) if present and const in (4, 16, 64) else (0, 0))
        self.layers = new

    def _measure(self, syms):
        """syms: 2-D array (symbols x carriers) to add to the current window."""
        start = 0
        for layer, (seg, const) in enumerate(self.layers):
            ncar = seg * self.carriers_per_segment
            if seg > 0 and const in (4, 16, 64):
                s, e = mer_sums(syms[:, start:start + ncar], const)
                self.sig[layer] += s
                self.err[layer] += e
            start += ncar

    def _close_window(self):
        for layer, (seg, const) in enumerate(self.layers):
            if seg > 0 and const in (4, 16, 64) and self.sig[layer] > 0:
                e = self.err[layer]
                self.value[layer] = 10.0 * np.log10(self.sig[layer] / e) if e > 0 else 99.0
            else:
                self.value[layer] = 0.0
        self.sig = [0.0, 0.0, 0.0]
        self.err = [0.0, 0.0, 0.0]
        self.count = 0

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(output_items[0])
        i = 0
        while i < n:
            # chunk = rest of the current window that is in this call
            m = min(n - i, self.avg_symbols - self.count)
            first = (-self.count) % self.stride          # first index with count % stride == 0
            if first < m:
                self._measure(x[i + first:i + m:self.stride])
            for layer in range(3):
                output_items[layer][i:i + m] = self.value[layer]
            self.count += m
            if self.count >= self.avg_symbols:
                self._close_window()
            i += m
        return n
