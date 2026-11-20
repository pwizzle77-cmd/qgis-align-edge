# -*- coding: utf-8 -*-
"""Align Edge settings dialog."""

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsColorButton, QgsDoubleSpinBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QSpinBox,
    QVBoxLayout,
)

from .compat import is_line_or_polygon
from .settings import DISPLAY_ALL, DISPLAY_SELECTED, _DEFAULTS


class SettingsDialog(QDialog):

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Align Edge Settings")
        s = settings
        lay = QVBoxLayout(self)

        disp = QGroupBox("Topology display")
        f = QFormLayout(disp)
        self.style = QComboBox()
        self.style.addItem("All elements (always show edges and nodes)", DISPLAY_ALL)
        self.style.addItem("Selected elements (show nodes; edges on hover)", DISPLAY_SELECTED)
        self.style.setCurrentIndex(max(0, self.style.findData(s.get("display_style"))))
        f.addRow("Display style", self.style)
        self.graph_color = QgsColorButton()
        self.graph_color.setColor(QColor(s.get("graph_color")))
        f.addRow("Topology graph color", self.graph_color)
        self.hi_color = QgsColorButton()
        self.hi_color.setColor(QColor(s.get("highlight_color")))
        f.addRow("Align highlight color", self.hi_color)
        self.fail_color = QgsColorButton()
        self.fail_color.setColor(QColor(s.get("fail_color")))
        f.addRow("Cannot-align color", self.fail_color)
        self.width = QSpinBox()
        self.width.setRange(1, 10)
        self.width.setValue(s.get("line_width"))
        f.addRow("Highlight width (px)", self.width)
        self.node_size = QSpinBox()
        self.node_size.setRange(3, 20)
        self.node_size.setValue(s.get("node_size"))
        f.addRow("Node size (px)", self.node_size)
        self.gap_fill = QCheckBox("Shade the area between the two edges")
        self.gap_fill.setChecked(s.get("show_gap_fill"))
        f.addRow("", self.gap_fill)
        self.tooltip = QCheckBox("Show tooltip describing the alignment")
        self.tooltip.setChecked(s.get("show_tooltip"))
        f.addRow("", self.tooltip)
        self.diag = QCheckBox("Write diagnostic log (align_edge_debug.log in the plugin folder)")
        self.diag.setChecked(s.get("diagnostic_log"))
        f.addRow("", self.diag)
        lay.addWidget(disp)

        tol = QGroupBox("Tolerances")
        f2 = QFormLayout(tol)
        self.tol_px = QSpinBox()
        self.tol_px.setRange(0, 100)
        self.tol_px.setSpecialValueText("Project snapping tolerance")
        self.tol_px.setSuffix(" px")
        self.tol_px.setValue(s.get("tolerance_px"))
        f2.addRow("Hover / click tolerance", self.tol_px)
        self.cluster = QgsDoubleSpinBox()
        self.cluster.setDecimals(10)
        self.cluster.setRange(0, 1e6)
        self.cluster.setSpecialValueText("Auto (1 mm)")
        self.cluster.setClearValue(0)
        self.cluster.setValue(s.get("cluster_tolerance"))
        f2.addRow("Cluster tolerance (map units)", self.cluster)
        self.nearby = QgsDoubleSpinBox()
        self.nearby.setDecimals(2)
        self.nearby.setRange(0, 100)
        self.nearby.setSingleStep(0.25)
        self.nearby.setSpecialValueText("No limit")
        self.nearby.setValue(s.get("max_shift_ratio"))
        self.nearby.setToolTip("Sanity limit: a target is offered only if the largest shift is at "
                               "most this multiple of the shorter edge's length.")
        f2.addRow("Max shift / edge length", self.nearby)
        self.max_v = QSpinBox()
        self.max_v.setRange(1000, 5000000)
        self.max_v.setSingleStep(10000)
        self.max_v.setValue(s.get("max_vertices"))
        f2.addRow("Max vertices in view", self.max_v)
        lay.addWidget(tol)
        for field in (self.style, self.width, self.node_size, self.tol_px, self.cluster, self.nearby, self.max_v):
            field.setMinimumHeight(field.fontMetrics().height() + 8)

        lg = QGroupBox("Participating layers (this project)")
        v = QVBoxLayout(lg)
        description = QLabel("Editable layers can move. Reference layers are target-only and need not be in edit mode.")
        description.setWordWrap(True)
        v.addWidget(description)
        self.layers = QTableWidget(0, 2)
        self.layers.setHorizontalHeaderLabels(["Layer", "Role"])
        self.layers.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.layers.setMaximumHeight(180)
        excluded = s.excluded_layer_ids()
        references = s.reference_layer_ids()
        for lyr in sorted(QgsProject.instance().mapLayers().values(), key=lambda layer: layer.name().lower()):
            if not isinstance(lyr, QgsVectorLayer) or not is_line_or_polygon(lyr):
                continue
            row = self.layers.rowCount()
            self.layers.insertRow(row)
            it = QTableWidgetItem(lyr.name())
            it.setData(Qt.ItemDataRole.UserRole, lyr.id())
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.layers.setItem(row, 0, it)
            role = QComboBox()
            role.addItem("Editable source / target", "edit")
            role.addItem("Reference (target only)", "reference")
            role.addItem("Excluded", "excluded")
            value = "excluded" if lyr.id() in excluded else "reference" if lyr.id() in references else "edit"
            role.setCurrentIndex(role.findData(value))
            self.layers.setCellWidget(row, 1, role)
        v.addWidget(self.layers)
        lay.addWidget(lg)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel
                              | QDialogButtonBox.StandardButton.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._defaults)
        lay.addWidget(bb)

    def _defaults(self):
        s = _DEFAULTS  # Defaults remain local to the dialog until OK.
        self.style.setCurrentIndex(max(0, self.style.findData(s.get("display_style"))))
        self.graph_color.setColor(QColor(s.get("graph_color")))
        self.hi_color.setColor(QColor(s.get("highlight_color")))
        self.fail_color.setColor(QColor(s.get("fail_color")))
        self.width.setValue(s.get("line_width"))
        self.node_size.setValue(s.get("node_size"))
        self.gap_fill.setChecked(s.get("show_gap_fill"))
        self.tooltip.setChecked(s.get("show_tooltip"))
        self.diag.setChecked(s.get("diagnostic_log"))
        self.tol_px.setValue(s.get("tolerance_px"))
        self.cluster.setValue(s.get("cluster_tolerance"))
        self.nearby.setValue(s.get("max_shift_ratio"))
        self.max_v.setValue(s.get("max_vertices"))
        for i in range(self.layers.rowCount()):
            self.layers.cellWidget(i, 1).setCurrentIndex(0)

    def accept(self):
        s = self.settings
        s.set("display_style", self.style.currentData())
        s.set("graph_color", self.graph_color.color().name())
        s.set("highlight_color", self.hi_color.color().name())
        s.set("fail_color", self.fail_color.color().name())
        s.set("line_width", self.width.value())
        s.set("node_size", self.node_size.value())
        s.set("show_gap_fill", self.gap_fill.isChecked())
        s.set("show_tooltip", self.tooltip.isChecked())
        s.set("diagnostic_log", self.diag.isChecked())
        s.set("tolerance_px", self.tol_px.value())
        s.set("cluster_tolerance", self.cluster.value())
        s.set("max_shift_ratio", self.nearby.value())
        s.set("max_vertices", self.max_v.value())
        excluded = set()
        references = set()
        for i in range(self.layers.rowCount()):
            lid = self.layers.item(i, 0).data(Qt.ItemDataRole.UserRole)
            role = self.layers.cellWidget(i, 1).currentData()
            if role == "excluded":
                excluded.add(lid)
            elif role == "reference":
                references.add(lid)
        s.set_excluded_layer_ids(excluded)
        s.set_reference_layer_ids(references)
        super().accept()
