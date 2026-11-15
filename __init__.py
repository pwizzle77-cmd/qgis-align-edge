# -*- coding: utf-8 -*-
"""Align Edge - ArcGIS Pro-style topology edge alignment for QGIS."""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import AlignEdgePlugin
    return AlignEdgePlugin(iface)
