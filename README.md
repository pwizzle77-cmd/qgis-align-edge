# Align Edge

Align Edge is a QGIS plugin that reshapes one topology edge onto another, in the same way as Align Edge in ArcGIS Pro. Hover an edge to see what will move and what it will snap to. Click once to make them coincident.

It is inspired by ArcGIS Pro. It is not affiliated with Esri.

The button sits on the **Advanced Digitizing** toolbar. The same commands are under **Vector → Align Edge**.

## Requirements

- QGIS 3.28 or newer, including QGIS 4
- Line or polygon layers that are visible and in edit mode

## Install

1. Download `align_edge-1.3.2.zip` from the [latest release](https://github.com/pwizzle77-cmd/qgis-align-edge/releases/latest).
2. In QGIS, open **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Choose the zip and install it.
4. Turn the plugin on if QGIS does not enable it automatically.

The zip has one folder, `align_edge`, with `metadata.txt` inside it.

To install from a clone instead, copy this folder to your QGIS profile:

`python/plugins/align_edge`

Then restart QGIS.

## Use

1. Start editing on every line or polygon layer that should take part.
2. Click **Align Edge** on the Advanced Digitizing toolbar.
3. Hover an edge.
   - The **solid** edge is the one that will move.
   - The **dashed** edge is the one it will snap to.
   - The shaded area is the gap that the click will close.
   - The tooltip names the features that will change, the features they align to, the area between the edges, and the largest shift.
4. Click to align. Each edited layer gets one undo step named **Align Edge**.
5. Press **Tab** or **Shift+Tab** to cycle through other edges that share the same two end nodes. **Esc** clears the highlight.
6. To turn the tool off, click the **Align Edge** button again or press **Esc** when nothing is highlighted. You go back to the tool you were using before (Pan if there was none).

An edge is offered when both of its end nodes are shared with another edge. The dropdown on the toolbar button opens **Align Edge Settings**.

If the map is still drawing, the cursor turns busy and the click waits. It runs when the map is idle, on the topology for the view you settled on.

## Settings

Open **Align Edge Settings** from the toolbar dropdown or from **Vector → Align Edge**.

| Setting | What it does |
| --- | --- |
| Display style | Show the whole topology graph, or only nodes until you hover |
| Colors, width, node size | Graph, highlight, and cannot-align colors |
| Shade the area between the two edges | Fill the gap in the preview |
| Tooltip | Turn the hover description on or off |
| Diagnostic log | Write `align_edge_debug.log` in the plugin folder |
| Hover / click tolerance | `0` follows the project snapping tolerance |
| Cluster tolerance | `0` uses about 1 mm in the map units (1e-8 degrees) |
| Max shift / edge length | Largest allowed shift, as a multiple of the shorter edge. `0` means no limit |
| Max vertices in view | Refuses to build a topology that is too large for the current view. Zoom in, or raise the limit |
| Participating layers | Visible line and polygon layers in edit mode are included. Uncheck a layer to leave it out of this project |

## How a click is applied

The plugin builds a map topology from the editable layers in view: shared vertices become nodes, and the chains between nodes become edges. The click replaces the solid edge with the shape of the dashed edge on every feature that owns that edge. Lines and polygons are both updated, including multipart features. Curved geometries are left unchanged and reported as a failure. Results that would be empty or invalid are not written. Undo uses the normal QGIS edit buffer.

## Develop

The topology rules live in `topology_core.py` and do not import QGIS. From this folder:

```text
python -m unittest discover -s tests
```

QGIS 3.28+ / Qt5 and QGIS 4 / Qt6 are both supported from the same code (`compat.py`).

## License

Copyright (C) 2026 Charles. Released under the GNU General Public License v2.0 or later. See [LICENSE](LICENSE).

QGIS is GPL-licensed, and a plugin that runs inside it is distributed under the same terms.
