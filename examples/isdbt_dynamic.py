"""
isdbt_dynamic.py - builds the per-layer part of the ISDB-Tb receiver from the
TMCC that is actually being received, and rebuilds it when the TMCC changes.

Fixed part (stays in the .grc):
    LimeSDR -> LPF -> ofdm_synchronization -> tmcc_decoder -> constellation
Dynamic part (built here, from the 'tmcc' message port of the patched
tmcc_decoder):
    frame_resync -> frequency_deinterleaver(partial reception)
                 -> time_deinterleaver(segments, I of each layer)
                 -> symbol_demapper(segments, modulation of each layer)
                 -> for each present layer:
                      bit_deinterleaver -> viterbi(modulation, code rate)
                      -> byte_deinterleaver -> energy_descrambler -> reed_solomon
                      -> TS file (append) + BER pre/post
                 -> MER of each layer (epy_isdbt_mer.py)

Rebuild = stop / wait / reconnect / start of the whole flowgraph, from a worker
thread (never from a block thread). A full stop is used instead of lock/unlock
because, in GNU Radio 3.8-3.10, a new block connected by lock/unlock to an
existing buffer counts its input items from 0 while the stream tags of that
buffer keep the writer's absolute offsets: the 'frame_begin' tags of the
tmcc_decoder would reach the new chain at the wrong symbol. stop/start
re-creates every buffer, so all counters and tags start aligned (D21).

Works with GNU Radio 3.8 (import isdbt) and 3.9+ (from gnuradio import isdbt).
"""

import collections
import os
import queue
import threading
import time

import numpy as np
import pmt
from gnuradio import blocks, gr
from gnuradio import filter as gr_filter

try:
    import isdbt                      # GNU Radio 3.8 (gr-isdbt maint-3.8)
except ImportError:                   # GNU Radio 3.9+
    from gnuradio import isdbt

import epy_isdbt_mer

LAYER_NAMES = "ABC"
CR_TEXT = ["1/2", "2/3", "3/4", "5/6", "7/8"]
MOD_TEXT = {4: "QPSK", 16: "16QAM", 64: "64QAM"}


# --------------------------------------------------------------------------
# TMCC configuration
# --------------------------------------------------------------------------
LayerParams = collections.namedtuple(
    "LayerParams", "segments constellation rate interleaving")
TmccConfig = collections.namedtuple("TmccConfig", "partial layers")
ABSENT = LayerParams(0, 0, 0, 0)


def config_from_msg(msg):
    """PMT dict of the 'tmcc' port -> TmccConfig, or None if not valid."""
    if not pmt.is_dict(msg):
        return None

    def get(key, default):
        v = pmt.dict_ref(msg, pmt.intern(key), pmt.PMT_NIL)
        if pmt.is_null(v):
            return default
        return pmt.to_bool(v) if pmt.is_bool(v) else pmt.to_long(v)

    if not get("valid", False):
        return None
    layers = []
    for L in LAYER_NAMES:
        if get(L + "_present", False):
            layers.append(LayerParams(get(L + "_segments", 0), get(L + "_constellation", 0),
                                      get(L + "_rate", 0), get(L + "_interleaving", 0)))
        else:
            layers.append(ABSENT)
    return TmccConfig(bool(get("partial_reception", False)), tuple(layers))


def config_problem(cfg):
    """Returns a reason why the receiver cannot be built for cfg, or None."""
    present = [lp.segments > 0 for lp in cfg.layers]
    if not present[0]:
        return "layer A absent"
    if present[2] and not present[1]:
        return "layer C present without layer B"
    if sum(lp.segments for lp in cfg.layers) != 13:
        return "segments do not add up to 13"
    if cfg.partial and cfg.layers[0].segments != 1:
        return "partial reception with layer A != 1 segment"
    for lp in cfg.layers:
        if lp.segments and (lp.constellation not in MOD_TEXT or not 0 <= lp.rate <= 4):
            return "unsupported modulation / code rate"
    return None


def describe_layer(lp):
    if not lp.segments:
        return "-"
    return "%s %s I=%d %dseg" % (MOD_TEXT.get(lp.constellation, "?"),
                                 CR_TEXT[lp.rate] if 0 <= lp.rate <= 4 else "?",
                                 lp.interleaving, lp.segments)


def describe(cfg):
    if cfg is None:
        return "none"
    return "partial=%s | A: %s | B: %s | C: %s" % (
        "on" if cfg.partial else "off",
        describe_layer(cfg.layers[0]), describe_layer(cfg.layers[1]),
        describe_layer(cfg.layers[2]))


# --------------------------------------------------------------------------
# Helper blocks
# --------------------------------------------------------------------------
class frame_resync(gr.sync_block):
    """Pass-through that adds a 'resync' tag on the first 'frame_begin' it sees.

    The viterbi_decoder only aligns to the frame on a 'resync' tag, which the
    tmcc_decoder emits once, when it first locks. A chain built later would
    never get one, so this block re-creates that start condition.
    """

    def __init__(self, vlen):
        gr.sync_block.__init__(self, name="frame_resync",
                               in_sig=[(np.complex64, vlen)],
                               out_sig=[(np.complex64, vlen)])
        self.armed = True
        self.key_frame = pmt.intern("frame_begin")

    def work(self, input_items, output_items):
        n = len(output_items[0])
        output_items[0][:] = input_items[0][:n]
        if self.armed:
            start = self.nitems_read(0)
            for tag in self.get_tags_in_range(0, start, start + n):
                if pmt.eq(tag.key, self.key_frame):
                    self.add_item_tag(0, tag.offset, pmt.intern("resync"),
                                      pmt.intern("isdbt_dynamic"))
                    self.armed = False
                    break
        return n


class tmcc_watcher(gr.basic_block):
    """Message-only block: forwards every 'tmcc' message to a Python callback."""

    def __init__(self, callback):
        gr.basic_block.__init__(self, name="tmcc_watcher", in_sig=None, out_sig=None)
        self.callback = callback
        self.message_port_register_in(pmt.intern("tmcc"))
        self.set_msg_handler(pmt.intern("tmcc"), self.handle)

    def handle(self, msg):
        try:
            self.callback(msg)
        except Exception as e:          # never let an exception kill the msg thread
            print("[dyn] tmcc handler error: %r" % (e,), flush=True)


# --------------------------------------------------------------------------
# Optional Qt part (widgets are created once; only connected when used)
# --------------------------------------------------------------------------
def _wrap_widget(sink):
    from PyQt5 import Qt
    try:
        import sip
    except ImportError:
        from PyQt5 import sip
    getw = getattr(sink, "pyqwidget", None) or getattr(sink, "qwidget")
    return sip.wrapinstance(getw(), Qt.QWidget)


def _number_sink(title, vmin, vmax, unit=""):
    from gnuradio import qtgui
    s = qtgui.number_sink(gr.sizeof_float, 0, qtgui.NUM_GRAPH_HORIZ, 1)
    s.set_update_time(0.25)
    s.set_title(title)
    s.set_min(0, vmin)
    s.set_max(0, vmax)
    s.set_label(0, title)
    s.set_unit(0, unit)
    s.enable_autoscale(False)
    return s


class LayerGui(object):
    """Widgets of the analyzer: a status line in the overview tab and, per layer
    tab, BER pre-Viterbi, BER post-Viterbi and MER."""

    def __init__(self, tabs, overview_layout, layer_layouts, first_layer_tab=1):
        from PyQt5 import Qt, QtCore

        class Bridge(QtCore.QObject):
            changed = QtCore.pyqtSignal(object)

        self.tabs = tabs
        self.first_tab = first_layer_tab
        self.status = Qt.QLabel("TMCC: waiting for a valid frame...")
        self.status.setWordWrap(True)
        overview_layout.addWidget(self.status)
        self.sinks = []
        for i, L in enumerate(LAYER_NAMES):
            ber_pre = _number_sink("BER pre-Viterbi layer %s (log10)" % L, -10, 0)
            ber_post = _number_sink("BER post-Viterbi layer %s (log10)" % L, -35, 0)
            mer = _number_sink("MER layer %s" % L, 0, 40, "dB")
            for s in (mer, ber_pre, ber_post):
                layer_layouts[i].addWidget(_wrap_widget(s))
            self.sinks.append({"ber_pre": ber_pre, "ber_post": ber_post, "mer": mer})
            tabs.setTabEnabled(first_layer_tab + i, False)
        self.bridge = Bridge()
        self.bridge.changed.connect(self._update)      # queued: runs in the GUI thread

    def publish(self, info):
        """Thread-safe: called from the controller threads."""
        self.bridge.changed.emit(info)

    def _update(self, info):
        cfg = info.get("cfg")
        self.status.setText("TMCC: %s\nRebuilds: %d   last: %s" % (
            describe(cfg), info.get("rebuilds", 0), info.get("last", "-")))
        for i, L in enumerate(LAYER_NAMES):
            lp = cfg.layers[i] if cfg else ABSENT
            idx = self.first_tab + i
            self.tabs.setTabEnabled(idx, lp.segments > 0)
            self.tabs.setTabText(idx, "Layer %s" % L if not lp.segments
                                 else "Layer %s - %s" % (L, describe_layer(lp)))


def gui_from_generated(tb):
    """LayerGui using the 'tab_widget_layers' QTabWidget of the generated
    stbcast_analyzer (tab 0 = Overview, tabs 1-3 = layers A-C)."""
    return LayerGui(tb.tab_widget_layers, tb.tab_widget_layers_layout_0,
                    [tb.tab_widget_layers_layout_1, tb.tab_widget_layers_layout_2,
                     tb.tab_widget_layers_layout_3])


# --------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------
class DynamicLayers(object):
    """
    tb          : the running gr.top_block (generated flowgraph, fixed part)
    tmcc_block  : its isdbt.tmcc_decoder (patched, with the 'tmcc' message port)
    ts_dir      : TS files are <ts_dir>/ts_layer_a|b|c (1316-byte writes, append)
    hysteresis  : identical valid TMCC frames needed before (re)building
    gui         : LayerGui or None (headless)
    on_change   : optional callback(info dict) for a future Qt facade
    split_ts    : False -> one TS file per layer, appended after every rebuild
                  True  -> a new file per rebuild: ts_layer_b.01, .02, ... (tests)
    """

    def __init__(self, tb, tmcc_block, mode=3, ts_dir="/tmp", hysteresis=3,
                 gui=None, on_change=None, mer_stride=4, fresh_ts=True, split_ts=False,
                 log=None):
        self.tb = tb
        self.tmcc_block = tmcc_block
        self.mode = int(mode)
        self.vlen = 13 * 96 * 2 ** (self.mode - 1)
        self.ts_dir = ts_dir
        self.hysteresis = max(1, int(hysteresis))
        self.gui = gui
        self.on_change = on_change
        self.mer_stride = mer_stride
        self.split_ts = split_ts
        self.log = log or (lambda s: print("[dyn %s] %s" % (time.strftime("%H:%M:%S"), s),
                                           flush=True))
        self.ts_paths = [os.path.join(ts_dir, "ts_layer_" + L.lower()) for L in LAYER_NAMES]
        if fresh_ts:
            import glob
            for p in self.ts_paths:
                for f in glob.glob(p) + glob.glob(p + ".[0-9][0-9]"):
                    os.remove(f)

        # state
        self.active = None              # TmccConfig currently built
        self.candidate = None
        self.count = 0
        self.pending = None
        self.failed = None             # TmccConfig whose build raised an error
        self.frames_ok = 0
        self.frames_bad = 0
        self.last_msg_time = None
        self.rebuilds = 0
        self.rebuild_log = []           # (time, seconds, description)
        self.edges = []
        self.blocks = []
        self.ts_sinks = []
        self.probes = [None, None, None]
        self.lock = threading.Lock()

        self.watcher = tmcc_watcher(self._on_tmcc)
        tb.msg_connect((tmcc_block, "tmcc"), (self.watcher, "tmcc"))

        self.jobs = queue.Queue()
        self.worker = threading.Thread(target=self._worker, name="isdbt_dynamic", daemon=True)
        self.worker.start()

    # ---- TMCC messages (message thread: never touch the flowgraph here) ----
    def _on_tmcc(self, msg):
        cfg = config_from_msg(msg)
        with self.lock:
            self.last_msg_time = time.time()
            if cfg is None:
                self.frames_bad += 1
                return
            self.frames_ok += 1
            if cfg != self.candidate:
                self.candidate, self.count = cfg, 1
            else:
                self.count += 1
            if (self.count >= self.hysteresis and cfg != self.active
                    and cfg != self.pending and cfg != self.failed):
                problem = config_problem(cfg)
                if problem:
                    if self.count == self.hysteresis:
                        self.log("TMCC ignored (%s): %s" % (problem, describe(cfg)))
                    return
                self.pending = cfg
                self.log("TMCC stable for %d frames -> rebuild: %s" % (self.count, describe(cfg)))
                self.jobs.put(cfg)

    # ---- worker thread ----
    def _worker(self):
        while True:
            cfg = self.jobs.get()
            if cfg is None:
                return
            try:
                self._rebuild(cfg)
            except Exception as e:
                self.log("REBUILD FAILED: %r" % (e,))
            finally:
                with self.lock:
                    self.pending = None

    def _rebuild(self, cfg):
        t0 = time.time()
        tb = self.tb
        tb.stop()
        tb.wait()
        t_stop = time.time() - t0
        for e in self.edges:
            tb.disconnect(*e)
        for s in self.ts_sinks:
            s.close()
        self.edges, self.blocks, self.ts_sinks = [], [], []
        done = []
        try:
            edges, blks, sinks = self._build(cfg)
            for e in edges:
                tb.connect(*e)
                done.append(e)
            self.edges, self.blocks, self.ts_sinks = edges, blks, sinks
        except Exception:
            # leave only the fixed part running and do not retry this TMCC
            for e in done:
                tb.disconnect(*e)
            self.probes = [None, None, None]
            with self.lock:
                self.active = None
                self.failed = cfg
            tb.start()
            raise
        tb.start()
        dt = time.time() - t0
        with self.lock:
            self.active = cfg
            self.rebuilds += 1
            self.rebuild_log.append((time.strftime("%H:%M:%S"), dt, describe(cfg)))
        self.log("rebuild #%d done in %.2f s (stop %.2f s): %s"
                 % (self.rebuilds, dt, t_stop, describe(cfg)))
        self._publish()

    def _publish(self):
        info = {"cfg": self.active, "rebuilds": self.rebuilds,
                "last": self.rebuild_log[-1][0] if self.rebuild_log else "-"}
        if self.gui:
            self.gui.publish(info)
        if self.on_change:
            self.on_change(info)

    def _build(self, cfg):
        """Creates the blocks for cfg; returns (edges, blocks, ts_sinks)."""
        m = self.mode
        L = cfg.layers
        edges, blks, ts_sinks = [], [], []

        def chain(*items):
            for a, b in zip(items, items[1:]):
                a = a if isinstance(a, tuple) else (a, 0)
                b = b if isinstance(b, tuple) else (b, 0)
                edges.append((a, b))

        gate = frame_resync(self.vlen)
        fdi = isdbt.frequency_deinterleaver(cfg.partial, m)
        tdi = isdbt.time_deinterleaver(m, L[0].segments, L[0].interleaving,
                                       L[1].segments, L[1].interleaving,
                                       L[2].segments, L[2].interleaving)
        dem = isdbt.symbol_demapper(m, L[0].segments, L[0].constellation or 4,
                                    L[1].segments, L[1].constellation or 64,
                                    L[2].segments, L[2].constellation or 64)
        mer = epy_isdbt_mer.blk(mode=m, avg_symbols=204,
                                seg_a=L[0].segments, const_a=L[0].constellation or 4,
                                seg_b=L[1].segments, const_b=L[1].constellation or 64,
                                seg_c=L[2].segments, const_c=L[2].constellation or 64,
                                stride=self.mer_stride)
        chain(self.tmcc_block, gate, fdi, tdi, dem)
        chain(tdi, mer)
        blks += [gate, fdi, tdi, dem, mer]

        probes = [None, None, None]
        for k, lp in enumerate(L):
            sinks = self.gui.sinks[k] if self.gui else None
            if not lp.segments:
                ns = blocks.null_sink(gr.sizeof_float)
                chain((mer, k), ns)
                blks.append(ns)
                continue
            bdi = isdbt.bit_deinterleaver(m, lp.segments, lp.constellation)
            vit = isdbt.viterbi_decoder(lp.constellation, lp.rate)
            byd = isdbt.byte_deinterleaver()
            eds = isdbt.energy_descrambler()
            rs = isdbt.reed_solomon_dec_isdbt()
            v2s = blocks.vector_to_stream(gr.sizeof_char, 188)
            s2v = blocks.stream_to_vector(gr.sizeof_char, 1316)
            s2v.set_min_output_buffer(797)
            path = self.ts_paths[k]
            if self.split_ts:
                path = "%s.%02d" % (path, self.rebuilds + 1)
            fs = blocks.file_sink(gr.sizeof_char * 1316, path, True)
            fs.set_unbuffered(True)
            chain((dem, k), bdi, vit, byd, eds, rs, v2s, s2v, fs)
            ts_sinks.append(fs)

            iir = gr_filter.single_pole_iir_filter_ff(1e-3, 1)
            lg_pre = blocks.nlog10_ff(1, 1, 0)
            lg_post = blocks.nlog10_ff(1, 1, 0)
            p_pre, p_post, p_mer = (blocks.probe_signal_f(), blocks.probe_signal_f(),
                                    blocks.probe_signal_f())
            chain((vit, 1), iir, lg_pre, p_pre)
            chain((rs, 1), lg_post, p_post)
            chain((mer, k), p_mer)
            if sinks:
                chain(lg_pre, sinks["ber_pre"])
                chain(lg_post, sinks["ber_post"])
                chain((mer, k), sinks["mer"])
            probes[k] = {"ber_pre": p_pre, "ber_post": p_post, "mer": p_mer}
            blks += [bdi, vit, byd, eds, rs, v2s, s2v, fs, iir, lg_pre, lg_post,
                     p_pre, p_post, p_mer]
        self.probes = probes
        return edges, blks, ts_sinks

    # ---- for the launcher / future facade ----
    def metrics(self):
        """{'A': {'mer': dB, 'ber_pre': log10, 'ber_post': log10}, ...} of the
        layers currently built."""
        out = {}
        for k, p in enumerate(self.probes):
            if p:
                out[LAYER_NAMES[k]] = dict((n, p[n].level()) for n in p)
        return out

    def close(self):
        """Stop the worker (call before tb.stop())."""
        self.jobs.put(None)
        self.worker.join(timeout=10)
