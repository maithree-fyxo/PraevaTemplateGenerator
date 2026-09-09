"""
PPTX fill engine.

Fills ONLY the candidate-driven parts of the Praeva "Search Update" template:
  - Cover slide (project name, prepared-for, date, title)
  - Engaged candidate profile slides (2 candidates per slide, cloned as needed)
  - Pipeline / Target / Discounted list tables (variable rows)
  - Discounted candidate profile slides (cloned as needed)

The executive-summary slide, the target-landscape slide, the section
dividers and the back cover are copied through UNTOUCHED for manual editing.

Design notes
------------
* Profile fields are located by POSITION, not shape name: the template names
  its placeholders inconsistently ("Text Placeholder 1/5/8" for the same
  field on different slides), but the geometry is stable. Each profile slide
  is two-up (left column left<5", right column left>=5") and within a column
  the field is fixed by vertical position (name~0.9", location~1.5",
  salary~1.86", availability~2.23", education~6.85").
* Rows and slides are duplicated by deep-copying the styled prototype element
  and swapping only the text of existing runs, so all fonts/colours/borders
  are preserved exactly.
"""
from __future__ import annotations

import copy
from typing import List, Optional

from pptx import Presentation
from pptx.util import Emu
from pptx.oxml.ns import qn

from .models import Assignment, Candidate, CareerEntry

# Column split (inches) for the two-up profile layout
_COL_SPLIT_IN = 5.0
# Expected vertical positions (inches) of each profile field within a column
_FIELD_TOPS = {
    "name": 0.93,
    "location": 1.51,
    "salary": 1.87,
    "availability": 2.24,
    "education": 6.85,
}
_TOP_TOLERANCE = 0.35  # inches

# Slide indices in the template (0-based)
IDX_COVER = 0
IDX_EXEC = 1
IDX_LANDSCAPE = 2
IDX_DIV_ENGAGED = 3
IDX_ENGAGED_PROTO = 4
IDX_ENGAGED_SAMPLE_1 = 5
IDX_ENGAGED_SAMPLE_2 = 6
IDX_DIV_PIPELINE = 7
IDX_PIPELINE_TABLE = 8
IDX_TARGET_TABLE = 9
IDX_DIV_DISCOUNTED = 10
IDX_DISCOUNTED_PROTO = 11
IDX_DISCOUNTED_TABLE = 12
IDX_BACK = 13


# --------------------------------------------------------------------------- #
# low-level helpers
# --------------------------------------------------------------------------- #
def _in(emu) -> float:
    return emu / 914400 if emu is not None else 0.0


def _first_run(paragraph):
    return paragraph.runs[0] if paragraph.runs else None


def set_shape_text(shape, text: str):
    """Replace a text shape's content while keeping the first run's formatting.

    Supports multi-line text: '\n' becomes separate paragraphs (matching how
    the template stores education, etc.). Falls back gracefully if the shape
    has no existing runs.
    """
    if not shape.has_text_frame:
        return
    tf = shape.text_frame
    lines = text.split("\n")

    # Capture a style prototype run element to clone for extra paragraphs.
    proto_para = tf.paragraphs[0]
    proto_run = _first_run(proto_para)
    proto_run_xml = copy.deepcopy(proto_run._r) if proto_run is not None else None

    # First paragraph
    _set_paragraph_text(proto_para, lines[0], proto_run_xml)

    # Remove any extra existing paragraphs beyond the first
    for extra in tf.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)

    # Append remaining lines as new paragraphs cloned from the first
    base_p = proto_para._p
    for line in lines[1:]:
        new_p = copy.deepcopy(base_p)
        base_p.getparent().append(new_p)
        # wrap and set
        from pptx.text.text import _Paragraph
        para = _Paragraph(new_p, proto_para._parent)
        _set_paragraph_text(para, line, proto_run_xml)


def _set_paragraph_text(paragraph, text: str, proto_run_xml=None):
    """Set a paragraph to a single run of `text`, keeping run formatting."""
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r._r.getparent().remove(r._r)
    else:
        # No run present; clone a prototype run if we have one, else add plain
        if proto_run_xml is not None:
            new_r = copy.deepcopy(proto_run_xml)
            paragraph._p.append(new_r)
            from pptx.text.text import _Run
            _Run(new_r, paragraph).text = text
        else:
            paragraph.text = text


def set_cell_text(cell, text: str):
    """Set a table cell to `text`, preserving the first run's formatting."""
    tf = cell.text_frame
    # find first paragraph that has a run to use as style anchor
    para = tf.paragraphs[0]
    _set_paragraph_text(para, text)
    for extra in tf.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)


# --------------------------------------------------------------------------- #
# slide cloning / ordering
# --------------------------------------------------------------------------- #
def _clone_relationships(src_part, dst_part):
    """Copy src part's relationships to dst, remapping rIds and updating the
    r:id / r:embed references inside dst's copied shape XML."""
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    skip = {RT.SLIDE_LAYOUT, RT.NOTES_SLIDE}
    rid_map = {}
    for rId, rel in src_part.rels.items():
        if rel.reltype in skip:
            continue
        new_rId = dst_part.relate_to(rel._target, rel.reltype, is_external=rel.is_external)
        rid_map[rId] = new_rId

    if not rid_map:
        return
    # Update references in dst xml
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    for el in dst_part._element.iter():
        for attr in ("id", "embed", "link"):
            key = f"{{{r_ns}}}{attr}"
            val = el.get(key)
            if val in rid_map:
                el.set(key, rid_map[val])


def clone_slide(prs, src_slide):
    """Deep-copy a slide (same layout) and append it to the presentation.
    Returns the new slide object."""
    dst = prs.slides.add_slide(src_slide.slide_layout)
    # remove placeholders the layout injected
    for sh in list(dst.shapes):
        sh._element.getparent().remove(sh._element)
    # copy shape tree children (skip the group props already present)
    for el in src_slide.shapes._spTree:
        tag = el.tag
        if tag.endswith("}nvGrpSpPr") or tag.endswith("}grpSpPr"):
            continue
        dst.shapes._spTree.append(copy.deepcopy(el))
    _clone_relationships(src_slide.part, dst.part)
    return dst


def reorder_and_prune(prs, ordered_slides):
    """Rewrite the slide id list to exactly `ordered_slides` (in order),
    dropping any slide not in the list."""
    sldIdLst = prs.slides._sldIdLst
    # map slide.part -> its sldId element
    part_to_sldId = {}
    for sldId in list(sldIdLst):
        rId = sldId.get(qn("r:id"))
        part = prs.part.rels[rId].target_part
        part_to_sldId[part] = sldId

    keep_parts = {s.part for s in ordered_slides}

    # detach all
    for sldId in list(sldIdLst):
        sldIdLst.remove(sldId)

    # re-append in desired order
    for s in ordered_slides:
        sldIdLst.append(part_to_sldId[s.part])

    # drop presentation->slide rels for pruned slides (keeps file clean)
    for part, sldId in part_to_sldId.items():
        if part not in keep_parts:
            rId = sldId.get(qn("r:id"))
            try:
                prs.part.drop_rel(rId)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# profile slide filling
# --------------------------------------------------------------------------- #
def _column_of(shape) -> str:
    return "left" if _in(shape.left) < _COL_SPLIT_IN else "right"


def _match_field(top_in: float) -> Optional[str]:
    best, best_d = None, _TOP_TOLERANCE
    for field, exp in _FIELD_TOPS.items():
        d = abs(top_in - exp)
        if d < best_d:
            best, best_d = field, d
    return best


def _profile_slots(slide):
    """Return {'left': {...}, 'right': {...}} mapping field-name -> shape,
    plus 'career_table' -> table shape, for each column."""
    slots = {"left": {}, "right": {}}
    for sh in slide.shapes:
        col = _column_of(sh)
        if sh.has_table:
            slots[col]["career_table"] = sh
            continue
        if not sh.has_text_frame:
            continue
        # skip the section header ("Engaged candidates") which spans full width top-left
        top = _in(sh.top)
        if top < 0.5:
            continue
        field = _match_field(top)
        if field and field not in slots[col]:
            slots[col][field] = sh
    return slots


def _fill_career_table(table_shape, career: List[CareerEntry]):
    """Rebuild the career table to exactly len(career) rows.

    The template's original rows mix two structures (some use an <a:br> line
    break, some use a real paragraph break). To stay robust we take row 0 as
    the single style prototype (company run bold + <a:br> + role run; dates in
    the right cell) and rebuild every row from a deep copy of it, so all rows
    share one clean, predictable structure.
    """
    tbl = table_shape.table._tbl
    trs = tbl.findall(qn("a:tr"))
    if not trs:
        return
    proto_tr = copy.deepcopy(trs[0])

    for tr in trs:
        tbl.remove(tr)

    entries = career if career else [CareerEntry(company="", role="", dates="")]
    for _ in entries:
        tbl.append(copy.deepcopy(proto_tr))

    table = table_shape.table
    for i, entry in enumerate(entries):
        _fill_career_row(table, i, entry)


def _fill_career_row(table, row_idx: int, entry: CareerEntry):
    left = table.cell(row_idx, 0)
    right = table.cell(row_idx, 1)

    # LEFT cell: para0 has run0 (company, bold) + <a:br> + run1 (role)
    p0 = left.text_frame.paragraphs[0]
    runs = p0.runs
    if len(runs) >= 2:
        runs[0].text = entry.company
        runs[1].text = entry.role
        for r in runs[2:]:
            r._r.getparent().remove(r._r)
    elif len(runs) == 1:
        runs[0].text = entry.company if not entry.role else f"{entry.company}  {entry.role}"
    # blank any extra paragraphs in the left cell
    for extra in left.text_frame.paragraphs[1:]:
        for r in extra.runs:
            r.text = ""

    # RIGHT cell: dates live in the first paragraph that has a run
    _set_dates_cell(right, entry.dates)


def _set_dates_cell(cell, dates: str):
    target_para = None
    for para in cell.text_frame.paragraphs:
        if para.runs:
            target_para = para
            break
    if target_para is None:
        target_para = cell.text_frame.paragraphs[-1]
    if target_para.runs:
        target_para.runs[0].text = dates
        for r in target_para.runs[1:]:
            r._r.getparent().remove(r._r)
    else:
        target_para.text = dates
    # clear other paragraphs' runs (compare underlying XML, not wrappers,
    # because .paragraphs returns fresh wrapper objects each call)
    for para in cell.text_frame.paragraphs:
        if para._p is target_para._p:
            continue
        for r in para.runs:
            r.text = ""


def _fill_profile_column(slots_col: dict, cand: Optional[Candidate]):
    """Fill one column's shapes with a candidate, or blank them if None."""
    def put(field, value):
        sh = slots_col.get(field)
        if sh is not None:
            set_shape_text(sh, value)

    if cand is None:
        for field in ("name", "location", "salary", "availability", "education"):
            put(field, "")
        tbl = slots_col.get("career_table")
        if tbl is not None:
            _fill_career_table(tbl, [])
        return

    put("name", cand.name)
    put("location", cand.location)
    put("salary", cand.salary)
    put("availability", cand.availability)
    put("education", cand.education)
    tbl = slots_col.get("career_table")
    if tbl is not None:
        _fill_career_table(tbl, cand.career)


# slide background colour (theme tx2) — used to mask an empty profile slot
_BG_HEX = "FBF9EF"


def _mask_empty_right_slot(slide):
    """Remove the right column's value shapes AND lay an opaque background
    rectangle over the right half, so the card decorations + field labels that
    come from the slide LAYOUT ("LOCATION", "SALARY", pink bar, green rules)
    don't show on a single-candidate slide."""
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    # remove the right column's slide-level value shapes (keep header)
    for sh in list(slide.shapes):
        if _in(sh.top) < 0.5:
            continue
        if _column_of(sh) == "right":
            sh._element.getparent().remove(sh._element)

    # cover the right half (below the header band) with a bg-coloured rectangle
    slide_w_in = _in(slide.part.package.presentation_part.presentation.slide_width)
    slide_h_in = _in(slide.part.package.presentation_part.presentation.slide_height)
    left = Inches(5.35)
    top = Inches(0.6)
    width = Inches(slide_w_in - 5.35)
    height = Inches(slide_h_in - 0.6)
    rect = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    rect.fill.solid()
    rect.fill.fore_color.rgb = __import__("pptx.dml.color", fromlist=["RGBColor"]).RGBColor.from_string(_BG_HEX)
    rect.line.fill.background()
    rect.shadow.inherit = False


def fill_profile_slide(slide, left_cand: Optional[Candidate], right_cand: Optional[Candidate]):
    slots = _profile_slots(slide)
    _fill_profile_column(slots["left"], left_cand)
    if right_cand is None:
        _mask_empty_right_slot(slide)
    else:
        _fill_profile_column(slots["right"], right_cand)


# --------------------------------------------------------------------------- #
# list-table filling  (pipeline / target / discounted)
# --------------------------------------------------------------------------- #
def fill_list_table(table_shape, candidates: List[Candidate]):
    """Header row stays; data rows are rebuilt from candidates.
    Columns: Name | Role | Company | Status."""
    tbl = table_shape.table._tbl
    trs = tbl.findall(qn("a:tr"))
    if len(trs) < 2:
        return
    header_tr = trs[0]
    proto_tr = trs[1]

    # remove all existing data rows
    for tr in trs[1:]:
        tbl.remove(tr)

    if not candidates:
        # keep one blank row so the table isn't headerless-only
        tbl.append(copy.deepcopy(proto_tr))
        table = table_shape.table
        for c in range(min(4, len(table.columns))):
            set_cell_text(table.cell(1, c), "")
        return

    for _ in candidates:
        tbl.append(copy.deepcopy(proto_tr))

    table = table_shape.table
    for i, cand in enumerate(candidates, start=1):
        values = [cand.name, cand.role, cand.company, cand.status]
        for c in range(min(4, len(table.columns))):
            set_cell_text(table.cell(i, c), values[c])


def placeholder_list_table(table_shape, rows: int = 5):
    """Blank the data rows (keeping the header + formatting) so the section is
    ready for manual entry. Used for the Target section, which Praeva fills in
    by hand."""
    tbl = table_shape.table._tbl
    trs = tbl.findall(qn("a:tr"))
    if len(trs) < 2:
        return
    proto_tr = trs[1]
    for tr in trs[1:]:
        tbl.remove(tr)
    for _ in range(max(1, rows)):
        tbl.append(copy.deepcopy(proto_tr))
    table = table_shape.table
    for i in range(1, rows + 1):
        for c in range(min(4, len(table.columns))):
            set_cell_text(table.cell(i, c), "")


# --------------------------------------------------------------------------- #
# cover slide
# --------------------------------------------------------------------------- #
def fill_cover(slide, assignment: Assignment):
    for sh in slide.shapes:
        name = sh.name
        if name == "Text Placeholder 1":
            set_shape_text(sh, assignment.name)
        elif name == "Text Placeholder 2":
            set_shape_text(sh, "\n".join(assignment.prepared_for))
        elif name == "Text Placeholder 4":
            set_shape_text(sh, assignment.date)
        elif name == "Text 1":
            set_shape_text(sh, assignment.title or f"Search Update – {assignment.name}")


# --------------------------------------------------------------------------- #
# top-level orchestration
# --------------------------------------------------------------------------- #
def _chunk_pairs(items):
    """Yield (left, right) tuples, right may be None for an odd tail."""
    for i in range(0, len(items), 2):
        left = items[i]
        right = items[i + 1] if i + 1 < len(items) else None
        yield left, right


def generate(assignment: Assignment, template_path: str, output_path: str):
    prs = Presentation(template_path)
    slides = list(prs.slides)

    # references to fixed slides (captured before mutation)
    cover = slides[IDX_COVER]
    exec_ = slides[IDX_EXEC]
    landscape = slides[IDX_LANDSCAPE]
    div_engaged = slides[IDX_DIV_ENGAGED]
    engaged_proto = slides[IDX_ENGAGED_PROTO]
    div_pipeline = slides[IDX_DIV_PIPELINE]
    pipeline_tbl = slides[IDX_PIPELINE_TABLE]
    target_tbl = slides[IDX_TARGET_TABLE]
    div_discounted = slides[IDX_DIV_DISCOUNTED]
    discounted_proto = slides[IDX_DISCOUNTED_PROTO]
    discounted_tbl = slides[IDX_DISCOUNTED_TABLE]
    back = slides[IDX_BACK]

    # ---- cover ----
    fill_cover(cover, assignment)

    # ---- engaged profiles (2 per slide) ----
    engaged = assignment.engaged()
    engaged_slides = []
    pairs = list(_chunk_pairs(engaged)) or []
    for idx, (l, r) in enumerate(pairs):
        target = engaged_proto if idx == 0 else clone_slide(prs, engaged_proto)
        fill_profile_slide(target, l, r)
        engaged_slides.append(target)
    if not pairs:
        # no engaged candidates: keep proto but blank it
        fill_profile_slide(engaged_proto, None, None)
        engaged_slides.append(engaged_proto)

    # ---- list tables ----
    fill_list_table(_table_shape(pipeline_tbl), assignment.pipeline())
    # Target section is completed manually -> leave blank placeholder rows.
    placeholder_list_table(_table_shape(target_tbl), rows=5)
    fill_list_table(_table_shape(discounted_tbl), assignment.discounted_table())

    # ---- discounted profiles ----
    disc_profiles = assignment.discounted_profiles()
    discounted_slides = []
    dpairs = list(_chunk_pairs(disc_profiles))
    for idx, (l, r) in enumerate(dpairs):
        target = discounted_proto if idx == 0 else clone_slide(prs, discounted_proto)
        fill_profile_slide(target, l, r)
        discounted_slides.append(target)
    if not dpairs:
        fill_profile_slide(discounted_proto, None, None)
        discounted_slides.append(discounted_proto)

    # ---- final order (drops the two sample engaged slides) ----
    ordered = (
        [cover, exec_, landscape, div_engaged]
        + engaged_slides
        + [div_pipeline, pipeline_tbl, target_tbl, div_discounted]
        + discounted_slides
        + [discounted_tbl, back]
    )
    reorder_and_prune(prs, ordered)

    prs.save(output_path)
    return output_path


def _table_shape(slide):
    for sh in slide.shapes:
        if sh.has_table:
            return sh
    raise ValueError(f"No table found on slide")
