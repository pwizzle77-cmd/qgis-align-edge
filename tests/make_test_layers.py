# -*- coding: utf-8 -*-
"""
Creates scratch layers for trying Align Edge.

Run in the QGIS Python Console:
    exec(open(r"<path to plugin>/tests/make_test_layers.py").read())

Then the layers are already in edit mode - pick Align Edge on the
Advanced Digitizing toolbar and hover the boundaries described below.
"""
from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

polys = QgsVectorLayer("Polygon?crs=EPSG:3857&field=name:string", "AE test parcels", "memory")
lines = QgsVectorLayer("LineString?crs=EPSG:3857&field=name:string", "AE test lines", "memory")

POLYS = [
    # 1 & 2: sliver GAP - B's west side bulges away from A
    ("A gap", "POLYGON((0 0, 100 0, 100 100, 0 100, 0 0))"),
    ("B gap", "POLYGON((100 0, 200 0, 200 100, 100 100, 104 50, 100 0))"),
    # 3 & 4: OVERLAP - D's west side crosses into C
    ("C overlap", "POLYGON((0 150, 100 150, 100 250, 0 250, 0 150))"),
    ("D overlap", "POLYGON((97 140, 200 140, 200 260, 97 260, 97 140))"),
    # 5 & 6: SHARED edge plus a zig-zag neighbour line
    ("E shared", "POLYGON((0 300, 100 300, 100 400, 0 400, 0 300))"),
    ("F shared", "POLYGON((100 300, 200 300, 200 400, 100 400, 100 300))"),
]
LINES = [
    # road that wanders off the gap polygons' south edge between x=20 and x=80
    ("road", "LINESTRING(-50 0, 20 0, 50 -6, 80 0, 250 0)"),
    # fence touching the shared E/F edge ends, bowing west
    ("fence", "LINESTRING(100 400, 95 350, 100 300)"),
]

for layer, rows in ((polys, POLYS), (lines, LINES)):
    feats = []
    for name, wkt in rows:
        f = QgsFeature(layer.fields())
        f.setAttribute("name", name)
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        feats.append(f)
    layer.dataProvider().addFeatures(feats)
    layer.setDisplayExpression('"name"')
    QgsProject.instance().addMapLayer(layer)
    layer.startEditing()

iface.mapCanvas().setExtent(polys.extent().buffered(30))  # noqa: F821 (QGIS console)
iface.mapCanvas().refresh()  # noqa: F821
print("Align Edge test layers ready (both in edit mode).")
