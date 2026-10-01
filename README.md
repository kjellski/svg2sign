# svg2sign

Turn a filled SVG into a layered, CNC-cuttable sign in FreeCAD.

Every filled shape in the SVG becomes a part cut from sheet material. Parts are
stacked "outside in": whatever is painted on top of something else sits one
layer higher. Each part gets a shallow pocket (inset) for the parts resting on
it, so everything locates itself during glue-up.

You get two FreeCAD files, and each one again as STEP:

| File | Contents |
|---|---|
| `<name>_sign.FCStd` | The assembled sign, coloured like the SVG, one group per layer. For looking at. |
| `<name>_layout.FCStd` | A copy with every part laid flat, same side up, spaced out on sheets. For cutting. |
| `<name>_sign.step`, `<name>_layout.step` | The same two, for Fusion 360 or any other CAD. One body per part, named like the part. |

The STEP files contain the parts only: no layer groups and no sheet outline.
They are large (tens of MB for a detailed logo) because every curve is a fine
polyline; a coarser `--tolerance` shrinks them. Skip them with `--no-step`.

With `--dxf` you also get `<name>_layout.dxf`, the flat layout as 2D outlines.
See [Exports](#exports).

## Requirements

- [FreeCAD](https://www.freecad.org/) 1.0 or newer
- Python 3.10 or newer with `svgelements`, `shapely` (2.0+) and `ezdxf` -
  [uv](https://docs.astral.sh/uv/) takes care of all of that for you

Developed and tested on macOS. Nothing in it is macOS-specific, but Windows and
Linux are untested.

## Install

Get the code:

```bash
git clone https://github.com/kjellski/svg2sign.git
cd svg2sign
```

### With uv (recommended)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/); that is
the whole setup. The script lists its own dependencies, and uv installs them -
and a suitable Python if needed - the first time you run it:

```bash
./svg2sign.py --help
```

On Windows, or if the script is not executable:

```bash
uv run svg2sign.py --help
```

### With pip

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python svg2sign.py --help
```

On Windows activate with `.venv\Scripts\activate`. Wherever this README says
`./svg2sign.py`, use `python svg2sign.py` instead.

### FreeCAD location

FreeCAD is found automatically if `FreeCAD` / `freecadcmd` is on your `PATH`, or
in `/Applications/FreeCAD.app` on macOS. Otherwise point to it with `--freecad`:

| System | Typical path |
|---|---|
| macOS | `/Applications/FreeCAD.app/Contents/MacOS/FreeCAD` |
| Windows | `C:\Program Files\FreeCAD 1.0\bin\FreeCAD.exe` |
| Linux AppImage | the `.AppImage` file itself |

With `--headless`, point to `freecadcmd` (`FreeCADCmd.exe` on Windows) instead.

## Usage

Interactive - it asks for everything, Enter accepts the default:

```bash
./svg2sign.py
```

```
SVG file: logo.svg
Overall width [mm] [1000.0]:
Overall height [mm] [1000.0]:
Material thickness [mm] [12.0]:
Inset (pocket) depth [mm], 0 = none [3.0]:
Inset play per side [mm] [0.2]:
Gap between parts on the sheet [mm] [10.0]:
```

Non-interactive - anything given as a flag is not asked; `--yes` takes defaults
for the rest:

```bash
./svg2sign.py --svg logo.svg --width 1000 --height 1000 \
              --thickness 12 --inset 3 --play 0.2 --gap 10 --yes
```

A FreeCAD window opens with both documents once the build is done. A sign with
around 80 parts takes about a minute.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--svg` | asked | Input SVG |
| `--width`, `--height` | 1000 | Overall size of the artwork in mm |
| `--thickness` | 12 | Material thickness in mm |
| `--inset` | 3 | Pocket depth in mm. `0` disables pockets. Must be smaller than the thickness |
| `--play` | 0.2 | Clearance added to each side of a pocket in mm |
| `--gap` | 10 | Space between parts, and to the sheet edge, in mm |
| `--sheet-width`, `--sheet-height` | 2500, 1250 | Sheet size in mm. More sheets are added as needed |
| `--stretch` | off | Stretch the artwork to exactly width x height instead of fitting it proportionally |
| `--tolerance` | 0.01 | Curve flattening tolerance in mm |
| `--min-overlap` | 0.05 | Fraction of a shape that must lie on another shape to count as stacked on it |
| `--out` | next to the SVG | Output directory |
| `--name` | SVG file name | Base name of the output files |
| `--no-step` | off | Do not export the STEP files |
| `--dxf` | off | Also write the flat layout as 2D DXF |
| `--headless` | off | Build with `freecadcmd`: no window, and no colours in the files |
| `--freecad` | auto | Path to the FreeCAD executable |
| `--yes` | off | Never prompt |

## Exports

**STEP** (default, `--no-step` to skip) - `<name>_sign.step` and
`<name>_layout.step`. Solids with pockets, one body per part, named like the
part, positioned as in the matching FreeCAD file. In Fusion 360: *File > Open >
Open from my computer*. Colours are not carried over reliably.

**DXF** (`--dxf`) - `<name>_layout.dxf`, millimetres, closed polylines, all
sheets side by side as in the layout file. One layer per kind of cut:

| Layer | Contents | Cut |
|---|---|---|
| `OUTLINE` | Outer contour of every part | Through, outside the line |
| `HOLES` | Holes in parts | Through, inside the line |
| `POCKET` | Pockets, including the islands left standing inside them | Inset depth, inside the line |
| `SHEET` | Sheet outline | Reference only |

Pockets that run off the edge of a part are drawn 1 mm past that edge so the
cutter clears it. The DXF is written directly and does not need FreeCAD.

## Preparing the SVG

- **Fills only.** Strokes are ignored. In Inkscape: *Path > Stroke to Path*.
- **Text must be paths.** In Inkscape: *Path > Object to Path*.
- **Paint order is stacking order.** Objects lower in the layer list are lower in
  the sign. A background shape that everything sits on becomes the backing plate.
- **Holes show the layer below.** A ring-shaped path is cut as a ring.
- **Label your objects** (Inkscape: *Object Properties > Label*). Labels become
  part names in FreeCAD; unlabelled paths inherit their group's label or use
  their id. Shapes sharing a label get `_a`, `_b`, ... appended.
- **Colours** are taken from the fill and only used for display.

## How stacking works

Shapes are processed in paint order:

1. A shape that lies on nothing is layer 0.
2. Otherwise it goes one layer above the highest shape it overlaps. Overlaps
   smaller than `--min-overlap` of the shape's own area are ignored, so parts
   that merely touch their neighbours stay on the same layer.
3. Each layer sits `thickness - inset` above the one below.
4. A part is pocketed for every shape exactly one layer above it. The pocket is
   the upper shape's footprint grown by `--play`, cut `--inset` deep.
5. Every connected piece of a shape is its own part - each letter of a text,
   each dot.

For the layout, each part is turned to its smallest bounding box and packed by
that box. Parts are only rotated in the sheet plane, never flipped.

## Messages

- `X and Y overlap by N mm2 on the same layer` - two parts on one layer collide.
  Small values need a little sanding; large ones mean the SVG wants fixing.
- `X would sink into Y - reduce the inset` - a part spans more than one layer
  down and would collide. Keep the inset at or below half the thickness.
- `more than one bottom-layer shape` - there is no single backing plate.
- `pocket failed on X, left without inset` - FreeCAD could not cut that pocket;
  the part is still there, just flat on top.

## Limitations

- Outlines are fine polylines, not true curves or arcs.
- Packing uses bounding boxes. Small parts are not nested into the waste inside
  larger ones.
- There is no check against the cutter diameter. Pockets and holes narrower than
  the bit cannot be milled, and inside corners of pockets come out rounded while
  the matching part corners are sharp - round those by hand or use a smaller bit.
- No toolpaths or tabs. Use the FreeCAD CAM workbench on the layout file, or
  take the layout STEP or DXF into Fusion 360 or your CAM of choice.
- `fill-rule` is treated as even-odd.

## Files

- `LICENSE` - coffee-ware.
- `requirements.txt` - dependencies for pip users; mirrors the header in `svg2sign.py`.
- `svg2sign.py` - prompts, SVG parsing, stacking, pockets and packing.
- `freecad_build.py` - runs inside FreeCAD and builds the two documents. Started
  by `svg2sign.py`; not meant to be run by hand.

## License

[Coffee-ware](LICENSE): do whatever you want with it, keep the notice, and if we
meet some day and you think it was worth it, buy me a coffee. No warranty.
