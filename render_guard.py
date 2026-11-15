# -*- coding: utf-8 -*-
"""Decide when Align Edge must wait for the map.

These checks are pure so they can be tested without QGIS. They never mean
"go ahead and read the layer". A stuck redraw is cancelled and painted
again; feature reads stay blocked until that redraw has finished.
"""

STUCK_RENDER_SECONDS = 2.0
MAX_RENDER_RECOVERIES = 2


def should_defer_layer_work(drawing, indexing, holding_refresh):
    """True while a redraw, a snap index, or a cancelled redraw still owns the data."""
    return bool(drawing or indexing or holding_refresh)


def should_cancel_stuck_draw(wait_seconds, drawing, recoveries, holding_for_refresh):
    """True when one busy redraw should be cancelled.

    ``holding_for_refresh`` is the beat after a cancel, before the replacement
    redraw is requested. Cancelling again in that beat stacks workers on the
    same dataset. Two cancels is the limit for one busy period.
    """
    if holding_for_refresh or not drawing or recoveries >= MAX_RENDER_RECOVERIES:
        return False
    return wait_seconds >= STUCK_RENDER_SECONDS
