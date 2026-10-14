#!/bin/bash
# monitor_app.sh - CPU, memory and network of the analyzer, one CSV line per
# interval (default 60 s), for long runs. Reads /proc only (no extra packages).
#
#   ./monitor_app.sh [interval_s] [output.csv]
#   ./monitor_app.sh 5                 # live table in the terminal only
#   ./monitor_app.sh 60 recursos.csv   # live table + CSV (for days)
#
# The terminal shows one line per interval (header repeated every 20 lines),
# including the memory growth since the monitor started (dRSS).
#
# Columns: time, analyzer pid, CPU % (average over the interval, 100 = one
# core), RSS MB (resident memory), threads, open fds, player pid, player CPU %,
# player RSS MB, loopback Mb/s (UDP of the TS viewers), system load (1 min),
# system memory available MB.
# A steadily growing RSS or fd count over days points to a leak.

INTERVAL=${1:-5}
OUT=${2:-}
HZ=$(getconf CLK_TCK)

find_pid() {           # the python process whose command line contains $1
    for p in $(pgrep -f "$1"); do
        case "$(cat /proc/$p/comm 2>/dev/null)" in python*) echo "$p"; return;; esac
    done
}
cpu_ticks() {          # utime+stime of a pid (ticks), or empty
    [ -n "$1" ] && [ -r /proc/$1/stat ] && awk '{print $14+$15}' /proc/$1/stat 2>/dev/null
}
rss_mb() { awk '/VmRSS/ {printf "%.1f", $2/1024}' /proc/$1/status 2>/dev/null; }
threads() { awk '/Threads/ {print $2}' /proc/$1/status 2>/dev/null; }
nfds() { ls /proc/$1/fd 2>/dev/null | wc -l; }
lo_bytes() { awk '/^ *lo:/ {print $2+$10}' /proc/net/dev; }

if [ -n "$OUT" ]; then
    [ -s "$OUT" ] || echo "time,pid,cpu_pct,rss_mb,threads,fds,player_pid,player_cpu_pct,player_rss_mb,lo_mbps,load1,mem_avail_mb" > "$OUT"
    echo "Also writing $OUT"
fi
echo "Every $INTERVAL s (Ctrl+C to stop). CPU 100% = one core of $(nproc)."
header() {
    printf "\n%-8s | %-7s %6s %8s %7s %4s %4s | %-7s %6s %7s | %8s | %5s %7s\n" \
        "time" "pid" "CPU%" "RSS MB" "dRSS" "thr" "fds" "player" "CPU%" "RSS MB" "lo Mb/s" "load" "free MB"
    printf -- "---------+-----------------------------------------------+------------------------+----------+--------------\n"
}
n=0; rss0=""

prev_t=$(date +%s.%N); prev_lo=$(lo_bytes)
pid=$(find_pid run_dynamic.py); prev_c=$(cpu_ticks "$pid")
ppid=$(find_pid ts_player.py); prev_pc=$(cpu_ticks "$ppid")
while sleep "$INTERVAL"; do
    now=$(date +%s.%N); dt=$(echo "$now - $prev_t" | bc -l)
    lo=$(lo_bytes)
    pid_now=$(find_pid run_dynamic.py)
    ppid_now=$(find_pid ts_player.py)
    c=$(cpu_ticks "$pid_now"); pc=$(cpu_ticks "$ppid_now")
    cpu=""; pcpu=""
    if [ -n "$c" ] && [ -n "$prev_c" ] && [ "$pid_now" = "$pid" ]; then
        cpu=$(echo "scale=1; 100*($c-$prev_c)/$HZ/$dt" | bc -l | sed 's/^\./0./'); fi
    if [ -n "$pc" ] && [ -n "$prev_pc" ] && [ "$ppid_now" = "$ppid" ]; then
        pcpu=$(echo "scale=1; 100*($pc-$prev_pc)/$HZ/$dt" | bc -l | sed 's/^\./0./'); fi
    lomb=$(echo "scale=2; 8*($lo-$prev_lo)/$dt/1000000" | bc -l | sed 's/^\./0./')
    load=$(cut -d' ' -f1 /proc/loadavg)
    mav=$(awk '/MemAvailable/ {printf "%.0f", $2/1024}' /proc/meminfo)
    rss=""; thr=""; fds=""; prss=""
    if [ -n "$pid_now" ]; then rss=$(rss_mb $pid_now); thr=$(threads $pid_now); fds=$(nfds $pid_now); fi
    [ -n "$ppid_now" ] && prss=$(rss_mb $ppid_now)
    [ -n "$OUT" ] && echo "$(date '+%Y-%m-%d %H:%M:%S'),${pid_now},${cpu},${rss},${thr},${fds},${ppid_now},${pcpu},${prss},${lomb},${load},${mav}" >> "$OUT"
    # terminal
    [ $((n % 20)) -eq 0 ] && header
    n=$((n + 1))
    if [ -n "$rss" ] && [ -z "$rss0" ]; then rss0=$rss; fi
    drss=""; [ -n "$rss" ] && [ -n "$rss0" ] && drss=$(echo "$rss - $rss0" | bc -l | sed 's/^\./0./;s/^-\./-0./')
    if [ -z "$pid_now" ]; then
        printf "%-8s | %s\n" "$(date +%H:%M:%S)" "ANALYZER NOT RUNNING"
    else
        printf "%-8s | %-7s %6s %8s %+7.1f %4s %4s | %-7s %6s %7s | %8s | %5s %7s\n" \
            "$(date +%H:%M:%S)" "$pid_now" "${cpu:--}" "$rss" "${drss:-0}" "$thr" "$fds" \
            "${ppid_now:--}" "${pcpu:--}" "${prss:--}" "$lomb" "$load" "$mav"
    fi
    prev_t=$now; prev_lo=$lo; pid=$pid_now; prev_c=$c; ppid=$ppid_now; prev_pc=$pc
done
