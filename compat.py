# -*- coding: utf-8 -*-
"""Small shims so one code base runs on QGIS 3.28+ (Qt5) and QGIS 4 (Qt6)."""

from qgis.core import Qgis, QgsUnitTypes, QgsWkbTypes


def ev(owner, scoped, member):
    """Enum value: prefer owner.Scoped.member, fall back to owner.member."""
    enum = getattr(owner, scoped, None)
    if enum is not None and hasattr(enum, member):
        return getattr(enum, member)
    return getattr(owner, member)


try:
    GEOM_POINT = Qgis.GeometryType.Point
    GEOM_LINE = Qgis.GeometryType.Line
    GEOM_POLYGON = Qgis.GeometryType.Polygon
except AttributeError:  # QGIS < 3.30
    GEOM_POINT = QgsWkbTypes.PointGeometry
    GEOM_LINE = QgsWkbTypes.LineGeometry
    GEOM_POLYGON = QgsWkbTypes.PolygonGeometry

try:
    UNIT_DEGREES = Qgis.DistanceUnit.Degrees
    UNIT_METERS = Qgis.DistanceUnit.Meters
except AttributeError:  # QGIS < 3.30
    UNIT_DEGREES = QgsUnitTypes.DistanceDegrees
    UNIT_METERS = QgsUnitTypes.DistanceMeters

MSG_INFO = ev(Qgis, "MessageLevel", "Info")
MSG_WARNING = ev(Qgis, "MessageLevel", "Warning")
MSG_CRITICAL = ev(Qgis, "MessageLevel", "Critical")


def reverse_transform():
    try:
        return Qgis.TransformDirection.Reverse
    except AttributeError:
        from qgis.core import QgsCoordinateTransform
        return QgsCoordinateTransform.ReverseTransform


def geometry_type(layer):
    return QgsWkbTypes.geometryType(layer.wkbType())


def is_line_or_polygon(layer):
    return geometry_type(layer) in (GEOM_LINE, GEOM_POLYGON)
