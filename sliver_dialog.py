# -*- coding: utf-8 -*-
"""Align All Slivers: batch-align every edge within tolerance of a chosen
base layer, previewed before anything is written."""

from qgis.PyQt.QtCore import QCoreApplication
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
from .sliver_align import build_graph, find_candidates, run_batch


class SliverAlignDialog(QDialog):
    """Pick a base layer and a tolerance, scan the current topology for
    every other edge within that tolerance of it, review the list, then
    Align All. Reads its own topology for the current view - it does not
    need the Align Edge tool itself turned on."""

    def __init__(self, iface, settings, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.settings = settings
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

    def _base_and_tolerance(self):
        base_id = self.base.currentData()
        if not base_id:
            self.status.setText("Pick a base layer first.")
            return None, None
        return base_id, self.tolerance.value()

    def _scan(self):
        self.list.clear()
        self.apply_btn.setEnabled(False)
        base_id, tolerance = self._base_and_tolerance()
        if base_id is None:
            return
        nearby = self.settings.get("max_shift_ratio")
        graph, ctx, reason = build_graph(self.iface.mapCanvas(), self.settings, tolerance)
        if graph is None:
            self.status.setText(reason)
            return
        candidates, skipped = find_candidates(graph, ctx, {base_id}, tolerance, nearby)
        for cand in candidates:
            self.list.addItem("{} - {:.6f} map units".format(cand.label, cand.shift))
        for _key, label, reason in skipped:
            self.list.addItem("(not aligned) {} - {}".format(label, reason))
        msg = "{} sliver(s) found within tolerance.".format(len(candidates))
        if skipped:
            msg += " {} more are close but can't be aligned automatically (see list).".format(len(skipped))
        if not candidates and not skipped:
            msg = "No edges within tolerance of the base layer were found."
        self.status.setText(msg)
        self.apply_btn.setEnabled(bool(candidates))

    def _apply(self):
        base_id, tolerance = self._base_and_tolerance()
        if base_id is None:
            return
        nearby = self.settings.get("max_shift_ratio")
        self.apply_btn.setEnabled(False)
        close_btn = self.bb.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.setEnabled(False)
        self.list.clear()

        def on_pass(pass_no, found, done_so_far):
            self.status.setText("Pass {}: {} left to align ({} done so far)...".format(
                pass_no, found, done_so_far))
            QCoreApplication.processEvents()

        try:
            done, failed = run_batch(self.iface.mapCanvas(), self.settings, {base_id}, tolerance, nearby,
                                     on_pass=on_pass)
        finally:
            if close_btn is not None:
                close_btn.setEnabled(True)
        for label in done:
            self.list.addItem("Aligned: {}".format(label))
        for label, reason in failed:
            self.list.addItem("Not aligned: {} - {}".format(label, reason))
        msg = "Aligned {} of {}.".format(len(done), len(done) + len(failed))
        if failed:
            msg += " The rest could not be aligned automatically - see the list."
        self.status.setText(msg)
        self.iface.mapCanvas().refresh()
