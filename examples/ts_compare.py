#!/usr/bin/env python3
"""
ts_compare.py - checks that a TS recorded by one receiver is contained, packet
for packet, in a TS recorded by another receiver fed with the same signal.

Used in V1: the static chain of the .grc and the dynamic chain run in the same
flowgraph on the same IQ file. The dynamic chain starts later (it waits for a
stable TMCC), so its TS must be an exact contiguous piece of the static TS.

    python3 ts_compare.py /tmp/ts_layer_b /tmp/dyn/ts_layer_b
"""
import sys


def packets(path):
    data = open(path, "rb").read()
    n = len(data) // 188
    return [data[i * 188:(i + 1) * 188] for i in range(n)]


def compare(ref_path, test_path, skip=0):
    ref, test = packets(ref_path), packets(test_path)
    print("reference: %s  %d packets" % (ref_path, len(ref)))
    print("test     : %s  %d packets" % (test_path, len(test)))
    if not test:
        print("RESULT: FAIL (test TS is empty)")
        return False
    index, count = {}, {}
    for i, p in enumerate(ref):
        index.setdefault(p, i)
        count[p] = count.get(p, 0) + 1
    # first test packet (after `skip`) that appears exactly once in the
    # reference: null packets and other repeated packets cannot be aligned
    start_t = None
    for t in range(skip, len(test)):
        if count.get(test[t]) == 1:
            start_t = t
            break
    if start_t is None:
        print("RESULT: FAIL (no unique packet of the test TS found in the reference)")
        return False
    start_r = index[test[start_t]]
    n = min(len(test) - start_t, len(ref) - start_r)
    mism = [k for k in range(n) if test[start_t + k] != ref[start_r + k]]
    print("aligned  : test packet %d = reference packet %d; %d packets overlap"
          % (start_t, start_r, n))
    print("before   : %d test packets before the aligned point (warm-up / not unique)"
          % start_t)
    print("after    : %d test packets after the end of the reference" % (len(test) - start_t - n))
    if mism:
        print("mismatch : %d packets differ, first at overlap index %d" % (len(mism), mism[0]))
        print("RESULT: FAIL")
        return False
    print("RESULT: IDENTICAL over the %d overlapping packets" % n)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    ok = compare(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0)
    sys.exit(0 if ok else 1)
