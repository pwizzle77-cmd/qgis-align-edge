"""Real QGIS checks for safe writes, exact XY, dimensions, references and UI state."""
import importlib.util
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

# Load this folder as the align_edge package, whatever the clone is called.
ROOT = Path(__file__).resolve().parents[1]
if 'align_edge' not in sys.modules:
    spec = importlib.util.spec_from_file_location('align_edge', ROOT / '__init__.py',
                                                  submodule_search_locations=[str(ROOT)])
    sys.modules['align_edge'] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sys.modules['align_edge'])
try:
    from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsLineString, QgsProject, QgsRectangle, QgsVectorLayer, QgsWkbTypes
    from qgis.gui import QgsMapCanvas
    from qgis.PyQt.QtCore import Qt
    HAS_QGIS = True
except ImportError:
    HAS_QGIS = False


class MemorySettings:
    def __init__(self):
        from align_edge.settings import _DEFAULTS
        self.values = dict(_DEFAULTS, diagnostic_log=False)
        self.excluded, self.references = set(), set()

    def get(self, key): return self.values[key]
    def set(self, key, value): self.values[key] = value
    def excluded_layer_ids(self): return self.excluded
    def reference_layer_ids(self): return self.references
    def search_radius(self, canvas): return 0.5
    def cluster_tolerance(self, canvas): return 0.01
    def set_excluded_layer_ids(self, ids): self.excluded = set(ids)
    def set_reference_layer_ids(self, ids): self.references = set(ids)


@unittest.skipUnless(HAS_QGIS, 'Requires real QGIS')
class QgisIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QgsApplication.instance() or QgsApplication([], False)
        cls.app.initQgis()
        from align_edge import diag
        diag.set_enabled(False)

    def setUp(self):
        self.project = QgsProject.instance()
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:26917'))
        self.canvas.setExtent(QgsRectangle(-20, -20, 40, 40))
        self.settings = MemorySettings()
        self.layers, self.caches = [], []

    def test_optional_keystone_declaration_registers_and_unregisters_without_a_dependency(self):
        from unittest.mock import Mock, patch
        from qgis.PyQt.QtWidgets import QMainWindow, QToolBar
        from align_edge import plugin
        win = QMainWindow()
        toolbar = QToolBar('Advanced Digitizing', win)
        iface = Mock()
        iface.mainWindow.return_value = win
        iface.mapCanvas.return_value = self.canvas
        iface.advancedDigitizeToolBar.return_value = toolbar
        win.setProperty('keystone/tool_placements', {'otherTool': {'tab': 'Map'}})
        try:
            for _ in range(2):
                with patch.object(plugin, 'Settings', return_value=self.settings):
                    controller = plugin.AlignEdgePlugin(iface)
                controller.initGui()
                declarations = win.property('keystone/tool_placements')
                declaration = declarations['mActionAlignEdge']
                self.assertEqual((declaration['tab'], declaration['group'], declaration['modify_group']),
                                 ('Edit', 'Tools', 'Alignment'))
                self.assertEqual(declaration['menu_actions'], ['mActionAlignEdgeSettings'])
                self.assertIs(win.findChild(type(controller.action), 'mActionAlignEdge'), controller.action)
                controller.unload()
                self.assertEqual(win.property('keystone/tool_placements'), {'otherTool': {'tab': 'Map'}})
                self.assertIsNone(win.findChild(type(controller.action), 'mActionAlignEdge'))
        finally:
            win.deleteLater()

    def tearDown(self):
        for cache in self.caches:
            cache.clear()
        self.canvas.setLayers([])
        for layer in self.layers:
            self.project.removeMapLayer(layer.id())
        self.canvas.deleteLater()

    def layer(self, name, wkt, kind='LineString', editable=True):
        layer = QgsVectorLayer(f'{kind}?crs=EPSG:26917', name, 'memory')
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        self.assertTrue(layer.dataProvider().addFeatures([feature])[0])
        self.project.addMapLayer(layer)
        if editable:
            self.assertTrue(layer.startEditing())
        self.layers.append(layer)
        return layer

    def geometry(self, layer):
        return list(layer.getFeatures())[0].geometry()     # an open iterator locks a GeoPackage

    def vertices(self, layer):
        geometry = self.geometry(layer)     # must outlive its vertex iterator
        return list(geometry.vertices())

    def graph(self, tolerance=0.01):
        from align_edge.graph_builder import FeatureCache, build_from_snapshot
        self.canvas.setLayers(self.layers)
        cache = FeatureCache(self.canvas, self.settings, lambda reason: None)
        self.caches.append(cache)
        self.assertTrue(cache.sync_layers())
        cache.read()
        return build_from_snapshot(cache.snapshot(tolerance))

    def plan(self, graph, source, target):
        src = next(e.id for e in graph.edges if any(k[0] == source.id() for k in e.owners))
        tgt = next(e.id for e in graph.edges if any(k[0] == target.id() for k in e.owners))
        plan = graph.plan_align(src, tgt)
        self.assertTrue(plan.ok, plan.error)
        return plan

    def test_clustered_endpoints_copy_target_exactly_without_editing_reference(self):
        from align_edge.edge_writer import build_changes, apply_changes
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        target = self.layer('reference', 'LINESTRING(0.005 0,5 2,10.005 0)', editable=False)
        self.settings.references.add(target.id())
        before = self.geometry(target).asWkt()
        graph, ctx = self.graph()
        changes = build_changes(self.plan(graph, source, target), graph, ctx)
        self.assertEqual(set(changes), {source.id()})
        apply_changes(changes, ctx)
        self.assertEqual(self.geometry(source).asWkb(), self.geometry(target).asWkb())
        self.assertEqual(self.geometry(target).asWkt(), before)
        self.assertFalse(target.isEditable())
        source.undoStack().undo()
        self.assertEqual(self.geometry(source).asWkt(), 'LineString (0 0, 5 1, 10 0)')

    def test_align_all_slivers_fixes_an_edge_within_tolerance_of_the_base_layer(self):
        # A bend partway along, far enough apart not to cluster with the graph's own 0.01
        # tolerance, keeps the two sides topologically distinct edges between the same
        # (clustered) end nodes: a straight 2-point sliver with no bend has nothing left to
        # distinguish it from its target once the ends cluster, and the graph merges the two
        # into one shared edge outright - there would be nothing left for this to find.
        from align_edge.sliver_align import find_candidates
        from align_edge.edge_writer import apply_changes, build_changes
        base = self.layer('base', 'LINESTRING(0 0,5 1,10 0)', editable=False)
        self.settings.references.add(base.id())
        edit = self.layer('edit', 'LINESTRING(0.005 0,5 1.05,10.005 0)')
        graph, ctx = self.graph()
        candidates, skipped = find_candidates(graph, ctx, {base.id()}, tolerance=0.1, nearby_factor=2.0)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(skipped, [])
        apply_changes(build_changes(candidates[0].plan, graph, ctx), ctx)
        self.assertEqual(self.geometry(edit).asWkb(), self.geometry(base).asWkb())

    def test_align_all_slivers_leaves_out_a_gap_wider_than_its_own_tolerance(self):
        from align_edge.sliver_align import find_candidates
        base = self.layer('base', 'LINESTRING(0 0,5 1,10 0)', editable=False)
        self.settings.references.add(base.id())
        edit = self.layer('edit', 'LINESTRING(0.005 0,5 1.05,10.005 0)')
        graph, ctx = self.graph()
        candidates, skipped = find_candidates(graph, ctx, {base.id()}, tolerance=0.01, nearby_factor=2.0)
        self.assertEqual(candidates, [])  # the bend is 0.05 apart - over the stricter 0.01 limit here
        self.assertEqual(skipped, [])
        self.assertEqual(self.geometry(edit).asWkt(), 'LineString (0.005 0, 5 1.05, 10.005 0)')

    def test_align_all_slivers_never_moves_the_base_layer_itself(self):
        from align_edge.sliver_align import find_candidates
        base = self.layer('base', 'LINESTRING(0 0,5 1,10 0)', editable=False)
        self.settings.references.add(base.id())
        edit = self.layer('edit', 'LINESTRING(0.005 0,5 1.05,10.005 0)')
        graph, ctx = self.graph()
        candidates, _skipped = find_candidates(graph, ctx, {base.id()}, tolerance=0.1, nearby_factor=2.0)
        self.assertEqual(len(candidates), 1)
        moved_owner_layers = {key[0] for key in graph.edges[candidates[0].src].owners}
        self.assertEqual(moved_owner_layers, {edit.id()})
        self.assertNotIn(base.id(), moved_owner_layers)

    def test_run_batch_fixes_a_second_sliver_after_the_first_changes_the_same_feature(self):
        # Both slivers are two parts of ONE 'edit' feature. Fixing the first rewrites the
        # whole feature's geometry (both parts), which is exactly what used to make every
        # later fix fail: a plan built before the edit, re-used after it, looks at a feature
        # whose cached version has since moved on. run_batch re-scans fresh instead of
        # reusing stale plans, so the second part still gets fixed in the same run.
        from align_edge.sliver_align import run_batch
        base = self.layer('base', 'MULTILINESTRING((0 0,5 1,10 0),(100 0,105 1,110 0))', editable=False)
        self.settings.references.add(base.id())
        edit = self.layer(
            'edit', 'MULTILINESTRING((0.005 0,5 1.05,10.005 0),(100.005 0,105 1.05,110.005 0))')
        self.canvas.setLayers(self.layers)
        done, failed = run_batch(self.canvas, self.settings, {base.id()}, tolerance=0.1, nearby_factor=2.0)
        self.assertEqual(failed, [])
        self.assertEqual(len(done), 2)
        self.assertEqual(self.geometry(edit).asWkb(), self.geometry(base).asWkb())

    def edge(self, graph, layer):
        return next(e.id for e in graph.edges if any(k[0] == layer.id() for k in e.owners))

    def test_edges_joined_to_a_moved_endpoint_stay_joined(self):
        from align_edge.edge_writer import apply_changes, build_changes
        source = self.layer('source', 'LINESTRING ZM(0 0 1 10,5 1 2 20,10 0 3 30)', 'LineStringZM')
        target = self.layer('target', 'LINESTRING(0.005 0,5 2,10.005 0)')
        incident = self.layer('incident', 'LINESTRING ZM(0 0 7 70,-5 0 8 80)', 'LineStringZM')
        graph, ctx = self.graph()
        plan = graph.plan_align(self.edge(graph, source), self.edge(graph, target))
        self.assertTrue(plan.ok, plan.error)
        apply_changes(build_changes(plan, graph, ctx), ctx)
        moved = list(self.vertices(incident))
        start = self.vertices(source)[0]
        self.assertEqual((start.x(), start.y()), (0.005, 0))
        self.assertEqual((moved[0].x(), moved[0].y(), moved[0].z(), moved[0].m()), (0.005, 0, 7, 70))
        self.assertEqual((moved[1].x(), moved[1].y(), moved[1].z(), moved[1].m()), (-5, 0, 8, 80))
        self.assertEqual(self.geometry(target).asWkt(3), 'LineString (0.005 0, 5 2, 10.005 0)')

    def test_endpoint_move_that_would_leave_a_reference_behind_is_blocked(self):
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        target = self.layer('target', 'LINESTRING(0.005 0,5 2,10.005 0)')
        incident = self.layer('incident', 'LINESTRING(0 0,-5 0)', editable=False)
        self.settings.references.add(incident.id())
        graph, ctx = self.graph()
        plan = graph.plan_align(self.edge(graph, source), self.edge(graph, target))
        self.assertFalse(plan.ok)
        self.assertIn('disconnect', plan.error)

    def test_transform_noise_at_a_joined_reference_does_not_block(self):
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        target = self.layer('target', 'LINESTRING(0.0000000001 0,5 2,10 0)')
        incident = self.layer('incident', 'LINESTRING(0 0,-5 0)', editable=False)
        self.settings.references.add(incident.id())
        graph, ctx = self.graph()
        plan = graph.plan_align(self.edge(graph, source), self.edge(graph, target))
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(plan.moved, {})

    def test_vertices_away_from_the_edge_are_never_thinned(self):
        """A wide cluster tolerance must not drop vertices of the rest of the feature."""
        from align_edge.edge_writer import build_changes
        source = self.layer('source', 'POLYGON((0 0,10 0,10 10,9.7 10.1,9.4 10,5 10,0 10,0 0.4,0 0))', 'Polygon')
        self.layer('target', 'LINESTRING(0 0,5 2,10 0)')
        graph, ctx = self.graph(0.5)
        src = next(e for e in graph.edges if any(k[0] == source.id() for k in e.owners)
                   and {graph.point(e.start), graph.point(e.end)} == {(0, 0), (10, 0)} and len(e.pids) == 2)
        tgt = graph.targets(src.id)[0][0]
        plan = graph.plan_align(src.id, tgt)
        self.assertTrue(plan.ok, plan.error)
        new = build_changes(plan, graph, ctx)[source.id()][0][1]
        xy = {(v.x(), v.y()) for v in new.vertices()}
        self.assertTrue({(10, 10), (9.7, 10.1), (9.4, 10), (5, 10), (0, 10), (5, 2)} <= xy, xy)

    def test_vertical_segments_away_from_the_edge_are_kept(self):
        from align_edge.edge_writer import build_changes
        source = self.layer('source', 'LINESTRING Z(-5 0 0,-5 0 9,0 0 9,5 1 9,10 0 9)', 'LineStringZ')
        self.layer('target', 'LINESTRING(0 0,5 2,10 0)')
        graph, ctx = self.graph()
        src = next(e.id for e in graph.edges if (5.0, 1.0) in graph.edge_coords(e.id))
        plan = graph.plan_align(src, graph.targets(src)[0][0])
        self.assertTrue(plan.ok, plan.error)
        new = build_changes(plan, graph, ctx)[source.id()][0][1]
        self.assertEqual([(v.x(), v.y(), v.z()) for v in new.vertices()][:3], [(-5, 0, 0), (-5, 0, 9), (0, 0, 9)])

    def test_edge_running_out_of_the_loaded_area_is_blocked(self):
        """A neighbour sharing the off-screen part isn't loaded and would be left behind."""
        source = self.layer('long', 'POLYGON((0 0,0 200,-50 200,-50 0,0 0))', 'Polygon')
        self.layer('neighbour', 'POLYGON((0 100,0 150,20 150,20 100,0 100))', 'Polygon')
        self.layer('target', 'LINESTRING(0 0,1 90,0 200)')
        graph, ctx = self.graph()
        src = graph.hit_test(0, 20, 0.1)[0]
        tgt = graph.targets(src)[0][0]
        plan = graph.plan_align(src, tgt)
        self.assertFalse(plan.ok)
        self.assertIn('zoom out', plan.error)
        self.canvas.setExtent(QgsRectangle(-300, -100, 300, 300))
        graph, ctx = self.graph()
        src = graph.hit_test(0, 20, 0.1)[0]
        self.assertEqual(graph.targets(src), [])            # the neighbour's corner is now a node

    def test_ring_keeps_its_starting_vertex(self):
        from align_edge.edge_writer import build_changes
        for start in ((0, 0), (10, 0), (10, 10), (0, 10)):
            with self.subTest(start=start):
                ring = [(0, 0), (10, 0), (10, 10), (0, 10)]
                k = ring.index(start)
                ring = ring[k:] + ring[:k + 1]
                source = self.layer('source', 'POLYGON((%s))' % ','.join('%d %d' % p for p in ring), 'Polygon')
                target = self.layer('target', 'LINESTRING(10 0,11 5,10 10)')
                graph, ctx = self.graph()
                src = graph.hit_test(10, 5, 0.1)[0]
                plan = graph.plan_align(src, graph.targets(src)[0][0])
                new = build_changes(plan, graph, ctx)[source.id()][0][1]
                first = list(new.vertices())[0]
                self.assertEqual((first.x(), first.y()), start)
                self.assertAlmostEqual(new.area(), 105)
                self.caches.pop().clear()
                self.canvas.setLayers([])
                for layer in (source, target):
                    self.layers.remove(layer)
                    self.project.removeMapLayer(layer.id())

    def test_straight_geometry_in_curve_layers_is_supported(self):
        from align_edge.edge_writer import apply_changes, build_changes
        from qgis.core import QgsVectorFileWriter
        for kind, wkt in (('CompoundCurve', 'COMPOUNDCURVE((0 0,5 1,10 0))'),
                          ('MultiCurve', 'MULTICURVE(COMPOUNDCURVE((0 0,5 1,10 0)))'),
                          ('MultiSurface', 'MULTISURFACE(CURVEPOLYGON(COMPOUNDCURVE((0 0,5 1,10 0,5 -9,0 0))))')):
            with self.subTest(kind=kind):
                memory = self.layer(kind, wkt, kind, editable=False)
                path = str(Path(tempfile.mkdtemp()) / 'curves.gpkg')
                options = QgsVectorFileWriter.SaveVectorOptions()
                QgsVectorFileWriter.writeAsVectorFormatV3(memory, path, self.project.transformContext(), options)
                self.layers.remove(memory)
                self.project.removeMapLayer(memory.id())
                source = QgsVectorLayer(path, kind, 'ogr')
                self.project.addMapLayer(source)
                self.layers.append(source)
                source.startEditing()
                target = self.layer('target', 'LINESTRING(0 0,5 2,10 0)')
                graph, ctx = self.graph()
                src = next(e.id for e in graph.edges if (5.0, 1.0) in graph.edge_coords(e.id))
                plan = graph.plan_align(src, graph.targets(src)[0][0])
                self.assertTrue(plan.ok, plan.error)
                apply_changes(build_changes(plan, graph, ctx), ctx)
                self.assertTrue(source.commitChanges(), source.commitErrors())
                self.assertEqual(self.geometry(source).wkbType(), source.wkbType())
                self.assertIn((5, 2), [(v.x(), v.y()) for v in self.vertices(source)])
                self.caches.pop().clear()
                self.canvas.setLayers([])
                for layer in (source, target):
                    self.layers.remove(layer)
                    self.project.removeMapLayer(layer.id())

    def test_layer_crs_change_drops_the_cached_coordinates(self):
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        self.layer('target', 'LINESTRING(0 0,5 2,10 0)')
        self.graph()
        cache = self.caches[-1]
        source.setCrs(QgsCoordinateReferenceSystem('EPSG:26918'))
        cache.sync_layers()
        self.assertFalse(cache.layers[source.id()].identity)
        self.assertTrue(cache.needs_read())

    def test_reference_filter_change_is_reread(self):
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        path = str(Path(tempfile.mkdtemp()) / 'ref.gpkg')
        from qgis.core import QgsVectorFileWriter
        QgsVectorFileWriter.writeAsVectorFormatV3(source, path, self.project.transformContext(),
                                                  QgsVectorFileWriter.SaveVectorOptions())
        reference = QgsVectorLayer(path, 'reference', 'ogr')
        self.project.addMapLayer(reference)
        self.layers.append(reference)
        self.settings.references.add(reference.id())
        self.graph()
        self.assertFalse(self.caches[-1].needs_read())
        reference.setSubsetString('fid < 0')
        self.assertTrue(self.caches[-1].needs_read())

    def test_unreadable_coordinates_skip_the_feature_not_the_layer(self):
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        bad = QgsFeature()
        bad.setGeometry(QgsGeometry(QgsLineString([1, float('nan')], [1, 2])))
        source.addFeature(bad)
        graph, ctx = self.graph()
        self.assertEqual(len(graph.parts), 1)

    def tool(self):
        from unittest.mock import Mock
        from align_edge.align_edge_tool import AlignEdgeTool
        iface = Mock()
        iface.mapCanvas.return_value = self.canvas
        tool = AlignEdgeTool(iface, self.settings)
        tool.overlay = Mock()
        self.addCleanup(tool.deleteLater)
        return tool

    def hover(self, tool, x, y):
        from qgis.core import QgsPointXY
        from qgis.PyQt.QtCore import QPoint
        tool.graph, tool.ctx = self.graph()
        tool._stale = False
        tool._last = (QgsPointXY(x, y), QPoint(1, 1))
        tool._update_hover()
        return tool.overlay.tip.show_at.call_args[0][0]

    def test_hover_explains_conflicting_target_owners(self):
        self.layer('a', 'POLYGON((0 0,10 0,10 -10,0 -10,0 0))', 'Polygon')
        self.layer('d', 'LINESTRING(0 0,5 2,10 0)')
        self.layer('b', 'LINESTRING(0 0,5 2.009,10 0)')
        self.layer('c', 'LINESTRING(0 0,5 1.991,10 0)')
        tip = self.hover(self.tool(), 5, 0)
        self.assertNotIn('Error', tip)
        self.assertIn('different coordinates', tip)

    def test_hover_does_not_offer_what_a_click_would_refuse(self):
        self.layer('source', 'LINESTRING(0 0,5 -1,10 0)')
        target = self.layer('curve', 'CIRCULARSTRING(0 0,5 2,10 0)', 'CircularString', False)
        self.settings.references.add(target.id())
        tip = self.hover(self.tool(), 5, -1)
        self.assertNotIn('Click to align', tip)
        self.assertIn('curved', tip)

    def test_dragging_is_not_a_click(self):
        from unittest.mock import Mock
        from qgis.PyQt.QtCore import QPoint
        tool = self.tool()
        tool._click = Mock()
        tool._stale, tool.graph = False, object()

        def event(x):
            return SimpleNamespace(button=lambda: Qt.MouseButton.LeftButton, mapPoint=lambda: None,
                                   pixelPoint=lambda: QPoint(x, 5))
        tool.canvasPressEvent(event(5))
        tool.canvasReleaseEvent(event(200))
        self.assertFalse(tool._click.called)
        tool.canvasPressEvent(event(5))
        tool.canvasReleaseEvent(event(6))
        self.assertTrue(tool._click.called)

    def test_roles_of_unavailable_layers_survive_the_dialog(self):
        from align_edge.settings_dialog import SettingsDialog
        broken = QgsVectorLayer('/nowhere/survey.gpkg', 'survey', 'ogr')
        self.project.addMapLayer(broken)
        self.layers.append(broken)
        self.settings.references.update({broken.id(), 'gone'})
        dialog = SettingsDialog(self.settings)
        dialog.accept()
        self.assertEqual(self.settings.references, {broken.id()})
        dialog.deleteLater()

    def test_settings_survive_bad_values_and_tolerance_belongs_to_the_project(self):
        from qgis.core import QgsSettings
        from align_edge.settings import Settings, _DEFAULTS
        s = Settings()
        store = QgsSettings()
        old = store.value('AlignEdge/line_width')
        try:
            store.setValue('AlignEdge/line_width', 'abc')
            self.assertEqual(s.get('line_width'), _DEFAULTS['line_width'])
            self.project.clear()
            s.set('cluster_tolerance', 0.25)
            self.assertEqual(s.get('cluster_tolerance'), 0.25)
            self.project.clear()
            self.assertEqual(s.get('cluster_tolerance'), 0.0)   # 0.25 ft must not become 0.25 degrees
        finally:
            store.remove('AlignEdge/cluster_tolerance')
            store.remove('AlignEdge/line_width') if old is None else store.setValue('AlignEdge/line_width', old)

    def test_zooming_past_a_scale_limit_does_not_switch_the_tool_off(self):
        from align_edge.plugin import AlignEdgePlugin
        layer = self.layer('source', 'LINESTRING(0 0,10 0)')
        layer.setScaleBasedVisibility(True)
        layer.setMinimumScale(1)
        self.canvas.setLayers([layer])
        plugin = AlignEdgePlugin(SimpleNamespace(mapCanvas=lambda: self.canvas))
        plugin.settings = self.settings
        state = []
        plugin.action = SimpleNamespace(setEnabled=state.append)
        plugin.update_enabled()
        self.assertEqual(state, [True])

    def test_zm_is_interpolated_from_source_not_2d_or_target_elevations(self):
        from align_edge.edge_writer import build_changes
        source = self.layer('source ZM', 'LINESTRING ZM(0 0 100 1000,5 1 150 1500,10 0 200 2000)', 'LineStringZM')
        target = self.layer('2D target', 'LINESTRING(0 0,2.5 2,5 2,7.5 2,10 0)')
        graph, ctx = self.graph()
        changes = build_changes(self.plan(graph, source, target), graph, ctx)
        result = changes[source.id()][0][1]
        points = list(result.vertices())
        self.assertTrue(QgsWkbTypes.hasZ(result.wkbType()))
        self.assertTrue(QgsWkbTypes.hasM(result.wkbType()))
        self.assertEqual((points[0].z(), points[-1].z()), (100, 200))
        self.assertEqual((points[0].m(), points[-1].m()), (1000, 2000))
        self.assertAlmostEqual(points[2].z(), 150)
        self.assertAlmostEqual(points[2].m(), 1500)
        self.assertTrue(all(100 <= point.z() <= 200 for point in points))

    def test_m_only_dimension_and_unknown_z_are_not_zeroed(self):
        from align_edge.edge_writer import build_changes, _point
        source = self.layer('source M', 'LINESTRING M(0 0 10,5 1 20,10 0 30)', 'LineStringM')
        target = self.layer('target', 'LINESTRING(0 0,5 2,10 0)')
        graph, ctx = self.graph()
        result = build_changes(self.plan(graph, source, target), graph, ctx)[source.id()][0][1]
        self.assertFalse(QgsWkbTypes.hasZ(result.wkbType()))
        self.assertTrue(QgsWkbTypes.hasM(result.wkbType()))
        self.assertEqual([p.m() for p in result.vertices()], [10, 20, 30])
        unknown = _point((1, 2, float('nan'), float('nan')), True, True)
        self.assertTrue(math.isnan(unknown.z()))
        self.assertTrue(math.isnan(unknown.m()))

    def test_target_change_after_snapshot_is_rejected(self):
        from align_edge.edge_writer import AlignFailed, build_changes
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        target = self.layer('target', 'LINESTRING(0 0,5 2,10 0)')
        graph, ctx = self.graph()
        plan = self.plan(graph, source, target)
        fid = next(target.getFeatures()).id()
        target.changeGeometry(fid, QgsGeometry.fromWkt('LINESTRING(0 0,5 3,10 0)'))
        with self.assertRaisesRegex(AlignFailed, 'changed'):
            build_changes(plan, graph, ctx)

    def test_reference_in_another_crs_is_transformed_into_the_source_crs(self):
        from align_edge.edge_writer import build_changes
        web = QgsCoordinateReferenceSystem('EPSG:3857')
        geo = QgsCoordinateReferenceSystem('EPSG:4326')
        self.canvas.setDestinationCrs(web)
        self.canvas.setExtent(QgsRectangle(-20, -20, 40, 40))
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        source.setCrs(web)
        geometry = QgsGeometry.fromWkt('LINESTRING(0 0,5 2,10 0)')
        geometry.transform(QgsCoordinateTransform(web, geo, self.project))
        target = self.layer('geographic reference', geometry.asWkt(17), editable=False)
        target.setCrs(geo)
        self.settings.references.add(target.id())
        graph, ctx = self.graph()
        result = build_changes(self.plan(graph, source, target), graph, ctx)[source.id()][0][1]
        expected = self.geometry(target)
        expected.transform(QgsCoordinateTransform(geo, web, self.project))
        for a, b in zip(result.vertices(), expected.vertices()):
            self.assertAlmostEqual(a.x(), b.x(), places=9)
            self.assertAlmostEqual(a.y(), b.y(), places=9)

    def test_curved_target_is_rejected_rather_than_written_as_a_straight_approximation(self):
        from align_edge.edge_writer import AlignFailed, build_changes
        source = self.layer('source', 'LINESTRING(0 0,5 -1,10 0)')
        target = self.layer('curved reference', 'CIRCULARSTRING(0 0,5 2,10 0)', 'CircularString', False)
        self.settings.references.add(target.id())
        graph, ctx = self.graph()
        with self.assertRaisesRegex(AlignFailed, 'curved targets'):
            build_changes(self.plan(graph, source, target), graph, ctx)

    def test_reference_stays_locked_even_if_layer_is_in_edit_mode(self):
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        target = self.layer('reference', 'LINESTRING(0 0,5 2,10 0)')
        self.settings.references.add(target.id())
        graph, ctx = self.graph()
        plan = self.plan(graph, source, target)
        reverse = graph.plan_align(plan.tgt.id, plan.src.id)
        self.assertFalse(reverse.ok)
        self.assertIn('target-only', reverse.error)

    def test_shared_polygon_boundary_is_written_for_both_owners(self):
        from align_edge.edge_writer import apply_changes, build_changes
        a = self.layer('west', 'POLYGON((0 0,10 0,10 10,0 10,0 0))', 'Polygon')
        b = self.layer('east', 'POLYGON((10 0,20 0,20 10,10 10,10 0))', 'Polygon')
        target = self.layer('survey', 'LINESTRING(10 0,9 5,10 10)', editable=False)
        self.settings.references.add(target.id())
        graph, ctx = self.graph()
        src = next(e.id for e in graph.edges if {a.id(), b.id()} == {key[0] for key in e.owners})
        tgt = next(e.id for e in graph.edges if any(key[0] == target.id() for key in e.owners))
        plan = graph.plan_align(src, tgt)
        self.assertTrue(plan.ok, plan.error)
        changes = build_changes(plan, graph, ctx)
        self.assertEqual(set(changes), {a.id(), b.id()})
        apply_changes(changes, ctx)
        self.assertTrue(self.geometry(a).isGeosValid())
        self.assertTrue(self.geometry(b).isGeosValid())
        self.assertAlmostEqual(self.geometry(a).intersection(self.geometry(b)).area(), 0)
        self.assertAlmostEqual(self.geometry(a).area() + self.geometry(b).area(), 200)
        for layer in (a, b):
            self.assertIn((9, 5), [(p.x(), p.y()) for p in self.vertices(layer)])

    def test_excluded_reference_is_ignored_and_reference_only_does_not_enable_tool(self):
        from align_edge.graph_builder import participating_layers
        from align_edge.plugin import AlignEdgePlugin
        reference = self.layer('reference', 'LINESTRING(0 0,10 0)', editable=False)
        self.canvas.setLayers([reference])
        self.settings.references.add(reference.id())
        self.assertEqual(participating_layers(self.canvas, self.settings), [reference])
        plugin = AlignEdgePlugin(SimpleNamespace(mapCanvas=lambda: self.canvas))
        plugin.settings = self.settings
        state = []
        plugin.action = SimpleNamespace(setEnabled=state.append)
        plugin.update_enabled()
        self.assertEqual(state, [False])
        self.settings.excluded.add(reference.id())
        self.assertEqual(participating_layers(self.canvas, self.settings), [])

    def test_dialog_accept_saves_reference_role(self):
        from align_edge.settings_dialog import SettingsDialog
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        reference = self.layer('reference', 'LINESTRING(0 0,5 2,10 0)', editable=False)
        dialog = SettingsDialog(self.settings)
        for row in range(dialog.layers.rowCount()):
            if dialog.layers.item(row, 0).data(Qt.ItemDataRole.UserRole) == reference.id():
                role = dialog.layers.cellWidget(row, 1)
                role.setCurrentIndex(role.findData('reference'))
        dialog.accept()
        self.assertEqual(self.settings.references, {reference.id()})
        self.assertNotIn(source.id(), self.settings.references)
        dialog.deleteLater()

    def test_write_failures_rollback_both_layers_but_preserve_prior_edits(self):
        from align_edge.edge_writer import AlignFailed, apply_changes
        a = self.layer('first', 'LINESTRING(0 0,10 0)')
        b = self.layer('second', 'LINESTRING(0 1,10 1)')
        aid, bid = next(a.getFeatures()).id(), next(b.getFeatures()).id()
        a.beginEditCommand('Earlier user edit')
        a.changeGeometry(aid, QgsGeometry.fromWkt('LINESTRING(0 2,10 2)'))
        a.endEditCommand()
        originals = [self.geometry(layer).asWkt() for layer in (a, b)]

        class FailingLayer:
            def __init__(self, layer, failure): self.layer, self.failure = layer, failure
            def __getattr__(self, name): return getattr(self.layer, name)
            def changeGeometry(self, fid, geometry):
                if self.failure == 'false': return False
                if self.failure == 'raise': raise RuntimeError('write failed')
                return self.layer.changeGeometry(fid, geometry)
            def endEditCommand(self):
                self.layer.endEditCommand()
                raise RuntimeError('failure after endEditCommand')

        for failure in ('false', 'raise', 'end'):
            with self.subTest(failure=failure):
                ctx = SimpleNamespace(layers={'a': a, 'b': FailingLayer(b, failure)})
                changes = {'a': [(aid, QgsGeometry.fromWkt('LINESTRING(0 3,10 3)'))],
                           'b': [(bid, QgsGeometry.fromWkt('LINESTRING(0 4,10 4)'))]}
                with self.assertRaises(AlignFailed):
                    apply_changes(changes, ctx)
                self.assertEqual([self.geometry(layer).asWkt() for layer in (a, b)], originals)
                self.assertFalse(a.isEditCommandActive())
                self.assertFalse(b.isEditCommandActive())
                self.assertEqual(a.undoStack().index(), 1)

    def test_active_user_edit_command_is_not_destroyed(self):
        from align_edge.edge_writer import AlignFailed, apply_changes
        layer = self.layer('source', 'LINESTRING(0 0,10 0)')
        fid = next(layer.getFeatures()).id()
        layer.beginEditCommand('User command')
        with self.assertRaises(AlignFailed):
            apply_changes({'source': [(fid, QgsGeometry.fromWkt('LINESTRING(0 1,10 1)'))]},
                          SimpleNamespace(layers={'source': layer}))
        self.assertTrue(layer.isEditCommandActive())
        layer.destroyEditCommand()

    def test_escape_cancels_queued_edit_and_stale_build_is_discarded(self):
        from align_edge.align_edge_tool import AlignEdgeTool
        tool = AlignEdgeTool(SimpleNamespace(mapCanvas=lambda: self.canvas), self.settings)
        tool._src = 1
        tool._pending_click = ('queued', None)
        tool._run_click_after_build = True
        tool.keyPressEvent(SimpleNamespace(key=lambda: Qt.Key.Key_Escape, accept=lambda: None))
        self.assertIsNone(tool._pending_click)
        self.assertFalse(tool._run_click_after_build)
        tool._job = ('obsolete',)
        old_generation = tool._gen
        tool._on_data_changed()
        self.assertIsNone(tool._job)
        self.assertGreater(tool._gen, old_generation)
        tool.cleanup()
        tool.deleteLater()

    def test_restore_defaults_then_cancel_does_not_change_saved_settings(self):
        from align_edge.settings_dialog import SettingsDialog
        from align_edge.settings import _DEFAULTS
        layer = self.layer('reference', 'LINESTRING(0 0,10 0)')
        self.settings.values['line_width'] = 8
        self.settings.references.add(layer.id())
        before = dict(self.settings.values)
        dialog = SettingsDialog(self.settings)
        dialog._defaults()
        dialog.reject()
        self.assertEqual(self.settings.values, before)
        self.assertEqual(self.settings.references, {layer.id()})
        dialog.deleteLater()
        dialog = SettingsDialog(self.settings)
        dialog._defaults()
        dialog.accept()
        self.assertEqual(self.settings.values, _DEFAULTS)
        self.assertEqual(self.settings.references, set())
        dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
