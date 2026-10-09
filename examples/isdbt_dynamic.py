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
                        corrected / lost, status OK / Low margin / Packet loss
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
import math
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
import ts_rtp

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


class layer_points(gr.sync_block):
    """Feeds the constellation with the symbols of each layer separately.

    Input : time_deinterleaver output (data carriers, layer A first, then B, C).
    Output: 3 vectors of `npts` symbols per OFDM symbol (layers A, B, C), taken
            evenly across each layer's carriers so that every layer gets the
            same number of points (a 12-segment layer would otherwise hide a
            1-segment one). An absent layer outputs NaN (not plotted).
    Sync block, 1 input item per call (D24)."""

    def __init__(self, vlen, carriers_per_segment, segments, npts=384):
        gr.sync_block.__init__(self, name="layer_points",
                               in_sig=[(np.complex64, vlen)],
                               out_sig=[(np.complex64, npts)] * 3)
        self.index = []
        start = 0
        for seg in segments:
            ncar = seg * carriers_per_segment
            if seg > 0:
                self.index.append(start + np.linspace(0, ncar - 1, npts).astype(np.int64))
            else:
                self.index.append(None)
            start += ncar

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(output_items[0])
        for k, idx in enumerate(self.index):
            if idx is None:
                output_items[k][:n] = np.nan
            else:
                output_items[k][:n] = x[:n, idx]
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
    status does not start as "Packet loss"."""

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
    """2.3e-07 -> '2.3×10⁻⁷' (or '2.3e-07' with ascii_only)."""
    if ascii_only:
        return "%.1e" % x
    m, e = ("%.1e" % x).split("e")
    return "%s×10%s" % (m, _sup(int(e)))


def fmt_int(n):
    return "{:,}".format(int(n))


def fmt_big(n):
    if n < 1e6:
        return fmt_int(n)
    if n < 1e9:
        return "%.1f M" % (n / 1e6)
    return "%.2f G" % (n / 1e9)


def fmt_duration(sec):
    sec = int(sec)
    if sec < 60:
        return "%d s" % sec
    if sec < 3600:
        return "%d min %02d s" % (sec // 60, sec % 60)
    return "%d h %02d min" % (sec // 3600, (sec % 3600) // 60)


def _ber_text(bits, nbits, ascii_only):
    if nbits == 0:
        return "-"
    if bits == 0:
        return "0 (< %s)" % fmt_sci(1.0 / nbits, ascii_only)
    return fmt_sci(bits / float(nbits), ascii_only)


def evaluate(snap, ascii_only=False):
    """Turns a LayerStats snapshot into what is shown: status, BER texts, ..."""
    if snap is None:
        return None
    if not snap["supported"]:
        return {"status": "BER unavailable", "color": "gray", "ber": None,
                "ber_text": "unavailable (gr-isdbt without patch 0003)", "win": None,
                "total": None, "since": snap["since"]}
    w, t = snap["win"], snap["total"]
    nbits = w["packets"] * BITS_PER_PACKET
    tbits = t["packets"] * BITS_PER_PACKET
    ber = (w["corrected_bits"] / float(nbits)) if nbits else None
    if not snap["started"]:
        status, color = "Waiting", "gray"          # deinterleaver filling after a rebuild
    elif snap["age"] is None or snap["age"] > 3.0:
        status, color = "No data", "gray"
    elif w["uncorrectable"] > 0:
        status, color = "Packet loss", "#d00000"
    elif ber is not None and ber > QEF_BER:
        status, color = "Low margin", "#e08000"
    else:
        status, color = "OK", "#00a000"
    return {"status": status, "color": color, "ber": ber,
            "ber_text": _ber_text(w["corrected_bits"], nbits, ascii_only),
            "total_ber_text": _ber_text(t["corrected_bits"], tbits, ascii_only),
            "nbits": nbits, "tbits": tbits, "win": w, "total": t,
            "since": snap["since"], "window_s": snap["window_s"]}


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


CONST_POINTS = 384          # symbols per layer per OFDM symbol sent to the plot
LAYER_COLORS = ("red", "blue", "orange")


def _layer_const_sink():
    """Constellation with one input per layer: A red, B blue, C orange."""
    from gnuradio import qtgui
    s = qtgui.const_sink_c(4 * CONST_POINTS, "Data carriers by layer", 3)
    s.set_update_time(0.10)
    s.set_y_axis(-1.5, 1.5)
    s.set_x_axis(-1.5, 1.5)
    s.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
    s.enable_autoscale(False)
    s.enable_grid(True)
    s.enable_axis_labels(True)
    for i, L in enumerate(LAYER_NAMES):
        s.set_line_label(i, "Layer %s" % L)
        s.set_line_color(i, LAYER_COLORS[i])
        s.set_line_width(i, 1)
        s.set_line_style(i, 0)
        s.set_line_marker(i, 0)
        s.set_line_alpha(i, 1.0)
    return s


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
    """Widgets of the analyzer (all texts in English).

    Constellation tab : constellation (from the .grc) + one row per layer
                        (modulation, code rate, interleaving, segments, status,
                        MER, BER and lost packets in the last 10 s).
    Layer A/B/C tabs  : transmission parameters, reception quality in the last
                        10 s, totals since the current configuration, MER bar.
    Refreshed once per second from DynamicLayers.metrics() (GUI thread)."""

    TABLE = "<table border='0' cellspacing='0' cellpadding='4'>"

    def __init__(self, tabs, overview_layout, layer_layouts, first_layer_tab=1):
        from PyQt5 import Qt, QtCore

        class Bridge(QtCore.QObject):
            changed = QtCore.pyqtSignal(object)

        self.Qt = Qt
        self.tabs = tabs
        self.first_tab = first_layer_tab
        # tab pages of layers A-C: found by page, not by index (Rodada 53:
        # the Spectrum tab is inserted at index 0)
        self.layer_pages = [lay.parentWidget() for lay in layer_layouts]
        self.controller = None            # set by DynamicLayers
        self.info = {}
        self.const = _layer_const_sink()
        overview_layout.insertWidget(0, _wrap_widget(self.const))
        self.summary = Qt.QLabel("Waiting for a valid TMCC frame...")
        self.summary.setTextFormat(QtCore.Qt.RichText)
        overview_layout.addWidget(self.summary)
        self.sinks = []
        self.panels = []
        for i, L in enumerate(LAYER_NAMES):
            panel = Qt.QLabel("")
            panel.setTextFormat(QtCore.Qt.RichText)
            panel.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
            layer_layouts[i].addWidget(panel)
            mer = _number_sink("MER", 0, 40, "dB")
            layer_layouts[i].addWidget(_wrap_widget(mer))
            self.panels.append(panel)
            self.sinks.append({"mer": mer})
            pg = self.layer_pages[i]
            pi = tabs.indexOf(pg) if pg is not None else -1
            tabs.setTabText(pi if pi >= 0 else first_layer_tab + i, "Layer %s" % L)
            tabs.setTabEnabled(pi if pi >= 0 else first_layer_tab + i, False)
        self.bridge = Bridge()
        self.bridge.changed.connect(self._update)      # queued: runs in the GUI thread
        self.timer = Qt.QTimer()
        self.timer.timeout.connect(self._refresh)
        self.timer.start(1000)

    def publish(self, info):
        """Thread-safe: called from the controller threads."""
        self.bridge.changed.emit(info)

    # ---- TS viewers (libVLC), one tab per layer, only the visible one plays
    def setup_viewers(self, host, ports, proto="udp"):
        import ts_viewer
        self.viewers = []
        idx = next((i for i in range(self.tabs.count()) if self.tabs.tabText(i) == "Control"),
                   self.tabs.count())
        for k, L in enumerate(LAYER_NAMES):
            v = ts_viewer.TsViewer(L, host, ports[k], proto)
            self.tabs.insertTab(idx + k, v.widget, "TS Viewer Layer %s" % L)
            self.tabs.setTabEnabled(idx + k, False)
            self.viewers.append(v)
        self.tabs.currentChanged.connect(self._tab_changed)

    def _tab_changed(self, _index=None):
        cur = self.tabs.currentWidget()
        for v in getattr(self, "viewers", []):
            if v.widget is cur and v.available:
                if not v.playing:
                    v.play()
            else:
                v.stop()

    def _restart_visible_viewer(self):
        for v in getattr(self, "viewers", []):
            v.stop()
        self._tab_changed()

    def release_viewers(self):
        for v in getattr(self, "viewers", []):
            v.release()

    def _update(self, info):
        self.info = info
        cfg = info.get("cfg")
        for i in range(len(LAYER_NAMES)):
            lp = cfg.layers[i] if cfg else ABSENT
            page = self.layer_pages[i]
            idx = self.tabs.indexOf(page) if page is not None else -1
            self.tabs.setTabEnabled(idx if idx >= 0 else self.first_tab + i, lp.segments > 0)
        for i, v in enumerate(getattr(self, "viewers", [])):
            lp = cfg.layers[i] if cfg else ABSENT
            self.tabs.setTabEnabled(self.tabs.indexOf(v.widget), lp.segments > 0)
            v.set_available(lp.segments > 0)
        # the RTP streams restart after a rebuild: restart the visible player
        # once the deinterleavers have filled
        self.Qt.QTimer.singleShot(1500, self._restart_visible_viewer)
        self._refresh()

    @staticmethod
    def _dot(color):
        return "<span style='color:%s'>&#9679;</span>" % color

    @staticmethod
    def _params(lp):
        return (MOD_TEXT.get(lp.constellation, "?"),
                CR_TEXT[lp.rate] if 0 <= lp.rate <= 4 else "?",
                "I = %d" % lp.interleaving, str(lp.segments))

    def _row(self, cells, header=False):
        if header:
            return "<tr>%s</tr>" % "".join(
                "<th align='left' style='color:#606060; padding-right:12px'>%s</th>" % c
                for c in cells)
        return "<tr>%s</tr>" % "".join(
            "<td style='padding-right:12px'>%s</td>" % c for c in cells)

    def _refresh(self):
        if self.controller is None:
            return
        cfg = self.info.get("cfg")
        if cfg is None:
            return
        m = self.controller.metrics()
        for k, v in enumerate(getattr(self, "viewers", [])):
            sink = self.controller.rtp_sinks[k]
            v.set_pat_inserted(bool(sink is not None and sink.injecting))
        rows = [self._row(["Status", "Layer", "MER", "BER (10 s)", "Lost (10 s)",
                           "Modulation", "Code rate", "Interleaving", "Segments"],
                          header=True)]
        for i, L in enumerate(LAYER_NAMES):
            lp = cfg.layers[i]
            e = m.get(L)
            if not lp.segments or e is None:
                self.panels[i].setText("")
                continue
            mod, cr, il, seg = self._params(lp)
            mer = "%.1f dB" % e["mer"] if e["mer"] > 0 else "-"
            status = "%s %s" % (self._dot(e["color"]), e["status"])
            ber = html_escape(e["ber_text"])
            lost = fmt_int(e["win"]["uncorrectable"]) if e["win"] else "-"
            rows.append(self._row([status, "<b>%s</b>" % L, mer, ber, lost, mod, cr, il, seg]))
            self.panels[i].setText(self._layer_html(L, lp, e, mer, status, ber))
        self.summary.setText(
            "<p>Partial reception: <b>%s</b></p>%s%s</table>"
            % ("ON" if cfg.partial else "OFF", self.TABLE, "".join(rows)))

    def _layer_html(self, L, lp, e, mer, status, ber):
        mod, cr, il, seg = self._params(lp)
        h = ["<h3>Layer %s</h3>" % L,
             "<p><b>Transmission parameters</b> (TMCC)</p>", self.TABLE,
             self._row(["Modulation", "Code rate", "Interleaving", "Segments"], True),
             self._row([mod, cr, il, seg]), "</table>"]
        if e["win"] is None:
            h += ["<p><b>Reception quality</b></p>", self.TABLE,
                  self._row(["Status", "MER", "BER after Viterbi"], True),
                  self._row([status, mer, ber]), "</table>"]
            return "".join(h)
        w, t = e["win"], e["total"]
        win_s = max(1, int(round(e["window_s"])))
        h += ["<p><b>Reception quality</b> - last %d s</p>" % win_s, self.TABLE,
              self._row(["Status", "MER", "BER after Viterbi", "Corrected bits",
                         "Corrected packets", "Lost packets"], True),
              self._row([status, mer, ber,
                         "%s of %s" % (fmt_int(w["corrected_bits"]), fmt_big(e["nbits"])),
                         "%s of %s" % (fmt_int(w["corrected_packets"]), fmt_int(w["packets"])),
                         fmt_int(w["uncorrectable"])]),
              "</table>"]
        elapsed = time.time() - e["since"]
        received = t["packets"] + t["uncorrectable"]
        loss = (100.0 * t["uncorrectable"] / received) if received else 0.0
        h += ["<p><b>Current configuration totals</b> - since %s (%s)</p>" % (
                  time.strftime("%H:%M:%S", time.localtime(e["since"])), fmt_duration(elapsed)),
              self.TABLE,
              self._row(["Packets", "Corrected packets", "Corrected bits",
                         "BER after Viterbi", "Lost packets", "Loss"], True),
              self._row([fmt_big(t["packets"]), fmt_int(t["corrected_packets"]),
                         fmt_int(t["corrected_bits"]), html_escape(e["total_ber_text"]),
                         fmt_int(t["uncorrectable"]),
                         "0 %" if t["uncorrectable"] == 0 else "%.3f %%" % loss]),
              "</table>",
              "<p style='color:gray'>Reference: BER after Viterbi &le; 2&times;10<sup>-4</sup>"
              " &rarr; quasi error-free TS after Reed-Solomon. Updated %s</p>"
              % time.strftime("%H:%M:%S")]
        return "".join(h)


# ISDB-Tb channel plan (ABNT NBR 15601): 6 MHz channels, center = band start
# of the channel + 3 MHz + 1/7 MHz. VHF-high 7-13 (174-216 MHz), UHF 14-69
# (470-806 MHz).
def channel_center_hz(ch):
    if 7 <= ch <= 13:
        low = 174e6 + 6e6 * (ch - 7)
    elif 14 <= ch <= 69:
        low = 470e6 + 6e6 * (ch - 14)
    else:
        raise ValueError("channel %r out of 7-13 / 14-69" % (ch,))
    return int(round(low + 3e6 + 1e6 / 7))


def channel_from_hz(hz):
    """(nearest channel, exact) for a center frequency in Hz."""
    best = min(list(range(7, 14)) + list(range(14, 70)),
               key=lambda c: abs(channel_center_hz(c) - hz))
    return best, abs(channel_center_hz(best) - hz) < 1000


def _knob_column(title, dial, value, info=None):
    """Title on top, knob, value (and optional info line) below, centered."""
    from PyQt5 import Qt, QtCore
    box = Qt.QWidget()
    v = Qt.QVBoxLayout(box)
    v.setSpacing(4)
    t = Qt.QLabel(title)
    f = t.font()
    f.setBold(True)
    f.setPointSize(f.pointSize() + 2)
    t.setFont(f)
    for w in [t, dial, value] + ([info] if info is not None else []):
        v.addWidget(w, 0, QtCore.Qt.AlignHCenter)
    v.addStretch(1)
    return box


def _build_control_tab(tb, rf=None):
    """Control tab built here (not in the .grc), so it does not depend on
    which version of the .grc was generated: 'RX Gain' knob and 'Channel'
    knob (ISDB-Tb channel; the center frequency is shown for information),
    title above each knob. The GRC widgets of rx_gain / center_freq are hidden;
    the new ones call the same setters of the flowgraph."""
    from PyQt5 import Qt, QtCore
    tabs = tb.tab_widget_layers
    const_page = getattr(tb, "tab_widget_layers_widget_0", None)
    ci = tabs.indexOf(const_page) if const_page is not None else -1
    tabs.setTabText(ci if ci >= 0 else 0, "Constellation")
    for name in ("_rx_gain_win", "_center_freq_tool_bar", "_qtgui_const_sink_x_0_win"):
        w = getattr(tb, name, None)
        if w is not None:
            w.setVisible(False)

    page = Qt.QWidget()
    outer = Qt.QVBoxLayout(page)
    knobs = Qt.QWidget()
    row = Qt.QHBoxLayout(knobs)
    outer.addWidget(knobs)
    big = Qt.QFont()
    big.setPointSize(big.pointSize() + 6)
    big.setBold(True)

    def make_dial(lo, hi, page_step):
        d = Qt.QDial()
        d.setRange(lo, hi)
        d.setSingleStep(1)
        d.setPageStep(page_step)
        d.setNotchesVisible(True)
        d.setWrapping(False)
        d.setFixedSize(170, 170)
        return d

    # RX Gain
    rng = getattr(tb, "_rx_gain_range", None)
    gmin = int(getattr(rng, "min", 0)) if rng else 0
    gmax = int(getattr(rng, "max", 50)) if rng else 50
    gain = make_dial(gmin, gmax, 5)
    gain.setValue(int(round(tb.get_rx_gain())))
    gain_value = Qt.QLabel()
    gain_value.setFont(big)

    def gain_changed(v):
        gain_value.setText("%d dB" % v)
        tb.set_rx_gain(float(v))
    gain.valueChanged.connect(gain_changed)
    gain_value.setText("%d dB" % gain.value())

    # Channel (applied when the knob is released, or at each wheel/key step)
    chan = make_dial(7, 69, 6)
    chan.setTracking(False)
    ch0, exact = channel_from_hz(tb.get_center_freq())
    chan.setValue(ch0)
    chan_value = Qt.QLabel()
    chan_value.setFont(big)
    chan_info = Qt.QLabel()
    chan_info.setStyleSheet("color: gray")

    def show_channel(ch, applied=True):
        chan_value.setText("Ch %d" % ch)
        hz = channel_center_hz(ch)
        band = "VHF" if ch <= 13 else "UHF"
        chan_info.setText("%s - %.6f MHz%s" % (band, hz / 1e6, "" if applied else " (release to tune)"))

    def chan_changed(ch):
        tb.set_center_freq(channel_center_hz(ch))
        show_channel(ch)
    chan.valueChanged.connect(chan_changed)
    chan.sliderMoved.connect(lambda ch: show_channel(ch, False))
    show_channel(ch0)
    if not exact:          # started off the channel plan: keep it, say so
        chan_info.setText("%.6f MHz (not a channel center; nearest Ch %d)"
                          % (tb.get_center_freq() / 1e6, ch0))

    row.addWidget(_knob_column("RX Gain", gain, gain_value))
    row.addSpacing(40)
    row.addWidget(_knob_column("Channel", chan, chan_value, chan_info))
    row.addStretch(1)

    # Level calibration (rf_level.py): makes the channel power readable in dBm
    if rf is not None:
        box = Qt.QGroupBox("Level calibration (dBm)")
        g = Qt.QGridLayout(box)
        level = Qt.QLabel("Measuring the signal level...")
        level.setTextFormat(QtCore.Qt.RichText)
        level.setSizePolicy(Qt.QSizePolicy.Ignored, Qt.QSizePolicy.Fixed)
        cal_info = Qt.QLabel(rf.cal_text())
        cal_info.setStyleSheet("color: gray")
        steps = Qt.QLabel(
            "The LimeSDR measures the signal only relative to its converter (dBFS). "
            "To show it in dBm it needs one reference:<br>"
            "<b>1.</b> Feed the LimeSDR with a signal of known level - e.g. the modulator: "
            "its output level minus the loss of cables and attenuators.<br>"
            "<b>2.</b> Type that level below.<br>"
            "<b>3.</b> Press <b>Calibrate</b>. From then on the channel power is shown in dBm "
            "(here and in the Spectrum tab). The calibration is saved and stays valid "
            "when the RX Gain changes; repeat it if you change the LimeSDR, the antenna "
            "port or the band.")
        steps.setTextFormat(QtCore.Qt.RichText)
        steps.setWordWrap(True)
        ref = Qt.QDoubleSpinBox()
        ref.setRange(-130.0, 20.0)
        ref.setDecimals(1)
        ref.setSingleStep(0.5)
        ref.setSuffix(" dBm")
        ref.setValue(rf.cal.get("reference_dbm", -40.0) if rf.cal else -40.0)
        btn = Qt.QPushButton("Calibrate")
        clr = Qt.QPushButton("Clear calibration")
        g.addWidget(steps, 0, 0, 1, 4)
        g.addWidget(Qt.QLabel("Known level at the LimeSDR input:"), 1, 0)
        g.addWidget(ref, 1, 1)
        g.addWidget(btn, 1, 2)
        g.addWidget(clr, 1, 3)
        g.addWidget(level, 2, 0, 1, 4)
        g.addWidget(cal_info, 3, 0, 1, 4)
        outer.addWidget(box)

        def level_text():
            r = rf.reading()
            if r["chan_dbfs"] is None:
                return "Measuring the signal level..."
            if r["dbm"] is not None:
                return ("Channel power now: <b>%.1f dBm</b> (%.1f dBFS at the ADC, RX Gain %.0f dB)"
                        % (r["dbm"], r["chan_dbfs"], r["gain"]))
            return ("Channel power now: <b>%.1f dBFS</b> (RX Gain %.0f dB) - not calibrated"
                    % (r["chan_dbfs"], r["gain"]))

        def do_cal():
            try:
                rf.calibrate(ref.value())
            except Exception as e:
                cal_info.setText("Calibration failed: %s" % e)
                return
            cal_info.setText(rf.cal_text())
            level.setText(level_text())

        def do_clear():
            rf.clear_cal()
            cal_info.setText(rf.cal_text())
            level.setText(level_text())
        btn.clicked.connect(do_cal)
        clr.clicked.connect(do_clear)

        def refresh_level():
            if page.isVisible():
                level.setText(level_text())
        level_timer = QtCore.QTimer()
        level_timer.timeout.connect(refresh_level)
        level_timer.start(1000)
        tb._dyn_level_timer = level_timer
    outer.addStretch(1)

    # put it in the 'Control' tab of the .grc if there is one, else create it
    idx = next((i for i in range(tabs.count()) if tabs.tabText(i) == "Control"), None)
    if idx is None:
        tabs.addTab(page, "Control")
    else:
        lay = getattr(tb, "tab_widget_layers_layout_%d" % idx, None)
        if lay is not None:
            lay.insertWidget(0, page, 100)    # on top, taking the space of the hidden GRC widgets
        else:
            tabs.addTab(page, "Control")
    tb._dyn_control = (page, gain, chan)       # keep references


def gui_from_generated(tb):
    """LayerGui using the 'tab_widget_layers' QTabWidget of the generated
    stbcast_analyzer_dyn. Final tab order: Spectrum, Constellation, Layer A-C,
    TS Viewer A-C, Control (the .grc has Constellation, Layer A-C, Control)."""
    import rf_level
    rf = None
    if os.environ.get("ISDBT_NO_SPECTRUM") == "1":     # test switch (Rodada 54)
        print("[dyn] Spectrum tab and RF level disabled (ISDBT_NO_SPECTRUM=1)", flush=True)
        try:
            _build_control_tab(tb, None)
        except Exception as e:
            print("[dyn] control tab not built: %r" % (e,), flush=True)
        return LayerGui(tb.tab_widget_layers, tb.tab_widget_layers_layout_0,
                        [tb.tab_widget_layers_layout_1, tb.tab_widget_layers_layout_2,
                         tb.tab_widget_layers_layout_3])
    try:
        rf = rf_level.RfLevel(tb, tb.limesdr_source_0, tb.low_pass_filter_0, float(tb.samp_rate))
        tb._rf_level = rf
    except Exception as e:
        print("[dyn] RF level not available: %r" % (e,), flush=True)
    try:
        _build_control_tab(tb, rf)
    except Exception as e:                   # the analyzer works without it
        print("[dyn] control tab not built: %r" % (e,), flush=True)
    if rf is not None:
        try:
            rf_level.build_spectrum_tab(tb, rf, index=0)   # left of Constellation
        except Exception as e:
            print("[dyn] spectrum tab not built: %r" % (e,), flush=True)
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
                 record=False, rtp=True, rtp_host="127.0.0.1", rtp_port=5004,
                 inject_pat=True, log=None, ts_proto="udp", pcr_restamp=True):
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
        self.record = record                  # TS files only with --record
        self.rtp = rtp
        self.rtp_host = rtp_host
        self.ts_proto = ts_proto              # 'udp' (plain TS, default) or 'rtp'
        self.pcr_restamp = pcr_restamp        # regenerate the PCR on the network output
        self.rtp_ports = [int(rtp_port) + 2 * k for k in range(3)]   # A, B, C
        self.inject_pat = inject_pat
        self.pat_registry = ts_rtp.PatRegistry()
        self.rtp_sinks = [None, None, None]
        self.log = log or (lambda s: print("[dyn %s] %s" % (time.strftime("%H:%M:%S"), s),
                                           flush=True))
        self.ts_paths = [os.path.join(ts_dir, "ts_layer_" + L.lower()) for L in LAYER_NAMES]
        if fresh_ts and record:
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
            if rtp:
                gui.setup_viewers(rtp_host, self.rtp_ports, ts_proto)

        tb._dyn_controller = self              # used by the Spectrum tab (total MER)
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
        if self.gui is not None:                    # constellation by layer
            pts = layer_points(self.vlen, self.vlen // 13, [lp.segments for lp in L],
                               CONST_POINTS)
            chain(tdi, pts)
            blks.append(pts)
            for k in range(3):
                v2s_c = blocks.vector_to_stream(gr.sizeof_gr_complex, CONST_POINTS)
                chain((pts, k), v2s_c, (self.gui.const, k))
                blks.append(v2s_c)

        probes = [None, None, None]
        rtp_sinks = [None, None, None]
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
            chain((dem, k), bdi, vit, byd, eds, rs, v2s, s2v)
            outputs = 0
            if self.record:
                path = self.ts_paths[k]
                if self.split_ts:
                    path = "%s.%02d" % (path, self.rebuilds + 1)
                fs = blocks.file_sink(gr.sizeof_char * 1316, path, True)
                fs.set_unbuffered(True)
                chain(s2v, fs)
                ts_sinks.append(fs)
                blks.append(fs)
                outputs += 1
            if self.rtp:
                rtp = ts_rtp.ts_rtp_sink(self.rtp_host, self.rtp_ports[k], self.pat_registry,
                                         self.inject_pat,
                                         rtp_header=(self.ts_proto == "rtp"),
                                         pkt_rate=ts_rtp.layer_packet_rate(
                                             m, lp.segments, lp.constellation, lp.rate),
                                         pcr_restamp=self.pcr_restamp)
                chain(s2v, rtp)
                blks.append(rtp)
                rtp_sinks[k] = rtp
                outputs += 1
            if not outputs:
                ns_ts = blocks.null_sink(gr.sizeof_char * 1316)
                chain(s2v, ns_ts)
                blks.append(ns_ts)

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
            blks += [bdi, vit, byd, eds, rs, v2s, s2v, p_mer]
        self.probes = probes
        self.rtp_sinks = rtp_sinks
        return edges, msg_edges, blks, ts_sinks

    def total_mer(self):
        """MER of the whole channel (all built layers), in dB, or None.
        Constellations are normalized to unit power per carrier, so the error
        powers add up weighted by the number of carriers (segments):
        MER = -10 log10( sum(seg_i * 10^(-MER_i/10)) / sum(seg_i) )."""
        cfg = self.active
        if cfg is None:
            return None
        num = den = 0.0
        for k, p in enumerate(self.probes):
            seg = cfg.layers[k].segments
            if not p or not seg:
                continue
            mer = p["mer"].level()
            if mer <= 0:                    # no complete window yet
                return None
            num += seg * 10 ** (-mer / 10.0)
            den += seg
        return -10 * math.log10(num / den) if den else None

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
        """Stop the worker and the video players (call before tb.stop(), from
        the GUI thread)."""
        if self.gui is not None:
            self.gui.release_viewers()
        self.jobs.put(None)
        self.worker.join(timeout=10)
