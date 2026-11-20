# -*- coding: utf-8 -*-
"""Feature cache for Align Edge, and the snapshot the topology graph is built from.

v1.3 design - touch the data providers as little as possible:
  * Features are read ONCE into a cache covering an area around the view,
    only while the map canvas is not drawing (the caller enforces that).
  * The cache is then kept current from the layers' own edit signals
    (geometryChanged carries the new geometry, editBuffer() holds added
    features, attributeValueChanged carries the new value). Aligning, undo
    and panning inside the cached area never read the provider again.
  * A re-read is needed only when the view leaves the cached area, a layer
    starts/stops editing, or edits are committed or rolled back.
  * Tooltip labels and the "has it changed?" check before writing use the
    cache too, so hovering never reads data.

The v1.2.0 diagnostic log showed QGIS hanging inside a feature read that
was waiting for a map render which never finished; this design keeps
Align Edge out of that contention.
"""

from qgis.core import (
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)

from . import diag
from .compat import GEOM_POLYGON, geometry_type, is_line_or_polygon, reverse_transform
from .topology_core import TopologyGraph

import math

CACHE_SCALE = 2.0      # cached area = view scaled by this factor


class BuildError(Exception):
    pass


class FeatureStruct(object):
    """Enough of a feature's geometry to rebuild it after an edit."""
    __slots__ = ("layer_id", "fid", "polygon", "multi", "has_z", "has_m",
                 "parts", "curved", "version")

    def __init__(self, layer_id, fid, polygon, multi, has_z, has_m, parts, curved, version):
        self.layer_id = layer_id
        self.fid = fid
        self.polygon = polygon
        self.multi = multi
        self.has_z = has_z
        self.has_m = has_m
        self.parts = parts          # [part][ring] -> [(x, y, z, m)] layer CRS, ring not closed
        self.curved = curved
        self.version = version      # cache version when this struct was made


class _Entry(object):
    __slots__ = ("feature", "geometry", "struct", "map_parts", "bbox", "nverts", "version")


def finite(v):
    return v is not None and math.isfinite(v)


def participating_layers(canvas, settings):
    excluded = settings.excluded_layer_ids()
    references = settings.reference_layer_ids()
    out = []
    for lyr in canvas.layers():
        if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
            continue
        if not is_line_or_polygon(lyr) or (not lyr.isEditable() and lyr.id() not in references):
            continue
        if lyr.id() in excluded:
            continue
        if lyr.hasScaleBasedVisibility() and not lyr.isInScaleRange(canvas.scale()):
            continue
        out.append(lyr)
    return out


def _rings(geom, polygon):
    """[part][ring] -> list of QgsPoint for a *linear* geometry."""
    abstract = geom.constGet()
    if QgsWkbTypes.isMultiType(abstract.wkbType()):
        parts = [abstract.geometryN(i) for i in range(abstract.numGeometries())]
    else:
        parts = [abstract]
    out = []
    for part in parts:
        if polygon:
            rings = [part.exteriorRing()] + [part.interiorRing(i)
                                             for i in range(part.numInteriorRings())]
            out.append([list(r.vertices()) for r in rings if r is not None])
        else:
            out.append([list(part.vertices())])
    return out


# ------------------------------------------------------------------ cache

class _LayerCache(object):

    def __init__(self, layer, dest_crs):
        self.layer = layer
        self.lid = layer.id()
        self.polygon = geometry_type(layer) == GEOM_POLYGON
        lw = layer.wkbType()
        self.has_z = QgsWkbTypes.hasZ(lw)
        self.has_m = QgsWkbTypes.hasM(lw)
        self.multi = QgsWkbTypes.isMultiType(lw)
        self.xf = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
        self.identity = self.xf.isShortCircuited()
        self.region = None          # QgsRectangle (map CRS) that is cached
        self.entries = {}           # fid -> _Entry
        self.dirty = True           # must (re)read before use

    def make_entry(self, fid, geometry, feature, version):
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            return None
        e = _Entry()
        e.feature = feature
        e.geometry = QgsGeometry(geometry)
        e.version = version
        g = geometry
        curved = QgsWkbTypes.isCurvedType(g.wkbType())
        if curved:
            g = QgsGeometry(g.constGet().segmentize())
        parts = []
        map_parts = []
        n = 0
        minx = miny = float("inf")
        maxx = maxy = float("-inf")
        for rings in _rings(g, self.polygon):
            prings, mrings = [], []
            for pts in rings:
                payload = [(p.x(), p.y(), p.z(), p.m()) for p in pts]
                if self.polygon and len(payload) > 1 and payload[0][:2] == payload[-1][:2]:
                    payload = payload[:-1]
                if self.identity:
                    coords = [(x, y) for x, y, _, _ in payload]
                else:
                    coords = []
                    for x, y, _, _ in payload:
                        q = self.xf.transform(QgsPointXY(x, y))
                        coords.append((q.x(), q.y()))
                for x, y in coords:
                    if x < minx:
                        minx = x
                    if x > maxx:
                        maxx = x
                    if y < miny:
                        miny = y
                    if y > maxy:
                        maxy = y
                n += len(payload)
                prings.append(payload)
                mrings.append(coords)
            parts.append(prings)
            map_parts.append(mrings)
        e.struct = FeatureStruct(self.lid, fid, self.polygon, self.multi, self.has_z,
                                 self.has_m, parts, curved, version)
        e.map_parts = map_parts
        e.bbox = (minx, miny, maxx, maxy)
        e.nverts = n
        return e


class FeatureCache(object):
    """Cached geometries of the participating layers, kept current by signals.

    All methods run on the main thread. Only read() touches data providers.
    """

    def __init__(self, canvas, settings, on_change):
        self.canvas = canvas
        self.settings = settings
        self.on_change = on_change      # callable(reason) -> tool marks topology stale
        self.layers = {}                # lid -> _LayerCache
        self._version = 0
        self._dest = None
        self.log = lambda msg: None

    # ---------------------------------------------------------- layers
    def sync_layers(self):
        """Match the cache to the currently participating layers."""
        dest = self.canvas.mapSettings().destinationCrs()
        if self._dest is None or dest != self._dest:
            self.clear()
            self._dest = dest
        want = {layer.id(): layer for layer in participating_layers(self.canvas, self.settings)}
        for lid in list(self.layers):
            if lid not in want:
                self._disconnect(self.layers.pop(lid))
        for lid, lyr in want.items():
            if lid not in self.layers:
                lc = _LayerCache(lyr, dest)
                self.layers[lid] = lc
                self._connect(lc)
        return bool(self.layers)

    def clear(self):
        for lc in self.layers.values():
            self._disconnect(lc)
        self.layers = {}

    def _connect(self, lc):
        lyr = lc.layer
        lc.slots = [
            (lyr.geometryChanged, lambda fid, geom, lc=lc: self._geom_changed(lc, fid, geom)),
            (lyr.featureAdded, lambda fid, lc=lc: self._added(lc, fid)),
            (lyr.featureDeleted, lambda fid, lc=lc: self._deleted(lc, fid)),
            (lyr.attributeValueChanged, lambda fid, idx, val, lc=lc: self._attr(lc, fid, idx, val)),
            (lyr.afterCommitChanges, lambda lc=lc: self._invalidate(lc, "commit")),
            (lyr.afterRollBack, lambda lc=lc: self._invalidate(lc, "rollback")),
            (lyr.editingStopped, lambda lc=lc: self._invalidate(lc, "editing stopped")),
        ]
        for sig, slot in lc.slots:
            sig.connect(slot)

    def _disconnect(self, lc):
        for sig, slot in getattr(lc, "slots", []):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        lc.slots = []

    # ---------------------------------------------------------- signal handlers
    def _bump(self):
        self._version += 1
        return self._version

    def _geom_changed(self, lc, fid, geom):
        if lc.dirty:
            return
        old = lc.entries.get(fid)
        feat = old.feature if old is not None else None
        if feat is None:
            feat = self._edit_buffer_feature(lc, fid)
        if feat is not None:
            feat = QgsFeature(feat)
            feat.setGeometry(geom)
        e = lc.make_entry(fid, geom, feat, self._bump())
        if e is None:
            lc.entries.pop(fid, None)
        else:
            lc.entries[fid] = e
        self.on_change("geometry changed")

    def _added(self, lc, fid):
        if lc.dirty:
            return
        f = self._edit_buffer_feature(lc, fid)
        if f is None:
            # e.g. undo of a delete restores a provider feature: re-read later
            lc.dirty = True
            self.log("cache: feature {} re-added from provider -> reread {}".format(
                fid, lc.layer.name()))
        else:
            e = lc.make_entry(fid, f.geometry(), QgsFeature(f), self._bump())
            if e is not None:
                lc.entries[fid] = e
        self.on_change("feature added")

    def _deleted(self, lc, fid):
        if lc.dirty:
            return
        lc.entries.pop(fid, None)
        self._bump()
        self.on_change("feature deleted")

    def _attr(self, lc, fid, idx, val):
        e = lc.entries.get(fid)
        if e is not None and e.feature is not None:
            try:
                e.feature.setAttribute(idx, val)
            except Exception as err:
                diag.log_once(("attr", lc.lid), "cache: attribute update failed on layer {}: {!r}".format(lc.lid, err))

    def _invalidate(self, lc, why):
        if not lc.dirty:
            self.log("cache: {} on {} -> reread".format(why, lc.layer.name()))
        lc.dirty = True
        self.on_change(why)

    @staticmethod
    def _edit_buffer_feature(lc, fid):
        try:
            buf = lc.layer.editBuffer()
            if buf is not None:
                added = buf.addedFeatures()
                if fid in added:
                    return added[fid]
        except Exception as err:
            diag.log_once(("edit buffer", lc.lid), "cache: edit buffer unavailable on layer {}: {!r}".format(lc.lid, err))
        return None

    # ---------------------------------------------------------- reading
    def view_rect(self):
        r = QgsRectangle(self.canvas.extent())
        r.scale(1.15)
        return r

    def needs_read(self):
        view = self.view_rect()
        for lc in self.layers.values():
            if lc.dirty or lc.region is None or not lc.region.contains(view):
                return True
        return False

    def read(self):
        """MAIN THREAD, map idle. Read layers whose cache misses the view."""
        view = self.view_rect()
        region = QgsRectangle(self.canvas.extent())
        region.scale(CACHE_SCALE)
        max_v = self.settings.get("max_vertices") * 4
        for lc in self.layers.values():
            if not (lc.dirty or lc.region is None or not lc.region.contains(view)):
                continue
            try:
                rect = region if lc.identity else lc.xf.transformBoundingBox(
                    region, reverse_transform())
            except Exception:
                rect = None
            req = QgsFeatureRequest()
            if rect is not None:
                req.setFilterRect(rect)
            entries = {}
            count = 0
            it = lc.layer.getFeatures(req)
            try:
                f = QgsFeature()
                while it.nextFeature(f):
                    fid = f.id()
                    e = lc.make_entry(fid, f.geometry(), QgsFeature(f), self._bump())
                    if e is None:
                        continue
                    entries[fid] = e
                    count += e.nverts
                    if count > max_v:
                        raise BuildError(
                            "Too many vertices around the view - zoom in "
                            "(limit is in Align Edge settings).")
            finally:
                # never leave an iterator open: it can hold the data source
                # that the map renderer is waiting for
                it.close()
            lc.entries = entries
            lc.region = region
            lc.dirty = False
            self.log("cache: read {} features ({} vertices) from {}".format(
                len(entries), count, lc.layer.name()))

    # ---------------------------------------------------------- snapshot
    def snapshot(self, cluster_tol):
        """Plain-Python input for the graph: features touching the view."""
        view = self.view_rect()
        vx0, vy0, vx1, vy1 = view.xMinimum(), view.yMinimum(), view.xMaximum(), view.yMaximum()
        ctx = GraphContext(self._dest, self)
        snap = Snapshot(ctx, cluster_tol)
        max_v = self.settings.get("max_vertices")
        references = self.settings.reference_layer_ids()
        count = 0
        for lc in self.layers.values():
            ctx.layers[lc.lid] = lc.layer
            for fid, e in lc.entries.items():
                x0, y0, x1, y1 = e.bbox
                if x1 < vx0 or x0 > vx1 or y1 < vy0 or y0 > vy1:
                    continue
                count += e.nverts
                if count > max_v:
                    raise BuildError("More than {:,} vertices in view - zoom in "
                                     "(limit is in Align Edge settings).".format(max_v))
                ctx.features[(lc.lid, fid)] = e.struct
                for pi, rings in enumerate(e.map_parts):
                    for ri, coords in enumerate(rings):
                        snap.parts.append(((lc.lid, fid, pi, ri), coords, lc.polygon,
                                           e.struct.curved or lc.lid in references or not lc.layer.isEditable()))
        return snap

    def entry(self, lid, fid):
        lc = self.layers.get(lid)
        return None if lc is None else lc.entries.get(fid)


# ------------------------------------------------------------------ graph input

class GraphContext(object):
    """Everything the tool and writer need besides the pure graph."""

    def __init__(self, canvas_crs, cache=None):
        self.canvas_crs = canvas_crs
        self.cache = cache
        self.layers = {}                      # id -> QgsVectorLayer
        self.features = {}                    # (lid, fid) -> FeatureStruct
        self._labels = {}

    def payload(self, part_key, idx):
        lid, fid, pi, ri = part_key
        return self.features[(lid, fid)].parts[pi][ri][idx]

    def label(self, feat_key, allow_fetch=True):
        """Display-expression label from the cached feature (no data read)."""
        if feat_key in self._labels:
            return self._labels[feat_key]
        lid, fid = feat_key
        text = "#{}".format(fid)
        if not allow_fetch:
            return text
        lyr = self.layers.get(lid)
        e = self.cache.entry(lid, fid) if self.cache is not None else None
        if lyr is not None and e is not None and e.feature is not None:
            try:
                exp = QgsExpression(lyr.displayExpression())
                ctx = QgsExpressionContext(
                    QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
                ctx.setFeature(e.feature)
                v = exp.evaluate(ctx)
                if v is not None and str(v) not in ("", "NULL"):
                    text = str(v)
            except Exception as err:
                diag.log_once(("label", lid), "label expression failed on layer {}: {!r}".format(lid, err))
        self._labels[feat_key] = text
        return text


class Snapshot(object):
    """Plain-Python input for building the topology graph."""

    def __init__(self, ctx, cluster_tol):
        self.ctx = ctx
        self.cluster_tol = cluster_tol
        self.parts = []          # (key, coords, closed, locked)


def graph_for_snapshot(snap):
    """Unbuilt TopologyGraph loaded with the snapshot's parts."""
    graph = TopologyGraph(snap.cluster_tol)
    for key, coords, closed, locked in snap.parts:
        graph.add_part(key, coords, closed, locked)
    return graph


def build_from_snapshot(snap, cancel=None):
    """Pure Python. Returns (graph, ctx)."""
    graph = graph_for_snapshot(snap)
    graph.build(cancel)
    return graph, snap.ctx
