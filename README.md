# Align Edge

Align Edge is a QGIS plugin that reshapes one topology edge onto another, in the same way as Align Edge in ArcGIS Pro. Hover an edge to see what will move and what it will snap to. Click once to make them coincident.

It is inspired by ArcGIS Pro. It is not affiliated with Esri.

The button sits on the **Advanced Digitizing** toolbar. The same commands are under **Vector → Align Edge**.

With **Keystone 0.7.0+** enabled, the existing action also appears automatically in
**Edit > Tools** and **Modify Features > Alignment**. The ribbon button's dropdown
opens the same Settings command. Enable Align Edge and the entries appear; disable
it and they disappear. The tool still requires an eligible editable line/polygon
layer. This optional placement does not require importing Keystone, and Align Edge
continues to work with QGIS's native interface when Keystone is absent or switched off.
No ribbon configuration is needed for these placements. Restart QGIS after updating
both installed plugins, then enable both. The tool is disabled until an eligible
line or polygon layer is in edit mode.

## Requirements

- QGIS 3.28 or newer, including QGIS 4
- Visible line or polygon layers in edit mode, with optional target-only reference layers

## Install

1. Download the plugin zip from the [latest release](https://github.com/pwizzle77-cmd/qgis-align-edge/releases/latest).
2. In QGIS, open **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Choose the zip and install it.
4. Turn the plugin on if QGIS does not enable it automatically.

The zip has one folder, `align_edge`, with `metadata.txt` inside it.

To install from a clone instead, copy this folder to your QGIS profile:

`python/plugins/align_edge`

Then restart QGIS.

## Use

1. Start editing on every line or polygon layer that should take part.
   For a protected survey or boundary layer, choose **Reference (target only)** under
   **Align Edge Settings → Participating layers** instead of starting an edit session.
2. Click **Align Edge** on the Advanced Digitizing toolbar.
3. Hover an edge.
   - The **solid** edge is the one that will move.
   - The **dashed** edge is the one it will snap to.
   - The shaded area is the gap that the click will close.
   - The tooltip names the features that will change, the features they align to, the area between the edges, and the largest shift.
4. Click to align. Each edited layer gets one undo step named **Align Edge**.
5. Press **Tab** or **Shift+Tab** to cycle through other edges that share the same two end nodes. **Esc** clears the highlight and cancels any waiting click.
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
| Diagnostic log | Write `align_edge_debug.log` in the plugin folder. Off by default |
| Hover / click tolerance | `0` follows the project snapping tolerance |
| Cluster tolerance | `0` uses about 1 mm in the map units (1e-8 degrees). Saved with the project |
| Max shift / edge length | Largest allowed shift, as a multiple of the shorter edge. `0` means no limit |
| Max vertices in view | Refuses to build a topology that is too large for the current view. Zoom in, or raise the limit |
| Participating layers | Choose Editable source / target, Reference (target only), or Excluded for each layer. References must be visible but need not be editable; they never move |

**Restore Defaults** changes the dialog only. Settings and layer roles are saved
when you press **OK**; **Cancel** keeps the previous values.

## How a click is applied

The plugin builds a map topology from participating layers in view: shared vertices
become nodes, and the chains between nodes become edges. The click replaces the
solid edge with the shape of the dashed edge on every editable feature that owns
it. A source edge that is also owned by a reference is protected and cannot move.
Lines and polygons are both updated, including multipart features.

The target's actual XY coordinates, including endpoints, are copied. When that shifts
an end node within the cluster tolerance, the vertices of other editable features
joined at exactly that point move with it, so nothing comes apart; they keep their own
Z/M, and the tooltip says how many. If a joined feature can't move (a reference or a
curve), or it meets the edge further away than the cluster tolerance, alignment is
blocked with a message to align the node first. Target owners with conflicting
coordinates are also rejected. Layers in different CRSs are transformed normally.

The whole moving edge must be in view. A neighbour sharing an off-screen part is not
loaded and would be left behind, so the preview asks you to zoom out instead.

For Z/M-enabled source data, existing vertices and values outside the changed edge stay as
they are. New vertices receive interpolated source Z/M values at the same relative
distance along the source edge; target elevations/measures are not borrowed.
Unknown source dimensions stay unknown, rather than becoming zero.

Features containing arcs are not supported; they are rejected instead of writing
a straight approximation. Straight features in curve layers are supported. Empty results and newly invalid polygons are rejected.
Both source and target cache versions are checked before writing. If any write
fails or raises an exception, this alignment is rolled back across its layers;
earlier edits are retained. Each successfully edited layer still has its own
normal QGIS undo step. This tool does not save layer edits automatically.

## Develop

The topology rules live in `topology_core.py` and do not import QGIS. From this folder, whatever it is called:

```text
python -m unittest discover -s tests
```

Without QGIS, the geometry-core tests run and QGIS integration tests are skipped.
With the QGIS Python launcher, tests also exercise actual memory-layer writes,
rollback, reference roles, exact endpoints, Z/M, mixed CRSs, settings and queued
click cancellation. Provider reads remain on the main thread while the map is
idle; native provider operations and individual large index operations can still
take longer than a topology work slice.

QGIS 3.28+ / Qt5 and QGIS 4 / Qt6 are supported from the same code (`compat.py`).

## License

Copyright (C) 2026 Charles. Released under the GNU General Public License v2.0 or later. See [LICENSE](LICENSE).

QGIS is GPL-licensed, and a plugin that runs inside it is distributed under the same terms.
