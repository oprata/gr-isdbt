"""
rf_level.py - "Spectrum" tab and RF level measurement (Rodada 53).

Spectrum : qtgui frequency sink of the LimeSDR samples (1024-point FFT,
           1 of every 64 FFT frames, ~125 FFT/s -> little CPU).
Levels   : measured on 1 of every 16 samples (statistically equivalent),
           with native GNU Radio blocks only (no Python in the sample path):
           - channel power after the 5.8 MHz low-pass filter, in dBFS
             (0 dBFS = a complex signal of magnitude 1 = ADC full scale);
           - ADC peak, max(|I|, |Q|) in dBFS (>= -1 dBFS: clipping).
dBm      : the LimeSDR has no calibrated power reading, so
               level_dBm = channel_dBFS - rx_gain_dB + offset_dB
           where offset_dB is set ONCE with a known reference signal (Control
           tab -> "Calibrate"): e.g. the modulator output level minus the
           cable/attenuator loss. Saved in ~/.config/isdbt_analyzer/level_cal.json.
           Valid for this LimeSDR, antenna port and frequency band; the gain
           steps of the LMS7002 are not exact, so recalibrate if the RX gain
           changes a lot (the gain used at calibration is shown).
"""

import json
import math
import os
import time

from gnuradio import gr, blocks

CAL_FILE = os.path.expanduser("~/.config/isdbt_analyzer/level_cal.json")


def db(x, floor=-200.0):
    return 10.0 * math.log10(x) if x > 0 else floor


# ---------------------------------------------------------------- calibration
def load_cal():
    try:
        with open(CAL_FILE) as f:
            c = json.load(f)
        return c if "offset_db" in c else None
    except (OSError, ValueError):
        return None


def save_cal(cal):
    os.makedirs(os.path.dirname(CAL_FILE), exist_ok=True)
    with open(CAL_FILE, "w") as f:
        json.dump(cal, f, indent=2)


class RfLevel(object):
    """Measurement chain attached to the fixed part of the flowgraph
    (call before tb.start()). Rodada 55: only native GNU Radio blocks - the
    first version used Python blocks, whose GIL contention with the receiver's
    Python blocks slowed the main chain enough to make the LimeSDR drop
    samples and the OFDM synchronizer lose lock.

      channel power : LPF -> keep 1/16 -> |x|^2 -> integrate 1000 -> integrate N
                      -> scale -> probe            (mean over 0.5 s)
      ADC peak      : source -> keep 1/16 -> I,Q interleaved -> |.| -> max over
                      1000 -> max over M -> probe   (max of |I|,|Q| over 0.5 s)
    """

    def __init__(self, tb, source, channel_filter, samp_rate, decim=16, window=0.5):
        self.tb = tb
        rate = samp_rate / decim
        n1 = 1000
        n2 = max(1, int(round(window * rate / n1)))
        k1 = blocks.keep_one_in_n(gr.sizeof_gr_complex, decim)
        k2 = blocks.keep_one_in_n(gr.sizeof_gr_complex, decim)
        # channel power
        mag2 = blocks.complex_to_mag_squared(1)
        i1 = blocks.integrate_ff(n1, 1)
        i2 = blocks.integrate_ff(n2, 1)
        sc = blocks.multiply_const_ff(1.0 / (n1 * n2))
        self.p_power = blocks.probe_signal_f()
        tb.connect(channel_filter, k2, mag2, i1, i2, sc, self.p_power)
        # ADC peak of |I| and |Q|
        c2f = blocks.complex_to_float(1)
        il = blocks.interleave(gr.sizeof_float, 1)
        ab = blocks.abs_ff(1)
        v1 = blocks.stream_to_vector(gr.sizeof_float, n1)
        m1 = blocks.max_ff(n1, 1)
        n3 = max(1, int(round(window * 2 * rate / n1)))
        v2 = blocks.stream_to_vector(gr.sizeof_float, n3)
        m2 = blocks.max_ff(n3, 1)
        self.p_peak = blocks.probe_signal_f()
        tb.connect(source, k1, c2f)
        tb.connect((c2f, 0), (il, 0))
        tb.connect((c2f, 1), (il, 1))
        tb.connect(il, ab, v1, m1, v2, m2, self.p_peak)
        self._blocks = [k1, k2, mag2, i1, i2, sc, self.p_power,
                        c2f, il, ab, v1, m1, v2, m2, self.p_peak]
        self.cal = load_cal()

    def gain(self):
        try:
            return float(self.tb.get_rx_gain())
        except Exception:
            return None

    def reading(self):
        """dict: chan_dbfs, peak_dbfs, dbm (None if not calibrated), gain."""
        pw = self.p_power.level()
        pkl = self.p_peak.level()
        p = db(pw) if pw > 0 else None
        pk = 20.0 * math.log10(pkl) if pkl > 0 else None
        g = self.gain()
        dbm = None
        if p is not None and self.cal is not None and g is not None:
            dbm = p - g + self.cal["offset_db"]
        return {"chan_dbfs": p, "peak_dbfs": pk, "dbm": dbm, "gain": g}

    def calibrate(self, reference_dbm):
        r = self.reading()
        if r["chan_dbfs"] is None or r["gain"] is None:
            raise RuntimeError("no level measured yet")
        self.cal = {"offset_db": reference_dbm - r["chan_dbfs"] + r["gain"],
                    "reference_dbm": reference_dbm, "gain_db": r["gain"],
                    "center_freq_hz": float(self.tb.get_center_freq()),
                    "date": time.strftime("%Y-%m-%d %H:%M")}
        save_cal(self.cal)
        return self.cal

    def clear_cal(self):
        self.cal = None
        try:
            os.remove(CAL_FILE)
        except OSError:
            pass

    # texts shared by the Spectrum and Control tabs and the console log
    def summary_html(self):
        r = self.reading()
        if r["chan_dbfs"] is None:
            return "Measuring the signal level..."
        parts = []
        if r["dbm"] is not None:
            parts.append("<b>Signal level: %.1f dBm</b>" % r["dbm"])
        else:
            parts.append("<b>Signal level: not calibrated</b> (Control tab &rarr; Level calibration)")
        parts.append("Channel power: %.1f dBFS" % r["chan_dbfs"])
        pk = r["peak_dbfs"]
        if pk is not None:
            if pk >= -1.0:
                parts.append("<span style='color:red'><b>ADC peak %.1f dBFS - clipping, "
                             "reduce RX Gain</b></span>" % pk)
            elif r["chan_dbfs"] < -45.0:
                parts.append("<span style='color:darkorange'>ADC peak %.1f dBFS - weak at the ADC, "
                             "increase RX Gain</span>" % pk)
            else:
                parts.append("ADC peak %.1f dBFS" % pk)
        return " &nbsp;|&nbsp; ".join(parts)

    def cal_text(self):
        c = self.cal
        if c is None:
            return "Status: not calibrated - the channel power is shown in dBFS."
        return ("Status: calibrated %s with %.1f dBm at %.0f dB RX Gain, %.3f MHz (offset %+.1f dB)"
                % (c.get("date", "?"), c.get("reference_dbm", float("nan")),
                   c.get("gain_db", float("nan")), c.get("center_freq_hz", 0) / 1e6,
                   c["offset_db"]))

    def log_text(self):
        r = self.reading()
        if r["chan_dbfs"] is None:
            return ""
        s = "RF %.1f dBFS (peak %s)" % (r["chan_dbfs"], "-" if r["peak_dbfs"] is None
                                          else "%.1f" % r["peak_dbfs"])
        if r["dbm"] is not None:
            s += " %.1f dBm" % r["dbm"]
        return s


def _channel_text(hz):
    """'Channel 14' (or the frequency if it is not an ISDB-Tb channel centre)."""
    try:
        import isdbt_dynamic
        ch, exact = isdbt_dynamic.channel_from_hz(hz)
        return ("Channel %d" % ch) if exact else ("%.3f MHz" % (hz / 1e6))
    except Exception:
        return "%.3f MHz" % (hz / 1e6)


def build_spectrum_tab(tb, rf, index=0):
    """Spectrum tab at `index` (0 = left of Constellation): the frequency sink
    and, on its right, the channel and the channel power in dBm (dBFS while
    not calibrated). Updated once per second, only while the tab is visible.
    The sink follows tb.set_center_freq()."""
    from PyQt5 import Qt, QtCore
    from gnuradio import qtgui
    from gnuradio.fft import window
    try:
        import sip
    except ImportError:
        from PyQt5 import sip

    fft = 1024
    samp_rate = float(tb.samp_rate)
    sink = qtgui.freq_sink_c(fft, window.WIN_BLACKMAN_hARRIS, float(tb.get_center_freq()),
                             samp_rate, "", 1)
    sink.set_update_time(0.10)
    sink.set_y_axis(-120, -10)
    sink.set_y_label("Relative level", "dBFS")
    sink.enable_grid(True)
    sink.set_fft_average(0.2)
    sink.enable_autoscale(False)
    sink.enable_control_panel(False)
    sink.disable_legend()                    # the channel is shown in the side panel
    s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, fft)
    keep = blocks.keep_one_in_n(gr.sizeof_gr_complex * fft, 64)
    v2s = blocks.vector_to_stream(gr.sizeof_gr_complex, fft)
    tb.connect(tb.limesdr_source_0, s2v, keep, v2s, sink)

    page = Qt.QWidget()
    lay = Qt.QHBoxLayout(page)
    getw = getattr(sink, "pyqwidget", None) or getattr(sink, "qwidget")
    lay.addWidget(sip.wrapinstance(getw(), Qt.QWidget), 1)

    side = Qt.QWidget()
    side.setFixedWidth(230)
    v = Qt.QVBoxLayout(side)
    big = Qt.QFont()
    big.setPointSize(big.pointSize() + 6)
    big.setBold(True)
    ch_label = Qt.QLabel()
    ch_label.setFont(big)
    freq_label = Qt.QLabel()
    freq_label.setStyleSheet("color: gray")
    pw_title = Qt.QLabel("Channel power")
    pw_value = Qt.QLabel("...")
    pw_value.setFont(big)
    pw_note = Qt.QLabel("")
    pw_note.setWordWrap(True)
    pw_note.setStyleSheet("color: gray")
    mer_title = Qt.QLabel("MER (all layers)")
    mer_value = Qt.QLabel("...")
    mer_value.setFont(big)
    adc = Qt.QLabel("")
    adc.setWordWrap(True)
    adc.setTextFormat(QtCore.Qt.RichText)
    for w in (ch_label, freq_label):
        v.addWidget(w)
    v.addSpacing(18)
    for w in (pw_title, pw_value, pw_note):
        v.addWidget(w)
    v.addSpacing(18)
    v.addWidget(mer_title)
    v.addWidget(mer_value)
    v.addSpacing(18)
    v.addWidget(adc)
    v.addStretch(1)
    lay.addWidget(side)

    tabs = tb.tab_widget_layers
    cur = tabs.currentWidget()
    tabs.insertTab(index, page, "Spectrum")
    if cur is not None:
        tabs.setCurrentWidget(cur)            # still opens on Constellation

    def show_channel(hz):
        ch_label.setText(_channel_text(hz))
        freq_label.setText("%.6f MHz" % (hz / 1e6))

    def refresh():
        if not page.isVisible():
            return
        dyn = getattr(tb, "_dyn_controller", None)
        try:
            mer = dyn.total_mer() if dyn is not None else None
        except Exception:
            mer = None
        mer_value.setText("%.1f dB" % mer if mer is not None else "-")
        r = rf.reading()
        if r["chan_dbfs"] is None:
            pw_value.setText("...")
            return
        if r["dbm"] is not None:
            pw_value.setText("%.1f dBm" % r["dbm"])
            pw_note.setText("%.1f dBFS at the ADC" % r["chan_dbfs"])
        else:
            pw_value.setText("%.1f dBFS" % r["chan_dbfs"])
            pw_note.setText("dBm: not calibrated (Control tab, Level calibration)")
        pk = r["peak_dbfs"]
        if pk is None:
            adc.setText("")
        elif pk >= -1.0:
            adc.setText("<span style='color:red'><b>ADC clipping</b> (peak %.1f dBFS)<br>"
                        "reduce RX Gain</span>" % pk)
        elif r["chan_dbfs"] < -45.0:
            adc.setText("<span style='color:darkorange'><b>Weak at the ADC</b><br>"
                        "increase RX Gain</span>")
        else:
            adc.setText("<span style='color:gray'>ADC peak %.1f dBFS</span>" % pk)
    timer = QtCore.QTimer()
    timer.timeout.connect(refresh)
    timer.start(1000)
    show_channel(float(tb.get_center_freq()))

    # keep the spectrum and the channel label in step with the Channel knob
    orig = tb.set_center_freq

    def set_center_freq(f):
        orig(f)
        sink.set_frequency_range(float(f), samp_rate)
        show_channel(float(f))
    tb.set_center_freq = set_center_freq
    tb._dyn_spectrum = (page, sink, s2v, keep, v2s, timer)
    return page
