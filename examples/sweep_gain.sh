#!/usr/bin/env bash
#
# sweep_gain.sh - run stbcast_analyzer.py for a range of LimeSDR RX gains and
# check the layer B transport stream recorded at each gain.
#
# Usage:
#   ./sweep_gain.sh                      # gains 0 10 20 30 40 50, 40 s each
#   ./sweep_gain.sh -g "0 5 10 15"       # custom gain list
#   ./sweep_gain.sh -t 60 -k             # 60 s per run, keep the TS files
#
# Requires in stbcast_analyzer.grc a Parameter block "rx_gain_init" (the
# generated .py then accepts --rx-gain-init) used as the default value of the
# QT GUI Range "rx_gain", which feeds the LimeSuite Source gain.

set -u

GAINS="0 10 20 30 40 50"
DURATION=40          # seconds per run
PAUSE=5              # seconds between runs (lets the LimeSDR USB device close)
KEEP_TS=0
FLOWGRAPH="stbcast_analyzer.py"
TS_FILE="/tmp/ts_layer_b"

# Layer B capacity for mode 3, 64QAM, CR 3/4, GI 1/16, 12 segments
LAYER_B_PKTS_PER_S=11865   # 17.84 Mb/s / (188*8)
FRAME_S=0.2184             # ISDB-T frame duration, mode 3, GI 1/16 (204 symbols)

while getopts "g:t:p:kh" opt; do
    case "$opt" in
        g) GAINS="$OPTARG" ;;
        t) DURATION="$OPTARG" ;;
        p) PAUSE="$OPTARG" ;;
        k) KEEP_TS=1 ;;
        h|*) sed -n '2,15p' "$0"; exit 0 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# --- pre-flight checks -------------------------------------------------------
for f in "$FLOWGRAPH" ts_check.py; do
    [ -f "$f" ] || { echo "ERROR: $f not found in $SCRIPT_DIR"; exit 1; }
done
if ! python3 "$FLOWGRAPH" --help 2>/dev/null | grep -q -- "--rx-gain-init"; then
    echo "ERROR: $FLOWGRAPH does not accept --rx-gain-init."
    echo "       Add the Parameter block 'rx_gain_init' to the .grc and regenerate (F5)."
    exit 1
fi
command -v stdbuf >/dev/null || { echo "ERROR: stdbuf not found (coreutils)"; exit 1; }

OUT="sweep_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
RESULTS="$OUT/results.jsonl"
: > "$RESULTS"

echo "Output directory : $OUT"
echo "Gains (dB)       : $GAINS"
echo "Duration per run : ${DURATION} s"
echo

# --- runs --------------------------------------------------------------------
for G in $GAINS; do
    echo "=== gain ${G} dB ==="
    rm -f "$TS_FILE"
    start=$(date +%s)
    stdbuf -oL -eL timeout -s TERM -k 10 "$DURATION" \
        python3 -u "$FLOWGRAPH" --rx-gain-init "$G" > "$OUT/run_g${G}.log" 2>&1
    rc=$?
    elapsed=$(( $(date +%s) - start ))
    # 124 = stopped by timeout (expected); anything else = flowgraph ended early
    status="ok"
    [ "$rc" -ne 124 ] && status="run_failed(rc=$rc)"

    tmcc_ok=$(grep -c "TMCC OK" "$OUT/run_g${G}.log")
    tmcc_bad=$(grep -c "TMCC NOT OK" "$OUT/run_g${G}.log")

    python3 ts_check.py "$TS_FILE" > "$OUT/ts_check_g${G}.log"
    json=$(python3 ts_check.py --json "$TS_FILE")
    python3 - "$G" "$status" "$elapsed" "$tmcc_ok" "$tmcc_bad" "$json" >> "$RESULTS" <<'PY'
import json, sys
g, status, elapsed, ok, bad, js = sys.argv[1:7]
r = json.loads(js)
r.update(gain=float(g), status=status, elapsed_s=int(elapsed),
         tmcc_ok=int(ok), tmcc_not_ok=int(bad))
print(json.dumps(r))
PY
    if [ "$KEEP_TS" -eq 1 ] && [ -f "$TS_FILE" ]; then
        cp "$TS_FILE" "$OUT/ts_layer_b_g${G}.ts"
    fi
    echo "    status=$status  TMCC OK=$tmcc_ok  NOT OK=$tmcc_bad  $(grep -E 'estimated loss|RESULT|EMPTY' "$OUT/ts_check_g${G}.log" | tr '\n' ' ')"
    sleep "$PAUSE"
done

# --- summary -----------------------------------------------------------------
python3 - "$RESULTS" "$LAYER_B_PKTS_PER_S" "$FRAME_S" > "$OUT/summary.md" <<'PY'
import json, sys
path, cap, frame_s = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
rows = [json.loads(l) for l in open(path) if l.strip()]

def f(v, fmt="{}"):
    return "-" if v is None else fmt.format(v)

print("| Gain (dB) | Status | TMCC OK | TMCC NOT OK | Packets | Throughput vs capacity* | Nulls (%) | Useful pkts | Est. loss (%) | Sync err | PIDs | TS result |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in rows:
    frames = r["tmcc_ok"] + r["tmcc_not_ok"]
    thr = None
    if frames:
        expected = frames * frame_s * cap
        thr = 100.0 * r["packets"] / expected
    print(f"| {r['gain']:g} | {r['status']} | {r['tmcc_ok']} | {r['tmcc_not_ok']} | {r['packets']} | "
          f"{f(thr, '{:.1f}%')} | {f(r['null_pct'])} | {r['useful']} | {f(r['loss_pct'])} | "
          f"{r['sync_errors']} | {r['pids']} | {r['result']} |")
print()
print("\\* Throughput vs capacity = TS packets recorded / (decoded ISDB-T frames x "
      f"{frame_s} s x {cap:.0f} pkt/s). Approximate: packets lost in the Reed-Solomon "
      "reduce it; 100% means no loss.")
print()
print("PIDs per gain:")
for r in rows:
    print(f"- {r['gain']:g} dB: {', '.join(r['pid_list']) or '-'}")
PY

echo
cat "$OUT/summary.md"
echo
echo "Files: $OUT/summary.md, $OUT/results.jsonl, $OUT/run_g*.log, $OUT/ts_check_g*.log"
