# svg2sign

Turn a filled SVG into a layered, CNC-cuttable sign in FreeCAD.

Every filled shape in the SVG becomes a part cut from sheet material. Parts are
stacked "outside in": whatever is painted on top of something else sits one
layer higher. Each part gets a shallow pocket (inset) for the parts resting on
it, so everything locates itself during glue-up.

You get two FreeCAD files:

| File | Contents |
|---|---|
| `<name>_sign.FCStd` | The assembled sign, coloured like the SVG, one group per layer. For looking at. |
| `<name>_layout.FCStd` | A copy with every part laid flat, same side up, spaced out on sheets. For cutting. |

## Requirements

- [FreeCAD](https://www.freecad.org/) 1.0 or newer
- [uv](https://docs.astral.sh/uv/) (installs the Python dependencies `svgelements` and `shapely` on first run)

FreeCAD is found automatically on `PATH` or in `/Applications/FreeCAD.app`.
Anywhere else, pass `--freecad /path/to/FreeCAD`.

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

If the script is not executable, run it as `uv run svg2sign.py ...`.

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
| `--headless` | off | Build with `freecadcmd`: no window, and no colours in the files |
| `--freecad` | auto | Path to the FreeCAD executable |
| `--yes` | off | Never prompt |

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
- No toolpaths, tabs or DXF export. Use the FreeCAD CAM workbench on the layout
  file.
- `fill-rule` is treated as even-odd.

## Files

- `svg2sign.py` - prompts, SVG parsing, stacking, pockets and packing.
- `freecad_build.py` - runs inside FreeCAD and builds the two documents. Started
  by `svg2sign.py`; not meant to be run by hand.
