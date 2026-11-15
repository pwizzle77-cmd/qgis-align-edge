# -*- coding: utf-8 -*-
"""The Align Edge map tool: hover to preview, single click to align.

Threading / data-access rules (v1.3):
  * No worker threads. The topology graph is built in short (~25 ms) slices
    driven by a QTimer, so QGIS stays responsive.
  * Layer data is read into a FeatureCache only when needed (tool start,
    view leaves the cached area, commit/rollback) and ONLY while the map
    canvas is not drawing and no snap index is building. There is no time
    limit on that wait. v1.2.0 froze by reading behind a render that never
    finished. v1.3.1 also waits to *build* during that time: the debug log
    showed a stuck redraw plus a full topology rebuild every 200 ms, which
    pegged the main thread. A redraw still busy after 2 s is cancelled and
    painted once. That cancel is not permission to read.
  * While the tool is on, the canvas draws layers one at a time and preview
    redraws are paused. Both flags are in-memory and put back on deactivate.
    Several edited layers in one GeoPackage otherwise share one parallel
    redraw and that redraw never finishes.
  * Edits, undo and panning are handled from the cache, which is kept
    current from the layers' edit signals - no provider reads.
  * After an edit the graph is "stale": clicks are ignored (busy cursor,
    "Updating topology..." tooltip) until the rebuilt graph is ready.
    A click made while the map is drawing is still run when the map is idle.
  * Every state change is written to align_edge_debug.log (diag.py).
"""

import html
import time

from qgis.core import (
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import QEvent, QObject, Qt, QTimer
from qgis.PyQt.QtGui import QCursor

from . import diag
from .compat import MSG_WARNING
from .edge_writer import AlignFailed, apply_changes, build_changes
from .graph_builder import BuildError, FeatureCache, finish_context, graph_for_snapshot
from .overlay import Overlay
from .render_guard import (
    MAX_RENDER_RECOVERIES,
    should_cancel_stuck_draw,
    should_defer_layer_work,
)

WAIT_LOG_EVERY = 10.0   # while waiting for the map, log every N seconds
SLICE_SECONDS = 0.025   # build work per event-loop turn
REFRESH_AFTER_CANCEL_MS = 400

MAX_NAMES = 4


def snap_index_busy(canvas):
    """True when a snap locator is reading a layer. Must not wait for it."""
    if canvas is None:
        return False
    try:
        utils = canvas.snappingUtils()
    except Exception:
        return False
    if utils is None:
        return False
    layers = []
    try:
        layers = [cfg.layer for cfg in (utils.layers() or [])
                  if cfg is not None and getattr(cfg, "layer", None) is not None]
    except Exception:
        layers = []
    if not layers:
        try:
            current = utils.currentLayer()
            if current is not None:
                layers = [current]
        except Exception:
            layers = []
    for layer in layers:
        try:
            locator = utils.locatorForLayer(layer)
        except Exception as err:
            diag.log_once(("snap", "locator"), "snap locator lookup failed: {!r}".format(err))
            continue
        try:
            if locator is not None and locator.isIndexing():
                return True
        except Exception as err:
            diag.log_once(("snap", "indexing"), "snap locator state unavailable: {!r}".format(err))
            continue
    return False


class _KeyFilter(QObject):
    """Grab Tab before Qt uses it for focus navigation."""

    def __init__(self, tool):
        super().__init__()
        self.tool = tool

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.tool.cycle_target(-1 if event.key() == Qt.Key.Key_Backtab else 1)
            return True
        if et in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
            if self.tool.overlay is not None:
                self.tool.overlay.tip.hide()
        return False


class AlignEdgeTool(QgsMapTool):

    def __init__(self, iface, settings):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.settings = settings
        self.overlay = None
        self.graph = None
        self.ctx = None
        self.build_error = None
        self.cache = None

        self._stale = False          # data changed since self.graph was built
        self._gen = 0                # generation of the build in progress
        self._job = None             # (gen, graph, snapshot, steps generator, t0)
        self._wait_since = None      # when we started waiting for the map
        self._wait_logged = 0.0
        self._pending_click = None   # click made while the map was drawing
        self._active = False
        self._render_t0 = None
        self._jobs_held = False
        self._parallel_was = True
        self._preview_was = None
        self._preview_pause = False  # one beat after cancelling preview jobs
        self._hold_render = False    # a stuck draw was cancelled; do not read yet
        self._await_refresh = False  # replacement redraw has been requested
        self._await_since = None
        self._render_recoveries = 0
        self._render_cap_logged = False
        self._rebuild_owed = False
        self._run_click_after_build = False

        self._timer = QTimer()       # debounce for rebuild requests
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._start_build_when_idle)
        self._step_timer = QTimer()  # drives the sliced build
        self._step_timer.setSingleShot(True)
        self._step_timer.setInterval(0)
        self._step_timer.timeout.connect(self._step)
        self._wait_timer = QTimer()  # re-checks the canvas while waiting
        self._wait_timer.setSingleShot(True)
        self._wait_timer.setInterval(200)
        self._wait_timer.timeout.connect(self._on_wait_tick)
        self._refresh_timer = QTimer()  # one redraw after a cancelled stuck draw
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_AFTER_CANCEL_MS)
        self._refresh_timer.timeout.connect(self._refresh_after_cancel)
        self._key_filter = _KeyFilter(self)
        self._layers = []
        self._last = None            # (map point, pixel point)
        self._reset_hover()
        self._set_busy(False)

    # ------------------------------------------------------------ lifecycle
    def activate(self):
        super().activate()
        diag.set_enabled(self.settings.get("diagnostic_log"))
        diag.log("=== activate (canvas drawing={})".format(self.canvas.isDrawing()))
        self._active = True
        if self.overlay is None:
            self.overlay = Overlay(self.canvas, self.settings)
        self.cache = FeatureCache(self.canvas, self.settings, self._on_cache_change)
        self.cache.log = diag.log
        self._hold_canvas_jobs(True)
        self.canvas.extentsChanged.connect(self._on_extent)
        self.canvas.layersChanged.connect(self._on_data_changed)
        self.canvas.destinationCrsChanged.connect(self._on_data_changed)
        self.canvas.mapCanvasRefreshed.connect(self._on_canvas_idle)
        self.canvas.renderStarting.connect(self._on_render_start)
        QgsProject.instance().layersAdded.connect(self._watch_layers)
        QgsProject.instance().layersWillBeRemoved.connect(self._layers_removed)
        self.canvas.installEventFilter(self._key_filter)
        self._watch_layers()
        self._stale = True
        self._set_busy(True)
        # Preview jobs do not set isDrawing, and they keep the dataset open.
        # Cancel them only when the visible map is idle, then wait one beat
        # before the first feature read.
        self._preview_pause = False
        try:
            if not self.canvas.isDrawing():
                self.canvas.stopRendering()
                self._preview_pause = True
        except Exception as err:
            diag.log("could not cancel preview redraw on start: {!r}".format(err))
        self._start_build_when_idle()

    def deactivate(self):
        diag.log("=== deactivate")
        self._active = False
        for t in (self._timer, self._step_timer, self._wait_timer, self._refresh_timer):
            t.stop()
        self._release_render_wait()
        self._hold_canvas_jobs(False)
        self._job = None
        for sig, slot in ((self.canvas.extentsChanged, self._on_extent),
                          (self.canvas.layersChanged, self._on_data_changed),
                          (self.canvas.destinationCrsChanged, self._on_data_changed),
                          (self.canvas.mapCanvasRefreshed, self._on_canvas_idle),
                          (self.canvas.renderStarting, self._on_render_start),
                          (QgsProject.instance().layersAdded, self._watch_layers),
                          (QgsProject.instance().layersWillBeRemoved, self._layers_removed)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.canvas.removeEventFilter(self._key_filter)
        self._unwatch_layers()
        if self.cache is not None:
            self.cache.clear()
            self.cache = None
        if self.overlay is not None:
            self.overlay.clear_all()
        self.graph = self.ctx = None
        self._wait_since = None
        self._pending_click = None
        self._reset_hover()
        super().deactivate()

    def cleanup(self):
        """Plugin unload: remove canvas items, close the log."""
        for t in (self._timer, self._step_timer, self._wait_timer, self._refresh_timer):
            t.stop()
        self._release_render_wait()
        self._hold_canvas_jobs(False)
        self._job = None
        if self.overlay is not None:
            self.overlay.remove()
            self.overlay = None
        diag.close()

    def restyle(self):
        diag.set_enabled(self.settings.get("diagnostic_log"))
        if self.overlay is not None:
            self.overlay.apply_style()
            self.overlay.show_graph(self.graph)
        if self._active:
            self._on_data_changed()

    # ------------------------------------------------------------ cursor
    def _set_busy(self, busy):
        self.setCursor(QCursor(Qt.CursorShape.BusyCursor if busy else Qt.CursorShape.CrossCursor))

    # ------------------------------------------------------------ layer watch
    def _watch_layers(self, *args):
        self._unwatch_layers()
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer):
                lyr.editingStarted.connect(self._on_data_changed)
                lyr.editingStopped.connect(self._on_data_changed)
                self._layers.append(lyr)

    def _unwatch_layers(self):
        for lyr in self._layers:
            for sig in (lyr.editingStarted, lyr.editingStopped):
                try:
                    sig.disconnect(self._on_data_changed)
                except (TypeError, RuntimeError):
                    pass
        self._layers = []

    def _layers_removed(self, ids):
        self._unwatch_layers()
        QTimer.singleShot(0, self._watch_layers)
        self._on_data_changed()

    # ------------------------------------------------------------ canvas state
    def _on_render_start(self, *args):
        self._render_t0 = time.perf_counter()
        diag.log("map render started")

    def _render_age(self):
        return 0.0 if self._render_t0 is None else time.perf_counter() - self._render_t0

    # ------------------------------------------------------------ build scheduling
    def _on_extent(self, *args):
        # data unchanged: keep using the current graph while a new one builds
        self._rebuild_owed = True
        self._timer.start()

    def _on_data_changed(self, *args):
        # data changed: current graph must not be used for edits any more
        if not self._stale:
            diag.log("data changed -> topology stale")
        self._stale = True
        self._set_busy(True)
        self._pending_click = None
        self._run_click_after_build = False
        self._rebuild_owed = True
        if self.overlay is not None:
            self.overlay.clear_hover()
        self._reset_hover()
        self._timer.start()

    def _on_cache_change(self, reason):
        """A participating layer's features changed (edit, undo, ...)."""
        self._on_data_changed()

    def schedule_rebuild(self, *args):
        self._on_data_changed()

    def _canvas_drawing(self):
        try:
            return bool(self.canvas.isDrawing())
        except Exception:
            return False

    def _map_busy(self):
        """True while a redraw, a snap index, or a cancelled redraw owns the layers.

        No time limit, and no "read anyway". v1.2.0 froze by reading after
        two seconds. Asking here must not wait on the redraw or the index.
        """
        if self._preview_pause or self._hold_render:
            if self._wait_since is None:
                self._wait_since = time.perf_counter()
            return True
        drawing = self._canvas_drawing()
        # A draw is enough. Do not touch locators while the canvas job is open.
        indexing = False if drawing else snap_index_busy(self.canvas)
        if not should_defer_layer_work(drawing, indexing, False):
            if self._wait_since is not None:
                diag.log("map finished drawing after waiting {:.1f} s".format(
                    time.perf_counter() - self._wait_since))
            return False
        now = time.perf_counter()
        if self._wait_since is None:
            self._wait_since = now
            self._wait_logged = now
            diag.log("map is drawing - waiting" if drawing else "snap index busy - waiting")
        elif now - self._wait_logged >= WAIT_LOG_EVERY:
            self._wait_logged = now
            diag.log("still waiting for the map: {:.0f} s (render age {:.0f} s)".format(
                now - self._wait_since, self._render_age()))
        return True

    def _start_build_when_idle(self):
        if not self._active or self.cache is None:
            return
        # Build as well as read. A stuck redraw used to start a full topology
        # build on every wait tick and the main thread never came back.
        if self._map_busy():
            self._set_busy(True)
            self._wait_timer.start()
            return
        self._wait_since = None
        self._render_recoveries = 0
        self._render_cap_logged = False
        try:
            has_layers = self.cache.sync_layers()
        except Exception as e:
            diag.log("sync layers failed: {!r}".format(e))
            has_layers = False
        if not has_layers:
            self._install(None, None, "No visible line or polygon layer is being edited.")
            return
        if self.cache.needs_read():
            if self._map_busy():
                self._set_busy(True)
                self._wait_timer.start()
                return
            self._wait_since = None
            try:
                with diag.watchdog("read layers"):
                    self._read_layers()
            except BuildError as e:
                diag.log("read: {}".format(e))
                self._install(None, None, str(e))
                return
            except Exception as e:
                diag.log("read failed: {!r}".format(e))
                self._install(None, None, "Could not read layers: {}".format(e))
                return
        self._start_build()

    def _read_layers(self):
        """Read the cache while a new map redraw cannot open the same dataset."""
        canvas = self.canvas
        frozen = False
        already = False
        try:
            already = bool(canvas.isFrozen())
        except Exception:
            already = False
        if not already:
            try:
                canvas.freeze(True)
                frozen = True
            except Exception:
                frozen = False
        try:
            self.cache.read()
        finally:
            if frozen:
                try:
                    canvas.freeze(False)
                except Exception as err:
                    diag.log("could not unfreeze the map canvas: {!r}".format(err))
                # freeze() drops a redraw that was requested during the read.
                try:
                    canvas.refresh()
                except Exception as err:
                    diag.log("map refresh after reading layers failed: {!r}".format(err))

    def _on_wait_tick(self):
        if not self._active:
            return
        if self._preview_pause:
            self._preview_pause = False
        if self._await_refresh and not self._canvas_drawing():
            since = self._await_since if self._await_since is not None else self._wait_since
            if since is not None and time.perf_counter() - since >= 2.0:
                diag.log("refresh after stuck draw gave no image")
                self._await_refresh = False
                self._hold_render = False
                self._await_since = None
        self._recover_stuck_render()
        self._dispatch_idle_work()

    def _recover_stuck_render(self):
        """Cancel one redraw that has stayed busy. Do not read features here."""
        if not self._active or self._wait_since is None:
            return
        elapsed = time.perf_counter() - self._wait_since
        drawing = self._canvas_drawing()
        holding = self._hold_render and not self._await_refresh
        if not should_cancel_stuck_draw(
                elapsed, drawing, self._render_recoveries, holding):
            if (drawing and self._render_recoveries >= MAX_RENDER_RECOVERIES
                    and elapsed >= 2.0 and not self._render_cap_logged):
                self._render_cap_logged = True
                diag.log("map still drawing after {} cancels".format(MAX_RENDER_RECOVERIES))
            return
        self._render_recoveries += 1
        self._hold_render = True
        self._await_refresh = False
        self._await_since = None
        self._wait_since = time.perf_counter()
        diag.log("map draw stuck {:.1f} s - cancelling ({})".format(
            elapsed, self._render_recoveries))
        try:
            self.canvas.setParallelRenderingEnabled(False)
        except Exception as err:
            diag.log("could not turn off parallel rendering: {!r}".format(err))
        try:
            self.canvas.stopRendering()
            diag.log("stopped stuck map draw")
        except Exception as e:
            diag.log("stop stuck map draw failed: {!r}".format(e))
        self._refresh_timer.start()

    def _refresh_after_cancel(self):
        """One redraw after the cancelled workers have had a beat to let go."""
        if not self._active or not self._hold_render:
            return
        if self._canvas_drawing():
            self._wait_timer.start()
            return
        self._await_refresh = True
        self._await_since = time.perf_counter()
        try:
            self.canvas.refresh()
            diag.log("refresh after stuck draw")
        except Exception as e:
            diag.log("refresh after stuck draw failed: {!r}".format(e))
            self._await_refresh = False
            self._hold_render = False
            self._await_since = None
            self._wait_timer.start()

    def _on_canvas_idle(self, *args):
        if not self._active:
            return
        # stopRendering can emit this before the replacement image exists.
        if self._hold_render and not self._await_refresh:
            diag.log("map render finished during cancel ({:.2f} s)".format(self._render_age()))
            return
        diag.log("map render finished ({:.2f} s)".format(self._render_age()))
        self._render_t0 = None
        if self._await_refresh:
            self._await_refresh = False
            self._hold_render = False
            self._await_since = None
        self._dispatch_idle_work()
        if (self._pending_click is None and not self._rebuild_owed
                and self._src is not None and self._last is not None and not self._stale):
            # labels can be fetched now that the map is idle
            self._render(self._last[1])

    def _dispatch_idle_work(self):
        """Run a stored click on a current graph, then build if one is owed.

        A click stored during a pan waits for the build of the settled view.
        A click stored while the view did not change runs on the graph already
        on screen, as soon as the map is idle.
        """
        if self._map_busy():
            if self._pending_click is not None or self._wait_since is not None or self._rebuild_owed:
                self._set_busy(True)
                self._wait_timer.start()
            return
        if self._pending_click is not None and self._rebuild_owed:
            self._run_click_after_build = True
        elif self._pending_click is not None:
            self._try_pending_click()
        if self._wait_since is not None or self._rebuild_owed or self._run_click_after_build:
            self._start_build_when_idle()

    def _try_pending_click(self):
        if self._pending_click is None:
            return
        if self._map_busy():
            self._wait_timer.start()
            return
        self._wait_since = None
        mp, pix = self._pending_click
        self._pending_click = None
        self._click(mp, pix)

    def _release_render_wait(self):
        self._preview_pause = False
        self._hold_render = False
        self._await_refresh = False
        self._await_since = None
        self._wait_since = None
        self._render_recoveries = 0
        self._render_cap_logged = False
        self._rebuild_owed = False
        self._run_click_after_build = False

    def _hold_canvas_jobs(self, hold):
        """One thread per redraw, and no preview jobs, while this tool is on.

        The flags are canvas members only. They go back when the tool turns off.
        """
        canvas = self.canvas
        if canvas is None:
            return
        if hold:
            if self._jobs_held:
                return
            try:
                self._parallel_was = bool(canvas.isParallelRenderingEnabled())
            except Exception:
                self._parallel_was = True
            try:
                canvas.setParallelRenderingEnabled(False)
                diag.log("parallel rendering off")
            except Exception as err:
                diag.log("could not turn off parallel rendering: {!r}".format(err))
            self._preview_was = None
            if hasattr(canvas, "setPreviewJobsEnabled"):
                try:
                    self._preview_was = bool(canvas.previewJobsEnabled())
                    canvas.setPreviewJobsEnabled(False)
                    diag.log("preview jobs paused")
                except Exception:
                    self._preview_was = None
            self._jobs_held = True
            return
        if not self._jobs_held:
            return
        try:
            canvas.setParallelRenderingEnabled(bool(self._parallel_was))
        except Exception as err:
            diag.log("could not restore parallel rendering: {!r}".format(err))
        if self._preview_was is not None:
            try:
                canvas.setPreviewJobsEnabled(bool(self._preview_was))
            except Exception as err:
                diag.log("could not restore preview jobs: {!r}".format(err))
        self._jobs_held = False
        diag.log("map draw settings restored")

    def _start_build(self):
        # Extent changes during the build set _rebuild_owed again.
        self._rebuild_owed = False
        self._gen += 1
        self._step_timer.stop()
        t0 = time.perf_counter()
        try:
            snap = self.cache.snapshot(self.settings.cluster_tolerance(self.canvas))
        except BuildError as e:
            diag.log("build: {}".format(e))
            self._install(None, None, str(e))
            return
        except Exception as e:
            diag.log("build: snapshot failed: {!r}".format(e))
            self._install(None, None, "Could not prepare topology: {}".format(e))
            return
        graph = graph_for_snapshot(snap)
        diag.log("build #{} started: {} rings, {} vertices".format(
            self._gen, len(snap.parts), graph.vertex_count()))
        self._job = (self._gen, graph, snap, graph.build_steps(), t0)
        if self._stale or self.graph is None:
            self._set_busy(True)
        self._step_timer.start()

    def _step(self):
        """Run one slice of the build, then yield to the event loop."""
        if self._job is None or not self._active:
            return
        gen, graph, snap, steps, t0 = self._job
        if gen != self._gen:
            return
        deadline = time.perf_counter() + SLICE_SECONDS
        try:
            while time.perf_counter() < deadline:
                next(steps)
        except StopIteration:
            self._job = None
            ctx = finish_context(graph, snap)
            diag.log("build #{} done in {:.2f} s: {} edges, {} nodes".format(
                gen, time.perf_counter() - t0, len(graph.edges), len(graph.nodes)))
            self._install(graph, ctx, None)
            return
        except Exception as e:
            self._job = None
            diag.log("build #{} failed: {!r}".format(gen, e))
            self._install(None, None, "Could not build topology: {}".format(e))
            return
        self._step_timer.start()

    def _install(self, graph, ctx, error):
        self.graph, self.ctx, self.build_error = graph, ctx, error
        self._stale = False
        self._set_busy(False)
        self._reset_hover()
        if self.overlay is not None:
            self.overlay.clear_hover()
            self.overlay.show_graph(self.graph)
        if self._last is not None:
            self._update_hover()
        if self._rebuild_owed:
            self._timer.start()
            return
        if self._run_click_after_build and self._pending_click is not None and graph is not None:
            self._run_click_after_build = False
            self._try_pending_click()

    # ------------------------------------------------------------ hover
    def _reset_hover(self):
        self._src = None
        self._targets = []
        self._plans = {}
        self._ti = 0

    def canvasMoveEvent(self, e):
        self._last = (e.mapPoint(), e.pixelPoint())
        self._update_hover()

    def _busy_tip(self, pix, text):
        if self.overlay is not None and self.settings.get("show_tooltip"):
            self.overlay.tip.show_at("<b>Align Edge</b><br><i>{}</i>".format(text), pix)

    def _update_hover(self):
        if self.overlay is None or self._last is None:
            return
        mp, pix = self._last
        if self._stale:
            self.overlay.clear_hover()
            self._busy_tip(pix, "Waiting for the map to finish drawing&hellip;"
                           if self._wait_since is not None else "Updating topology&hellip;")
            return
        if self.graph is None:
            self.overlay.clear_hover()
            if self.build_error and self.settings.get("show_tooltip"):
                self.overlay.tip.show_at(
                    "<b>Align Edge</b><br>{}".format(html.escape(self.build_error)), pix)
            return
        try:
            hit = self.graph.hit_test(mp.x(), mp.y(), self.settings.search_radius(self.canvas))
            if hit is None:
                self._reset_hover()
                self.overlay.clear_hover()
                return
            eid = hit[0]
            if eid != self._src:
                self._src = eid
                self._targets = [t for t, _ in self.graph.targets(
                    eid, self.settings.get("max_shift_ratio"))]
                self._plans = {}
                # put the first target that can actually be aligned first
                for i, t in enumerate(self._targets):
                    if self._plan(t).ok:
                        self._targets.insert(0, self._targets.pop(i))
                        break
                self._ti = 0
            self._render(pix)
        except Exception as ex:  # never let a hover error escape into Qt
            self._reset_hover()
            self.overlay.clear_hover()
            self._busy_tip(pix, html.escape("Error: {}".format(ex)))

    def _plan(self, tgt):
        if tgt not in self._plans:
            self._plans[tgt] = self.graph.plan_align(self._src, tgt)
        return self._plans[tgt]

    def cycle_target(self, step=1):
        if self._src is None or len(self._targets) < 2 or self._stale:
            return
        self._ti = (self._ti + step) % len(self._targets)
        if self._last is not None:
            self._render(self._last[1])

    def current_plan(self):
        if self._src is None or not self._targets:
            return None
        return self._plan(self._targets[self._ti])

    def _render(self, pix):
        g = self.graph
        if g is None or self._src is None:
            return
        src = g.edges[self._src]
        plan = self.current_plan()
        src_coords = g.edge_coords(self._src)
        ends = [g.point(src.start), g.point(src.end)]
        if plan is None:
            self.overlay.show_hover(src_coords, None, None, ends, False)
        else:
            self.overlay.show_hover(src_coords, g.edge_coords(plan.tgt.id),
                                    plan.gap_ring, ends, plan.ok)
        if self.settings.get("show_tooltip"):
            self.overlay.tip.show_at(self._tooltip(src, plan), pix)
        else:
            self.overlay.tip.hide()

    # ------------------------------------------------------------ tooltip
    def _names(self, feat_keys):
        fetch = not self.canvas.isDrawing()
        by_layer = {}
        for lid, fid in sorted(feat_keys, key=repr):
            by_layer.setdefault(lid, []).append(self.ctx.label((lid, fid), fetch))
        out = []
        for lid, labels in by_layer.items():
            lyr = self.ctx.layers.get(lid)
            name = lyr.name() if lyr is not None else lid
            shown = ", ".join(html.escape(x) for x in labels[:MAX_NAMES])
            if len(labels) > MAX_NAMES:
                shown += " +{} more".format(len(labels) - MAX_NAMES)
            out.append("{}: {}".format(html.escape(name), shown))
        return "; ".join(out)

    def _measure(self):
        da = QgsDistanceArea()
        da.setSourceCrs(self.ctx.canvas_crs, QgsProject.instance().transformContext())
        da.setEllipsoid(QgsProject.instance().ellipsoid())
        return da

    def _fmt_len(self, da, coords):
        pts = [QgsPointXY(x, y) for x, y in coords]
        v = da.measureLine(pts)
        unit = QgsProject.instance().distanceUnits()
        return QgsDistanceArea.formatDistance(da.convertLengthMeasurement(v, unit), 2, unit, False)

    def _fmt_area(self, da, ring):
        v = da.measureArea(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]]))
        unit = QgsProject.instance().areaUnits()
        return QgsDistanceArea.formatArea(da.convertAreaMeasurement(v, unit), 2, unit, False)

    def _tooltip(self, src, plan):
        g = self.graph
        da = self._measure()
        src_coords = g.edge_coords(src.id)
        rows = ["<b>Align Edge</b>"]
        moves = "{} &nbsp;<span style='color:#555'>({} vertices, {})</span>".format(
            self._names({k[:2] for k in src.owners}), len(src.pids),
            self._fmt_len(da, src_coords))
        rows.append("<b>Moves:</b> " + moves)
        if plan is None:
            rows.append("<span style='color:#b00020'>No adjacent edge shares both end "
                        "nodes &mdash; nothing to align.</span>")
            rows.append("<span style='color:#666'>Esc to clear</span>")
            return "<br>".join(rows)

        tgt = plan.tgt
        rows.append("<b>Onto:</b> {} &nbsp;<span style='color:#555'>({} vertices)</span>".format(
            self._names({k[:2] for k in tgt.owners}), len(tgt.pids)))
        if plan.ok:
            shift = self._fmt_len(da, list(plan.shift_pair)) if plan.shift_pair else "0"
            stats = "max shift {}".format(shift)
            if plan.gap_ring and len(plan.gap_ring) >= 3:
                stats = "area between edges {} &middot; ".format(
                    self._fmt_area(da, plan.gap_ring)) + stats
            rows.append(stats)
            touching = g.touching_features(src.id)
            if touching:
                rows.append("<span style='color:#555'>Unchanged: {} feature(s) touching the "
                            "end nodes</span>".format(len(touching)))
        else:
            rows.append("<span style='color:#b00020'>Can't align: {}</span>".format(
                html.escape(plan.error)))
        if len(self._targets) > 1:
            rows.append("<span style='color:#555'>Target {} of {} &mdash; Tab for next"
                        "</span>".format(self._ti + 1, len(self._targets)))
        rows.append("<span style='color:#666'>{}Esc to clear</span>".format(
            "Click to align &middot; " if plan.ok else ""))
        return "<br>".join(rows)

    # ------------------------------------------------------------ actions
    def canvasReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        mp, pix = e.mapPoint(), e.pixelPoint()
        if self._stale:
            self._busy_tip(pix, "Updating topology&hellip; try again in a moment")
            return
        if self.graph is None:
            if self.build_error:
                self.iface.messageBar().pushMessage("Align Edge", self.build_error,
                                                    MSG_WARNING, 4)
            return
        diag.log("click (map drawing={}, render age {:.1f} s)".format(
            self.canvas.isDrawing(), self._render_age()))
        if self._map_busy():
            # don't edit while the renderer is reading the layers; the click
            # runs as soon as the map finishes drawing
            self._pending_click = (mp, pix)
            self._set_busy(True)
            self._busy_tip(pix, "Waiting for the map to finish drawing&hellip;")
            self._wait_timer.start()
            return
        self._wait_since = None
        self._click(mp, pix)

    def _click(self, mp, pix):
        if self._stale or self.graph is None:
            return
        self._set_busy(False)
        # make sure the hover state matches the click location
        self._last = (mp, pix)
        self._update_hover()
        if self._src is None:
            return
        plan = self.current_plan()
        if plan is None:
            self._fail("no adjacent edge shares both end nodes")
            return
        if not plan.ok:
            self._fail(plan.error)
            return
        try:
            with diag.watchdog("align edge {} -> {}".format(plan.src.id, plan.tgt.id)):
                changes = build_changes(plan, self.graph, self.ctx)
                apply_changes(changes, self.ctx)
        except AlignFailed as ex:
            diag.log("align failed: {}".format(ex))
            self._fail(str(ex))
            self._on_data_changed()
            return
        except Exception as ex:
            diag.log("align error: {!r}".format(ex))
            self._fail("unexpected error: {}".format(ex))
            self._on_data_changed()
            return
        n = sum(len(v) for v in changes.values())
        self.iface.statusBarIface().showMessage(
            "Align Edge: {} feature(s) updated".format(n), 3000)
        # the cache's geometryChanged handlers already marked it stale; make sure
        self._on_data_changed()
        self._busy_tip(pix, "Updating topology&hellip;")

    def _fail(self, reason):
        self.iface.messageBar().pushMessage(
            "Align Edge failed", reason, MSG_WARNING, 5)

    def keyPressEvent(self, e):
        key = e.key()
        if key == Qt.Key.Key_Escape:
            self._reset_hover()
            if self.overlay is not None:
                self.overlay.clear_hover()
            e.accept()
            return
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_N):
            self.cycle_target(1)
            e.accept()
            return
        e.ignore()
