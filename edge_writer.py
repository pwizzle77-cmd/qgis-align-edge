# -*- coding: utf-8 -*-
"""Turns an AlignPlan into geometry edits on the owning layers."""

from collections import defaultdict

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
)

from .graph_builder import finite


class AlignFailed(Exception):
    pass


def _resolve(item, part_key, graph, ctx, layer):
    """Return (x, y, z, m) in the owner layer's CRS for one emit item."""
    kind, v = item
    if kind == "orig":
        return ctx.payload(part_key, v)

    lid, fid = part_key[0], part_key[1]
    sources = ctx.pid_sources.get(v, [])
    # Prefer the owner feature itself, then the same layer, then any layer in
    # the same CRS: those coordinates are copied verbatim (exact coincidence).
    ranked = sorted(sources, key=lambda s: (s[0][:2] != (lid, fid), s[0][0] != lid))
    for src_key, idx in ranked:
        if src_key[0] == lid or ctx.layers[src_key[0]].crs() == layer.crs():
            return ctx.payload(src_key, idx)
    # Otherwise transform the canonical node/vertex from map CRS.
    x, y = graph.point(v)
    xf = QgsCoordinateTransform(ctx.canvas_crs, layer.crs(), QgsProject.instance())
    q = xf.transform(QgsPointXY(x, y))
    z = m = float("nan")
    if ranked:
        _, _, z, m = ctx.payload(*ranked[0])
    return (q.x(), q.y(), z, m)


def _point(xyzm, has_z, has_m):
    x, y, z, m = xyzm
    if has_z and not finite(z):
        z = 0.0
    if has_m and not finite(m):
        m = 0.0
    if has_z and has_m:
        return QgsPoint(x, y, z, m)
    if has_z:
        return QgsPoint(x, y, z)
    if has_m:
        return QgsPoint(x, y, float("nan"), m)
    return QgsPoint(x, y)


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
        if entry is None or entry.version != fs.version:
            raise AlignFailed("data changed since the topology was built - try again")
        old = entry.geometry
        rings = [[list(r) for r in part] for part in fs.parts]
        for (pi, ri), items in ring_items.items():
            rings[pi][ri] = [_resolve(it, (lid, fid, pi, ri), graph, ctx, layer)
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
    done = []
    for lid, items in changes.items():
        layer = ctx.layers[lid]
        layer.beginEditCommand("Align Edge")
        ok = True
        for fid, geom in items:
            if not layer.changeGeometry(fid, geom):
                ok = False
                break
        if not ok:
            layer.destroyEditCommand()
            for lyr in reversed(done):
                lyr.undoStack().undo()
            raise AlignFailed("could not write geometry to {}".format(layer.name()))
        layer.endEditCommand()
        done.append(layer)
    for lyr in done:
        lyr.triggerRepaint()
    return done
