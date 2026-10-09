#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Stbcast Analyzer (dynamic layers)
# Description: Fixed part of the ISDB-Tb analyzer. Run: the per-layer part is built from the TMCC by isdbt_dynamic.py (via run_dynamic.py, see Run Command).
# GNU Radio version: 3.8.1.0

from distutils.version import StrictVersion

if __name__ == '__main__':
    import ctypes
    import sys
    if sys.platform.startswith('linux'):
        try:
            x11 = ctypes.cdll.LoadLibrary('libX11.so')
            x11.XInitThreads()
        except:
            print("Warning: failed to XInitThreads()")

from PyQt5 import Qt
from gnuradio import eng_notation
from gnuradio import qtgui
import sip
from gnuradio import blocks
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
import sys
import signal
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio.qtgui import Range, RangeWidget
import isdbt
import limesdr
from gnuradio import qtgui

class stbcast_analyzer_dyn(gr.top_block, Qt.QWidget):

    def __init__(self, rx_gain_init=38):
        gr.top_block.__init__(self, "Stbcast Analyzer (dynamic layers)")
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Stbcast Analyzer (dynamic layers)")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except:
            pass
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("GNU Radio", "stbcast_analyzer_dyn")

        try:
            if StrictVersion(Qt.qVersion()) < StrictVersion("5.0.0"):
                self.restoreGeometry(self.settings.value("geometry").toByteArray())
            else:
                self.restoreGeometry(self.settings.value("geometry"))
        except:
            pass

        ##################################################
        # Parameters
        ##################################################
        self.rx_gain_init = rx_gain_init

        ##################################################
        # Variables
        ##################################################
        self.mode = mode = 3
        self.total_carriers = total_carriers = 2**(10+mode)
        self.samp_rate = samp_rate = 8e6*64/63
        self.rx_gain = rx_gain = rx_gain_init
        self.guard = guard = 1.0/16
        self.data_carriers = data_carriers = 13*96*2**(mode-1)
        self.center_freq = center_freq = 473142857
        self.active_carriers = active_carriers = 13*108*2**(mode-1)+1

        ##################################################
        # Blocks
        ##################################################
        self.tab_widget_layers = Qt.QTabWidget()
        self.tab_widget_layers_widget_0 = Qt.QWidget()
        self.tab_widget_layers_layout_0 = Qt.QBoxLayout(Qt.QBoxLayout.TopToBottom, self.tab_widget_layers_widget_0)
        self.tab_widget_layers_grid_layout_0 = Qt.QGridLayout()
        self.tab_widget_layers_layout_0.addLayout(self.tab_widget_layers_grid_layout_0)
        self.tab_widget_layers.addTab(self.tab_widget_layers_widget_0, 'Constellation')
        self.tab_widget_layers_widget_1 = Qt.QWidget()
        self.tab_widget_layers_layout_1 = Qt.QBoxLayout(Qt.QBoxLayout.TopToBottom, self.tab_widget_layers_widget_1)
        self.tab_widget_layers_grid_layout_1 = Qt.QGridLayout()
        self.tab_widget_layers_layout_1.addLayout(self.tab_widget_layers_grid_layout_1)
        self.tab_widget_layers.addTab(self.tab_widget_layers_widget_1, 'Layer A')
        self.tab_widget_layers_widget_2 = Qt.QWidget()
        self.tab_widget_layers_layout_2 = Qt.QBoxLayout(Qt.QBoxLayout.TopToBottom, self.tab_widget_layers_widget_2)
        self.tab_widget_layers_grid_layout_2 = Qt.QGridLayout()
        self.tab_widget_layers_layout_2.addLayout(self.tab_widget_layers_grid_layout_2)
        self.tab_widget_layers.addTab(self.tab_widget_layers_widget_2, 'Layer B')
        self.tab_widget_layers_widget_3 = Qt.QWidget()
        self.tab_widget_layers_layout_3 = Qt.QBoxLayout(Qt.QBoxLayout.TopToBottom, self.tab_widget_layers_widget_3)
        self.tab_widget_layers_grid_layout_3 = Qt.QGridLayout()
        self.tab_widget_layers_layout_3.addLayout(self.tab_widget_layers_grid_layout_3)
        self.tab_widget_layers.addTab(self.tab_widget_layers_widget_3, 'Layer C')
        self.tab_widget_layers_widget_4 = Qt.QWidget()
        self.tab_widget_layers_layout_4 = Qt.QBoxLayout(Qt.QBoxLayout.TopToBottom, self.tab_widget_layers_widget_4)
        self.tab_widget_layers_grid_layout_4 = Qt.QGridLayout()
        self.tab_widget_layers_layout_4.addLayout(self.tab_widget_layers_grid_layout_4)
        self.tab_widget_layers.addTab(self.tab_widget_layers_widget_4, 'Control')
        self.top_grid_layout.addWidget(self.tab_widget_layers)
        self._rx_gain_range = Range(0, 50, 1, rx_gain_init, 200)
        self._rx_gain_win = RangeWidget(self._rx_gain_range, self.set_rx_gain, 'RX Gain (dB)', "dial", float)
        self.tab_widget_layers_grid_layout_4.addWidget(self._rx_gain_win, 0, 0, 1, 1)
        for r in range(0, 1):
            self.tab_widget_layers_grid_layout_4.setRowStretch(r, 1)
        for c in range(0, 1):
            self.tab_widget_layers_grid_layout_4.setColumnStretch(c, 1)
        self._center_freq_tool_bar = Qt.QToolBar(self)
        self._center_freq_tool_bar.addWidget(Qt.QLabel('Center frequency (Hz)' + ": "))
        self._center_freq_line_edit = Qt.QLineEdit(str(self.center_freq))
        self._center_freq_tool_bar.addWidget(self._center_freq_line_edit)
        self._center_freq_line_edit.returnPressed.connect(
            lambda: self.set_center_freq(int(str(self._center_freq_line_edit.text()))))
        self.tab_widget_layers_grid_layout_4.addWidget(self._center_freq_tool_bar, 1, 0, 1, 1)
        for r in range(1, 2):
            self.tab_widget_layers_grid_layout_4.setRowStretch(r, 1)
        for c in range(0, 1):
            self.tab_widget_layers_grid_layout_4.setColumnStretch(c, 1)
        self.qtgui_const_sink_x_0 = qtgui.const_sink_c(
            data_carriers, #size
            "Data carriers (all layers)", #name
            1 #number of inputs
        )
        self.qtgui_const_sink_x_0.set_update_time(0.10)
        self.qtgui_const_sink_x_0.set_y_axis(-2, 2)
        self.qtgui_const_sink_x_0.set_x_axis(-2, 2)
        self.qtgui_const_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.qtgui_const_sink_x_0.enable_autoscale(False)
        self.qtgui_const_sink_x_0.enable_grid(True)
        self.qtgui_const_sink_x_0.enable_axis_labels(True)

        self.qtgui_const_sink_x_0.disable_legend()

        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "red", "red", "red",
            "red", "red", "red", "red", "red"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_const_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_const_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_const_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_const_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_const_sink_x_0.set_line_style(i, styles[i])
            self.qtgui_const_sink_x_0.set_line_marker(i, markers[i])
            self.qtgui_const_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_const_sink_x_0_win = sip.wrapinstance(self.qtgui_const_sink_x_0.pyqwidget(), Qt.QWidget)
        self.tab_widget_layers_layout_0.addWidget(self._qtgui_const_sink_x_0_win)
        self.low_pass_filter_0 = filter.fir_filter_ccf(
            1,
            firdes.low_pass(
                1,
                samp_rate,
                5.8e6/2.0,
                0.5e6,
                firdes.WIN_HAMMING,
                6.76))
        self.limesdr_source_0 = limesdr.source('', 0, '')


        self.limesdr_source_0.set_sample_rate(samp_rate)


        self.limesdr_source_0.set_center_freq(center_freq, 0)

        self.limesdr_source_0.set_bandwidth(8e6, 0)


        self.limesdr_source_0.set_digital_filter(samp_rate, 0)


        self.limesdr_source_0.set_gain(rx_gain, 0)


        self.limesdr_source_0.set_antenna(3, 0)


        self.limesdr_source_0.calibrate(8e6, 0)
        self.isdbt_tmcc_decoder_0 = isdbt.tmcc_decoder(3, False)
        self.isdbt_ofdm_synchronization_0 = isdbt.ofdm_synchronization(3, 0.0625, False)
        self.blocks_vector_to_stream_0_2 = blocks.vector_to_stream(gr.sizeof_gr_complex*1, data_carriers)



        ##################################################
        # Connections
        ##################################################
        self.connect((self.blocks_vector_to_stream_0_2, 0), (self.qtgui_const_sink_x_0, 0))
        self.connect((self.isdbt_ofdm_synchronization_0, 0), (self.isdbt_tmcc_decoder_0, 0))
        self.connect((self.isdbt_tmcc_decoder_0, 0), (self.blocks_vector_to_stream_0_2, 0))
        self.connect((self.limesdr_source_0, 0), (self.low_pass_filter_0, 0))
        self.connect((self.low_pass_filter_0, 0), (self.isdbt_ofdm_synchronization_0, 0))

    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "stbcast_analyzer_dyn")
        self.settings.setValue("geometry", self.saveGeometry())
        event.accept()

    def get_rx_gain_init(self):
        return self.rx_gain_init

    def set_rx_gain_init(self, rx_gain_init):
        self.rx_gain_init = rx_gain_init
        self.set_rx_gain(self.rx_gain_init)

    def get_mode(self):
        return self.mode

    def set_mode(self, mode):
        self.mode = mode
        self.set_active_carriers(13*108*2**(self.mode-1)+1)
        self.set_data_carriers(13*96*2**(self.mode-1))
        self.set_total_carriers(2**(10+self.mode))

    def get_total_carriers(self):
        return self.total_carriers

    def set_total_carriers(self, total_carriers):
        self.total_carriers = total_carriers

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.limesdr_source_0.set_digital_filter(self.samp_rate, 0)
        self.limesdr_source_0.set_digital_filter(self.samp_rate, 1)
        self.low_pass_filter_0.set_taps(firdes.low_pass(1, self.samp_rate, 5.8e6/2.0, 0.5e6, firdes.WIN_HAMMING, 6.76))

    def get_rx_gain(self):
        return self.rx_gain

    def set_rx_gain(self, rx_gain):
        self.rx_gain = rx_gain
        self.limesdr_source_0.set_gain(self.rx_gain, 0)

    def get_guard(self):
        return self.guard

    def set_guard(self, guard):
        self.guard = guard

    def get_data_carriers(self):
        return self.data_carriers

    def set_data_carriers(self, data_carriers):
        self.data_carriers = data_carriers

    def get_center_freq(self):
        return self.center_freq

    def set_center_freq(self, center_freq):
        self.center_freq = center_freq
        Qt.QMetaObject.invokeMethod(self._center_freq_line_edit, "setText", Qt.Q_ARG("QString", str(self.center_freq)))
        self.limesdr_source_0.set_center_freq(self.center_freq, 0)

    def get_active_carriers(self):
        return self.active_carriers

    def set_active_carriers(self, active_carriers):
        self.active_carriers = active_carriers


def argument_parser():
    description = 'Fixed part of the ISDB-Tb analyzer. Run: the per-layer part is built from the TMCC by isdbt_dynamic.py (via run_dynamic.py, see Run Command).'
    parser = ArgumentParser(description=description)
    parser.add_argument(
        "--rx-gain-init", dest="rx_gain_init", type=eng_float, default="38.0",
        help="Set rx_gain_init [default=%(default)r]")
    return parser


def main(top_block_cls=stbcast_analyzer_dyn, options=None):
    if options is None:
        options = argument_parser().parse_args()
    if gr.enable_realtime_scheduling() != gr.RT_OK:
        print("Error: failed to enable real-time scheduling.")

    if StrictVersion("4.5.0") <= StrictVersion(Qt.qVersion()) < StrictVersion("5.0.0"):
        style = gr.prefs().get_string('qtgui', 'style', 'raster')
        Qt.QApplication.setGraphicsSystem(style)
    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(rx_gain_init=options.rx_gain_init)
    tb.start()
    tb.show()

    def sig_handler(sig=None, frame=None):
        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    def quitting():
        tb.stop()
        tb.wait()
    qapp.aboutToQuit.connect(quitting)
    qapp.exec_()


if __name__ == '__main__':
    main()
