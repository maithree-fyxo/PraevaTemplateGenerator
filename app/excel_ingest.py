"""
Excel ingest: build an Assignment from the Ezekia "Project Candidate Report"
Excel export, so a deck can be generated without hitting the API.

Expected workbook (sheets):
  - "Assignment Info"        : project title / client (row1=keys, row2=labels, row3=values)
  - "Candidates Positions"   : fullname, current pipeline tag, positions 1-7
  - "Candidates Positions (2)": fullname, positions 8-10, first education (edus*1)
  - "Candidates Current"     : fullname, current city / country
  - "Candidates Confidential": fullname, confidential salary / bonus / notice

Each candidate sheet uses row1 = machine keys (":candidates:..."), row2 = human
labels, row3+ = data. Candidates are matched across sheets by full name.

NOTE: the Excel export carries NO position dates, so career tables built from
Excel show company + role without a date range (the source doesn't include them).
Routing (which section a candidate lands in) reuses the exact same tag rules as
the API path — see ezekia.TAG_ROUTING / SUPPRESS_TAGS.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import openpyxl

from . import ezekia
from .models import Assignment, Candidate, CareerEntry, CareerGroup


# --------------------------------------------------------------------------- #
# sheet reading
# --------------------------------------------------------------------------- #
def _sheet_records(ws) -> List[Dict[str, Any]]:
    """Return each data row (row 3+) as {machine_key: value}. Row 1 = keys."""
    keys = [str(ws.cell(1, c).value or "") for c in range(1, ws.max_column + 1)]
    records = []
    for r in range(3, ws.max_row + 1):
        rec = {keys[c]: ws.cell(r, c + 1).value for c in range(len(keys))}
        records.append(rec)
    return records


def _by_name(records: List[Dict[str, Any]],
             name_key: str = ":candidates:fullname") -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        nm = rec.get(name_key)
        if nm and str(nm).strip():
            out[str(nm).strip()] = rec
    return out


def _s(v) -> str:
    return "" if v is None else str(v).strip()


# --------------------------------------------------------------------------- #
# field extractors
# --------------------------------------------------------------------------- #
def _route_from_tag(tag: str):
    """Route on the single 'current' pipeline tag, reusing the API rules."""
    person = {"meta": {"candidate": {"pipelineTags": [{"text": tag}] if tag else []}}}
    return ezekia._candidate_route(person)


def _fmt_dates(start: Any, end: Any) -> str:
    """Build 'YYYY - YYYY' from the export's start/end (e.g. '2025/10/01').
    An open-ended role — end 'Present'/'Current' or 9999 — shows 'P'."""
    s = ezekia._year(start)
    end_raw = "" if end is None else str(end).strip()
    if end_raw.lower() in ("present", "current", "ongoing") or end_raw == "9999":
        e = "P"
    else:
        e = ezekia._year(end)
        if e == "9999":
            e = "P"
    return f"{s} - {e}".strip(" -") if (s or e) else ""


def _collect_positions(row1: Dict[str, Any], row2: Dict[str, Any]) -> List[Dict[str, str]]:
    merged = {**(row1 or {}), **(row2 or {})}
    out = []
    for i in range(1, 11):
        pre = f":candidates:positions*{i}:"
        title = _s(merged.get(pre + "title")) or _s(merged.get(pre + "career"))
        company = _s(merged.get(pre + "company"))
        location = _s(merged.get(pre + "location"))
        dates = _fmt_dates(merged.get(pre + "start"), merged.get(pre + "end"))
        if title or company:
            out.append({"title": title, "company": company,
                        "location": location, "dates": dates})
    return out


def _career_groups(positions: List[Dict[str, str]]) -> List[CareerGroup]:
    """Group consecutive same-company positions, keeping each role's dates."""
    groups: List[CareerGroup] = []
    for p in positions:
        company = p["company"]
        entry = CareerEntry(role=p["title"], dates=p.get("dates", ""))
        if groups and groups[-1].company.strip().lower() == company.strip().lower():
            groups[-1].roles.append(entry)
        else:
            groups.append(CareerGroup(company=company, roles=[entry]))
    return groups


def _education(row2: Dict[str, Any]) -> str:
    uni = _s(row2.get(":candidates:edus*1:university"))
    deg = _s(row2.get(":candidates:edus*1:degree"))
    start = row2.get(":candidates:edus*1:start")
    end = row2.get(":candidates:edus*1:end")
    if not (uni or deg):
        return ""
    edu = [{"school": uni, "degree": deg, "field": None,
            "startDate": start, "endDate": end}]
    return ezekia._education(edu)


def _location(cur_row: Dict[str, Any]) -> str:
    city = _s(cur_row.get(":candidates:cities*1"))
    country = _s(cur_row.get(":candidates:country"))
    city = ezekia._loc_str(city)      # blanks a location marked "Private"
    country = ezekia._loc_str(country)
    if city and country and country.lower() in city.lower():
        return city
    return ", ".join(p for p in (city, country) if p)


def _salary(conf_row: Dict[str, Any]) -> str:
    sal = conf_row.get(":candidates:confidential:salary")
    bonus = conf_row.get(":candidates:confidential:bonus")
    parts = []
    if sal not in (None, ""):
        try:
            parts.append(f"£{int(float(sal)):,} base")
        except (ValueError, TypeError):
            parts.append(_s(sal))
    if bonus not in (None, ""):
        if isinstance(bonus, (int, float)):
            parts.append(f"{bonus:g}% bonus")
        else:
            parts.append(_s(bonus))
    return ", ".join(parts)


def _availability(conf_row: Dict[str, Any]) -> str:
    notice = conf_row.get(":candidates:confidential:notice")
    return ezekia._availability({"confidential": {"notice": notice}})


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
class ExcelIngestError(Exception):
    pass


def assignment_from_excel(source) -> Assignment:
    """Build an Assignment from an Excel file (path or file-like object)."""
    try:
        wb = openpyxl.load_workbook(source, data_only=True)
    except Exception as e:
        raise ExcelIngestError(f"Could not read the Excel file: {e}")

    names = set(wb.sheetnames)
    required = "Candidates Positions"
    if required not in names:
        raise ExcelIngestError(
            f"Missing the '{required}' sheet. Please upload the Ezekia "
            "Project Candidate Report export."
        )

    # --- assignment info ---
    client = title = ""
    if "Assignment Info" in names:
        ws = wb["Assignment Info"]
        keys = [str(ws.cell(1, c).value or "") for c in range(1, ws.max_column + 1)]
        vals = [ws.cell(3, c).value for c in range(1, ws.max_column + 1)]
        info = dict(zip(keys, vals))
        client = _s(info.get(":project:client"))
        title = _s(info.get(":project:title"))
    name = client or title or "Search Update"

    # --- candidate sheets keyed by full name ---
    pos1 = _by_name(_sheet_records(wb["Candidates Positions"]))
    pos2 = _by_name(_sheet_records(wb["Candidates Positions (2)"])) if "Candidates Positions (2)" in names else {}
    cur = _by_name(_sheet_records(wb["Candidates Current"])) if "Candidates Current" in names else {}
    conf = _by_name(_sheet_records(wb["Candidates Confidential"])) if "Candidates Confidential" in names else {}

    assignment = Assignment(
        name=name,
        title=f"Search Update – {name}" if name else "Search Update",
        prepared_for=[],
        date=datetime.now().strftime("%d %B %Y"),
    )

    for nm, row in pos1.items():
        tag = _s(row.get(":candidates:dateOrderedPipelineTags:current"))
        route = _route_from_tag(tag)
        if route is None:
            continue  # suppressed / unrouted tag
        stage, status_label, has_profile = route

        positions = _collect_positions(row, pos2.get(nm, {}))
        current = positions[0] if positions else {}
        cand = Candidate(
            name=nm,
            stage=stage,
            role=current.get("title", ""),
            company=current.get("company", ""),
            status=status_label,
            has_profile=has_profile,
            name_url="",  # LinkedIn is not present in the Excel export
            salary=_salary(conf.get(nm, {})),
            location=_location(cur.get(nm, {})),
            availability=_availability(conf.get(nm, {})),
            education=_education(pos2.get(nm, {})),
            career=_career_groups(positions),
        )
        assignment.candidates.append(cand)

    return assignment
