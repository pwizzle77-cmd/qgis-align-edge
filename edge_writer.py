# -*- coding: utf-8 -*-
"""Turns an AlignPlan into geometry edits on the owning layers."""

from collections import defaultdict
from bisect import bisect_right
import math

from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsLineString,
    QgsMultiLineString,
    QgsMultiPolygon,
    QgsPoint,
    QgsPointXY,
    QgsPolygon,
    QgsProject,
    QgsWkbTypes,
)

from .graph_builder import finite
from .topology_core import _proj


class AlignFailed(Exception):
    pass


def _resolve(item, part_key, plan, ctx, layer, dimensions):
    """Return (x, y, z, m) in the owner layer's CRS for one emit item."""
    kind, v = item
    if kind == "orig":
        return ctx.payload(part_key, v)

    src_key, index, xy = plan.target_vertices[v]
    if index is not None and ctx.layers[src_key[0]].crs() == layer.crs():
        x, y, _, _ = ctx.payload(src_key, index)
    else:
        xf = QgsCoordinateTransform(ctx.canvas_crs, layer.crs(), QgsProject.instance())
        q = xf.transform(QgsPointXY(*xy))
        x, y = q.x(), q.y()
    z, m = dimensions.get(v, (float("nan"), float("nan")))
    return x, y, z, m


def _mix(a, b, t):
    if t <= 0:
        return a
    if t >= 1:
        return b
    return a + (b - a) * t if finite(a) and finite(b) else float("nan")


def _dimensions(plan, key, graph, ctx):
    """Transfer the source's Z/M profile by relative distance along the aligned edge."""
    fs = ctx.features[key[:2]]
    if not fs.has_z and not fs.has_m:
        return {}
    part = graph.parts[key]
    originals = {pid: index for pid, index in part.entries if index is not None}
    profile = []
    points = []
    for pid in plan.src.pids:
        index = originals.get(pid)
        if index is not None:
            points.append(part.coords[index])
            profile.append(ctx.payload(key, index)[2:])
            continue
        # An endpoint inserted by noding lies between original source vertices.
        best = None
        count = len(part.coords) if part.closed else len(part.coords) - 1
        for i in range(count):
            j = (i + 1) % len(part.coords)
            a, b = part.coords[i], part.coords[j]
            t, distance = _proj(graph.point(pid), a, b)
            if best is None or distance < best[0]:
                t = max(0.0, min(1.0, t))
                pa, pb = ctx.payload(key, i), ctx.payload(key, j)
                best = (distance, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])),
                        tuple(_mix(pa[k], pb[k], t) for k in (2, 3)))
        points.append(best[1])
        profile.append(best[2])
    source_dist = [0.0]
    for a, b in zip(points, points[1:]):
        source_dist.append(source_dist[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    pids = list(plan.tgt.pids)
    if plan.tgt.start != plan.src.start:
        pids.reverse()
    target_dist = [0.0]
    for a, b in zip(pids, pids[1:]):
        x, y = plan.target_vertices[a][2], plan.target_vertices[b][2]
        target_dist.append(target_dist[-1] + math.hypot(y[0] - x[0], y[1] - x[1]))
    dimensions = {}
    for pid, distance in zip(pids, target_dist):
        position = source_dist[-1] * distance / target_dist[-1] if target_dist[-1] else 0.0
        i = min(max(bisect_right(source_dist, position) - 1, 0), len(profile) - 2)
        length = source_dist[i + 1] - source_dist[i]
        t = (position - source_dist[i]) / length if length else 0.0
        dimensions[pid] = tuple(_mix(profile[i][k], profile[i + 1][k], t) for k in (0, 1))
    return dimensions


def _point(xyzm, has_z, has_m):
    x, y, z, m = xyzm
    kind = (QgsWkbTypes.PointZM if has_m else QgsWkbTypes.PointZ) if has_z else (
        QgsWkbTypes.PointM if has_m else QgsWkbTypes.Point)
    return QgsPoint(x, y, z, m, wkbType=kind)


def _line(coords, fs, close):
    pts = []
    for c in coords:
        p = _point(c, fs.has_z, fs.has_m)
        if pts and pts[-1].x() == p.x() and pts[-1].y() == p.y():
            continue
        pts.append(p)
    if close:
        while len(pts) > 1 and pts[0].x() == pts[-1].x() and pts[0].y() == pts[-1].y():
            pts.pop()
        pts.append(pts[0].clone())
    return QgsLineString(pts)


def _rebuild(fs, rings):
    """rings: [part][ring] -> list of (x, y, z, m)."""
    if fs.polygon:
        polys = []
        for part in rings:
            poly = QgsPolygon()
            poly.setExteriorRing(_line(part[0], fs, True))
            for hole in part[1:]:
                poly.addInteriorRing(_line(hole, fs, True))
            polys.append(poly)
        if fs.multi or len(polys) > 1:
            mp = QgsMultiPolygon()
            for p in polys:
                mp.addGeometry(p)
            return QgsGeometry(mp)
        return QgsGeometry(polys[0])
    lines = [_line(part[0], fs, False) for part in rings]
    if fs.multi or len(lines) > 1:
        ml = QgsMultiLineString()
        for ln in lines:
            ml.addGeometry(ln)
        return QgsGeometry(ml)
    return QgsGeometry(lines[0])


def build_changes(plan, graph, ctx):
    """Compute new geometries. Returns {layer_id: [(fid, QgsGeometry)]}."""
    if not plan.ok:
        raise AlignFailed(plan.error)
    for lid, fid in set(plan.features) | {key[:2] for key in plan.tgt.owners}:
        fs = ctx.features[(lid, fid)]
        entry = ctx.cache.entry(lid, fid) if ctx.cache is not None else None
        if entry is None or entry.version != fs.version:
            raise AlignFailed("source or target changed since the topology was built - try again")
    for key in plan.tgt.owners:
        fs = ctx.features[key[:2]]
        if fs.curved:
            raise AlignFailed("curved targets cannot be copied exactly; use a linear target")
    by_feature = defaultdict(dict)
    for key, items in plan.parts.items():
        by_feature[key[:2]][(key[2], key[3])] = items

    changes = defaultdict(list)
    for (lid, fid), ring_items in by_feature.items():
        fs = ctx.features[(lid, fid)]
        layer = ctx.layers[lid]
        if fs.curved:
            raise AlignFailed("{} has curved geometry".format(ctx.label((lid, fid))))
        # compare with the cache (kept current by the layer's edit signals)
        # instead of reading the feature from the data provider
        entry = ctx.cache.entry(lid, fid) if ctx.cache is not None else None
        old = entry.geometry
        rings = [[list(r) for r in part] for part in fs.parts]
        for (pi, ri), items in ring_items.items():
            key = (lid, fid, pi, ri)
            dimensions = _dimensions(plan, key, graph, ctx)
            rings[pi][ri] = [_resolve(it, key, plan, ctx, layer, dimensions)
                             for it in items]
        new = _rebuild(fs, rings)
        if new.isNull() or new.isEmpty():
            raise AlignFailed("result for {} is empty".format(ctx.label((lid, fid))))
        if fs.polygon and old.isGeosValid() and not new.isGeosValid():
            raise AlignFailed("result for {} {} would be invalid".format(
                layer.name(), ctx.label((lid, fid))))
        changes[lid].append((fid, new))
    return changes


def apply_changes(changes, ctx):
    """Write all changes, one undo command per layer; all-or-nothing."""
    layers = [(ctx.layers[lid], items) for lid, items in changes.items()]
    for layer, _items in layers:
        if not layer.isEditable() or layer.isEditCommandActive():
            raise AlignFailed("{} is not editable or already has an active edit command".format(layer.name()))
    started = []
    try:
        for layer, items in layers:
            started.append((layer, layer.undoStack().index()))
            layer.beginEditCommand("Align Edge")
            for fid, geom in items:
                if not layer.changeGeometry(fid, geom):
                    raise AlignFailed("could not write geometry to {}".format(layer.name()))
            layer.endEditCommand()
        for layer, _items in layers:
            layer.triggerRepaint()
    except Exception as error:
        failures = []
        for layer, index in reversed(started):
            try:
                if layer.isEditCommandActive():
                    layer.destroyEditCommand()
                if layer.undoStack().index() > index:
                    layer.undoStack().setIndex(index)
                layer.triggerRepaint()
            except Exception as rollback_error:
                failures.append("{}: {}".format(layer.name(), rollback_error))
        if failures:
            raise AlignFailed("{}; rollback incomplete: {}".format(error, "; ".join(failures))) from error
        raise AlignFailed(str(error)) from error
    done = [layer for layer, _items in layers]
    return done
