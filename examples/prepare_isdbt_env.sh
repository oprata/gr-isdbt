#!/bin/bash
#
# prepare_isdbt_env.sh
#
# Prepares the environment for the stbcast_analyzer flowgraph, which
# extracts all three ISDB-T layers (A, B, C) into three separate named
# FIFOs. Each FIFO is drained by its own ffmpeg process (regenerating
# PTSs so any standard decoder works) and forwarded over multicast UDP:
#
#   /tmp/ts_layer_a  →  ffmpeg  →  udp://239.1.1.1:1234  (1 seg / QPSK)
#   /tmp/ts_layer_b  →  ffmpeg  →  udp://239.1.1.2:1234  (12 seg / 64-QAM, main video)
#   /tmp/ts_layer_c  →  ffmpeg  →  udp://239.1.1.3:1234  (0 seg today / diagnostic)
#
# Idempotent: safe to run multiple times. It only acts when something
# is out of the desired state.
#
# Usage:
#   sudo ./prepare_isdbt_env.sh          # apply desired state
#   sudo ./prepare_isdbt_env.sh --status # report only, do not change
#   sudo ./prepare_isdbt_env.sh --clean  # remove the FIFOs (debug helper)
#

set -euo pipefail

# ────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────

FIFO_PATHS=(
#    "/tmp/ts_layer_a"
    "/tmp/ts_layer_b"
#    "/tmp/ts_layer_c"
)
FIFO_MODE="666"
FIFO_OWNER="${SUDO_USER:-root}"   # desired owner (user that ran sudo)

# Desired sysctl values (key → value)
declare -A SYSCTLS=(
    [net.core.wmem_max]=26214400
    [net.core.wmem_default]=26214400
    [net.core.rmem_max]=26214400
    [net.core.netdev_max_backlog]=5000
    [fs.pipe-max-size]=4194304
)

# ────────────────────────────────────────────────────────────────
# Logging helpers
# ────────────────────────────────────────────────────────────────

if [ -t 1 ]; then
    C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'
    C_BLUE='\033[0;34m';  C_DIM='\033[0;2m';    C_NC='\033[0m'
else
    C_GREEN=''; C_YELLOW=''; C_RED=''; C_BLUE=''; C_DIM=''; C_NC=''
fi

info() { echo -e "${C_BLUE}[INFO]${C_NC} $*"; }
ok()   { echo -e "${C_GREEN}[ OK ]${C_NC} $*"; }
warn() { echo -e "${C_YELLOW}[WARN]${C_NC} $*"; }
err()  { echo -e "${C_RED}[ERR ]${C_NC} $*" >&2; }
skip() { echo -e "${C_DIM}[skip]${C_NC} $*"; }

# ────────────────────────────────────────────────────────────────
# Argument parsing
# ────────────────────────────────────────────────────────────────

MODE="apply"
case "${1:-}" in
    --status) MODE="status" ;;
    --clean)  MODE="clean"  ;;
    --help|-h)
        sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    "") ;;
    *)
        err "Unknown argument: $1"
        err "Use --help to see available options."
        exit 2
        ;;
esac

# ────────────────────────────────────────────────────────────────
# Privilege check
# ────────────────────────────────────────────────────────────────

if [ "$MODE" != "status" ] && [ "$(id -u)" -ne 0 ]; then
    err "This script must be run as root (use sudo)."
    err "  e.g.: sudo $0"
    exit 1
fi

# ────────────────────────────────────────────────────────────────
# --clean mode: remove FIFO and exit
# ────────────────────────────────────────────────────────────────

if [ "$MODE" = "clean" ]; then
    info "--clean mode: removing FIFOs"
    for FIFO_PATH in "${FIFO_PATHS[@]}"; do
        if [ -e "$FIFO_PATH" ]; then
            rm -f "$FIFO_PATH"
            ok "  $FIFO_PATH removed."
        else
            skip "  $FIFO_PATH does not exist."
        fi
    done
    exit 0
fi

# ────────────────────────────────────────────────────────────────
# 1. sysctls
# ────────────────────────────────────────────────────────────────

info "1/3 — Kernel network parameters (sysctls)"

for key in "${!SYSCTLS[@]}"; do
    want="${SYSCTLS[$key]}"
    have="$(sysctl -n "$key" 2>/dev/null || echo N/A)"

    if [ "$have" = "$want" ]; then
        skip "  $key = $want"
    else
        if [ "$MODE" = "status" ]; then
            warn "  $key = $have  (desired: $want)"
        else
            sysctl -w "$key=$want" > /dev/null
            ok "  $key: $have → $want"
        fi
    fi
done

# ────────────────────────────────────────────────────────────────
# 2. FIFO
# ────────────────────────────────────────────────────────────────

info "2/3 — Named FIFOs (${#FIFO_PATHS[@]} total)"

for FIFO_PATH in "${FIFO_PATHS[@]}"; do
    fifo_needs_action=0
    fifo_action=""

    if [ -e "$FIFO_PATH" ]; then
        if [ -p "$FIFO_PATH" ]; then
            current_mode="$(stat -c '%a' "$FIFO_PATH")"
            current_owner="$(stat -c '%U' "$FIFO_PATH")"

            if [ "$current_mode" != "$FIFO_MODE" ]; then
                fifo_needs_action=1
                fifo_action="adjust permissions ($current_mode → $FIFO_MODE)"
            elif [ "$current_owner" != "$FIFO_OWNER" ]; then
                fifo_needs_action=1
                fifo_action="adjust owner ($current_owner → $FIFO_OWNER)"
            else
                skip "  $FIFO_PATH healthy (owner=$current_owner, mode=$current_mode)"
            fi
        else
            fifo_needs_action=1
            fifo_action="remove non-FIFO entry and create FIFO"
        fi
    else
        fifo_needs_action=1
        fifo_action="create FIFO"
    fi

    if [ "$fifo_needs_action" = "1" ]; then
        if [ "$MODE" = "status" ]; then
            warn "  $FIFO_PATH: $fifo_action"
        else
            # If path exists but is either not a FIFO or has wrong attrs,
            # decide between adjusting in place or recreating.
            if [ -e "$FIFO_PATH" ] && { [ ! -p "$FIFO_PATH" ] \
                 || [ "$(stat -c '%a' "$FIFO_PATH")" != "$FIFO_MODE" ] \
                 || [ "$(stat -c '%U' "$FIFO_PATH")" != "$FIFO_OWNER" ]; }; then
                if [ -p "$FIFO_PATH" ]; then
                    chmod "$FIFO_MODE" "$FIFO_PATH"
                    chown "$FIFO_OWNER:$FIFO_OWNER" "$FIFO_PATH" 2>/dev/null \
                        || chown "$FIFO_OWNER" "$FIFO_PATH"
                    ok "  $FIFO_PATH adjusted (owner=$FIFO_OWNER, mode=$FIFO_MODE)"
                else
                    rm -f "$FIFO_PATH"
                    mkfifo -m "$FIFO_MODE" "$FIFO_PATH"
                    chown "$FIFO_OWNER:$FIFO_OWNER" "$FIFO_PATH" 2>/dev/null \
                        || chown "$FIFO_OWNER" "$FIFO_PATH"
                    ok "  $FIFO_PATH recreated"
                fi
            else
                mkfifo -m "$FIFO_MODE" "$FIFO_PATH"
                chown "$FIFO_OWNER:$FIFO_OWNER" "$FIFO_PATH" 2>/dev/null \
                    || chown "$FIFO_OWNER" "$FIFO_PATH"
                ok "  $FIFO_PATH created"
            fi
        fi
    fi
done

# ────────────────────────────────────────────────────────────────
# 3. Environment diagnostics
# ────────────────────────────────────────────────────────────────

info "3/3 — Environment diagnostics"

# ffmpeg (essential — pipeline emitter)
if command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg_ver="$(ffmpeg -version 2>&1 | head -n1)"
    ok "  ffmpeg: $ffmpeg_ver"
else
    err "  ffmpeg NOT found in PATH — required by the current pipeline."
    err "    Install with: sudo apt install ffmpeg"
fi

# GNU Radio
if command -v gnuradio-config-info >/dev/null 2>&1; then
    gr_ver="$(gnuradio-config-info --version 2>&1 | head -n1)"
    ok "  GNU Radio: $gr_ver"
else
    warn "  GNU Radio not found in PATH."
fi

# LimeSDR
if command -v LimeUtil >/dev/null 2>&1; then
    if LimeUtil --find 2>/dev/null | grep -q "LimeSDR\|Lime "; then
        lime_info="$(LimeUtil --find 2>/dev/null | head -n1)"
        ok "  LimeSDR detected: $lime_info"
    else
        warn "  LimeUtil installed but no LimeSDR detected (check USB cable)."
    fi
else
    skip "  LimeUtil not found (optional diagnostic)."
fi

# TSDuck (optional — useful for stream debugging with 'tsp -P analyze')
if command -v tsp >/dev/null 2>&1; then
    tsp_ver="$(tsp --version 2>&1 | head -n1)"
    ok "  TSDuck (optional, for stream analysis): $tsp_ver"
else
    skip "  TSDuck not found (optional — install for 'tsp -P analyze' debugging)."
fi

# Network interfaces
info "  IPv4 network interfaces (for localaddr in ffmpeg URL):"
ip -4 -o addr show scope global 2>/dev/null \
    | awk '{printf "    %-12s → %s\n", $2, $4}' \
    || warn "    Could not list network interfaces."

# ────────────────────────────────────────────────────────────────
# Summary
# ────────────────────────────────────────────────────────────────

echo
if [ "$MODE" = "status" ]; then
    info "--status mode: no changes were applied."
else
    ok "Environment ready."
fi

cat <<'EOF'

Next steps (order matters — start ALL three ffmpeg first, then the flowgraph):

  Terminal A  — Layer A emitter (1 seg / QPSK):
    ffmpeg -loglevel info \
           -probesize 5000000 -analyzeduration 5000000 \
           -fflags +genpts \
           -i /tmp/ts_layer_a \
           -c copy \
           -muxdelay 0 -muxpreload 0 \
           -f mpegts \
           "udp://239.1.1.1:1234?pkt_size=1316&ttl=16&localaddr=<YOUR_NIC_IP>"

  Terminal B  — Layer B emitter (12 seg / 64-QAM, main video):
    ffmpeg -loglevel info \
           -probesize 5000000 -analyzeduration 5000000 \
           -fflags +genpts \
           -i /tmp/ts_layer_b \
           -c copy \
           -muxdelay 0 -muxpreload 0 \
           -f mpegts \
           "udp://239.1.1.2:1234?pkt_size=1316&ttl=16&localaddr=<YOUR_NIC_IP>"

  Terminal C  — Layer C emitter (0 seg today, diagnostic slot):
    ffmpeg -loglevel info \
           -probesize 5000000 -analyzeduration 5000000 \
           -fflags +genpts \
           -i /tmp/ts_layer_c \
           -c copy \
           -muxdelay 0 -muxpreload 0 \
           -f mpegts \
           "udp://239.1.1.3:1234?pkt_size=1316&ttl=16&localaddr=<YOUR_NIC_IP>"

    NOTE on Layer C: with segments_C=0 in the flowgraph, this pipe stays
    empty. ffmpeg on Terminal C will block on probesize forever. That's
    expected — the terminal is there for symmetry and future use.

  IMPORTANT: do NOT add '-re' or '-copyts' to any of the above.
  The stream's original PTSs are ~4h into virtual time; '-re' would try
  to wait 4h before emitting, filling buffers and back-pressuring the
  GNU Radio FIFO, which then stalls the whole flowgraph.

  Terminal GR  — Run the GNU Radio flowgraph (this unblocks the ffmpeg
                 processes waiting on Layer A and Layer B FIFOs):
    python3 /path/to/stbcast_analyzer.py

  Terminals VLC  — Play each layer independently:
    vlc --network-caching 300 udp://@239.1.1.1:1234    # Layer A
    vlc --network-caching 300 udp://@239.1.1.2:1234    # Layer B (main video)
    vlc --network-caching 300 udp://@239.1.1.3:1234    # Layer C (empty today)

  Optional  — Validate bitrates on the wire:
    tsp -I ip 239.1.1.2:1234 --local-address <YOUR_NIC_IP> \
        -P analyze --interval 5 -O drop
    (Layer B should stabilize around 13.4 Mbps; A around a few hundred kbps.)

EOF

exit 0
