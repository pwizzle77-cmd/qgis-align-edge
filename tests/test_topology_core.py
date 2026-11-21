# -*- coding: utf-8 -*-
"""Unit tests for the QGIS-free topology core.

Run from the plugin folder:  python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topology_core import TopologyGraph, ring_area  # noqa: E402

TOL = 1e-6


def resolve(g, key, items):
    p = g.parts[key]
    return [p.coords[v] if k == "orig" else g.points[v] for k, v in items]


def find_edge(g, owner_key, must_contain):
    for e in g.edges:
        if owner_key in e.owners and must_contain in g.edge_coords(e.id):
            return e.id
    raise AssertionError("edge not found")


class GapTests(unittest.TestCase):
    """Square A and polygon B whose left side bulges away -> sliver gap."""

    def setUp(self):
        self.g = TopologyGraph(TOL)
        self.A = ("A", 1, 0, 0)
        self.B = ("B", 2, 0, 0)
        self.g.add_part(self.A, [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)], True)
        self.g.add_part(self.B, [(10, 0), (20, 0), (20, 10), (10, 10), (10.5, 5)], True)
        self.g.build()

    def test_nodes(self):
        nodes = {self.g.points[n] for n in self.g.nodes}
        self.assertIn((10.0, 0.0), nodes)
        self.assertIn((10.0, 10.0), nodes)

    def test_target_and_plan(self):
        hit = self.g.hit_test(10.0, 5.0, 0.1)
        self.assertIsNotNone(hit)
        src = hit[0]
        self.assertEqual(self.g.edges[src].owners, frozenset([self.A]))
        tg = self.g.targets(src)
        self.assertEqual(len(tg), 1)
        self.assertAlmostEqual(tg[0][1], 0.5)
        plan = self.g.plan_align(src, tg[0][0])
        self.assertTrue(plan.ok, plan.error)
        self.assertAlmostEqual(plan.area, 2.5)
        ring = resolve(self.g, self.A, plan.parts[self.A])
        self.assertIn((10.5, 5.0), ring)
        self.assertEqual(len(ring), 5)
        self.assertAlmostEqual(ring_area(ring), 102.5)

    def test_reverse_direction(self):
        src = self.g.hit_test(10.4, 5.0, 0.1)[0]
        self.assertEqual(self.g.edges[src].owners, frozenset([self.B]))
        tgt = self.g.targets(src)[0][0]
        plan = self.g.plan_align(src, tgt)
        self.assertTrue(plan.ok)
        ring = resolve(self.g, self.B, plan.parts[self.B])
        self.assertNotIn((10.5, 5.0), ring)
        self.assertAlmostEqual(ring_area(ring), 100.0)

    def test_touching(self):
        src = self.g.hit_test(10.0, 5.0, 0.1)[0]
        self.assertEqual(self.g.touching_features(src), {("B", 2)})


class OverlapTests(unittest.TestCase):
    """B's left side crosses A's top and bottom: crossing nodes inserted."""

    def setUp(self):
        self.g = TopologyGraph(TOL)
        self.A = ("A", 1, 0, 0)
        self.B = ("B", 2, 0, 0)
        self.g.add_part(self.A, [(0, 0), (10, 0), (10, 10), (0, 10)], True)
        self.g.add_part(self.B, [(9.9, -5), (20, -5), (20, 15), (9.9, 15)], True)
        self.g.build()

    def test_align_a_onto_b(self):
        src = find_edge(self.g, self.A, (10.0, 0.0))
        tg = self.g.targets(src)
        self.assertEqual(len(tg), 1)
        plan = self.g.plan_align(src, tg[0][0])
        self.assertTrue(plan.ok, plan.error)
        ring = resolve(self.g, self.A, plan.parts[self.A])
        self.assertNotIn((10.0, 0.0), ring)
        self.assertAlmostEqual(ring_area(ring), 99.0)


class LineTests(unittest.TestCase):
    """A line that wiggles away from a polygon edge between two touch points."""

    def test_line_onto_polygon(self):
        g = TopologyGraph(TOL)
        P = ("P", 1, 0, 0)
        Ln = ("L", 5, 0, 0)
        g.add_part(P, [(0, 0), (10, 0), (10, 10), (0, 10)], True)
        g.add_part(Ln, [(-5, 0), (2, 0), (5, -1), (8, 0), (15, 0)], False)
        g.build()
        src = find_edge(g, Ln, (5.0, -1.0))
        tg = g.targets(src)
        self.assertEqual(len(tg), 1)
        plan = g.plan_align(src, tg[0][0])
        self.assertTrue(plan.ok, plan.error)
        line = resolve(g, Ln, plan.parts[Ln])
        self.assertEqual(line, [(-5.0, 0.0), (2.0, 0.0), (8.0, 0.0), (15.0, 0.0)])

    def test_polygon_onto_line(self):
        g = TopologyGraph(TOL)
        P = ("P", 1, 0, 0)
        Ln = ("L", 5, 0, 0)
        g.add_part(P, [(0, 0), (10, 0), (10, 10), (0, 10)], True)
        g.add_part(Ln, [(-5, 0), (2, 0), (5, -1), (8, 0), (15, 0)], False)
        g.build()
        src = g.hit_test(5.0, 0.0, 0.01)[0]
        self.assertIn(P, g.edges[src].owners)
        plan = g.plan_align(src, g.targets(src)[0][0])
        self.assertTrue(plan.ok, plan.error)
        ring = resolve(g, P, plan.parts[P])
        self.assertIn((5.0, -1.0), ring)
        # (2,0) and (8,0) were not original polygon vertices -> inserted as nodes
        self.assertIn((2.0, 0.0), ring)
        self.assertIn((8.0, 0.0), ring)


class SharedEdgeTests(unittest.TestCase):
    """A shared boundary moves for every owner at once."""

    def test_shared_edge_updates_both(self):
        g = TopologyGraph(TOL)
        A = ("A", 1, 0, 0)
        B = ("B", 2, 0, 0)
        C = ("C", 3, 0, 0)
        g.add_part(A, [(0, 0), (10, 0), (10, 10), (0, 10)], True)
        g.add_part(B, [(10, 0), (20, 0), (20, 10), (10, 10)], True)
        # C sits on the right side of the shared edge's ends, bulging left
        g.add_part(C, [(10, 10), (9, 5), (10, 0), (10, -3), (12, -3)], False)
        g.build()
        src = g.hit_test(10.0, 5.0, 0.01)[0]
        self.assertEqual(g.edges[src].owners, frozenset([A, B]))
        plan = g.plan_align(src, g.targets(src)[0][0])
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(set(plan.parts), {A, B})
        for key in (A, B):
            self.assertIn((9.0, 5.0), resolve(g, key, plan.parts[key]))


class CollapseTests(unittest.TestCase):
    def test_sliver_polygon_collapses(self):
        g = TopologyGraph(TOL)
        S = ("S", 1, 0, 0)
        g.add_part(S, [(0, 0), (5, 0.1), (10, 0), (5, -0.1)], True)
        # a neighbour so the sliver's corners become nodes
        g.add_part(("N", 2, 0, 0), [(-5, 0), (0, 0)], False)
        g.add_part(("N", 3, 0, 0), [(10, 0), (15, 0)], False)
        g.build()
        src = g.hit_test(5.0, 0.1, 0.01)[0]
        # the other side of the same polygon is never offered as a target
        self.assertEqual(g.targets(src), [])
        # forcing it is caught by the collapse check
        other = [e.id for e in g.edges
                 if e.id != src and {e.start, e.end} == {g.edges[src].start, g.edges[src].end}]
        plan = g.plan_align(src, other[0])
        self.assertFalse(plan.ok)
        self.assertIn("collapse", plan.error)


class SnapTests(unittest.TestCase):
    def test_cluster_and_t_junction(self):
        g = TopologyGraph(0.01)
        g.add_part(("A", 1, 0, 0), [(0, 0), (10, 0), (10, 10), (0, 10)], True)
        g.add_part(("L", 2, 0, 0), [(5, 0.005), (5, -5)], False)  # T on A's bottom
        g.build()
        node_pts = {g.points[n] for n in g.nodes}
        self.assertTrue(any(abs(x - 5) < 1e-9 and abs(y - 0.005) < 1e-9 for x, y in node_pts))

    def test_no_targets_without_shared_nodes(self):
        g = TopologyGraph(TOL)
        g.add_part(("A", 1, 0, 0), [(0, 0), (10, 0), (10, 10), (0, 10)], True)
        g.add_part(("B", 2, 0, 0), [(11, 0), (20, 0), (20, 10), (11, 10)], True)
        g.build()
        src = g.hit_test(10, 5, 0.1)[0]
        self.assertEqual(g.targets(src), [])


class ClusteredTargetOwnerTests(unittest.TestCase):
    """Target owners whose own vertex sits a hair off the clustered node.

    Real adjoining layers are rarely digitized to the exact same vertex;
    they only need to fall within the cluster tolerance to be treated as
    the same topology node. plan_align must accept that, not demand
    bit-for-bit equality.
    """

    def test_within_cluster_tolerance_aligns(self):
        g = TopologyGraph(TOL)
        A = ("A", 1, 0, 0)
        B = ("B", 2, 0, 0)
        C = ("C", 3, 0, 0)
        g.add_part(A, [(0, 0), (10, 0), (10, 10), (0, 10)], True)
        g.add_part(B, [(10, 0), (20, 0), (20, 10), (10, 10), (10.5, 5)], True)
        # C traces the same bulge as B but its own vertex is a third of a
        # tolerance away - still the same clustered node.
        g.add_part(C, [(10, 10), (10.5, 5 + TOL / 3.0), (10, 0), (9, 0), (9, 10)], True)
        g.build()
        src = g.hit_test(10.0, 5.0, 0.1)[0]
        self.assertEqual(g.edges[src].owners, frozenset([A]))
        tgt = g.targets(src)[0][0]
        self.assertEqual(g.edges[tgt].owners, frozenset([B, C]))
        plan = g.plan_align(src, tgt)
        self.assertTrue(plan.ok, plan.error)

    def test_beyond_cluster_tolerance_is_rejected(self):
        g = TopologyGraph(TOL)
        A = ("A", 1, 0, 0)
        B = ("B", 2, 0, 0)
        C = ("C", 3, 0, 0)
        D = ("D", 4, 0, 0)
        g.add_part(A, [(0, 0), (10, 0), (10, 10), (0, 10)], True)
        # D is added first so its own (unperturbed) vertex becomes the
        # clustered node's spatial anchor; B and C each sit within
        # tolerance of D (so all three still cluster together) but on
        # opposite sides of it, almost two tolerances apart from each other.
        g.add_part(D, [(10, 10), (10.5, 5), (10, 0), (7, 0), (7, 10)], True)
        g.add_part(B, [(10, 0), (20, 0), (20, 10), (10, 10), (10.5, 5 + TOL * 0.9)], True)
        g.add_part(C, [(10, 10), (10.5, 5 - TOL * 0.9), (10, 0), (9, 0), (9, 10)], True)
        g.build()
        src = g.hit_test(10.0, 5.0, 0.1)[0]
        self.assertEqual(g.edges[src].owners, frozenset([A]))
        tgt = g.targets(src)[0][0]
        self.assertEqual(g.edges[tgt].owners, frozenset([B, C, D]))
        plan = g.plan_align(src, tgt)
        self.assertFalse(plan.ok)
        self.assertIn("clustered vertex", plan.error)


class SlicedBuildTests(unittest.TestCase):
    def test_a_long_part_yields_during_vertex_clustering(self):
        graph = TopologyGraph(TOL)
        key = ('long line', 1, 0, 0)
        graph.add_part(key, [(i, i % 2) for i in range(10000)], False)
        next(graph.build_steps())
        self.assertLessEqual(len(graph.parts[key].vpids), 256)

    def test_dense_intersections_yield_inside_the_pair_loop(self):
        class CountingGraph(TopologyGraph):
            calls = 0

            def _intersect(self, *args):
                self.calls += 1
                return super()._intersect(*args)

        graph = CountingGraph(TOL)
        for i in range(120):
            graph.add_part(('lines', i, 0, 0), [(-100, (i + 1) * 0.1), (100, -(i + 1) * 0.1)], False)
        steps = graph.build_steps()
        previous = 0
        for _ in steps:
            self.assertLessEqual(graph.calls - previous, 256)
            previous = graph.calls
        self.assertTrue(graph.built)
        self.assertGreater(graph.calls, 1000)


if __name__ == "__main__":
    unittest.main()
