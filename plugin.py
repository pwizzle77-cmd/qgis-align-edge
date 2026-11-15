# -*- coding: utf-8 -*-
"""Registers the Align Edge button on the Advanced Digitizing toolbar."""

import os

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMenu, QToolButton

from .align_edge_tool import AlignEdgeTool
from .graph_builder import participating_layers
from .settings import Settings
from .settings_dialog import SettingsDialog

HERE = os.path.dirname(__file__)
MENU = "&Align Edge"


class AlignEdgePlugin(object):

    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.settings = Settings()
        self.tool = None
        self._watched = []

    # ------------------------------------------------------------ GUI
    def initGui(self):
        mw = self.iface.mainWindow()
        icon = QIcon(os.path.join(HERE, "icons", "align_edge.svg"))

        self.action = QAction(icon, "Align Edge", mw)
        self.action.setObjectName("mActionAlignEdge")
        self.action.setCheckable(True)
        self.action.setToolTip(
            "<b>Align Edge</b><br>Hover an edge to preview it (solid) and the adjacent "
            "edge it will snap to (dashed); click to align. Works on all visible line and "
            "polygon layers in edit mode. Tab cycles targets, Esc clears.")
        self.action.triggered.connect(self.activate_tool)

        self.settings_action = QAction(QIcon(), "Align Edge Settings…", mw)
        self.settings_action.triggered.connect(self.show_settings)

        self.menu = QMenu(mw)
        self.menu.addAction(self.settings_action)
        self.button = QToolButton(mw)
        self.button.setDefaultAction(self.action)
        self.button.setMenu(self.menu)
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

        self.toolbar = self.iface.advancedDigitizeToolBar()
        self.toolbar_action = self.toolbar.addWidget(self.button)

        self.iface.addPluginToVectorMenu(MENU, self.action)
        self.iface.addPluginToVectorMenu(MENU, self.settings_action)

        self.tool = AlignEdgeTool(self.iface, self.settings)
        self.tool.setAction(self.action)

        self.canvas.layersChanged.connect(self.update_enabled)
        self.canvas.scaleChanged.connect(self.update_enabled)
        QgsProject.instance().layersAdded.connect(self._watch)
        QgsProject.instance().layersRemoved.connect(self._layers_removed)
        self._watch()
        self.update_enabled()

    def unload(self):
        if self.tool is not None and self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)
        for sig, slot in ((self.canvas.layersChanged, self.update_enabled),
                          (self.canvas.scaleChanged, self.update_enabled),
                          (QgsProject.instance().layersAdded, self._watch),
                          (QgsProject.instance().layersRemoved, self._layers_removed)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._unwatch()
        self.iface.removePluginVectorMenu(MENU, self.action)
        self.iface.removePluginVectorMenu(MENU, self.settings_action)
        self.toolbar.removeAction(self.toolbar_action)
        if self.tool is not None:
            self.tool.cleanup()
            self.tool.deleteLater()
            self.tool = None
        self.button.deleteLater()

    # ------------------------------------------------------------ actions
    def activate_tool(self):
        if not self.action.isEnabled():
            return
        self.canvas.setMapTool(self.tool)
        self.action.setChecked(True)

    def show_settings(self):
        dlg = SettingsDialog(self.settings, self.iface.mainWindow())
        if dlg.exec():
            if self.tool is not None:
                self.tool.restyle()
            self.update_enabled()

    # ------------------------------------------------------------ enabled state
    def _watch(self, *args):
        self._unwatch()
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer):
                lyr.editingStarted.connect(self.update_enabled)
                lyr.editingStopped.connect(self.update_enabled)
                self._watched.append(lyr)

    def _unwatch(self):
        for lyr in self._watched:
            for sig in (lyr.editingStarted, lyr.editingStopped):
                try:
                    sig.disconnect(self.update_enabled)
                except (TypeError, RuntimeError):
                    pass
        self._watched = []

    def _layers_removed(self, *args):
        self._watched = [l for l in self._watched if _alive(l)]
        self._watch()
        self.update_enabled()

    def update_enabled(self, *args):
        enabled = bool(participating_layers(self.canvas, self.settings))
        self.action.setEnabled(enabled)
        if not enabled and self.tool is not None and self.canvas.mapTool() is self.tool:
            # like the native editing tools: drop back to Pan when nothing is editable
            self.iface.actionPan().trigger()


def _alive(obj):
    try:
        obj.id()
        return True
    except RuntimeError:
        return False
