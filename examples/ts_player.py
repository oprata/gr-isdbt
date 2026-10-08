#!/usr/bin/env python3
"""
ts_player.py - libVLC player run as a SEPARATE, low-priority process by the
TS Viewer tabs (ts_viewer.py). It draws into the X11 window of the tab
(--wid) and prints one statistics line per second on stdout.

Why a separate process (Rodada 51): VLC decoding + drawing every picture in
software (no GPU acceleration on the stb: "DRI2: failed to authenticate")
took CPU from the GNU Radio receiver: the LimeSDR samples overflowed and the
receiver lost sync every 10-30 s while a video was shown (0 times without
video). Here VLC runs with nice 10, so the receiver always has priority, and
it can be killed without touching the analyzer.

Exits when stdin is closed (the analyzer ended) or on SIGTERM.
"""
import argparse
import os
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wid", type=int, required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--nice", type=int, default=10)
    ap.add_argument("vlc_args", nargs="*")
    a = ap.parse_args()
    try:
        os.nice(a.nice)
    except OSError:
        pass
    import vlc
    inst = vlc.Instance(a.vlc_args)
    player = inst.media_player_new()
    if sys.platform.startswith("linux"):
        player.set_xwindow(a.wid)
    elif sys.platform == "win32":
        player.set_hwnd(a.wid)
    player.video_set_mouse_input(False)
    player.video_set_key_input(False)
    media = inst.media_new(a.url)
    player.set_media(media)
    player.play()

    done = threading.Event()

    def watch_stdin():                    # parent gone -> stdin EOF -> quit
        try:
            while sys.stdin.read(1):
                pass
        except Exception:
            pass
        done.set()
    threading.Thread(target=watch_stdin, daemon=True).start()

    try:
        while not done.wait(1.0):
            st = vlc.MediaStats()
            if media.get_stats(st):
                print("STATS %d %d %d %d" % (st.displayed_pictures, st.decoded_video,
                                             st.lost_pictures, st.demux_discontinuity),
                      flush=True)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        player.stop()
        player.release()


if __name__ == "__main__":
    main()
