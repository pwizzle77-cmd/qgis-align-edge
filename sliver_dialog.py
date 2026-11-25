# -*- coding: utf-8 -*-
"""Align All Slivers: batch-align every edge within tolerance of a chosen
base layer, previewed before anything is written."""

from qgis.core import QgsProject
from qgis.gui import QgsDoubleSpinBox
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from .graph_builder import participating_layers
from .sliver_align import apply_candidates, find_candidates


class SliverAlignDialog(QDialog):
    """Pick a base layer and a tolerance, scan the current topology for
    every other edge within that tolerance of it, review the list, then
    apply. Works on the topology already built for the current view -
    activate Align Edge first (so it has something to pan/zoom over and
    build) if nothing is found."""

    def __init__(self, iface, tool, settings, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.tool = tool
        self.settings = settings
        self.candidates = []
        self.setWindowTitle("Align All Slivers")
        self.resize(480, 420)
        lay = QVBoxLayout(self)

        intro = QLabel(
            "Finds every edge, on any layer except the base layer, that is within tolerance of "
            "the base layer's own edge at the same corners - a shared boundary digitized a hair "
            "apart - and aligns it exactly to the base layer. A genuine overlap or gap too wide "
            "for the tolerance is left for you to align by hand.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QFormLayout()
        self.base = QComboBox()
        form.addRow("Base layer (stays put)", self.base)
        self.tolerance = QgsDoubleSpinBox()
        self.tolerance.setDecimals(10)
        self.tolerance.setRange(0, 1e6)
        self.tolerance.setValue(settings.cluster_tolerance(iface.mapCanvas()))
        form.addRow("Tolerance (map units)", self.tolerance)
        lay.addLayout(form)

        self._fill_layers()

        row = QVBoxLayout()
        scan = QPushButton("Scan")
        scan.clicked.connect(self._scan)
        row.addWidget(scan)
        lay.addLayout(row)

        self.status = QLabel("Pick a base layer and Scan.")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)

        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.apply_btn = QPushButton("Align All")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        self.bb.addButton(self.apply_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self.bb.rejected.connect(self.close)
        lay.addWidget(self.bb)

    # ------------------------------------------------------------------
    def _fill_layers(self):
        canvas = self.iface.mapCanvas()
        layers = participating_layers(canvas, self.settings, any_scale=True)
        references = self.settings.reference_layer_ids()
        for lyr in sorted(layers, key=lambda l: l.name().lower()):
            self.base.addItem(lyr.name(), lyr.id())
        if len(references) == 1:
            idx = self.base.findData(next(iter(references)))
            if idx >= 0:
                self.base.setCurrentIndex(idx)

    def _scan(self):
        self.candidates = []
        self.list.clear()
        self.apply_btn.setEnabled(False)
        graph = self.tool.graph if self.tool is not None else None
        ctx = self.tool.ctx if self.tool is not None else None
        if graph is None or ctx is None:
            self.status.setText(
                "No topology is built yet. Click the Align Edge button to activate it over the "
                "area you want to clean up, then Scan again.")
            return
        base_id = self.base.currentData()
        if not base_id:
            self.status.setText("Pick a base layer first.")
            return
        tolerance = self.tolerance.value()
        nearby = self.settings.get("max_shift_ratio")

        def label_of(eid):
            key = next(iter(graph.edges[eid].owners))
            return ctx.label(key[:2])

        candidates, skipped = find_candidates(graph, {base_id}, tolerance, nearby, label_of)
        self.candidates = candidates
        for cand in candidates:
            self.list.addItem("{} - {:.6f} map units".format(cand.label, cand.shift))
        for label, reason in skipped:
            self.list.addItem("(not aligned) {} - {}".format(label, reason))
        msg = "{} sliver(s) found within tolerance.".format(len(candidates))
        if skipped:
            msg += " {} more are close but can't be aligned automatically (see list).".format(len(skipped))
        if not candidates and not skipped:
            msg = "No edges within tolerance of the base layer were found."
        self.status.setText(msg)
        self.apply_btn.setEnabled(bool(candidates))

    def _apply(self):
        graph = self.tool.graph if self.tool is not None else None
        ctx = self.tool.ctx if self.tool is not None else None
        if graph is None or ctx is None or not self.candidates:
            return
        self.apply_btn.setEnabled(False)
        done, failed = apply_candidates(self.candidates, graph, ctx)
        self.candidates = []
        self.list.clear()
        for label in done:
            self.list.addItem("Aligned: {}".format(label))
        for label, reason in failed:
            self.list.addItem("Not aligned: {} - {}".format(label, reason))
        msg = "Aligned {} of {}.".format(len(done), len(done) + len(failed))
        if failed:
            msg += (" The rest were skipped, usually because a fix just above it in this "
                    "batch already changed something they share - Scan again to pick up "
                    "what's left.")
        self.status.setText(msg)
        if self.tool is not None:
            self.tool.schedule_rebuild()
        self.iface.mapCanvas().refresh()
