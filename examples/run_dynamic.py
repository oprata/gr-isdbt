#!/usr/bin/env python3
"""
run_dynamic.py - runs the generated stbcast_analyzer (fixed part) with the
per-layer part built automatically from the TMCC (isdbt_dynamic.py).

Live (LimeSDR):
    python3 run_dynamic.py --rx-gain-init 35 2>&1 | tee dyn_live.log
Offline (recorded IQ instead of the LimeSDR):
    python3 run_dynamic.py --iq /dev/shm/iq_T1.cfile [--throttle] 2>&1 | tee dyn_off.log

Every 2 s a line with the TMCC frame counters and MER/BER of each built layer
is printed. At the end, ts_check is run on every TS file that was written.

From GRC (the Run/Execute button of stbcast_analyzer_dyn.grc): the .grc's
"Run Command" option calls this script with the generated .py as argument.

Options:
  FLOWGRAPH.py       optional: generated flowgraph file (module/class = its name)
  --ts-dir DIR       where ts_layer_a/b/c are written (default /tmp/dyn)
  --hysteresis N     identical valid TMCC frames before (re)building (default 3)
  --duration S       stop after S seconds (default: until the window is closed;
                     offline: until the IQ file has been processed)
  --flowgraph NAME   generated module/class (default stbcast_analyzer_dyn)
  --profile          print the CPU time of each GNU Radio thread at the end
  --split-ts         a new TS file per rebuild (ts_layer_b.01, .02 ...), for V2
  --no-gui           do not show the window (offline/regression runs)
"""
import argparse
import importlib
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("flowgraph_file", nargs="?", metavar="FLOWGRAPH.py")
    ap.add_argument("--rx-gain-init", type=float, default=None)
    ap.add_argument("--iq", metavar="FILE.cfile")
    ap.add_argument("--throttle", action="store_true")
    ap.add_argument("--ts-dir", default="/tmp/dyn")
    ap.add_argument("--hysteresis", type=int, default=3)
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--flowgraph", default="stbcast_analyzer_dyn")
    ap.add_argument("--no-gui", action="store_true")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--split-ts", action="store_true")
    a = ap.parse_args()
    if a.flowgraph_file:
        fgpath = os.path.abspath(a.flowgraph_file)
        sys.path.insert(0, os.path.dirname(fgpath))
        a.flowgraph = os.path.splitext(os.path.basename(fgpath))[0]

    if a.iq:
        import limesdr
        from run_offline import FileAsLimeSource
        iq = os.path.abspath(a.iq)
        if not os.path.isfile(iq):
            sys.exit("ERROR: %s not found" % iq)
        limesdr.source = lambda *x, **k: FileAsLimeSource(iq, a.throttle)
        print("Source: %s (%s)" % (iq, "throttled" if a.throttle else "as fast as possible"))
    else:
        print("Source: LimeSDR")

    from PyQt5 import Qt
    from gnuradio import gr
    import isdbt_dynamic
    import ts_check

    qapp = Qt.QApplication(sys.argv[:1])
    fg = importlib.import_module(a.flowgraph)
    cls = getattr(fg, a.flowgraph)
    tb = cls() if a.rx_gain_init is None else cls(rx_gain_init=a.rx_gain_init)

    os.makedirs(a.ts_dir, exist_ok=True)
    gui = None if a.no_gui else isdbt_dynamic.gui_from_generated(tb)
    dyn = isdbt_dynamic.DynamicLayers(tb, tb.isdbt_tmcc_decoder_0, mode=tb.mode,
                                      ts_dir=a.ts_dir, hysteresis=a.hysteresis, gui=gui,
                                      split_ts=a.split_ts)
    print("TS files: %s/ts_layer_{a,b,c}   hysteresis: %d frames" % (a.ts_dir, a.hysteresis))

    t0 = time.time()
    if a.iq and not a.throttle:
        print("(offline, unthrottled: timestamps are processing time, not signal time)")
    if gr.enable_realtime_scheduling() != gr.RT_OK and not a.iq:
        print("Note: real-time scheduling not enabled")
    prof = None
    if a.profile:
        from run_offline import ThreadProfiler
        prof = ThreadProfiler()
        prof.start()
    tb.start()
    if not a.no_gui:
        tb.show()

    state = {"last_ok": 0}

    def tick():
        el = time.time() - t0
        m = dyn.metrics()
        parts = []
        for L in "ABC":
            if L in m:
                parts.append("%s: MER %5.1f dB BERpre %6.2f BERpost %6.2f"
                             % (L, m[L]["mer"], m[L]["ber_pre"], m[L]["ber_post"]))
        print("[%6.1f s] TMCC ok %d bad %d | %s" % (el, dyn.frames_ok, dyn.frames_bad,
                                                   " | ".join(parts) or "no layer built"),
              flush=True)
        if a.duration and el >= a.duration:
            Qt.QApplication.quit()
        # offline: finished when no TMCC frame arrived for 5 s
        if a.iq and dyn.last_msg_time and time.time() - dyn.last_msg_time > 5 \
                and dyn.pending is None:
            print("IQ file processed.")
            Qt.QApplication.quit()

    timer = Qt.QTimer()
    timer.timeout.connect(tick)
    timer.start(2000)
    signal.signal(signal.SIGINT, lambda *x: Qt.QApplication.quit())
    signal.signal(signal.SIGTERM, lambda *x: Qt.QApplication.quit())

    qapp.exec_()

    dyn.close()
    tb.stop()
    tb.wait()
    for s in dyn.ts_sinks:
        s.close()

    wall = time.time() - t0
    print()
    if prof:
        prof.running = False
        prof.report(wall)
        print()
    print("TMCC frames: %d OK, %d not OK" % (dyn.frames_ok, dyn.frames_bad))
    print("Rebuilds: %d" % dyn.rebuilds)
    for when, dt, desc in dyn.rebuild_log:
        print("  %s  %.2f s  %s" % (when, dt, desc))
    import glob
    for p in dyn.ts_paths:
        for f in sorted(glob.glob(p) + glob.glob(p + ".[0-9][0-9]")):
            print()
            ts_check.check(f)


if __name__ == "__main__":
    main()
