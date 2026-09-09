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

from .models import Assignment, Candidate, CareerEntry, CareerGroup

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


def _s(v) -> str:
    """Coerce any value to a safe string. Live Ezekia data returns explicit
    nulls for absent fields (e.g. {"title": null}), and python-pptx runs an
    illegal-char regex over run text that raises on None — so every value that
    reaches a text run passes through here first."""
    return "" if v is None else str(v)


def set_shape_text(shape, text: str):
    """Replace a text shape's content while keeping the first run's formatting.

    Supports multi-line text: '\n' becomes separate paragraphs (matching how
    the template stores education, etc.). Falls back gracefully if the shape
    has no existing runs.
    """
    if not shape.has_text_frame:
        return
    tf = shape.text_frame
    lines = _s(text).split("\n")

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
    text = _s(text)
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


def _fill_career_table(table_shape, career: List[CareerGroup]):
    """Rebuild the career table to one ROW PER COMPANY GROUP.

    Each group renders as a multi-line block:
        Google              (company, bold — run0 style)
        Director   2024 - P (role, regular — run1 style)
        VP         2022 - 2024
    The left cell holds the company then each role on its own line; the right
    cell holds a blank line (aligning with the company) then each role's dates.
    Row 0 is the style prototype (company-bold run + role run; dates run in the
    right cell); every row is rebuilt from a deep copy of it so fonts/borders
    are preserved.
    """
    tbl = table_shape.table._tbl
    trs = tbl.findall(qn("a:tr"))
    if not trs:
        return
    proto_tr = copy.deepcopy(trs[0])

    for tr in trs:
        tbl.remove(tr)

    groups = career if career else [CareerGroup(company="", roles=[CareerEntry(role="", dates="")])]
    for _ in groups:
        tbl.append(copy.deepcopy(proto_tr))

    table = table_shape.table
    for i, grp in enumerate(groups):
        _fill_career_row(table, i, grp)


def _proto_run(paragraph):
    """Deep-copy the first run element of a paragraph (style prototype), or None."""
    runs = paragraph.runs
    return copy.deepcopy(runs[0]._r) if runs else None


def _render_cell_lines(cell, lines):
    """Rebuild a table cell as ONE paragraph whose lines are separated by
    <a:br> — matching the template's tight, aligned career layout (company
    then roles beneath; blank then dates beneath). `lines` is a list of
    (text, proto_run_element); each becomes a run cloned from proto_run_element
    (preserving its font, e.g. bold company vs regular role). Using a single
    paragraph with line breaks (rather than separate paragraphs) avoids the
    inter-paragraph spacing that was pushing roles out of line with dates."""
    tf = cell.text_frame
    txBody = tf._txBody
    # keep the paragraph props (e.g. right-align on the dates cell) from para 0
    first_p = txBody.find(qn("a:p"))
    pPr = None
    if first_p is not None:
        pPr_el = first_p.find(qn("a:pPr"))
        if pPr_el is not None:
            pPr = copy.deepcopy(pPr_el)
    # remove all existing paragraphs (keep bodyPr / lstStyle)
    for p in txBody.findall(qn("a:p")):
        txBody.remove(p)

    new_p = txBody.makeelement(qn("a:p"), {})
    if pPr is not None:
        new_p.append(pPr)
    for i, (text, proto_run) in enumerate(lines):
        if i > 0:
            new_p.append(new_p.makeelement(qn("a:br"), {}))
        if proto_run is not None:
            new_r = copy.deepcopy(proto_run)
        else:
            new_r = new_p.makeelement(qn("a:r"), {})
        t = new_r.find(qn("a:t"))
        if t is None:
            t = new_r.makeelement(qn("a:t"), {})
            new_r.append(t)
        t.text = _s(text)
        new_p.append(new_r)
    txBody.append(new_p)


def _fill_career_row(table, row_idx: int, group: CareerGroup):
    left = table.cell(row_idx, 0)
    right = table.cell(row_idx, 1)

    # capture style prototypes from the cloned prototype row's cells
    lp0 = left.text_frame.paragraphs[0]
    lruns = lp0.runs
    company_proto = copy.deepcopy(lruns[0]._r) if len(lruns) >= 1 else None
    role_proto = copy.deepcopy(lruns[1]._r) if len(lruns) >= 2 else company_proto
    date_proto = None
    for para in right.text_frame.paragraphs:
        if para.runs:
            date_proto = copy.deepcopy(para.runs[0]._r)
            break

    # LEFT: company (bold) then each role (regular)
    left_lines = [(_s(group.company), company_proto)]
    for e in group.roles:
        left_lines.append((_s(e.role), role_proto))
    _render_cell_lines(left, left_lines)

    # RIGHT: blank line (aligns with company) then each role's dates
    right_lines = [("", date_proto)]
    for e in group.roles:
        right_lines.append((_s(e.dates), date_proto))
    _render_cell_lines(right, right_lines)


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
    # always (re)set the name hyperlink — clears any stale link cloned from the
    # template's sample profile, then applies this candidate's LinkedIn if any
    _hyperlink_shape(slots_col.get("name"), cand.name_url)
    put("location", cand.location)
    put("salary", cand.salary)
    put("availability", cand.availability)
    put("education", cand.education)
    tbl = slots_col.get("career_table")
    if tbl is not None:
        _fill_career_table(tbl, cand.career)


def _clear_hyperlinks(text_frame):
    """Remove any hyperlink cloned from the template's sample content."""
    for para in text_frame.paragraphs:
        for r in para.runs:
            try:
                r.hyperlink.address = None
            except Exception:
                pass


def _hyperlink_shape(shape, url: str):
    """Clear any stale hyperlink on a text shape, then link its first run to
    `url` (a falsy url just clears)."""
    if shape is None or not shape.has_text_frame:
        return
    _clear_hyperlinks(shape.text_frame)
    if not url:
        return
    for para in shape.text_frame.paragraphs:
        if para.runs:
            try:
                para.runs[0].hyperlink.address = url
            except Exception:
                pass
            return


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


def _remove_stray_right_masks(slide):
    """Remove any template artefact rectangle sitting over the RIGHT column's
    lower area (e.g. the discounted slide's "Rectangle 49" that covers the 2nd
    profile's education). Only no-text auto-shapes low on the right are removed,
    so real card decorations and labels are left untouched."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    for sh in list(slide.shapes):
        try:
            is_auto = sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE
            has_text = sh.has_text_frame and sh.text_frame.text.strip()
            if is_auto and not has_text and _column_of(sh) == "right" and _in(sh.top) > 5.5:
                sh._element.getparent().remove(sh._element)
        except Exception:
            pass


def fill_profile_slide(slide, left_cand: Optional[Candidate], right_cand: Optional[Candidate]):
    slots = _profile_slots(slide)
    _fill_profile_column(slots["left"], left_cand)
    if right_cand is None:
        _mask_empty_right_slot(slide)
    else:
        _remove_stray_right_masks(slide)
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
            # clear any hyperlink cloned from the template's sample row
            _clear_hyperlinks(table.cell(i, c).text_frame)
        # hyperlink the NAME cell to this candidate's LinkedIn (if any)
        _hyperlink_cell(table.cell(i, 0), cand.name_url)


def _hyperlink_cell(cell, url: str):
    """Link the first run of a table cell to `url` (falsy url leaves it plain)."""
    if not url:
        return
    for para in cell.text_frame.paragraphs:
        if para.runs:
            try:
                para.runs[0].hyperlink.address = url
            except Exception:
                pass
            return


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
