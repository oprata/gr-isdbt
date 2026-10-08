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
                      -> TS file (append)
                      + error counters of the RS decoder ('stats' port, patch 0003):
                        BER after Viterbi counted in a 10 s window, packets
                        corrected / lost, status OK / Margem baixa / Perda
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
from html import escape as html_escape
import os
import queue
import threading
import time

import numpy as np
import pmt
from gnuradio import blocks, gr

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


class rs_stats_sink(gr.basic_block):
    """Message-only block: receives the 'stats' messages of the patched
    reed_solomon_dec_isdbt (patch 0003) and passes them to a LayerStats."""

    def __init__(self, stats):
        gr.basic_block.__init__(self, name="rs_stats_sink", in_sig=None, out_sig=None)
        self.stats = stats
        self.message_port_register_in(pmt.intern("stats"))
        self.set_msg_handler(pmt.intern("stats"), self.handle)

    def handle(self, msg):
        try:
            self.stats.update(msg)
        except Exception as e:
            print("[dyn] stats handler error: %r" % (e,), flush=True)


def rs_has_stats(rs):
    """True / False if we can tell whether this reed_solomon_dec_isdbt has the
    'stats' port (patch 0003), None if we cannot tell.

    GR 3.10 (pybind11) exposes has_msg_port(); GR 3.8 (SWIG) exposes neither
    has_msg_port() nor a readable message_ports_out(), so there the answer is
    None and _rebuild() simply tries msg_connect() (it raises if the port does
    not exist, and the BER display is then disabled for that layer)."""
    try:
        return bool(rs.has_msg_port(pmt.intern("stats")))
    except Exception:
        pass
    try:
        ports = rs.message_ports_out()             # PMT vector (or list) of symbols
        get = pmt.vector_ref if pmt.is_vector(ports) else (lambda v, i: pmt.nth(i, v))
        return any(pmt.symbol_to_string(get(ports, i)) == "stats"
                   for i in range(pmt.length(ports)))
    except Exception:
        return None


# BER after Viterbi up to which the RS(204,188) still delivers a quasi error
# free TS (the usual DVB-T / ISDB-T reference value).
QEF_BER = 2e-4
BITS_PER_PACKET = 204 * 8
STAT_FIELDS = ("packets", "corrected_packets", "corrected_bytes", "corrected_bits",
               "uncorrectable")


class LayerStats(object):
    """Counters of one layer: a sliding window (WINDOW seconds) and the totals
    since the current configuration was built. Thread-safe.

    Warm-up: right after a (re)build the time deinterleaver is still filling and
    the RS decoder rejects the first packets. Everything before the first
    successfully decoded packet is ignored (kept only in warmup_lost), so the
    status does not start as "Perda de pacotes"."""

    WINDOW = 10.0

    def __init__(self):
        self.lock = threading.Lock()
        self.reset(False)

    def reset(self, available=True, supported=True):
        with self.lock:
            self.available = available        # layer present in the current config
            self.supported = supported        # RS block has the 'stats' port
            self.t0 = time.time()
            zero = (self.t0,) + (0,) * len(STAT_FIELDS)
            self.samples = collections.deque([zero])
            self.base = zero                  # totals are counted from here
            self.started = False              # first good packet seen?
            self.warmup_lost = 0
            self.last = None

    def update(self, msg):
        def get(k):
            v = pmt.dict_ref(msg, pmt.intern(k), pmt.PMT_NIL)
            return 0 if pmt.is_null(v) else int(pmt.to_uint64(v))
        now = time.time()
        sample = (now,) + tuple(get(k) for k in STAT_FIELDS)
        with self.lock:
            self.last = now
            if not self.started:
                if sample[1] == 0:            # no packet decoded yet: warm-up
                    self.warmup_lost = sample[5]
                    return
                self.started = True           # first sample with decoded packets
                self.base = sample
                self.samples = collections.deque([sample])
                return
            self.samples.append(sample)
            # keep, as samples[0], the last sample at or before the window start
            while len(self.samples) > 2 and self.samples[1][0] <= now - self.WINDOW:
                self.samples.popleft()

    def snapshot(self):
        with self.lock:
            if not self.available:
                return None
            first, cur = self.samples[0], self.samples[-1]
            return {
                "supported": self.supported,
                "since": self.t0,
                "age": (time.time() - self.last) if self.last else None,
                "window_s": cur[0] - first[0],
                "win": dict(zip(STAT_FIELDS, [c - f for c, f in zip(cur[1:], first[1:])])),
                "total": dict(zip(STAT_FIELDS, [c - b for c, b in zip(cur[1:], self.base[1:])])),
                "warmup_lost": self.warmup_lost,
                "started": self.started,
            }


def _sup(n):
    return str(n).translate(str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹"))


def fmt_sci(x, ascii_only=False):
    """2.3e-07 -> '2,3×10⁻⁷' (or '2.3e-07' with ascii_only)."""
    if ascii_only:
        return "%.1e" % x
    m, e = ("%.1e" % x).split("e")
    return "%s×10%s" % (m.replace(".", ","), _sup(int(e)))


def fmt_int(n):
    return "{:,}".format(int(n)).replace(",", ".")


def fmt_big(n):
    if n < 1e6:
        return fmt_int(n)
    if n < 1e9:
        return ("%.1f milhões" % (n / 1e6)).replace(".", ",")
    return ("%.2f bilhões" % (n / 1e9)).replace(".", ",")


def evaluate(snap, ascii_only=False):
    """Turns a LayerStats snapshot into what is shown: status, BER text, ..."""
    if snap is None:
        return None
    if not snap["supported"]:
        return {"status": "BER indisponível", "color": "gray", "ber": None,
                "ber_text": "indisponível (gr-isdbt sem o patch 0003)", "win": None,
                "total": None, "since": snap["since"]}
    w, t = snap["win"], snap["total"]
    nbits = w["packets"] * BITS_PER_PACKET
    ber = (w["corrected_bits"] / float(nbits)) if nbits else None
    if nbits == 0:
        ber_text = "-"
    elif w["corrected_bits"] == 0:
        ber_text = "0 (< %s)" % fmt_sci(1.0 / nbits, ascii_only)
    else:
        ber_text = fmt_sci(ber, ascii_only)
    if not snap["started"]:
        status, color = "Aguardando", "gray"    # deinterleaver filling after a rebuild
    elif snap["age"] is None or snap["age"] > 3.0:
        status, color = "Sem dados", "gray"
    elif w["uncorrectable"] > 0:
        status, color = "Perda de pacotes", "#d00000"
    elif ber is not None and ber > QEF_BER:
        status, color = "Margem baixa", "#e08000"
    else:
        status, color = "OK", "#00a000"
    return {"status": status, "color": color, "ber": ber, "ber_text": ber_text,
            "nbits": nbits, "win": w, "total": t, "since": snap["since"],
            "window_s": snap["window_s"]}


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
    """Widgets of the analyzer.

    Overview tab : TMCC line + one summary row per layer.
    Layer tabs   : status panel (situation, MER, BER after Viterbi counted in a
                   10 s window, packets corrected / lost, totals) + MER bar.
    Refreshed once per second from DynamicLayers.metrics() (GUI thread)."""

    def __init__(self, tabs, overview_layout, layer_layouts, first_layer_tab=1):
        from PyQt5 import Qt, QtCore

        class Bridge(QtCore.QObject):
            changed = QtCore.pyqtSignal(object)

        self.Qt = Qt
        self.tabs = tabs
        self.first_tab = first_layer_tab
        self.controller = None            # set by DynamicLayers
        self.info = {}
        self.status = Qt.QLabel("TMCC: aguardando um quadro válido...")
        self.status.setWordWrap(True)
        overview_layout.addWidget(self.status)
        self.summary = Qt.QLabel("")
        self.summary.setTextFormat(QtCore.Qt.RichText)
        overview_layout.addWidget(self.summary)
        self.sinks = []
        self.panels = []
        for i, L in enumerate(LAYER_NAMES):
            panel = Qt.QLabel("")
            panel.setTextFormat(QtCore.Qt.RichText)
            panel.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
            layer_layouts[i].addWidget(panel)
            mer = _number_sink("MER camada %s" % L, 0, 40, "dB")
            layer_layouts[i].addWidget(_wrap_widget(mer))
            self.panels.append(panel)
            self.sinks.append({"mer": mer})
            tabs.setTabEnabled(first_layer_tab + i, False)
        self.bridge = Bridge()
        self.bridge.changed.connect(self._update)      # queued: runs in the GUI thread
        self.timer = Qt.QTimer()
        self.timer.timeout.connect(self._refresh)
        self.timer.start(1000)

    def publish(self, info):
        """Thread-safe: called from the controller threads."""
        self.bridge.changed.emit(info)

    def _update(self, info):
        self.info = info
        cfg = info.get("cfg")
        self.status.setText("TMCC: %s\nReconstruções: %d   última: %s" % (
            describe(cfg), info.get("rebuilds", 0), info.get("last", "-")))
        for i, L in enumerate(LAYER_NAMES):
            lp = cfg.layers[i] if cfg else ABSENT
            idx = self.first_tab + i
            self.tabs.setTabEnabled(idx, lp.segments > 0)
            self.tabs.setTabText(idx, "Layer %s" % L if not lp.segments
                                 else "Layer %s - %s" % (L, describe_layer(lp)))
        self._refresh()

    @staticmethod
    def _dot(color):
        return "<span style='color:%s; font-size:14pt'>&#9679;</span>" % color

    def _refresh(self):
        if self.controller is None:
            return
        cfg = self.info.get("cfg")
        m = self.controller.metrics()
        now = time.strftime("%H:%M:%S")
        rows = []
        for i, L in enumerate(LAYER_NAMES):
            lp = cfg.layers[i] if cfg else ABSENT
            e = m.get(L)
            if not lp.segments or e is None:
                self.panels[i].setText("")
                continue
            mer = ("%.1f dB" % e["mer"]).replace(".", ",") if e["mer"] > 0 else "-"
            ber_html = html_escape(e["ber_text"])
            rows.append("<tr><td><b>%s</b></td><td>%s</td><td>%s %s</td><td>%s</td>"
                        "<td>%s</td><td>%s</td></tr>" % (
                            L, describe_layer(lp), self._dot(e["color"]), e["status"], mer,
                            ber_html,
                            fmt_int(e["win"]["uncorrectable"]) if e["win"] else "-"))
            html = ["<p><b>Camada %s - %s</b> &nbsp; <span style='color:gray'>atualizado %s</span></p>"
                    % (L, describe_layer(lp), now), "<table cellpadding='3'>"]
            html.append("<tr><td>Situação</td><td>%s <b>%s</b></td></tr>"
                        % (self._dot(e["color"]), e["status"]))
            html.append("<tr><td>MER</td><td>%s</td></tr>" % mer)
            if e["win"] is not None:
                w, t = e["win"], e["total"]
                win_s = int(round(e["window_s"]))
                html.append("<tr><td>Erros pós-Viterbi (últimos %d s)</td><td>%s bits corrigidos em %s"
                            " &rarr; BER %s</td></tr>" % (win_s, fmt_int(w["corrected_bits"]),
                                                          fmt_big(e["nbits"]), ber_html))
                html.append("<tr><td>Pacotes corrigidos pelo RS (%d s)</td><td>%s de %s</td></tr>"
                            % (win_s, fmt_int(w["corrected_packets"]), fmt_int(w["packets"])))
                html.append("<tr><td>Pacotes perdidos (%d s)</td><td>%s</td></tr>"
                            % (win_s, fmt_int(w["uncorrectable"])))
                html.append("<tr><td>Desde %s (config. atual)</td><td>%s pacotes &middot; %s bits"
                            " corrigidos &middot; %s perdidos</td></tr>" % (
                                time.strftime("%H:%M:%S", time.localtime(e["since"])),
                                fmt_big(t["packets"]), fmt_int(t["corrected_bits"]),
                                fmt_int(t["uncorrectable"])))
                html.append("<tr><td colspan='2' style='color:gray'>Referência: BER pós-Viterbi"
                            " &le; 2&times;10<sup>-4</sup> &rarr; TS sem erros após o Reed-Solomon"
                            "</td></tr>")
            else:
                html.append("<tr><td>BER pós-Viterbi</td><td>%s</td></tr>" % ber_html)
            html.append("</table>")
            self.panels[i].setText("".join(html))
        if rows:
            self.summary.setText(
                "<p>atualizado %s</p><table cellpadding='4' border='0'>"
                "<tr><th align='left'>Camada</th><th align='left'>Configuração</th>"
                "<th align='left'>Situação</th><th align='left'>MER</th>"
                "<th align='left'>BER pós-Viterbi (10 s)</th><th align='left'>Perdidos (10 s)</th></tr>"
                "%s</table>" % (now, "".join(rows)))
        else:
            self.summary.setText("")


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
        self.msg_edges = []
        self.stats = [LayerStats() for _ in LAYER_NAMES]
        self.lock = threading.Lock()
        if gui is not None:
            gui.controller = self

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
        for e in self.msg_edges:
            tb.msg_disconnect(*e)
        for s in self.ts_sinks:
            s.close()
        self.edges, self.msg_edges, self.blocks, self.ts_sinks = [], [], [], []
        done, done_msg = [], []
        try:
            edges, msg_edges, blks, sinks = self._build(cfg)
            for e in edges:
                tb.connect(*e)
                done.append(e)
            for e in msg_edges:
                try:
                    tb.msg_connect(*e)
                    done_msg.append(e)
                except Exception as ex:          # RS without the 'stats' port
                    e[1][0].stats.reset(True, False)
                    self.log("stats port not available (%r): BER display disabled" % (ex,))
            self.edges, self.msg_edges, self.blocks, self.ts_sinks = edges, done_msg, blks, sinks
        except Exception:
            # leave only the fixed part running and do not retry this TMCC
            for e in done:
                tb.disconnect(*e)
            for e in done_msg:
                tb.msg_disconnect(*e)
            self.probes = [None, None, None]
            for st in self.stats:
                st.reset(False)
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
        """Creates the blocks for cfg; returns (edges, msg_edges, blocks, ts_sinks)."""
        m = self.mode
        L = cfg.layers
        edges, msg_edges, blks, ts_sinks = [], [], [], []

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
                self.stats[k].reset(False)
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

            # Post-Viterbi errors: exact counters of the RS decoder (patch 0003),
            # shown as counts in a 10 s window instead of the gr-isdbt moving
            # average (which floors at log10(FLT_MIN) = -37.93 with no errors).
            if rs_has_stats(rs) is not False:   # True, or unknown (GR 3.8): try it
                ss = rs_stats_sink(self.stats[k])
                msg_edges.append(((rs, "stats"), (ss, "stats")))
                blks.append(ss)
                self.stats[k].reset(True, True)
            else:
                self.stats[k].reset(True, False)
            p_mer = blocks.probe_signal_f()
            chain((mer, k), p_mer)
            if sinks:
                chain((mer, k), sinks["mer"])
            probes[k] = {"mer": p_mer}
            blks += [bdi, vit, byd, eds, rs, v2s, s2v, fs, p_mer]
        self.probes = probes
        return edges, msg_edges, blks, ts_sinks

    # ---- for the launcher / future facade ----
    def metrics(self, ascii_only=False):
        """Per built layer: {'A': {'mer': dB, 'status': 'OK'|..., 'color': ...,
        'ber': float|None, 'ber_text': str, 'win': {...}, 'total': {...},
        'since': epoch, 'window_s': s}, ...}. Used by the GUI, the launcher
        log and (later) the Qt facade."""
        out = {}
        for k, p in enumerate(self.probes):
            if not p:
                continue
            e = evaluate(self.stats[k].snapshot(), ascii_only)
            if e is None:
                continue
            e["mer"] = p["mer"].level()
            out[LAYER_NAMES[k]] = e
        return out

    def close(self):
        """Stop the worker (call before tb.stop())."""
        self.jobs.put(None)
        self.worker.join(timeout=10)
