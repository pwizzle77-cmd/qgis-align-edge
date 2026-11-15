# -*- coding: utf-8 -*-
"""Rubber bands for the topology graph and hover highlight, plus the tooltip."""

from qgis.core import QgsGeometry, QgsPointXY
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QPoint, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QLabel

from . import diag
from .compat import GEOM_LINE, GEOM_POINT, GEOM_POLYGON, ev
from .settings import DISPLAY_ALL

ICON_CIRCLE = ev(QgsRubberBand, "IconType", "ICON_CIRCLE")
ICON_FULL_BOX = ev(QgsRubberBand, "IconType", "ICON_FULL_BOX")


def _pts(coords):
    return [QgsPointXY(x, y) for x, y in coords]


class TipWidget(QLabel):
    """Tooltip-styled label that follows the cursor over the canvas.

    It is its own tooltip window (not a child of the canvas), so moving it
    never forces the map canvas to repaint the topology overlay.
    """

    def __init__(self, canvas):
        super().__init__(canvas, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.canvas = canvas
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setStyleSheet(
            "QLabel { background-color: rgba(255, 255, 225, 242); color: #111;"
            " border: 1px solid #8a8a8a; border-radius: 3px; padding: 5px 8px; }")
        self.hide()

    def show_at(self, html, pos):
        self.setText(html)
        self.adjustSize()
        w, h = self.width(), self.height()
        cw, ch = self.canvas.width(), self.canvas.height()
        x, y = pos.x() + 18, pos.y() + 22
        if x + w > cw - 4:
            x = pos.x() - w - 12
        if y + h > ch - 4:
            y = pos.y() - h - 12
        self.move(self.canvas.mapToGlobal(QPoint(max(2, x), max(2, y))))
        if not self.isVisible():
            self.show()


class Overlay(object):

    def __init__(self, canvas, settings):
        self.canvas = canvas
        self.settings = settings
        self.graph_band = QgsRubberBand(canvas, GEOM_LINE)
        self.node_band = QgsRubberBand(canvas, GEOM_POINT)
        self.gap_band = QgsRubberBand(canvas, GEOM_POLYGON)
        self.src_band = QgsRubberBand(canvas, GEOM_LINE)
        self.tgt_band = QgsRubberBand(canvas, GEOM_LINE)
        self.end_band = QgsRubberBand(canvas, GEOM_POINT)
        self.tip = TipWidget(canvas)
        self._bands = [self.graph_band, self.node_band, self.gap_band,
                       self.src_band, self.tgt_band, self.end_band]
        self._types = [GEOM_LINE, GEOM_POINT, GEOM_POLYGON, GEOM_LINE, GEOM_LINE, GEOM_POINT]
        self.apply_style()

    # ----------------------------------------------------------- style
    def apply_style(self):
        s = self.settings
        graph = QColor(s.get("graph_color"))
        hi = QColor(s.get("highlight_color"))
        width = s.get("line_width")

        g = QColor(graph)
        g.setAlpha(170)
        self.graph_band.setColor(g)
        self.graph_band.setWidth(1)

        self.node_band.setColor(graph)
        self.node_band.setIcon(ICON_CIRCLE)
        self.node_band.setIconSize(s.get("node_size"))
        self.node_band.setWidth(2)

        fill = QColor(hi)
        fill.setAlpha(70)
        self.gap_band.setFillColor(fill)
        self.gap_band.setStrokeColor(QColor(0, 0, 0, 0))
        self.gap_band.setWidth(0)

        self.src_band.setColor(hi)
        self.src_band.setWidth(width)

        self.tgt_band.setColor(hi)
        self.tgt_band.setWidth(width)
        self.tgt_band.setLineStyle(Qt.PenStyle.DashLine)

        self.end_band.setColor(hi)
        self.end_band.setIcon(ICON_FULL_BOX)
        self.end_band.setIconSize(s.get("node_size") + 3)

    # ----------------------------------------------------------- graph
    def show_graph(self, graph):
        self.graph_band.reset(GEOM_LINE)
        self.node_band.reset(GEOM_POINT)
        if graph is None:
            return
        if self.settings.get("display_style") == DISPLAY_ALL and graph.edges:
            lines = [_pts(graph.edge_coords(e.id)) for e in graph.edges]
            self.graph_band.setToGeometry(QgsGeometry.fromMultiPolylineXY(lines))
        if graph.nodes:
            nodes = [QgsPointXY(*graph.point(n)) for n in graph.nodes]
            self.node_band.setToGeometry(QgsGeometry.fromMultiPointXY(nodes))

    # ----------------------------------------------------------- hover
    def show_hover(self, src_coords, tgt_coords, gap_ring, end_points, ok):
        hi = QColor(self.settings.get("highlight_color"))
        bad = QColor(self.settings.get("fail_color"))
        self.src_band.setColor(hi if ok else bad)
        self.tgt_band.setColor(hi if tgt_coords else bad)
        self.end_band.setColor(hi if ok else bad)

        self.src_band.setToGeometry(QgsGeometry.fromPolylineXY(_pts(src_coords)))
        if tgt_coords:
            self.tgt_band.setToGeometry(QgsGeometry.fromPolylineXY(_pts(tgt_coords)))
            self.src_band.setLineStyle(Qt.PenStyle.SolidLine)
        else:
            # ArcMap: an edge that cannot be aligned stays dashed
            self.tgt_band.reset(GEOM_LINE)
            self.src_band.setLineStyle(Qt.PenStyle.DashLine)
        if ok and gap_ring and len(gap_ring) >= 3 and self.settings.get("show_gap_fill"):
            self.gap_band.setToGeometry(QgsGeometry.fromPolygonXY([_pts(gap_ring)]))
        else:
            self.gap_band.reset(GEOM_POLYGON)
        self.end_band.setToGeometry(QgsGeometry.fromMultiPointXY(_pts(end_points)))

    def clear_hover(self):
        self.src_band.reset(GEOM_LINE)
        self.tgt_band.reset(GEOM_LINE)
        self.gap_band.reset(GEOM_POLYGON)
        self.end_band.reset(GEOM_POINT)
        self.tip.hide()

    def clear_all(self):
        for b, t in zip(self._bands, self._types):
            b.reset(t)
        self.tip.hide()

    def remove(self):
        self.clear_all()
        scene = self.canvas.scene()
        for b in self._bands:
            try:
                scene.removeItem(b)
            except Exception as err:
                diag.log("overlay: could not remove a rubber band: {!r}".format(err))
        self._bands = []
        self.tip.deleteLater()
