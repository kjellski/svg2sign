"""FreeCAD side of svg2sign: builds the sign and the flat layout from a job file.

Started by svg2sign.py (FreeCAD or freecadcmd) with SVG2SIGN_JOB pointing at the
job JSON. Not meant to be run by hand.
"""
import json
import os
import shutil
import traceback

import FreeCAD as App
import Part
from FreeCAD import Vector as V


def _wire(ring, z):
    pts = [V(x, y, z) for x, y in ring]
    if (pts[0] - pts[-1]).Length > 1e-9:
        pts.append(pts[0])
    return Part.makePolygon(pts)


def _face(rings, z):
    outer = _wire(rings[0], z)
    if len(rings) == 1:
        return Part.Face(outer)
    face = Part.Face([outer] + [_wire(r, z) for r in rings[1:]])
    if not face.isValid():
        face = Part.Face(outer)
        for r in rings[1:]:
            face = face.cut(Part.Face(_wire(r, z)))
    return face


def _solid(part, thickness, inset, messages):
    solid = _face(part["rings"], 0).extrude(V(0, 0, thickness))
    if part["pockets"] and inset > 0:
        cutters = [_face(r, thickness - inset).extrude(V(0, 0, inset + 1)) for r in part["pockets"]]
        cut = solid.cut(Part.makeCompound(cutters))
        if not cut.isValid() or not cut.Solids:
            cut = solid
            for c in cutters:  # slower, but more forgiving
                cut = cut.cut(c)
        if cut.isValid() and cut.Solids:
            solid = cut
        else:
            messages.append("warning: pocket failed on %s, left without inset" % part["name"])
    return solid


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _export_step(doc, path, messages):
    """All solid parts of a document, placed as they are, into one STEP file."""
    objs = [o for o in doc.Objects if o.isDerivedFrom("Part::Feature") and o.Shape.Solids]
    try:
        if App.GuiUp:
            import ImportGui  # keeps the colours
            ImportGui.export(objs, path)
        else:
            import Import
            Import.export(objs, path)
        return True
    except Exception as exc:
        messages.append("warning: STEP export failed for %s: %s" % (os.path.basename(path), exc))
        return False


def build(job, result):
    messages = result["messages"]
    T, D = job["thickness"], job["inset"]

    # --- 1. assembled sign --------------------------------------------------
    doc = App.newDocument(job["doc_name"] + "_sign")
    groups = {}
    for part in job["parts"]:
        obj = doc.addObject("Part::Feature", part["name"])
        obj.Shape = _solid(part, T, D, messages)
        obj.Placement = App.Placement(V(0, 0, part["z"]), App.Rotation())
        lvl = part["level"]
        if lvl not in groups:
            groups[lvl] = doc.addObject("App::DocumentObjectGroup", "Layer%d" % lvl)
            groups[lvl].Label = "Layer%d_z%g" % (lvl, part["z"])
        groups[lvl].addObject(obj)
        if App.GuiUp:
            obj.ViewObject.ShapeColor = _rgb(part["color"])
        part["object"] = obj.Name
    doc.recompute()
    doc.saveAs(job["sign_file"])
    messages.append("sign built: %d parts" % len(job["parts"]))

    # --- 2. copy, then lay the parts flat ------------------------------------
    shutil.copyfile(job["sign_file"], job["layout_file"])
    lay = App.openDocument(job["layout_file"])
    for g in [o for o in lay.Objects if o.isDerivedFrom("App::DocumentObjectGroup")]:
        lay.removeObject(g.Name)
    sheet_groups = []
    for i in range(job["sheets"]):
        x0 = i * (job["sheet_width"] + 100)
        w, h = job["sheet_width"], job["sheet_height"]
        outline = lay.addObject("Part::Feature", "Sheet%d_outline" % (i + 1))
        outline.Shape = Part.makePolygon(
            [V(x0, 0, 0), V(x0 + w, 0, 0), V(x0 + w, h, 0), V(x0, h, 0), V(x0, 0, 0)])
        grp = lay.addObject("App::DocumentObjectGroup", "Sheet%d" % (i + 1))
        grp.addObject(outline)
        sheet_groups.append(grp)
    for part in job["parts"]:
        obj, lo = lay.getObject(part["object"]), part["layout"]
        obj.Placement = App.Placement(V(lo["x"], lo["y"], 0), App.Rotation(V(0, 0, 1), lo["angle"]))
        sheet_groups[lo["sheet"]].addObject(obj)
    lay.recompute()
    lay.save()
    messages.append("layout built: %d sheet(s)" % job["sheets"])

    # --- 3. STEP export of both -----------------------------------------------
    if job.get("step"):
        for d, fcstd in ((doc, job["sign_file"]), (lay, job["layout_file"])):
            path = os.path.splitext(fcstd)[0] + ".step"
            if _export_step(d, path, messages):
                result["step_files"].append(path)

    if App.GuiUp:
        import FreeCADGui as Gui
        for d, view in ((lay, "viewTop"), (doc, "viewIsometric")):
            try:
                v = Gui.getDocument(d.Name).ActiveView
                getattr(v, view)()
                v.fitAll()
            except Exception:
                pass
        App.setActiveDocument(doc.Name)


def main():
    job = json.load(open(os.environ["SVG2SIGN_JOB"]))
    result = {"ok": False, "messages": [], "step_files": []}
    try:
        build(job, result)
        result["ok"] = True
    except Exception as exc:
        result["error"] = "%s\n%s" % (exc, traceback.format_exc())
    with open(job["result_file"], "w") as fh:
        json.dump(result, fh)
    if job.get("headless"):
        os._exit(0)


main()
