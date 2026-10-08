#!/usr/bin/env python3
"""
vlc_probe.py - plays a URL or file with libVLC (same library as the TS Viewer)
and prints, every 2 s, frames decoded / shown / lost. Used to tell apart:
content (file), network input (udp:// or rtp://), and video output.

  python3 vlc_probe.py /tmp/dyn/ts_layer_b                 # recorded file
  python3 vlc_probe.py udp://@:5006                        # live (analyzer on another tab)
  python3 vlc_probe.py udp://@:5006 --vout=xcb_x11         # extra libVLC options
  python3 vlc_probe.py udp://@:5006 -vv 2> vlc_debug.log   # full VLC log
"""
import sys
import time
import warnings
warnings.filterwarnings("ignore")
import vlc

url = sys.argv[1]
extra = sys.argv[2:]
inst = vlc.Instance(["--no-video-title-show"] + extra)
player = inst.media_player_new()
media = inst.media_new(url)
player.set_media(media)
player.play()
print("libVLC %s  url=%s  options=%s" % (vlc.libvlc_get_version().decode(), url, extra or "-"))
last = 0
try:
    for _ in range(15):
        time.sleep(2)
        st = vlc.MediaStats()
        media.get_stats(st)
        print("decoded %6d | shown %6d (%3.0f/s) | lost %5d | demux discontinuities %4d | input %.1f Mb/s"
              % (st.decoded_video, st.displayed_pictures, (st.displayed_pictures - last) / 2.0,
                 st.lost_pictures, st.demux_discontinuity, st.input_bitrate * 8000 / 1e6),
              flush=True)
        last = st.displayed_pictures
finally:
    player.stop()
