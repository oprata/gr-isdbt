"""
mer_cal.py - estimated MER of the transmitter, corrected for the receiver's
own MER floor (Rodada 57). Indicative values (reference monitor).

The measured MER combines the transmitter error and the receiver error
(LimeSDR phase noise, I/Q imbalance, ADC, channel estimation):

    10^(-MER_meas/10) = 10^(-MER_tx/10) + 10^(-floor/10)

so, with the receiver floor known,

    MER_tx = -10 log10( 10^(-MER_meas/10) - 10^(-floor/10) )

The floor is calibrated once per layer (and for the whole channel) with the
transmitter in a known state: the user types the transmitter MER (from the
exciter display or a reference analyzer) in the Control tab and the floor is
derived from the MER measured at that moment. Near the floor the correction
diverges, so within MARGIN dB of it the estimate is shown as "> X dB".
Saved in ~/.config/isdbt_analyzer/mer_cal.json. Keep the RX gain fixed after
calibrating (the floor depends a little on gain and level).
"""

import json
import math
import os
import time

CAL_FILE = os.path.expanduser("~/.config/isdbt_analyzer/mer_cal.json")
MARGIN = 0.7          # dB: closer than this to the floor -> "> X dB"
LEVEL_TOL = 4.0       # dB: channel power at the ADC must stay within this of
                      # the calibration level, otherwise the floor no longer
                      # applies (gain/cable/attenuator changed) -> no correction
KEYS = ("A", "B", "C", "total")


def _lin(db):
    return 10.0 ** (-db / 10.0)


class MerCal(object):
    def __init__(self):
        self.cal = None
        self.show = True
        self.level_fn = None          # callable -> channel power at the ADC (dBFS) or None
        try:
            with open(CAL_FILE) as f:
                c = json.load(f)
            if isinstance(c.get("floor_db"), dict):
                self.cal = c
                self.show = bool(c.get("show", True))
        except (OSError, ValueError):
            pass

    def _save(self):
        os.makedirs(os.path.dirname(CAL_FILE), exist_ok=True)
        with open(CAL_FILE, "w") as f:
            json.dump(self.cal, f, indent=2)

    def floor(self, key):
        if self.cal is None:
            return None
        return self.cal["floor_db"].get(key)

    def level_ok(self):
        """False when the channel power at the ADC moved more than LEVEL_TOL
        from the calibration level (the receiver floor changed with it)."""
        if self.cal is None or self.cal.get("chan_dbfs") is None or self.level_fn is None:
            return True
        try:
            lv = self.level_fn()
        except Exception:
            return True
        return lv is None or abs(lv - self.cal["chan_dbfs"]) <= LEVEL_TOL

    def estimate(self, mer_meas, key):
        """(value_db or None, text or None). text is None when there is no
        estimate to show (not calibrated, display off, level changed since the
        calibration, no measurement)."""
        fl = self.floor(key)
        if not self.show or fl is None or mer_meas is None or mer_meas <= 0:
            return None, None
        if not self.level_ok():
            return None, None
        if mer_meas >= fl - MARGIN:
            bound = -10 * math.log10(_lin(fl - MARGIN) - _lin(fl))
            return None, "> %.0f dB" % bound
        v = -10 * math.log10(_lin(mer_meas) - _lin(fl))
        return v, "~%.1f dB" % v

    def calibrate(self, measured, tx_mer, gain=None):
        """measured: {key: MER_meas dB}. Returns the floors; raises ValueError
        if a measured MER is not below the reference (floor undefined)."""
        floors, bad = {}, []
        for k, m in measured.items():
            if m is None or m <= 0:
                continue
            d = _lin(m) - _lin(tx_mer)
            if d <= 0:
                bad.append("%s (%.1f dB)" % (k, m))
                continue
            floors[k] = -10 * math.log10(d)
        if bad:
            raise ValueError("measured MER not below the reference %.1f dB for: %s"
                             % (tx_mer, ", ".join(bad)))
        if not floors:
            raise ValueError("no MER measured yet")
        lv = None
        if self.level_fn is not None:
            try:
                lv = self.level_fn()
            except Exception:
                lv = None
        self.cal = {"floor_db": floors, "reference_tx_mer_db": tx_mer, "chan_dbfs": lv,
                    "measured_db": {k: v for k, v in measured.items() if v},
                    "gain_db": gain, "date": time.strftime("%Y-%m-%d %H:%M"),
                    "show": self.show}
        self._save()
        return floors

    def clear(self):
        self.cal = None
        try:
            os.remove(CAL_FILE)
        except OSError:
            pass

    def set_show(self, flag):
        self.show = bool(flag)
        if self.cal is not None:
            self.cal["show"] = self.show
            self._save()

    def text(self):
        if self.cal is None:
            return "Status: not calibrated - only the measured MER is shown."
        fl = ", ".join("%s %.1f" % (k, self.cal["floor_db"][k]) for k in KEYS
                       if k in self.cal["floor_db"])
        g = self.cal.get("gain_db")
        lv = self.cal.get("chan_dbfs")
        t = ("Status: calibrated %s with TX MER %.1f dB%s%s. Receiver floor (dB): %s"
             % (self.cal.get("date", "?"), self.cal.get("reference_tx_mer_db", float("nan")),
                " at %.0f dB RX Gain" % g if g is not None else "",
                ", channel power %.1f dBFS" % lv if lv is not None else "", fl))
        if not self.level_ok():
            t += (". SUSPENDED: the channel power at the ADC moved more than %.0f dB from the "
                  "calibration level - showing the measured MER" % LEVEL_TOL)
        return t


_INSTANCE = None


def get():
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MerCal()
    return _INSTANCE
