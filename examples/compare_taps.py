#!/usr/bin/env python3
"""
compare_taps.py - compare two run_offline.py --taps files made from the SAME IQ
file (e.g. fast vs --throttle) and show, block by block, where the outputs start
to differ. The first block whose output differs is where the pace-dependent
corruption is introduced (its inputs were still identical).

Usage:
    python3 compare_taps.py taps_fast.json taps_thr.json
"""
import json
import sys


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    a = json.load(open(sys.argv[1]))
    b = json.load(open(sys.argv[2]))
    seg = a["seg_bytes"]
    print(f"A: {sys.argv[1]}  {a['info']}")
    print(f"B: {sys.argv[2]}  {b['info']}")
    print()
    print(f"{'stage':<36s} {'itemsize':>8s} {'items A':>10s} {'items B':>10s} "
          f"{'segments':>8s} {'differ':>7s} {'%':>6s}  first diff (item)")
    sb = {s["name"]: s for s in b["stages"]}
    first_bad = None
    for s in a["stages"]:
        t = sb.get(s["name"])
        if t is None:
            continue
        ha, hb = s["hashes"], t["hashes"]
        n = min(len(ha), len(hb))
        diff = [i for i in range(n) if ha[i] != hb[i]]
        first = diff[0] if diff else None
        first_item = "-" if first is None else f"{first * seg // s['itemsize']}"
        pct = 100.0 * len(diff) / n if n else 0.0
        print(f"{s['name']:<36s} {s['itemsize']:8d} {s['items']:10d} {t['items']:10d} "
              f"{n:8d} {len(diff):7d} {pct:6.1f}  {first_item}")
        if diff and first_bad is None:
            first_bad = s["name"]
    print()
    if first_bad:
        print(f"First stage whose output differs: {first_bad}")
        print("-> the pace-dependent behaviour is inside this block (its input was identical).")
    else:
        print("All compared stages are identical in both runs.")


if __name__ == "__main__":
    main()
