"""
ts_viewer.py - "TS Viewer" tab: plays the RTP stream of one layer with an
embedded libVLC player (python-vlc).

Only the viewer whose tab is visible plays (decoding H.264 costs CPU and the
layer B Viterbi already takes most of one core); the others are stopped.
Rodada 51: the player runs in its own process (ts_player.py) with nice 10 and
plain X11 output, so it can never starve the receiver; ISDBT_VIEWER_INPROC=1
keeps it inside the analyzer process as before. The
UDP stream itself is always sent. If python-vlc / libVLC is missing the tab
shows how to install it and the analyzer keeps working.
"""

import os
import shlex
import sys

try:
    import vlc
    _VLC_ERROR = None
except Exception as e:                       # ImportError or missing libvlc
    vlc = None
    _VLC_ERROR = e

_INSTANCE = None


def vlc_args():
    """libVLC options of the viewer. --vout=xcb_x11: plain X11 output, no
    OpenGL (without GPU acceleration the GL output draws in software and
    competes with the receiver); --avcodec-threads=2 bounds the decoder.
    Extra options: ISDBT_VLC_ARGS (appended, so they override these)."""
    args = ["--quiet", "--no-video-title-show", "--network-caching=1000",
            "--no-snapshot-preview", "--no-skip-frames", "--no-drop-late-frames",
            "--avcodec-threads=2"]
    if sys.platform.startswith("linux"):
        args.append("--vout=xcb_x11")
    return args + shlex.split(os.environ.get("ISDBT_VLC_ARGS", ""))


def vlc_instance():
    global _INSTANCE
    if _INSTANCE is None and vlc is not None:
        # --no-skip-frames / --no-drop-late-frames: VLC otherwise discards
        # ~40% of the one-seg (layer A) pictures as "late" even from a file,
        # which looks jerky / flickering (Rodada 50)
        args = vlc_args()
        _INSTANCE = vlc.Instance(args)
    return _INSTANCE


def ts_url(host, port, proto="udp"):
    """URL VLC listens on: multicast group, or any local address for unicast.
    proto 'udp' (plain TS over UDP, default) or 'rtp'."""
    try:
        first = int(host.split(".")[0])
    except ValueError:
        first = 0
    return "%s://@%s:%d" % (proto, host if 224 <= first <= 239 else "", int(port))


def rtp_url(host, port):                       # kept for compatibility
    return ts_url(host, port, "rtp")


class TsViewer(object):
    def __init__(self, layer, host, port, proto="udp"):
        from PyQt5 import Qt, QtCore
        self.layer = layer
        self.url = ts_url(host, port, proto)
        self.widget = Qt.QWidget()
        lay = Qt.QVBoxLayout(self.widget)
        self.status = Qt.QLabel()
        # the text changes every second: never let it resize the video area
        self.status.setSizePolicy(Qt.QSizePolicy.Ignored, Qt.QSizePolicy.Fixed)
        lay.addWidget(self.status)
        self.video = Qt.QFrame()
        self.video.setStyleSheet("background-color: black")
        self.video.setSizePolicy(Qt.QSizePolicy.Expanding, Qt.QSizePolicy.Expanding)
        self.video.setAttribute(QtCore.Qt.WA_NativeWindow, True)
        lay.addWidget(self.video, 1)
        self.show_stats = os.environ.get("ISDBT_VIEWER_STATS", "0") == "1"
        self.player = None
        self.media = None
        self.proc = None                     # ts_player.py process (default mode)
        self._proc_stats = None
        self.playing = False
        self.stats_text = ""
        self._last = None                    # (time, displayed) for the frame rate
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._update_stats)
        # Rodada 52: by default the tab shows only the video - no text line
        # while playing and no 1 Hz updates. ISDBT_VIEWER_STATS=1 shows the
        # status line with the libVLC counters (diagnostics).
        self.show_stats = os.environ.get("ISDBT_VIEWER_STATS", "0") == "1"
        if self.show_stats:
            self.timer.start(1000)
        self.available = False
        self.pat_inserted = False
        if vlc is None:
            self.status.setText("Layer %s - libVLC not available (%s). Install: "
                                "sudo apt install vlc python3-vlc" % (layer, _VLC_ERROR))
        else:
            self._show()

    def _show(self):
        state = "playing" if self.playing else ("stopped" if self.available else "layer not present")
        pat = " - PAT inserted by the analyzer" if self.pat_inserted else ""
        stats = (" | " + self.stats_text) if (self.playing and self.stats_text) else ""
        self.status.setText("Layer %s - %s - %s%s%s" % (self.layer, self.url, state, pat, stats))
        # only the video while playing (unless diagnostics are on)
        self.status.setVisible(self.show_stats or not self.playing)

    def _update_stats(self):
        """Frames shown per second, decoded, lost (libVLC counters), read from
        the player process (or from the in-process player)."""
        if not self.playing:
            return
        st = None
        if self.proc is not None:
            st = self._proc_stats
            if self.proc.poll() is not None:
                self.stats_text = "player process ended (code %s)" % self.proc.returncode
                self._show()
                return
        elif self.media is not None and vlc is not None:
            try:
                m = vlc.MediaStats()
                if self.media.get_stats(m):
                    st = (m.displayed_pictures, m.decoded_video, m.lost_pictures,
                          m.demux_discontinuity)
            except Exception:
                st = None
        if st is None:
            return
        import time
        now = time.monotonic()
        fps = ""
        if self._last is not None and now > self._last[0]:
            fps = "%.0f/s" % (max(0, st[0] - self._last[1]) / (now - self._last[0]))
        self._last = (now, st[0])
        self.stats_text = ("frames shown %s (total %d), decoded %d, lost %d, discontinuities %d"
                           % ((fps or "-",) + tuple(st)))
        self._show()

    def set_available(self, available):
        self.available = available
        if not available:
            self.stop()
        self._show()

    def set_pat_inserted(self, flag):
        if flag != self.pat_inserted:
            self.pat_inserted = flag
            self._show()

    def play(self):
        if vlc is None or not self.available:
            return
        self._last = None
        self.stats_text = ""
        if os.environ.get("ISDBT_VIEWER_INPROC") == "1":
            self._play_inproc()
        else:
            self._play_process()
        self.playing = True
        self._show()

    def _play_process(self):
        """Default: VLC in its own low-priority process (ts_player.py) that
        draws into this tab's native window."""
        import subprocess
        import threading
        here = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, "-u", os.path.join(here, "ts_player.py"),
               "--wid", str(int(self.video.winId())), "--url", self.url, "--"] + vlc_args()
        self._proc_stats = None
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     universal_newlines=True)
        proc = self.proc

        def reader():
            for line in proc.stdout:
                p = line.split()
                if len(p) == 5 and p[0] == "STATS":
                    try:
                        self._proc_stats = tuple(int(v) for v in p[1:])
                    except ValueError:
                        pass
        threading.Thread(target=reader, daemon=True).start()

    def _play_inproc(self):
        """ISDBT_VIEWER_INPROC=1: libVLC inside the analyzer process (old way)."""
        if self.player is None:
            inst = vlc_instance()
            self.player = inst.media_player_new()
            wid = int(self.video.winId())
            if sys.platform.startswith("linux"):
                self.player.set_xwindow(wid)
            elif sys.platform == "win32":
                self.player.set_hwnd(wid)
            self.player.video_set_mouse_input(False)
            self.player.video_set_key_input(False)
        self.media = vlc_instance().media_new(self.url)
        self.player.set_media(self.media)
        self.player.play()

    def stop(self):
        if self.proc is not None:
            proc, self.proc = self.proc, None
            try:
                proc.stdin.close()            # the player quits on stdin EOF
                proc.wait(2.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(1.0)
                except Exception:
                    pass
        if self.player is not None and self.playing:
            self.player.stop()
        self.playing = False
        if vlc is not None:
            self._show()

    def restart(self):
        """After a rebuild the RTP stream restarts (new SSRC / sequence)."""
        if self.playing:
            self.stop()
            self.play()

    def release(self):
        self.timer.stop()
        self.stop()
        if self.player is not None:
            self.player.release()
            self.player = None
