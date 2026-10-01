#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["svgelements", "shapely>=2.0", "ezdxf"]
# ///
"""svg2sign - turn a filled SVG into a stacked, CNC-cuttable sign in FreeCAD.

Pipeline:
  1. Ask for size, material thickness, inset depth/play, part gap and the SVG.
  2. Read every filled SVG shape in paint order and stack them "outside in":
     a shape sits one layer above the highest shape it is painted on top of.
  3. Each piece gets a pocket (inset) for the pieces resting on it, enlarged by
     the play, so the pieces locate themselves during glue-up.
  4. FreeCAD builds <name>_sign.FCStd (assembled, to look at), then copies it to
     <name>_layout.FCStd and lays all parts flat, same side up, on sheets.
  5. Both are exported as STEP (<name>_sign.step, <name>_layout.step).
  6. With --dxf, the flat layout is also written as 2D DXF (<name>_layout.dxf).

Run:  ./svg2sign.py            (interactive)
      ./svg2sign.py --svg logo.svg --width 1000 --height 1000 --thickness 12 \
                    --inset 3 --play 0.2 --gap 10 --yes
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union
from svgelements import SVG, Close, Line, Move, Path as SvgPath, Shape

HERE = Path(__file__).resolve().parent
BUILDER = HERE / "freecad_build.py"
INKSCAPE_LABEL = "{http://www.inkscape.org/namespaces/inkscape}label"
CURVE_SAMPLES = 24  # points per curve segment before simplification


# --------------------------------------------------------------------------- #
# parameters
# --------------------------------------------------------------------------- #
PROMPTS = [  # (attr, question, default)
    ("width", "Overall width [mm]", 1000.0),
    ("height", "Overall height [mm]", 1000.0),
    ("thickness", "Material thickness [mm]", 12.0),
    ("inset", "Inset (pocket) depth [mm], 0 = none", 3.0),
    ("play", "Inset play per side [mm]", 0.2),
    ("gap", "Gap between parts on the sheet [mm]", 10.0),
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--svg", type=Path, help="input SVG (filled shapes; strokes are ignored)")
    for attr, question, _ in PROMPTS:
        p.add_argument(f"--{attr}", type=float, help=question)
    p.add_argument("--sheet-width", type=float, default=2500.0, help="sheet width [mm] (default 2500)")
    p.add_argument("--sheet-height", type=float, default=1250.0, help="sheet height [mm] (default 1250)")
    p.add_argument("--stretch", action="store_true", help="stretch artwork to width x height instead of fitting proportionally")
    p.add_argument("--tolerance", type=float, default=0.01, help="curve flattening tolerance [mm] (default 0.01)")
    p.add_argument("--min-overlap", type=float, default=0.05, help="fraction of a shape that must lie on another to count as stacked (default 0.05)")
    p.add_argument("--out", type=Path, help="output directory (default: next to the SVG)")
    p.add_argument("--name", help="output base name (default: SVG file name)")
    p.add_argument("--dxf", action="store_true", help="also write the flat layout as 2D DXF (layers OUTLINE, HOLES, POCKET, SHEET)")
    p.add_argument("--no-step", action="store_true", help="skip the STEP export (for Fusion 360 and other CAD)")
    p.add_argument("--headless", action="store_true", help="build with freecadcmd (no window, no colours)")
    p.add_argument("--freecad", type=Path, help="path to the FreeCAD / freecadcmd executable")
    p.add_argument("--yes", action="store_true", help="never prompt; use defaults for anything not given")
    return p.parse_args(argv)


def ask(question, default, cast):
    while True:
        raw = input(f"{question} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return cast(raw.replace(",", "."))
        except ValueError:
            print(f"  not a valid value: {raw!r}")


def validate(a) -> list[str]:
    errors = []
    if a.svg is None or not Path(a.svg).is_file():
        errors.append(f"SVG file not found: {a.svg}")
    for attr in ("width", "height", "thickness"):
        if getattr(a, attr) <= 0:
            errors.append(f"{attr} must be > 0")
    if a.inset < 0:
        errors.append("inset must be >= 0")
    elif a.inset >= a.thickness:
        errors.append(f"inset ({a.inset}) must be smaller than the material thickness ({a.thickness})")
    if a.play < 0:
        errors.append("play must be >= 0")
    if a.gap < 0:
        errors.append("gap must be >= 0")
    if a.sheet_width <= 0 or a.sheet_height <= 0:
        errors.append("sheet size must be > 0")
    return errors


def collect_params(a):
    interactive = sys.stdin.isatty() and not a.yes
    while True:
        if a.svg is None:
            if not interactive:
                sys.exit("error: --svg is required")
            a.svg = Path(input("SVG file: ").strip().strip("'\"")).expanduser()
        for attr, question, default in PROMPTS:
            if getattr(a, attr) is None:
                setattr(a, attr, ask(question, default, float) if interactive else default)
        errors = validate(a)
        if not errors:
            return a
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        if not interactive:
            sys.exit(1)
        # ask again for everything that was wrong
        if any("SVG" in e for e in errors):
            a.svg = None
        for attr, _, _ in PROMPTS:
            if any(e.startswith(attr) or f"{attr} (" in e for e in errors):
                setattr(a, attr, None)
        if any(e.startswith("inset") for e in errors):
            a.inset = None


# --------------------------------------------------------------------------- #
# SVG -> polygons
# --------------------------------------------------------------------------- #
def shape_to_geometry(shape):
    """Filled region of an SVG shape (even-odd over its subpaths), in SVG px."""
    path = SvgPath(shape)
    path.reify()
    rings, cur = [], None
    for seg in path:
        if isinstance(seg, Move):
            cur = [(seg.end.x, seg.end.y)] if seg.end is not None else []
            rings.append(cur)
        elif cur is None or seg.end is None:
            continue
        elif isinstance(seg, (Line, Close)):
            cur.append((seg.end.x, seg.end.y))
        else:  # any curve or arc
            for i in range(1, CURVE_SAMPLES + 1):
                pt = seg.point(i / CURVE_SAMPLES)
                cur.append((pt.x, pt.y))
    geom = None
    for ring in rings:
        if len(ring) < 3:
            continue
        poly = Polygon(ring).buffer(0)
        if poly.is_empty:
            continue
        geom = poly if geom is None else geom.symmetric_difference(poly)
    return geom


def polygons(geom, min_area=0.0):
    if geom is None or geom.is_empty:
        return []
    geoms = geom.geoms if hasattr(geom, "geoms") else [geom]
    return [g for g in geoms if isinstance(g, Polygon) and g.area > min_area]


def clean(geom, tol, min_area):
    """Simplify and drop sliver polygons / sliver holes."""
    out = []
    for p in polygons(geom.simplify(tol).buffer(0), min_area):
        holes = [h for h in p.interiors if Polygon(h).area > min_area]
        out.append(Polygon(p.exterior, holes))
    if not out:
        return None
    return out[0] if len(out) == 1 else MultiPolygon(out)


def load_shapes(a):
    svg = SVG.parse(str(a.svg), reify=True)
    raw, skipped_stroke = [], 0
    for e in svg.elements():
        if not isinstance(e, Shape):
            continue
        if e.values.get("display") == "none" or e.values.get("visibility") == "hidden":
            continue
        if e.fill is None or e.fill.value is None:
            if e.stroke is not None and e.stroke.value is not None:
                skipped_stroke += 1
            continue
        geom = shape_to_geometry(e)
        if geom is None or geom.is_empty:
            continue
        label = e.values.get(INKSCAPE_LABEL) or e.values.get("inkscape:label") or e.id or "shape"
        raw.append({"label": str(label), "color": e.fill.hex[:7], "geom": geom})
    if not raw:
        sys.exit("error: no filled shapes found in the SVG (convert strokes/text to paths first)")
    if skipped_stroke:
        print(f"note: ignored {skipped_stroke} stroke-only shape(s); use 'Stroke to Path' in Inkscape to include them")

    # fit artwork to width x height, centred on the origin, Y up
    minx, miny, maxx, maxy = unary_union([r["geom"] for r in raw]).bounds
    sx, sy = a.width / (maxx - minx), a.height / (maxy - miny)
    if not a.stretch:
        sx = sy = min(sx, sy)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    matrix = [sx, 0, 0, -sy, -cx * sx, cy * sy]
    min_area = 0.5  # mm^2
    shapes = []
    for r in raw:
        g = clean(affinity.affine_transform(r["geom"], matrix), a.tolerance, min_area)
        if g is not None:
            shapes.append({"label": r["label"], "color": r["color"], "geom": g})
    bx = unary_union([s["geom"] for s in shapes]).bounds
    print(f"artwork: {len(shapes)} shapes, {bx[2] - bx[0]:.1f} x {bx[3] - bx[1]:.1f} mm")
    return shapes


# --------------------------------------------------------------------------- #
# stacking, pockets
# --------------------------------------------------------------------------- #
def assign_levels(shapes, a):
    """Level 0 = painted first with nothing below; else one above what it lies on."""
    notes = []
    for i, s in enumerate(shapes):
        level = 0
        for below in shapes[:i]:
            if not s["geom"].intersects(below["geom"]):
                continue
            overlap = s["geom"].intersection(below["geom"]).area
            if overlap >= a.min_overlap * s["geom"].area:
                level = max(level, below["level"] + 1)
        s["level"] = level
    step = a.thickness - a.inset
    for i, s in enumerate(shapes):
        for o in shapes[i + 1:]:
            overlap = s["geom"].intersection(o["geom"]).area
            if overlap <= 0.5:
                continue
            diff = abs(s["level"] - o["level"])
            if diff == 0:
                notes.append(f"{s['name']} and {o['name']} overlap by {overlap:.1f} mm2 on the same layer")
            elif diff >= 2 and diff * step < a.thickness - 1e-9:
                sys.exit(
                    f"error: {o['name']} (layer {o['level']}) would sink into {s['name']} (layer {s['level']}) "
                    f"- reduce the inset to at most {a.thickness / 2:g} mm"
                )
    if sum(1 for s in shapes if s["level"] == 0) > 1:
        notes.append("more than one bottom-layer shape: the sign has no single backing plate")
    return notes


def unique_names(shapes):
    """FreeCAD-safe names from the SVG labels; shapes sharing a label get a letter suffix."""
    bases = []
    for s in shapes:
        base = "".join(c if c.isalnum() else "_" for c in s["label"]).strip("_") or "shape"
        bases.append("_" + base if base[0].isdigit() else base)
    seen = {}
    for s, base in zip(shapes, bases):
        if bases.count(base) == 1:
            s["name"] = base
        else:
            seen[base] = seen.get(base, 0) + 1
            s["name"] = f"{base}_{chr(96 + seen[base]) if bases.count(base) <= 26 else seen[base]}"
    return shapes


def rings_of(poly):
    r = lambda ring: [[round(x, 4), round(y, 4)] for x, y in ring.coords]
    return [r(poly.exterior)] + [r(h) for h in poly.interiors]


def build_parts(shapes, a):
    """One part per connected polygon; pockets come from shapes one layer up."""
    parts = []
    for s in shapes:
        pocket = None
        if a.inset > 0:
            uppers = [o["geom"] for o in shapes if o["level"] == s["level"] + 1 and o["geom"].intersects(s["geom"])]
            if uppers:
                pocket = unary_union(uppers).buffer(a.play, join_style=1, resolution=8).simplify(a.tolerance)
        pieces = sorted(polygons(s["geom"]), key=lambda p: (p.centroid.x, p.centroid.y))
        for k, poly in enumerate(pieces):
            name = s["name"] if len(pieces) == 1 else f"{s['name']}_{k + 1:02d}"
            pockets = []
            if pocket is not None and pocket.intersects(poly):
                # clip slightly beyond the edge so open pockets cut cleanly through it
                pockets = polygons(pocket.intersection(poly.buffer(1.0)), 0.5)
            parts.append({
                "name": name, "level": s["level"], "color": s["color"], "poly": poly,
                "rings": rings_of(poly), "pockets": [rings_of(p) for p in pockets], "pocket_polys": pockets,
                "z": s["level"] * (a.thickness - a.inset),
            })
    return parts


# --------------------------------------------------------------------------- #
# flat layout
# --------------------------------------------------------------------------- #
def flat_angle(poly):
    """Rotation (deg) that makes the minimum bounding rectangle axis-aligned, long side along X."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # shapely is noisy on near-degenerate hulls
        rect = poly.minimum_rotated_rectangle
    if not isinstance(rect, Polygon) or rect.is_empty or not math.isfinite(rect.area):
        return 0.0
    c = list(rect.exterior.coords)
    e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
    e2 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
    e = e1 if math.hypot(*e1) >= math.hypot(*e2) else e2
    return -math.degrees(math.atan2(e[1], e[0]))


def layout(parts, a):
    """Guillotine bin packing of rotated bounding boxes; adds part['layout']."""
    edge, gap = a.gap, a.gap
    inner_w, inner_h = a.sheet_width - 2 * edge + gap, a.sheet_height - 2 * edge + gap
    items = []
    for p in parts:
        ang = flat_angle(p["poly"])
        minx, miny, maxx, maxy = affinity.rotate(p["poly"], ang, origin=(0, 0)).bounds
        items.append((p, ang, maxx - minx + gap, maxy - miny + gap))
    items.sort(key=lambda i: -max(i[2], i[3]))
    sheets = []  # each: list of free rects [x, y, w, h]
    for p, ang, w, h in items:
        if not ((w <= inner_w and h <= inner_h) or (h <= inner_w and w <= inner_h)):
            sys.exit(f"error: part {p['name']} ({w - gap:.0f} x {h - gap:.0f} mm) does not fit on a "
                     f"{a.sheet_width:g} x {a.sheet_height:g} mm sheet")
        si = 0
        while True:
            if si == len(sheets):
                sheets.append([[edge - gap / 2, edge - gap / 2, inner_w, inner_h]])
            best = None
            for idx, (x, y, fw, fh) in enumerate(sheets[si]):
                for turned, (ww, hh) in enumerate(((w, h), (h, w))):
                    if ww <= fw + 1e-6 and hh <= fh + 1e-6:
                        score = min(fw - ww, fh - hh)
                        if best is None or score < best[0]:
                            best = (score, idx, turned, ww, hh)
            if best is None:
                si += 1
                continue
            _, idx, turned, ww, hh = best
            x, y, fw, fh = sheets[si].pop(idx)
            if fw - ww < fh - hh:
                sheets[si] += [[x + ww, y, fw - ww, hh], [x, y + hh, fw, fh - hh]]
            else:
                sheets[si] += [[x + ww, y, fw - ww, fh], [x, y + hh, ww, fh - hh]]
            angle = ang + (90 if turned else 0)
            minx, miny, _, _ = affinity.rotate(p["poly"], angle, origin=(0, 0)).bounds
            x0 = si * (a.sheet_width + 100)  # sheets side by side
            p["layout"] = {"sheet": si, "angle": angle,
                           "x": x0 + x + gap / 2 - minx, "y": y + gap / 2 - miny}
            break
    return len(sheets)


def write_dxf(parts, n_sheets, a, path):
    """Flat layout as closed 2D polylines, one layer per kind of cut."""
    import ezdxf

    dxf = ezdxf.new("R2010")
    dxf.units = ezdxf.units.MM
    for layer, color in (("OUTLINE", 7), ("HOLES", 1), ("POCKET", 5), ("SHEET", 8)):
        dxf.layers.add(layer, color=color)
    msp = dxf.modelspace()

    def placed(geom, lo):
        return affinity.translate(affinity.rotate(geom, lo["angle"], origin=(0, 0)), lo["x"], lo["y"])

    def add(ring, layer):
        msp.add_lwpolyline(list(ring.coords)[:-1], close=True, dxfattribs={"layer": layer})

    for i in range(n_sheets):
        x0, w, h = i * (a.sheet_width + 100), a.sheet_width, a.sheet_height
        msp.add_lwpolyline([(x0, 0), (x0 + w, 0), (x0 + w, h), (x0, h)], close=True, dxfattribs={"layer": "SHEET"})
    for p in parts:
        poly = placed(p["poly"], p["layout"])
        add(poly.exterior, "OUTLINE")
        for hole in poly.interiors:
            add(hole, "HOLES")
        for pocket in p["pocket_polys"]:
            pocket = placed(pocket, p["layout"])
            add(pocket.exterior, "POCKET")
            for island in pocket.interiors:
                add(island, "POCKET")
    dxf.saveas(path)


# --------------------------------------------------------------------------- #
# FreeCAD
# --------------------------------------------------------------------------- #
def find_freecad(a):
    if a.freecad:
        return str(a.freecad)
    names = ["freecadcmd", "FreeCADCmd"] if a.headless else ["FreeCAD", "freecad"]
    candidates = [shutil.which(n) for n in names]
    if a.headless:
        candidates += ["/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd"]
    else:
        candidates += ["/Applications/FreeCAD.app/Contents/MacOS/FreeCAD",
                       "/Applications/FreeCAD.app/Contents/Resources/bin/freecad"]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit("error: FreeCAD executable not found; pass --freecad /path/to/FreeCAD")


def run_freecad(job, a):
    workdir = Path(tempfile.mkdtemp(prefix="svg2sign_"))
    job_file, result_file = workdir / "job.json", workdir / "result.json"
    job["result_file"] = str(result_file)
    job_file.write_text(json.dumps(job))
    env = dict(os.environ, SVG2SIGN_JOB=str(job_file))
    exe = find_freecad(a)
    print(f"building in FreeCAD ({'headless' if a.headless else 'GUI'}) - pockets can take a few minutes ...")
    if a.headless:
        subprocess.run([exe, str(BUILDER)], env=env, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen([exe, str(BUILDER)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 3600
        while not result_file.exists() and time.time() < deadline:
            time.sleep(1)
    if not result_file.exists():
        sys.exit("error: FreeCAD did not report a result")
    result = json.loads(result_file.read_text())
    shutil.rmtree(workdir, ignore_errors=True)
    return result


def main(argv=None):
    a = collect_params(parse_args(argv))
    a.svg = Path(a.svg).expanduser().resolve()
    out_dir = (a.out or a.svg.parent).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = a.name or a.svg.stem

    shapes = unique_names(load_shapes(a))
    notes = assign_levels(shapes, a)
    parts = build_parts(shapes, a)
    n_sheets = layout(parts, a)

    levels = max(p["level"] for p in parts) + 1
    total = (levels - 1) * (a.thickness - a.inset) + a.thickness
    print(f"stack: {levels} layers, {len(parts)} parts, total height {total:g} mm, "
          f"{sum(1 for p in parts if p['pockets'])} parts with pockets")
    for lvl in range(levels):
        names = sorted({s["name"] for s in shapes if s["level"] == lvl})
        print(f"  layer {lvl} (z {lvl * (a.thickness - a.inset):g}): {', '.join(names)}")
    print(f"layout: {n_sheets} sheet(s) of {a.sheet_width:g} x {a.sheet_height:g} mm, gap {a.gap:g} mm")
    for note in notes:
        print(f"warning: {note}")

    job = {
        "sign_file": str(out_dir / f"{name}_sign.FCStd"),
        "layout_file": str(out_dir / f"{name}_layout.FCStd"),
        "doc_name": "".join(c if c.isalnum() else "_" for c in name),
        "thickness": a.thickness, "inset": a.inset,
        "sheet_width": a.sheet_width, "sheet_height": a.sheet_height, "sheets": n_sheets,
        "headless": a.headless, "step": not a.no_step,
        "parts": [{k: p[k] for k in ("name", "level", "color", "rings", "pockets", "z", "layout")} for p in parts],
    }
    result = run_freecad(job, a)
    for msg in result.get("messages", []):  # includes export failures
        print(f"  {msg}")
    if not result.get("ok"):
        sys.exit(f"error: FreeCAD build failed: {result.get('error')}")
    print(f"sign:   {job['sign_file']}")
    print(f"layout: {job['layout_file']}")
    for path in result.get("step_files", []):
        print(f"step:   {path}")
    if a.dxf:
        dxf_file = out_dir / f"{name}_layout.dxf"
        write_dxf(parts, n_sheets, a, dxf_file)
        print(f"dxf:    {dxf_file}")


if __name__ == "__main__":
    main()
