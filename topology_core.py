# -*- coding: utf-8 -*-
"""
Pure-Python topology graph used by the Align Edge tool.

No QGIS imports live here so the geometry logic can be unit tested
outside QGIS. All coordinates are plain (x, y) tuples in one planar
coordinate system (the map canvas CRS).

Terminology follows ArcGIS Pro map topology:
  * node  - a point where edges meet (degree != 2), a line end point,
            or a point where the set of owning features changes.
  * edge  - a maximal run of coincident segments between two nodes.
  * owner - a feature "part key" whose boundary/line runs along an edge.

A part key is any hashable the caller chooses; the QGIS adapter uses
(layer_id, fid, part_index, ring_index).
"""

import math
from collections import defaultdict


class GraphError(Exception):
    pass


class GraphCancelled(Exception):
    pass


# A target enclosing more than this multiple of the best target's area is
# treated as the far side of a neighbouring polygon and not offered.
AREA_RATIO = 10.0


class _Part(object):
    __slots__ = ("key", "coords", "closed", "locked", "vpids", "entries")

    def __init__(self, key, coords, closed, locked):
        self.key = key
        self.coords = coords        # original vertices (map CRS), ring not repeated
        self.closed = closed
        self.locked = locked        # True -> may be a target but never edited
        self.vpids = []             # point id of each original vertex
        self.entries = []           # noded sequence: (pid, original_index or None)


class Edge(object):
    __slots__ = ("id", "pids", "owners", "start", "end")

    def __init__(self, eid, pids, owners):
        self.id = eid
        self.pids = pids
        self.owners = owners
        self.start = pids[0]
        self.end = pids[-1]

    @property
    def is_loop(self):
        return self.start == self.end


class AlignPlan(object):
    """Result of planning an alignment of `src` edge onto `tgt` edge."""

    def __init__(self, src, tgt):
        self.src = src
        self.tgt = tgt
        self.parts = {}            # part key -> list of emit items
        self.error = None
        self.max_shift = 0.0
        self.shift_pair = None     # ((x,y),(x,y)) realising max_shift
        self.gap_ring = []         # polygon between the two edges
        self.area = 0.0

    @property
    def ok(self):
        return self.error is None

    @property
    def features(self):
        return sorted({k[:2] for k in self.parts} | {k[:2] for k in self.src.owners},
                      key=repr)


# --------------------------------------------------------------------- helpers

def _proj(p, a, b):
    """Parameter t of the projection of p on segment ab, and the distance."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L2 = dx * dx + dy * dy
    if L2 == 0.0:
        return 0.0, math.hypot(p[0] - ax, p[1] - ay)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2
    tc = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    cx, cy = ax + tc * dx, ay + tc * dy
    return t, math.hypot(p[0] - cx, p[1] - cy)


def _closest_on_seg(p, a, b):
    t, _ = _proj(p, a, b)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def _dist_pt_line(p, pts):
    best = None
    best_q = None
    for i in range(len(pts) - 1):
        q = _closest_on_seg(p, pts[i], pts[i + 1])
        d = math.hypot(p[0] - q[0], p[1] - q[1])
        if best is None or d < best:
            best, best_q = d, q
    if best is None:  # single point
        q = pts[0]
        return math.hypot(p[0] - q[0], p[1] - q[1]), q
    return best, best_q


def _rasterize(a, b, cell, pad):
    """Grid cells touched by segment ab (with optional padding)."""
    # fast path: short segments just take their (padded) bounding-box cells
    fl = math.floor
    x0 = int(fl((min(a[0], b[0]) - pad) / cell))
    x1 = int(fl((max(a[0], b[0]) + pad) / cell))
    y0 = int(fl((min(a[1], b[1]) - pad) / cell))
    y1 = int(fl((max(a[1], b[1]) + pad) / cell))
    if (x1 - x0 + 1) * (y1 - y0 + 1) <= 6:
        return {(i, j) for i in range(x0, x1 + 1) for j in range(y0, y1 + 1)}
    # long segment: walk it in cell units, adding a neighbour cell only when
    # the sample is within `pad` of a cell boundary
    ax, ay = a[0] / cell, a[1] / cell
    dx, dy = b[0] / cell - ax, b[1] / cell - ay
    n = int(2.0 * math.hypot(dx, dy)) + 1
    p = pad / cell
    out = set()
    add = out.add
    for s in range(n + 1):
        f = s / n
        fx = ax + dx * f
        fy = ay + dy * f
        ix = int(fl(fx))
        iy = int(fl(fy))
        add((ix, iy))
        if p > 0.0:
            rx, ry = fx - ix, fy - iy
            if rx < p:
                add((ix - 1, iy))
            elif rx > 1.0 - p:
                add((ix + 1, iy))
            if ry < p:
                add((ix, iy - 1))
            elif ry > 1.0 - p:
                add((ix, iy + 1))
    return out


class _SegIndex(object):
    """Grid index over one polyline for fast nearest-distance queries."""

    BRUTE = 24

    def __init__(self, pts):
        self.pts = pts
        self.n = len(pts) - 1
        self.grid = None
        if self.n <= self.BRUTE:
            return
        L = _length(pts)
        self.cell = cell = max(L / self.n * 4.0, 1e-12)
        g = defaultdict(list)
        for i in range(self.n):
            for c in _rasterize(pts[i], pts[i + 1], cell, 0.0):
                g[c].append(i)
        self.grid = g
        xs = [c[0] for c in g]
        ys = [c[1] for c in g]
        self.c0x, self.c1x, self.c0y, self.c1y = min(xs), max(xs), min(ys), max(ys)

    def _ring(self, cx, cy, r):
        if r == 0:
            yield (cx, cy)
            return
        x0, x1, y0, y1 = cx - r, cx + r, cy - r, cy + r
        ix0, ix1 = max(x0, self.c0x), min(x1, self.c1x)
        for y in (y0, y1):
            if self.c0y <= y <= self.c1y:
                for x in range(ix0, ix1 + 1):
                    yield (x, y)
        iy0, iy1 = max(y0 + 1, self.c0y), min(y1 - 1, self.c1y)
        for x in (x0, x1):
            if self.c0x <= x <= self.c1x:
                for y in range(iy0, iy1 + 1):
                    yield (x, y)

    def nearest(self, p):
        if self.grid is None:
            return _dist_pt_line(p, self.pts)
        pts, cell = self.pts, self.cell
        cx, cy = int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell))
        dx = max(self.c0x - cx, 0, cx - self.c1x)
        dy = max(self.c0y - cy, 0, cy - self.c1y)
        r = max(dx, dy)
        rmax = max(abs(cx - self.c0x), abs(cx - self.c1x),
                   abs(cy - self.c0y), abs(cy - self.c1y))
        best, bq = float("inf"), None
        seen = set()
        while r <= rmax:
            for c in self._ring(cx, cy, r):
                for s in self.grid.get(c, ()):
                    if s in seen:
                        continue
                    seen.add(s)
                    q = _closest_on_seg(p, pts[s], pts[s + 1])
                    d = math.hypot(p[0] - q[0], p[1] - q[1])
                    if d < best:
                        best, bq = d, q
            # anything in later rings is at least (r - 1) cells away
            if best <= (r - 1) * cell:
                break
            r += 1
        if bq is None:
            return _dist_pt_line(p, pts)
        return best, bq


def hausdorff(pa, pb, limit=None):
    """Discrete Hausdorff distance (vertices vs. polyline, both ways).

    Indexed, so long edges stay fast. If `limit` is given, returns as soon
    as the distance is known to exceed it.
    """
    best = (0.0, None)
    ib = _SegIndex(pb)
    for p in pa:
        d, q = ib.nearest(p)
        if d > best[0]:
            best = (d, (p, q))
            if limit is not None and d > limit:
                return best
    ia = _SegIndex(pa)
    for p in pb:
        d, q = ia.nearest(p)
        if d > best[0]:
            best = (d, (q, p))
            if limit is not None and d > limit:
                return best
    return best


def _length(pts):
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
               for i in range(len(pts) - 1))


def _signed_area(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s * 0.5


def ring_area(pts):
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _seg_key(a, b):
    return (a, b) if a < b else (b, a)


# ----------------------------------------------------------------------- graph

class TopologyGraph(object):

    def __init__(self, cluster_tolerance):
        if not cluster_tolerance or cluster_tolerance <= 0:
            raise GraphError("cluster tolerance must be > 0")
        self.tol = float(cluster_tolerance)
        self.points = []                    # pid -> (x, y)
        self._pgrid = defaultdict(list)
        self.parts = {}                     # key -> _Part
        self.edges = []                     # eid -> Edge
        self.nodes = set()                  # pids
        self.seg_edge = {}                  # (a,b) -> eid
        self.node_edges = defaultdict(set)  # pid -> {eid}
        self._by_ends = defaultdict(list)   # frozenset({a,b}) -> [eid]
        self._hgrid = None
        self._hcell = 1.0
        self._hsegs = []
        self.built = False

    # ------------------------------------------------------------- input
    def add_part(self, key, coords, closed, locked=False):
        coords = [(float(x), float(y)) for x, y in coords]
        if closed and len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if (closed and len(coords) < 3) or len(coords) < 2:
            return False
        self.parts[key] = _Part(key, coords, closed, locked)
        return True

    def vertex_count(self):
        return sum(len(p.coords) for p in self.parts.values())

    # ------------------------------------------------------------- snapping
    def _snap(self, x, y):
        t = self.tol
        cx, cy = int(math.floor(x / t)), int(math.floor(y / t))
        best, bd = None, t * t
        for i in (cx - 1, cx, cx + 1):
            for j in (cy - 1, cy, cy + 1):
                for pid in self._pgrid.get((i, j), ()):
                    px, py = self.points[pid]
                    d = (px - x) ** 2 + (py - y) ** 2
                    if d <= bd:
                        bd, best = d, pid
        if best is None:
            best = len(self.points)
            self.points.append((x, y))
            self._pgrid[(cx, cy)].append(best)
        return best

    # ------------------------------------------------------------- grid util
    @staticmethod
    def _cells_for_segment(a, b, cell, pad):
        return _rasterize(a, b, cell, pad)

    # ------------------------------------------------------------- build
    def build(self, cancel=None):
        """Build the graph in one go. `cancel` is an optional callable; when
        it returns True the build stops with GraphCancelled."""
        for _ in self.build_steps():
            if cancel is not None and cancel():
                raise GraphCancelled()
        return self

    def build_steps(self):
        """Generator that builds the graph, yielding every few thousand
        operations so a caller can run it in small slices on the GUI thread
        (no worker threads, nothing to deadlock with map rendering)."""
        P = self.points
        tol = self.tol
        ticks = 0
        STEP = 3000

        # 1. cluster vertices
        for p in self.parts.values():
            p.vpids = [self._snap(x, y) for x, y in p.coords]
            ticks += len(p.coords)
            if ticks >= STEP:
                ticks = 0
                yield

        # 2. raw segments
        segs = []          # (part, k, a, b)
        for p in self.parts.values():
            n = len(p.coords)
            m = n if p.closed else n - 1
            for k in range(m):
                segs.append((p, k, p.vpids[k], p.vpids[(k + 1) % n]))
        splits = [[] for _ in segs]

        lengths = sorted(math.hypot(P[b][0] - P[a][0], P[b][1] - P[a][1])
                         for (_, _, a, b) in segs if a != b)
        # median, not mean: a few long rural lines must not make the cells
        # so big that dense curves pile thousands of segments into one cell
        med = lengths[len(lengths) // 2] if lengths else tol
        cell = max(med * 2.0, tol * 4.0)

        grid = defaultdict(list)
        seg_cells = [None] * len(segs)
        bbox = [None] * len(segs)
        for si, (_, _, a, b) in enumerate(segs):
            if a == b:
                continue
            A, B = P[a], P[b]
            cs = _rasterize(A, B, cell, tol)
            seg_cells[si] = cs
            bbox[si] = (min(A[0], B[0]) - tol, min(A[1], B[1]) - tol,
                        max(A[0], B[0]) + tol, max(A[1], B[1]) + tol)
            for c in cs:
                grid[c].append(si)
            ticks += len(cs)
            if ticks >= STEP:
                ticks = 0
                yield

        # 3. noding: T-junctions and proper crossings.
        # Each pair is tested once, in the lowest cell the two share; no
        # global "seen" set (that used gigabytes on dense data).
        for c, lst in grid.items():
            L = len(lst)
            ticks += L * L // 2 + 1
            if ticks >= STEP:
                ticks = 0
                yield
            for ii in range(L):
                i = lst[ii]
                bi = bbox[i]
                ci = seg_cells[i]
                for jj in range(ii + 1, L):
                    j = lst[jj]
                    bj = bbox[j]
                    if bj[0] > bi[2] or bj[2] < bi[0] or bj[1] > bi[3] or bj[3] < bi[1]:
                        continue
                    cj = seg_cells[j]
                    if len(ci) > 1 and len(cj) > 1 and min(ci & cj) != c:
                        continue
                    self._intersect(segs, splits, i, j)

        # 4. noded vertex sequences
        idx = 0
        for p in self.parts.values():
            n = len(p.coords)
            m = n if p.closed else n - 1
            entries = []
            for k in range(m):
                entries.append((p.vpids[k], k))
                for t, pid in sorted(splits[idx + k]):
                    entries.append((pid, None))
            if not p.closed:
                entries.append((p.vpids[-1], n - 1))
            idx += m
            out = []
            for e in entries:
                if out and out[-1][0] == e[0]:
                    if out[-1][1] is None and e[1] is not None:
                        out[-1] = e
                    continue
                out.append(e)
            if p.closed:
                while len(out) > 1 and out[0][0] == out[-1][0]:
                    last = out.pop()
                    if out[0][1] is None and last[1] is not None:
                        out[0] = last
            p.entries = out

        # 5. planar graph segments
        occ = defaultdict(list)
        for p in self.parts.values():
            E = p.entries
            L = len(E)
            if (p.closed and L < 3) or L < 2:
                continue
            m = L if p.closed else L - 1
            for i in range(m):
                a, b = E[i][0], E[(i + 1) % L][0]
                if a != b:
                    occ[_seg_key(a, b)].append((p.key, i))
        owners = {sk: frozenset(k for k, _ in o) for sk, o in occ.items()}
        adj = defaultdict(set)
        for a, b in occ:
            adj[a].add(b)
            adj[b].add(a)

        nodes = set()
        for v, nb in adj.items():
            if len(nb) != 2:
                nodes.add(v)
                continue
            n1, n2 = tuple(nb)
            if owners[_seg_key(v, n1)] != owners[_seg_key(v, n2)]:
                nodes.add(v)
        for p in self.parts.values():
            if not p.closed and len(p.entries) >= 2:
                nodes.add(p.entries[0][0])
                nodes.add(p.entries[-1][0])
        self.nodes = nodes

        # 6. walk edges node -> node
        visited = set()
        edges = []

        def walk(start, nxt):
            path = [start, nxt]
            visited.add(_seg_key(start, nxt))
            prev, cur = start, nxt
            while cur not in nodes:
                cands = [w for w in adj[cur] if w != prev]
                if not cands:
                    break
                w = cands[0]
                sk = _seg_key(cur, w)
                if sk in visited:
                    path.append(w)
                    break
                visited.add(sk)
                path.append(w)
                prev, cur = cur, w
                if cur == start:
                    break
            return path

        for nd in sorted(nodes):
            for nb in sorted(adj.get(nd, ())):
                if _seg_key(nd, nb) in visited:
                    continue
                path = walk(nd, nb)
                edges.append(path)
        # node-less cycles (e.g. an isolated ring)
        for sk in sorted(occ):
            if sk in visited:
                continue
            path = walk(sk[0], sk[1])
            edges.append(path)

        self.edges = []
        for path in edges:
            eid = len(self.edges)
            e = Edge(eid, path, owners[_seg_key(path[0], path[1])])
            self.edges.append(e)
            for i in range(len(path) - 1):
                self.seg_edge[_seg_key(path[i], path[i + 1])] = eid
            self.node_edges[e.start].add(eid)
            self.node_edges[e.end].add(eid)
            if not e.is_loop:
                self._by_ends[frozenset((e.start, e.end))].append(eid)

        yield
        self._build_hit_index()
        self.built = True

    def _intersect(self, segs, splits, i, j):
        P = self.points
        tol = self.tol
        _, _, a, b = segs[i]
        _, _, c, d = segs[j]
        A, B, C, D = P[a], P[b], P[c], P[d]

        # vertex on segment (T-junctions / collinear overlaps)
        for pid, pt in ((c, C), (d, D)):
            if pid == a or pid == b:
                continue
            t, dist = _proj(pt, A, B)
            if 0.0 < t < 1.0 and dist <= tol:
                splits[i].append((t, pid))
        for pid, pt in ((a, A), (b, B)):
            if pid == c or pid == d:
                continue
            t, dist = _proj(pt, C, D)
            if 0.0 < t < 1.0 and dist <= tol:
                splits[j].append((t, pid))

        # proper crossing
        rx, ry = B[0] - A[0], B[1] - A[1]
        sx, sy = D[0] - C[0], D[1] - C[1]
        den = rx * sy - ry * sx
        if den == 0.0:
            return
        qx, qy = C[0] - A[0], C[1] - A[1]
        t = (qx * sy - qy * sx) / den
        u = (qx * ry - qy * rx) / den
        if 0.0 < t < 1.0 and 0.0 < u < 1.0:
            X = (A[0] + t * rx, A[1] + t * ry)
            for E_ in (A, B, C, D):
                if math.hypot(X[0] - E_[0], X[1] - E_[1]) <= tol:
                    return
            pid = self._snap(X[0], X[1])
            if pid not in (a, b):
                splits[i].append((t, pid))
            if pid not in (c, d):
                splits[j].append((u, pid))

    # ------------------------------------------------------------- queries
    def edge_coords(self, eid):
        return [self.points[p] for p in self.edges[eid].pids]

    def point(self, pid):
        return self.points[pid]

    def _build_hit_index(self):
        P = self.points
        self._hsegs = []
        for sk, eid in self.seg_edge.items():
            self._hsegs.append((P[sk[0]], P[sk[1]], eid))
        if not self._hsegs:
            self._hgrid = {}
            return
        lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b, _ in self._hsegs]
        self._hcell = max(sum(lens) / len(lens), self.tol * 4.0)
        g = defaultdict(list)
        for si, (a, b, _) in enumerate(self._hsegs):
            for c in self._cells_for_segment(a, b, self._hcell, 0.0):
                g[c].append(si)
        self._hgrid = g

    def hit_test(self, x, y, radius):
        """Nearest edge within `radius` of (x, y) -> (eid, distance) or None."""
        if not self._hsegs:
            return None
        cell = self._hcell
        span = int(math.ceil(radius / cell))
        if span > 40:
            cands = range(len(self._hsegs))
        else:
            cx, cy = int(math.floor(x / cell)), int(math.floor(y / cell))
            cs = set()
            for i in range(cx - span - 1, cx + span + 2):
                for j in range(cy - span - 1, cy + span + 2):
                    cs.update(self._hgrid.get((i, j), ()))
            cands = cs
        best = None
        for si in cands:
            a, b, eid = self._hsegs[si]
            _, d = _proj((x, y), a, b)
            if d <= radius and (best is None or d < best[1]):
                best = (eid, d)
        return best

    def targets(self, eid, nearby_factor=2.0):
        """Edges that share both end nodes with `eid`, best first.

        Like Pro, the edge snaps to the *nearby* edge. Candidates are ranked
        by maximum shift; a candidate is dropped when the area it encloses
        with the hovered edge is far larger than the best candidate's - that
        is the far side of a neighbouring polygon (it encloses the whole
        polygon), not a gap or overlap. `nearby_factor` (0 = off) is only a
        sanity cap: max shift may not exceed that multiple of the shorter
        edge's length.

        Returns a list of (target_eid, max_shift).
        """
        e = self.edges[eid]
        if e.is_loop:
            return []
        src = self.edge_coords(eid)
        src_len = _length(src)
        cands = []   # (shift, area, target)
        for t in self._by_ends.get(frozenset((e.start, e.end)), ()):
            if t == eid:
                continue
            te = self.edges[t]
            # The other side of the same ring/line is never "adjacent".
            if te.owners & e.owners:
                continue
            tc = self.edge_coords(t)
            if te.start != e.start:
                tc = list(reversed(tc))
            limit = nearby_factor * min(src_len, _length(tc)) if nearby_factor > 0 else None
            d, _ = hausdorff(src, tc, limit)
            if limit is not None and d > limit:
                continue
            area = abs(_signed_area(src + list(reversed(tc))[1:-1]))
            cands.append((d, area, t))
        if not cands:
            return []
        cands.sort()
        best_d, best_a = cands[0][0], min(c[1] for c in cands)
        out = [(t, d) for d, a, t in cands
               if a <= AREA_RATIO * best_a + self.tol or d <= 1.5 * best_d]
        return out

    def touching_features(self, eid):
        """Features with an edge at either end node but not owning `eid`."""
        e = self.edges[eid]
        own = {k[:2] for k in e.owners}
        out = set()
        for n in (e.start, e.end):
            for other in self.node_edges.get(n, ()):
                for k in self.edges[other].owners:
                    if k[:2] not in own:
                        out.add(k[:2])
        return out

    # ------------------------------------------------------------- planning
    def plan_align(self, src_id, tgt_id):
        src = self.edges[src_id]
        tgt = self.edges[tgt_id]
        plan = AlignPlan(src, tgt)

        if src.is_loop or {src.start, src.end} != {tgt.start, tgt.end}:
            plan.error = "edges do not share two topology nodes"
            return plan

        tpids = list(tgt.pids) if tgt.start == src.start else list(reversed(tgt.pids))
        scoords = self.edge_coords(src_id)
        tcoords = [self.points[p] for p in tpids]
        plan.max_shift, plan.shift_pair = hausdorff(scoords, tcoords)
        plan.gap_ring = scoords + list(reversed(tcoords))[1:-1]
        plan.area = ring_area(plan.gap_ring)

        k = len(src.pids) - 1
        for key in sorted(src.owners, key=repr):
            p = self.parts[key]
            if p.locked:
                plan.error = "curved geometry cannot be aligned"
                return plan
            E = p.entries
            L = len(E)
            m = L if p.closed else L - 1
            pos = [i for i in range(m)
                   if self.seg_edge.get(_seg_key(E[i][0], E[(i + 1) % L][0])) == src.id]
            if len(pos) != k:
                plan.error = "edge is traversed more than once by a feature"
                return plan
            posset = set(pos)
            if p.closed:
                if k >= L:
                    plan.error = "edge is a whole ring"
                    return plan
                starts = [i for i in pos if (i - 1) % L not in posset]
                if len(starts) != 1:
                    plan.error = "edge is not contiguous in a feature"
                    return plan
                i0 = starts[0]
            else:
                i0 = min(pos)
                if max(pos) - i0 != k - 1:
                    plan.error = "edge is not contiguous in a feature"
                    return plan

            first = E[i0][0]
            if first == tpids[0]:
                interior = tpids[1:-1]
            elif first == tpids[-1]:
                interior = list(reversed(tpids))[1:-1]
            else:
                plan.error = "internal: edge orientation mismatch"
                return plan

            def emit(i):
                pid, orig = E[i % L]
                return ("orig", orig) if orig is not None else ("pid", pid)

            run_end = i0 + k
            run = [emit(i0)] + [("pid", q) for q in interior] + [emit(run_end)]
            if p.closed:
                rest = [("orig", E[i % L][1]) for i in range(run_end + 1, i0 + L)
                        if E[i % L][1] is not None]
                items = run + rest
            else:
                before = [("orig", o) for _, o in E[:i0] if o is not None]
                after = [("orig", o) for _, o in E[run_end + 1:] if o is not None]
                items = before + run + after

            # collapse / degeneracy check in map space
            pts = []
            for kind, v in items:
                c = p.coords[v] if kind == "orig" else self.points[v]
                if not pts or pts[-1] != c:
                    pts.append(c)
            if p.closed:
                while len(pts) > 1 and pts[0] == pts[-1]:
                    pts.pop()
                if len(pts) < 3 or ring_area(pts) <= self.tol * self.tol:
                    plan.error = "alignment would collapse a polygon"
                    plan.parts[key] = items
                    return plan
            elif len(pts) < 2:
                plan.error = "alignment would collapse a line"
                plan.parts[key] = items
                return plan
            plan.parts[key] = items
        return plan
