# -*- coding: utf-8 -*-
"""Small diagnostic log for Align Edge.

Writes timestamped lines to align_edge_debug.log in the plugin folder
(capped at ~1 MB). Around risky steps a faulthandler watchdog is armed:
if the step does not return within the timeout, the Python stack of
every thread is appended to the log, so a hang can be located exactly.
"""

import faulthandler
import os
import time

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "align_edge_debug.log")
_MAX_BYTES = 1024 * 1024
_enabled = True
_fh = None


def set_enabled(on):
    global _enabled
    _enabled = bool(on)


def _handle():
    global _fh
    if _fh is None or _fh.closed:
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > _MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".old")
        except OSError:
            pass
        _fh = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    return _fh


def log(msg):
    if not _enabled:
        return
    try:
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + ".%03d" % (int(now * 1000) % 1000)
        _handle().write("{} {}\n".format(stamp, msg))
    except Exception:
        pass


class watchdog(object):
    """with watchdog("apply"): ... -> dumps all thread stacks if it hangs."""

    def __init__(self, what, timeout=15):
        self.what = what
        self.timeout = timeout
        self.armed = False
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        if _enabled:
            try:
                fh = _handle()
                fh.write("--- watchdog armed: {} (dumps stacks if > {} s)\n".format(
                    self.what, self.timeout))
                faulthandler.dump_traceback_later(self.timeout, repeat=False, file=fh, exit=False)
                self.armed = True
            except Exception:
                self.armed = False
        return self

    def __exit__(self, *exc):
        if self.armed:
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                pass
        dt = time.perf_counter() - self.t0
        log("{} took {:.3f} s{}".format(self.what, dt, "" if exc[0] is None
                                          else " (error: {})".format(exc[1])))
        return False


def close():
    global _fh
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    if _fh is not None:
        try:
            _fh.close()
        except Exception:
            pass
        _fh = None
