"""Real QGIS checks for safe writes, exact XY, dimensions, references and UI state."""
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsProject, QgsRectangle, QgsVectorLayer, QgsWkbTypes
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
        return next(layer.getFeatures()).geometry()

    def graph(self):
        from align_edge.graph_builder import FeatureCache, build_from_snapshot
        self.canvas.setLayers(self.layers)
        cache = FeatureCache(self.canvas, self.settings, lambda reason: None)
        self.caches.append(cache)
        self.assertTrue(cache.sync_layers())
        cache.read()
        return build_from_snapshot(cache.snapshot(0.01))

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

    def test_endpoint_move_that_disconnects_another_edge_is_blocked(self):
        source = self.layer('source', 'LINESTRING(0 0,5 1,10 0)')
        target = self.layer('target', 'LINESTRING(0.005 0,5 2,10.005 0)')
        self.layer('incident', 'LINESTRING(0 0,-5 0)')
        graph, ctx = self.graph()
        src = next(e.id for e in graph.edges if any(k[0] == source.id() for k in e.owners))
        tgt = next(e.id for e in graph.edges if any(k[0] == target.id() for k in e.owners))
        plan = graph.plan_align(src, tgt)
        self.assertFalse(plan.ok)
        self.assertIn('disconnect', plan.error)

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
            self.assertIn((9, 5), [(p.x(), p.y()) for p in self.geometry(layer).vertices()])

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
