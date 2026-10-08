#!/usr/bin/env python3
"""
run_dynamic.py - runs the generated stbcast_analyzer (fixed part) with the
per-layer part built automatically from the TMCC (isdbt_dynamic.py).

Live (LimeSDR):
    python3 run_dynamic.py --rx-gain-init 35 2>&1 | tee dyn_live.log
Offline (recorded IQ instead of the LimeSDR):
    python3 run_dynamic.py --iq /dev/shm/iq_T1.cfile [--throttle] 2>&1 | tee dyn_off.log

Every 2 s a line with the TMCC frame counters and, for each built layer, the
status, MER, BER after Viterbi and packets corrected/lost in the last 10 s is
printed. At the end, ts_check is run on every TS file that was written.

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
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


STOP = threading.Event()
STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


def start_signal_thread():
    """Ctrl+C / timeout (SIGTERM) handling that does not depend on Python
    signal handlers (Rodada 45: on the stb, with the LimeSDR, the SIGTERM
    handler was lost and 'timeout' killed the run without the summary).

    The signals are blocked here, BEFORE GNU Radio, Qt or LimeSuite create any
    thread (threads inherit the mask), and a dedicated thread takes them with
    sigwait(): whatever a library does to the handlers, the signal stays
    pending until this thread reads it."""
    signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)

    def wait():
        sig = signal.sigwait(STOP_SIGNALS)
        print("Signal %d received" % sig, flush=True)
        STOP.set()
        sig = signal.sigwait(STOP_SIGNALS)          # second signal: leave now
        print("Signal %d received again: exiting immediately" % sig, flush=True)
        os._exit(1)

    threading.Thread(target=wait, name="signals", daemon=True).start()


def main():
    start_signal_thread()
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
        m = dyn.metrics(ascii_only=True)
        parts = []
        for L in "ABC":
            if L in m:
                e = m[L]
                if e["win"] is not None:
                    parts.append("%s: %s | MER %4.1f dB | BER %s | corr %d/%d pkt | lost %d"
                                 % (L, e["status"], e["mer"], e["ber_text"],
                                    e["win"]["corrected_packets"], e["win"]["packets"],
                                    e["win"]["uncorrectable"]))
                else:
                    parts.append("%s: MER %4.1f dB | BER n/a" % (L, e["mer"]))
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
    # Ctrl+C / timeout: the signal thread sets STOP; leave the Qt loop then
    stop_timer = Qt.QTimer()
    stop_timer.timeout.connect(lambda: Qt.QApplication.quit() if STOP.is_set() else None)
    stop_timer.start(200)

    qapp.exec_()
    print("Stopping...", flush=True)

    # Stop with a time limit: if the flowgraph (e.g. the LimeSDR source) does
    # not stop in 10 s, print the summary anyway and exit (Rodada 44).
    dyn.close()
    stopper = threading.Thread(target=lambda: (tb.stop(), tb.wait()), daemon=True)
    stopper.start()
    stopper.join(timeout=10)
    hung = stopper.is_alive()
    if hung:
        print("WARNING: flowgraph did not stop within 10 s; summary from the files written so far",
              flush=True)
    else:
        for s in dyn.ts_sinks:
            s.close()

    wall = time.time() - t0
    print()
    if prof:
        prof.running = False
        prof.report(wall)
        print()
    print("Run time: %.0f s" % wall)
    print("TMCC frames: %d OK, %d not OK" % (dyn.frames_ok, dyn.frames_bad))
    print("Rebuilds: %d" % dyn.rebuilds)
    for when, dt, desc in dyn.rebuild_log:
        print("  %s  %.2f s  %s" % (when, dt, desc))
    import glob
    for p in dyn.ts_paths:
        for f in sorted(glob.glob(p) + glob.glob(p + ".[0-9][0-9]")):
            print()
            ts_check.check(f)
    sys.stdout.flush()
    if hung:
        os._exit(0)


if __name__ == "__main__":
    main()
