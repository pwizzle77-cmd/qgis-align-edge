# -*- coding: utf-8 -*-
"""Persistent options for Align Edge (QgsSettings + per-project layer filter)."""

from qgis.core import Qgis, QgsProject, QgsSettings, QgsTolerance, QgsUnitTypes

from . import diag
from .compat import UNIT_DEGREES, UNIT_METERS

DISPLAY_ALL = "all"            # Pro "All Elements"
DISPLAY_SELECTED = "selected"  # Pro "Selected Elements"

_DEFAULTS = {
    "display_style": DISPLAY_ALL,
    "graph_color": "#ff00ff",       # Pro's classic magenta topology graph
    "highlight_color": "#00b4ff",
    "fail_color": "#e0002a",
    "line_width": 3,
    "node_size": 7,
    "tolerance_px": 0,              # 0 = follow the project snapping tolerance
    "cluster_tolerance": 0.0,       # 0 = auto (1 mm / 1e-8 deg)
    "show_tooltip": True,
    "show_gap_fill": True,
    "max_vertices": 60000,
    "max_shift_ratio": 2.0,         # 0 = no limit
    "diagnostic_log": True,
}

_PREFIX = "AlignEdge/"
_PROJECT_SCOPE = "AlignEdge"


class Settings(object):

    def get(self, key):
        default = _DEFAULTS[key]
        val = QgsSettings().value(_PREFIX + key, default)
        if isinstance(default, bool):
            if isinstance(val, str):
                return val.lower() in ("1", "true", "yes")
            return bool(val)
        if isinstance(default, int):
            return int(val)
        if isinstance(default, float):
            return float(val)
        return str(val)

    def set(self, key, value):
        QgsSettings().setValue(_PREFIX + key, value)

    def reset(self):
        s = QgsSettings()
        for k in _DEFAULTS:
            s.remove(_PREFIX + k)

    # --------------------------------------------------- per-project layers
    def excluded_layer_ids(self):
        val, ok = QgsProject.instance().readListEntry(_PROJECT_SCOPE, "excluded_layers")
        return set(val) if ok else set()

    def set_excluded_layer_ids(self, ids):
        QgsProject.instance().writeEntry(_PROJECT_SCOPE, "excluded_layers", sorted(ids))

    def reference_layer_ids(self):
        val, ok = QgsProject.instance().readListEntry(_PROJECT_SCOPE, "reference_layers")
        return set(val) if ok else set()

    def set_reference_layer_ids(self, ids):
        QgsProject.instance().writeEntry(_PROJECT_SCOPE, "reference_layers", sorted(ids))

    # --------------------------------------------------- derived values
    def search_radius(self, canvas):
        """Hover / click tolerance in map units.

        Highlight and click use the same radius, so an edge is never
        highlighted without being clickable (ESRI BUG-000167028).
        """
        mupp = canvas.mapUnitsPerPixel()
        px = self.get("tolerance_px")
        if px > 0:
            return px * mupp
        try:
            cfg = QgsProject.instance().snappingConfig()
            units = cfg.units()
            layer_units = [u for u in (
                getattr(getattr(Qgis, "MapToolUnit", None), "Layer", None),
                getattr(QgsTolerance, "LayerUnits", None)) if u is not None]
            if units in layer_units:
                # needs a layer to convert; treat as map units
                r = cfg.tolerance()
            else:
                r = QgsTolerance.toleranceInProjectUnits(
                    cfg.tolerance(), None, canvas.mapSettings(), units)
            if r and r > 0:
                return max(r, 3 * mupp)
        except Exception as err:
            diag.log_once("search radius", "snapping tolerance unavailable, using 10 px: {!r}".format(err))
        return 10 * mupp

    def cluster_tolerance(self, canvas):
        tol = self.get("cluster_tolerance")
        if tol > 0:
            return tol
        units = canvas.mapSettings().destinationCrs().mapUnits()
        if units == UNIT_DEGREES:
            return 1e-8
        try:
            f = QgsUnitTypes.fromUnitToUnitFactor(UNIT_METERS, units)
            if f and f > 0:
                return 0.001 * f
        except Exception as err:
            diag.log_once("cluster tolerance", "unit conversion failed, using 0.001: {!r}".format(err))
        return 0.001
